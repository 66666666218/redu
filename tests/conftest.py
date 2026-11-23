"""pytest 共用 fixture。"""
import os

import pytest

from config.settings import Settings

#: 测试进程里**必须有值**的三个配置(见下方 `_isolate_settings_env` 的说明)。
_TEST_ENV_DEFAULTS = {
    # 内存库。★ **这一条是本文件的重点**:不钉住它,`get_engine()` 会按 `.env`
    # 连上**生产的** `sqlite:///./data/platform.db`(`create_app()` 里还有 `init_db()`
    # 会建表/迁移)。单独跑 `pytest tests/test_shutdown_marker.py` 时没有任何文件
    # 设过 `DATABASE_URL`,那条路是**真的会走到的**。
    "DATABASE_URL": "sqlite://",
    # `app/security.py` 每次签发/校验令牌都读它;钉成固定值,令牌才在用例间可比。
    "JWT_SECRET": "test_secret_0123456789abcdef0123456789abcdef",
    # `TestClient(create_app())` 会走 lifespan 里的 `scheduler.start()` ——
    # 真起一个每分钟触发的后台调度器,可能在本进程里打出网请求。
    "SCHEDULER_ENABLED": "false",
}


def _isolate_settings_env() -> dict[str, str]:
    """把**生产环境变量**从测试进程里摘掉,只留下受控的测试值。

    ⚠️ **为什么**(2026-11-23 实测,不是理论风险):

    `Settings` 除了 `.env` 还会读**进程环境变量**,而本机跑测试的那个 shell
    是从生产环境继承的 —— 实测有 **115 个** 与 `Settings` 字段同名(约 20 个
    `FEISHU_WEBHOOK*`、`SCHEDULER_ROLE=wechat`、`QUARK_COOKIE`、`WEREAD_COOKIE`、
    `XHS_BROWSER_PROFILES`、`XIANYU_REQUEST_DELAY=15` …)。
    `Settings(_env_file=None)` **并不会**因此变干净:它只是不读 `.env`,
    环境变量照样生效。后果(7 个文件的对照实测):

        带着生产环境变量  → 21 failed, 294 passed
        摘掉之后(同代码) → **315 passed**

    典型形状:`_settings(feishu_webhook="main")` 里凭空多出一个真实群的
    `feishu_webhook_baidu/douhot`(本该是空),`SCHEDULER_ROLE=wechat` 让
    `douhot` 样本被归属护栏静默过滤 —— **同一份代码开发机红、CI 绿**。

    ⚠️ **必须在模块导入时执行**,不能只放进 fixture:各测试文件在**导入时**就
    用 `os.environ.setdefault(...)` 设那三个值,fixture 跑起来时它们已经设好了。
    conftest 在测试模块之前导入,所以这里直接 `pop` 掉整批、再写回测试值,
    那些 `setdefault` 之后就成了空操作(值已经一致)。

    ⚠️ **`.env` 本身不动**:仍由 `get_settings()` 读(不少用例就是按它写的)。
    本次只治"环境变量污染",这是两件事。
    """
    fields = {name.upper() for name in Settings.model_fields}
    removed = {k: v for k, v in os.environ.items() if k.upper() in fields}
    for key in removed:
        os.environ.pop(key, None)
    os.environ.update(_TEST_ENV_DEFAULTS)
    return removed


_REMOVED_ENV = _isolate_settings_env()


@pytest.fixture(scope="session", autouse=True)
def _restore_outer_env():
    """会话结束把摘掉的环境变量还回去(同进程内再跑别的工具时不留副作用)。"""
    yield
    for key in _TEST_ENV_DEFAULTS:
        os.environ.pop(key, None)
    os.environ.update(_REMOVED_ENV)


@pytest.fixture
def settings() -> Settings:
    """测试用 Settings:跳过 .env,使用受控阈值。"""
    return Settings(
        _env_file=None,
        is_dev=True,
        use_proxy=False,
        top_n=5,
        growth_threshold=0.3,
        request_delay_seconds=0.0,
        xianyu_request_delay=0.0,
    )


@pytest.fixture(autouse=True)
def _no_shutdown_marker(monkeypatch):
    """测试**不许**往仓库的 `data/` 写「优雅关停」标记。

    ⚠️ **为什么**(2026-10-08 发现):

    1. 4 处测试用 `with TestClient(create_app()) as c:` —— 退出时走 lifespan 的关停,
       于是 `app.platform._mark_graceful_shutdown()` 把 `data/last_shutdown.txt`
       写成**当前时间**;
    2. 而 `scripts/win/notify_restart.py` 的判据是「标记 **< 180 秒** ⇒ 计划内重启,
       **跳过告警**」。

    ⇒ **跑完测试的 3 分钟内,服务真崩了也不会推飞书**(重启照常,只是没人告诉你)。
    这正是本仓最在意的那一类 —— **告警没响 = 假成功**。

    ⚠️ 生产行为**不变**(人主动停机时写标记是对的);这里只是让**测试**别去写它。
    端到端守卫见 `tests/test_shutdown_marker.py` —— 那条测试与"怎么修"无关,
    它直接跑一次真实 lifespan 看标记有没有被动过。
    """
    import app.platform as platform

    monkeypatch.setattr(platform, "_mark_graceful_shutdown", lambda: None)


@pytest.fixture(autouse=True)
def _reset_source_breaker():
    """★ **列表源的熔断是进程级状态**(2026-10-15 从"实例"搬到"模块级")。

    ⚠️ 不每个用例清一次,前一个用例熔断掉的源会让后一个用例**第一次调用就被跳过** ——
    于是"通过 / 失败"取决于**执行顺序**。实测(搬完当天就撞上):
    同一个文件**单跑 1 条红、全量跑 2 条红**,红的那条还换了一个。
    这正是"进程级状态"必须配"每例重置"的原因;顺带也是那条老教训的又一例:
    **一个只在特定顺序下才成立的测试,等于没测。**
    """
    from app.services.wechat import _source as S

    S._SOURCE_TRIPPED.clear()
    yield
    S._SOURCE_TRIPPED.clear()


@pytest.fixture(autouse=True)
def _reset_douyin_verify_cooldown():
    """★ **抖音的验证冷却也是进程级状态**(2026-10-16 接线时发现)。

    `douyin_protocol_source._VERIFY_CACHE` 是**按 user_id 缓存、且从不主动过期**的
    (只有 `note_verify` / `clear_verify` 会动它)。而 `check_credentials` 现在要**读它**
    来决定"这一档到底探没探、能不能自动重导" ⇒ 前一个用例设过冷却,后一个用例就会
    走进"冷却分支" —— 于是**通过与否取决于执行顺序**(与 `_reset_source_breaker`
    是同一条教训:**一个只在特定顺序下才成立的测试,等于没测**)。
    """
    from app.services import douyin_protocol_source as D

    D._VERIFY_CACHE.clear()
    yield
    D._VERIFY_CACHE.clear()


@pytest.fixture(autouse=True)
def _no_real_xhs_cdp(monkeypatch):
    """测试**绝不许**走真实的小红书 CDP 路(2026-10-16 实测踩到)。

    ⚠️ **事故形状**:`resource_presence` 的小红书分支现在**先试 CDP**,而那条路问的是
    `http://127.0.0.1:9222` —— **开发机上那个 Edge 窗口正好开着** ⇒ 单测里 `available()`
    回 True、于是**真的对着小红书搜了一遍**(整个测试文件从 3 秒涨到 **71 秒**,而且
    **拿真账号发了真实请求**)。与"测试发了真飞书"是同一类:**测试碰真世界**。
    ⇒ 默认把 **CDP 端点指向一个死端口** —— `available()` 照旧跑它自己的真代码,
    只是永远连不上(于是回落页面路,而页面路各用例本来就自己桩着)。
    ⚠️ **不桩 `available` 本身**:那会连"测 `available()` 自己"的用例一起按死
    (实测踩到:`test_available_连不上时的提示要指向_launch` 当场变红)。
    """
    from app.services import xhs_cdp_source as C

    monkeypatch.setattr(C, "endpoint", lambda settings=None: "http://127.0.0.1:9")
    #: ⚠️ **连"按需自启"也要按死**(2026-10-16 又踩):CDP 连不上时那条代码会**真的去起一个
    #: Edge** 并等它起来 —— 于是单测**开真浏览器、整个文件从 3 秒涨到 79 秒**
    #: (第二次同类事故:测试碰了真世界)。
    #: ⚠️⚠️ **但不要替换 `ensure_running` 本身** —— 那会让 `inspect.getsource(ensure_running)`
    #: 拿到**替身的半个 lambda**,于是"查启动参数"那条守卫 `ast.parse` 直接 IndentationError
    #: (与之前替换 `available` 是同一个坑)。⇒ **清空可执行文件候选**:真代码照跑、
    #: 只是永远找不到浏览器 ⇒ 起不来 ✓。
    monkeypatch.setattr(C, "EDGE_CANDIDATES", ())
    #: ⚠️ 同理:**进程检查**也要按死 —— 它会真的去问 PowerShell(`Get-CimInstance`,超时 30 秒),
    #: 于是一个用例能拖 30 秒(实测:两个文件从 18 秒涨到 82 秒)。
    #: 返回 False = "查不到" ⇒ 退化成"只看端口"(正是文档里写的降级路径)。
    monkeypatch.setattr(C, "_profile_browser_running", lambda d: False)
    yield


@pytest.fixture(autouse=True)
def _no_real_feishu(monkeypatch):
    """测试**绝不许**真的往飞书投递消息(2026-10-16 实测踩到)。

    ⚠️ **事故**:`test_续期后刷新失败要告警` 触发了 `notify_incident`,而它**真的发出去了** ——
    管理员群收到一条「🔴 迅雷 refresh_token 已失效,需重新扫码」,**里面还带着测试替身的
    traceback**(`_wire.<locals>.<lambda>() takes from 1 to 2 positional arguments but 3 were given`)。
    后果:用户拿着这条**假告警**去重扫一个**根本没坏**的账号。

    ⇒ **假告警与假成功一样有害**(狼来了:多来几次,真出事时就没人信了)。
    ⇒ 拦在**唯一的出口**上(`FeishuClient.send` / `send_card`),默认**不可投递**。
    要断言"发了什么"的用例**自己 monkeypatch**(本仓既有用例一直都是那么写的)。
    ⚠️ 返回 `False`(而不是抛)是为了不误伤"网络失败返回 False"这类既有语义 ——
    这次要治的是**真的投递出去了**,不是返回值。
    """
    from app.services import feishu_client

    def _blocked(self, *a, **k):  # noqa: ARG001
        return False

    monkeypatch.setattr(feishu_client.FeishuClient, "send", _blocked, raising=False)
    monkeypatch.setattr(feishu_client.FeishuClient, "send_card", _blocked, raising=False)
