"""链路全检:每条采集/转存链到底通不通(2026-10-04)。

用户口径:「**链路都跑通之后来完善 agent**」—— 所以先要有一个**可重复跑**的体检,
而不是靠人回忆。每条链给出 绿/黄/红 + **判据**(判据要能自己复核)。

运行:`python scripts/chain_health.py`(`--json` 出机器可读)

判据分三层,缺一层都不算"通":
  ① **依赖在不在**(容器/库/venv/登录档案)—— "没装"和"装坏了"要分开;
  ② **凭证活不活**(各平台 Cookie 有没有、最近更新);
  ③ **最近有没有真产出**(运行记录 + 落库数据)—— ⚠️ **这一层最容易被漏掉**:
     前三层全绿而产出恒 0,那才是最难查的("静默失败=假成功")。

⚠️ 本脚本**只读**:不打任何写接口、不改库。探测用的网络请求都是 GET/只读。
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta

from app.utils import get_logger

logger = get_logger(__name__)

GREEN, YELLOW, RED = "🟢", "🟡", "🔴"

# 每条链:`(名称, 运行记录 kind, 期望产出的字段, 说明, 侧)`
# 「期望产出字段」取运行记录 detail 里的 `key=数字`;`None` = 该链没有产出计数
# 「侧」:`"local"`(默认,跑在本机)/ **`"remote"`(跑在远程,本机库看不到它的运行记录)**
#   ⚠️ **远程侧不能按本地标准判红** —— 本机与远程**库是独立的**,本机查不到它的记录是**正常的**,
#   报红就是误报。而"一条永远红的项会训练人忽略整份报告"(与"闸门失准"同一条教训)。
CHAINS: tuple[tuple[str, str, str | None, str, str], ...] = (
    ("公众号·发现候选号", "wechat_candidates", "new", "搜狗微信搜索(免费免账号)"),
    ("公众号·收录对标号", "wechat_candidate_import", "listenable", "WeRSS search_mp → MP_WXS_*"),
    ("公众号·监听采文", "wechat_listen", "new", "微信读书 cover + 列表轮转"),
    ("抖音·口令线索", "douyin_leads", "线索", "MediaCrawler 搜索 → 《口令》→ 转存"),
    ("知乎/贴吧·盘链发现", "pan_discovery", None, "自研搜知乎 + MediaCrawler 搜贴吧"),
    ("小红书/贴吧·名字型热度", "resource_presence", "命中", "按资源名探平台 → 库内配对 → 转存"),
    ("迅雷·群转存", "xunlei_group", None, "群消息 → 转存 → 推卡"),
    ("迅雷·盘同步", "xunlei_sync", None, "扫盘入库"),
    ("闲鱼·采集", "xianyu", None, "Playwright 页面内调 mtop", "local"),
    ("本机→远程·公众号数据同步", "remote_sync", "文章", "SSH 隧道推到远程(补 Agent 的输入)"),
    # ⚠️ 这一条重点看 detail 里的「证据触发」:**恒为 0 的那几档**是"接了但没生效"的信号
    # (数据源没接上 / 阈值太高),不是"今天恰好没有" —— 那是本仓反复出现的"废弃链只摘了一半"。
    ("热点选题 Agent(远程)", "hotspot_agent", "热点",
     "detail 尾部带各档证据的触发次数,恒 0 的档要查;⚠️ 它跑在远程,本机库看不到", "remote"),
)


def _rows(db, kind: str, n: int = 5):
    from sqlalchemy import desc, select

    from app.db.models import RunRecord

    return db.scalars(select(RunRecord).where(RunRecord.kind == kind)
                      .order_by(desc(RunRecord.id)).limit(n)).all()


def _latest_map(db, days: int = 7) -> dict[str, datetime]:
    """各 kind 最近一次成功的时间(用于判"多久没动静了")。"""
    from sqlalchemy import func, select

    from app.db.models import RunRecord

    since = datetime.now() - timedelta(days=days)
    rows = db.execute(
        select(RunRecord.kind, func.max(RunRecord.started_at))
        .where(RunRecord.started_at >= since).group_by(RunRecord.kind)).all()
    return {str(k): v for k, v in rows}


def check_dependencies() -> list[dict]:
    """① 依赖层:容器 / 库 / venv / 登录档案。**只读探测**。"""
    import importlib
    import os
    import subprocess

    out: list[dict] = []

    def add(name: str, ok: bool, detail: str) -> None:
        out.append({"name": name, "level": GREEN if ok else RED, "detail": detail})

    # WeRSS 容器(免费列表源;它同时是"号名→fakeid"的解析器)
    try:
        import requests

        r = requests.get("http://127.0.0.1:8001/", timeout=6)
        add("WeRSS 容器(列表源 + 号名解析)", r.status_code < 500,
            f"HTTP {r.status_code}")
    except Exception as exc:  # noqa: BLE001
        add("WeRSS 容器(列表源 + 号名解析)", False, f"连不上:{type(exc).__name__}")

    # aiotieba(贴吧点赞)
    try:
        importlib.import_module("aiotieba")
        add("aiotieba(贴吧点赞 agree)", True, "已安装")
    except ImportError:
        add("aiotieba(贴吧点赞 agree)", False, "未安装 —— pip install aiotieba")

    # MediaCrawler 独立 venv
    mc_py = os.path.join("tools", "MediaCrawler", ".venv", "Scripts", "python.exe")
    add("MediaCrawler venv", os.path.exists(mc_py),
        "存在" if os.path.exists(mc_py) else f"缺 {mc_py}")

    # 贴吧登录档案(MediaCrawler 需要先扫码一次)
    prof = os.path.join("tools", "MediaCrawler", "browser_data")
    n = len([d for d in os.listdir(prof) if "tieba" in d.lower()]) if os.path.isdir(prof) else 0
    add("贴吧登录档案", n > 0, f"{prof} 下 {n} 个贴吧档案")

    # newsnow(热榜长尾)—— ⚠️ **按实例角色判断**:它只服务于 `hot_source`(role=hotspot),
    # 而本机是 role=wechat ⇒ **本来就不该有它**,报红是**误报**。
    # (一条永远红的项会训练人忽略整份报告 —— 与"闸门失准"告警同一条教训。)
    from config.settings import get_settings

    role = str(getattr(get_settings(), "scheduler_role", "all") or "all")
    if role not in ("all", "hotspot"):
        out.append({"name": "newsnow 容器(热榜长尾)", "level": GREEN,
                    "detail": f"本机 role={role} **不该跑热榜**,不需要它"
                              f"(远程 hotspot 侧才要,且需配 HOT_NEWSNOW_URL)"})
        return out
    try:
        r = subprocess.run(["docker", "ps", "-a", "--filter", "name=newsnow",
                            "--format", "{{.Status}}"],
                           capture_output=True, text=True, timeout=15)
        st = (r.stdout or "").strip()
        add("newsnow 容器(热榜长尾)", bool(st) and st.startswith("Up"),
            st or "容器不存在 —— 本机 role=hotspot 需要它,且 .env 要配 HOT_NEWSNOW_URL")
    except Exception as exc:  # noqa: BLE001
        out.append({"name": "newsnow 容器(热榜长尾)", "level": YELLOW,
                    "detail": f"探不到 docker:{type(exc).__name__}"})
    return out


def check_leaked_browsers() -> list[dict]:
    """①b **残留的采集浏览器** —— 它们占住 profile,会让下一轮同平台**撞锁**。

    ⚠️ **为什么必须单独看**(2026-10-05 实测):小红书连挂几天,**根因不是登录过期**
    —— 档案里 `web_session` 到期 **2027-10-05**,登录一直是好的;
    真正的原因是**上一轮超时留下的 10 个 Edge 一直占着那个 profile**
    (`subprocess.run(timeout=)` 只杀直接子进程,而 Edge 是 `main.py` **另起**的),
    于是失败**自我延续**。

    这类东西**不看进程就发现不了**:运行记录里只有一行"xiaohongshu 抓取失败",
    与"要重新扫码"长得**一模一样**,而处置完全相反(一个是清理残留,一个是人工扫码)。

    ⚠️ 判据只看**命令行里带 MediaCrawler 路径的**:机器上还有用户自己的浏览器,
    数错了会得出"采个集开 36 个浏览器"这种离谱结论。
    """
    import sys

    if sys.platform != "win32":
        return [{"name": "采集浏览器残留", "level": GREEN, "detail": "非 Windows,不适用"}]
    try:
        import subprocess

        ps = ("(Get-CimInstance Win32_Process -Filter \"Name='msedge.exe'\" | "
              "Where-Object { $_.CommandLine -like '*MediaCrawler*browser_data*' } | "
              "Measure-Object).Count")
        p = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                           capture_output=True, timeout=60)
        n = int((p.stdout or b"0").decode("utf-8", "ignore").strip() or 0)
    except Exception as exc:  # noqa: BLE001 - 查不到就说不知道,别报成"有残留"
        return [{"name": "采集浏览器残留", "level": YELLOW,
                 "detail": f"查不到({type(exc).__name__})"}]
    if n == 0:
        return [{"name": "采集浏览器残留", "level": GREEN, "detail": "0 个(干净)"}]
    return [{"name": "采集浏览器残留", "level": YELLOW,
             "detail": f"**{n} 个** MediaCrawler 的 Edge 没退,正占着 profile ⇒ "
                       f"该平台下一轮会**撞锁**。现在采集前会自动清(`mediacrawler_source."
                       f"kill_stale_browsers`),下一轮跑到就会好;若持续不为 0 说明"
                       f"**有进程逃出了清理**"}]


def check_credentials(db) -> list[dict]:
    """② 凭证层:各平台 Cookie 在不在、最后一次更新是什么时候。

    ⚠️ **2026-10-05 补:微信读书要"验活",不能只看更新时间**。
    当天实测踩到:这里显示 `🟢 微信读书 —— 更新于 07:50(0 天前)`,
    **而那个 cookie 其实已经 `-2012 登录态失效`** —— 因为 08:02~12:57 之间死的。
    **只看"多久前更新过"是假绿**;一个不验证的指标,绿灯毫无意义
    (与"静默失败=假成功"同一条线)。
    微信读书正好有一个**只读且便宜**的验活口:`/web/shelf/sync`。
    """
    from sqlalchemy import select

    from app.db.models import UserCookie
    from config.settings import get_settings

    out: list[dict] = []
    rows = db.scalars(select(UserCookie).where(UserCookie.user_id == 1)).all()
    have = {str(r.platform): r.updated_at for r in rows}
    # 只需要凭证的平台(公众号/抖音/贴吧走匿名或档案,不在这张表里)
    for plat, label in (("weread", "微信读书(Cookie/书架)"),
                        ("zhihu", "知乎(盘链搜索)"),
                        ("xunlei", "迅雷(转存)"),
                        ("quark", "夸克(转存)"),
                        ("baidupan", "百度网盘(转存)"),
                        ("goofish", "闲鱼(采集)")):
        at = have.get(plat)
        # ⚠️⚠️ **这一行只验 `shelf`,根本不碰列表接口** —— 而"公众号阅读数"走的正是列表接口
        # (`/web/mp/articles` 或 App `book/articles`)。所以这句说明要**跟着这一行一直挂着**,
        # 不管这个 Cookie 在不在。2026-10-06 实测的假绿:这里报 **🟢「验活 ✓(142 个号)」**
        # 的同时,阅读数**已经断了 8 天**(覆盖率 1%)。**一个绿灯去管它管不着的事,就是假绿**;
        # 用户正是被这一行骗过去的(他来问"为什么不带阅读数了",而报告上这里是绿的)。
        # 处置:标签改名为它**实际验的东西**,并把判阅读数的责任明确推给
        # `check_read_num_coverage`(产出层那条,判据取库里的真值)。
        note = ("(⚠️ 本条**只验书架**,不含列表接口 —— 阅读数看「产出」层的「精确阅读数」)"
                if plat == "weread" else "")
        if at is None:
            # 夸克/百度缺失时转存会降级,不一定是红
            out.append({"name": label, "level": YELLOW if plat in ("quark", "baidupan") else RED,
                        "detail": "库里没有该平台 Cookie" + note})
            continue
        age = (datetime.now() - at).days
        level = GREEN if age <= 7 else YELLOW
        detail = f"更新于 {str(at)[:16]}({age} 天前)"
        # **验活**(只对微信读书做:它有一个只读且便宜的探针)
        if plat == "weread":
            alive, why = _weread_alive(db)
            nxt = _next_renewal(get_settings())
            if alive is True:
                detail += f" · 验活 ✓({why})"
            elif alive is False:
                # ⚠️ **失效 ≠ 故障**:`wr_skey` 短效,而续期每 6 小时一次 ——
                # 两次续期之间它本来就可能过期,**那个空窗里没有任何作业要用它**,
                # 下一轮续期会在监听前 10 分钟接上。所以判 **🟡 而不是 🔴**。
                # (我第一次就是在这里误报的:12:57 测到 -2012 就喊"监听要挂了",
                #  而 13:50 的续期会接上 —— 假红比漏报更糟,它会训练人忽略整份报告。)
                # ⚠️ **真正的告警在续期作业那边**:`weread_refresh_tick` 失败时会直接推飞书
                # "请重新复制 Cookie" —— 那个才是"凭据真死了"的信号,不靠这里。
                when = f"下一次续期 {nxt:%H:%M}" if nxt else "续期时间未知"
                level = YELLOW
                detail = (f"当前未通过验活({why}),但**这多半是正常空窗** —— "
                          f"{when},会接在监听之前。若那次续期也失败,"
                          f"续期作业会单独推飞书告警。更新于 {str(at)[:16]}")
            else:
                detail += f" · 验活跳过({why})"
        out.append({"name": label, "level": level, "detail": detail + note})
    out.extend(check_xhs_accounts())
    return out


def check_xhs_accounts() -> list[dict]:
    """**小红书账号档位**:几个、有没有需要人工处理的(过验证 / 重新登录)。

    ⚠️ **为什么单列这一条**(2026-10-07 实测):小红书走**页面渲染**之后,
    它最大的失败模式是"**某个号被踢下线**"——实测 `给号2登录之后号1变成
    「电脑设备登录超限,请重新登录」`(它按**设备/IP**限制同时登录数,与档案隔离无关)。
    而这件事**不会自己好**:得人去重登。
    不主动报的话,表现是"小红书这条链悄悄不产出了",要等到有人问才发现。
    ⇒ 把"账号可用性"做成**每天能看见**的一行,而不是等轮次失败反推。
    """
    try:
        from config.settings import get_settings

        from app.services import xhs_page_source as xp

        profs = xp._profiles(get_settings())
        bad = xp.need_verify_profiles()
        names = [p.name for p in profs]
        need = [p.name for p in profs if str(p) in bad]
        if not names:
            return [{"name": "小红书账号", "level": YELLOW,
                     "detail": "**一个档位都没配**(`XHS_BROWSER_PROFILES`)—— "
                               "这条链不会产出"}]
        if need:
            return [{"name": "小红书账号", "level": RED,
                     "detail": f"{len(need)}/{len(names)} 个号**需要人工处理**"
                               f"(被踢下线或要过安全验证):{'、'.join(need)} —— "
                               f"修法:`python tools/xhs_pass_verify.py <档案目录>`"}]
        return [{"name": "小红书账号", "level": GREEN,
                 "detail": f"{len(names)} 个号({','.join(names)}),没有待处理的"}]
    except Exception as exc:  # noqa: BLE001 - 体检自己挂了要说出来,别静默
        logger.exception("小红书账号体检失败")
        return [{"name": "小红书账号", "level": YELLOW,
                 "detail": f"检查失败:{type(exc).__name__}: {str(exc)[:100]}"}]


def _next_renewal(settings) -> datetime | None:
    """下一次微信读书续期的时间(从 `weread_refresh_cron` 算)。算不出返回 None。"""
    try:
        from apscheduler.triggers.cron import CronTrigger

        return CronTrigger.from_crontab(str(settings.weread_refresh_cron)).get_next_fire_time(
            None, datetime.now()).replace(tzinfo=None)
    except Exception:  # noqa: BLE001 - 算不出就当不知道
        return None


def _weread_alive(db) -> tuple[bool | None, str]:
    """微信读书凭据**验活**(只读):读一次书架。

    返回 `(True/False/None, 原因)`;`None` = **验不了**(别把"不知道"当成"活着")。

    ⚠️ **返回 False 不等于"故障"**(2026-10-05 血的教训):
    `wr_skey` 是**短效**的,而续期是**每 6 小时一次**(03:50/07:50/13:50/19:50,对齐四个监听定点)。
    所以**两次续期之间它本来就可能已经过期** —— 那个空窗里没有任何作业要用它,
    下一轮续期会在监听前 10 分钟接上。**只看"此刻活不活"就会把正常空窗报成故障**
    (我第一次就是这么误报的:12:57 测到 `-2012`,就喊"监听要挂了",而 13:50 的续期会接上)。
    ⇒ 调用方要**结合下次续期时间**判断,别把空窗当故障。
    """
    try:
        import requests

        from app.services.cookie_store import get_cookie
        # ⚠️ `get_cookie` **返回的已经是明文**(它在内部解密)—— 别再 `decrypt_cookie` 一次,
        # 那会双重解密直接 `InvalidToken`(我第一版就这么写的)。
        ck = get_cookie(db, 1, "weread") or ""
        if not ck:
            return False, "库里没有 Cookie"
        r = requests.get("https://weread.qq.com/web/shelf/sync",
                         params={"userVid": "", "synckey": "0", "lectureSynckey": "0"},
                         headers={"User-Agent": "Mozilla/5.0", "Cookie": ck,
                                  "Accept": "application/json"}, timeout=15)
        data = r.json()
        # ⚠️ 微信读书**失败也回 HTTP 200**,判据只能是 `errCode` ——
        # 光看状态码会把它当成功(本仓的"HTTP 200 ≠ 成功",B站那处也栽过)
        code = data.get("errCode")
        if code in (None, 0) and data.get("books"):
            return True, f"{len(data['books'])} 个号"
        return False, f"errCode={code}"
    except Exception as exc:  # noqa: BLE001 - 探针拿不到就说不知道,别报成"死了"
        return None, f"{type(exc).__name__}"


#: 「这个源的失效**等多久都不会自己好**」—— 只有这类才判 🔴。判据宁可漏、不可假红:
#: 假红会训练人忽略整份报告(2026-10-05 我给微信读书误报 🔴 那次就是这么栽的)。
_AUTH_MARKERS = ("200003", "会话失效", "重新扫码", "invalid session", "登录态失效",
                 "授权失效", "token 失效", "重新登录", "凭据失效")
#: **自愈类**(限流/网络/超时)—— 报红就是假红,一律 🟡。
_SELFHEAL_MARKERS = ("200013", "频率限制", "限流", "超时", "timeout", "connection",
                     "ssl", "proxy", "代理")


def _classify_source_error(exc: BaseException) -> tuple[str, str]:
    """源报错归成 `("auth"|"selfheal"|"unknown", 原因文本)`。

    `auth` = **凭据死了、等多久都不会自己好** ⇒ 要人动手,判 🔴;
    其余(限流/网络/超时/认不出)= **自愈或未知** ⇒ 判 🟡 —— **不能报红**。
    """
    text = f"{type(exc).__name__}: {exc}"
    low = text.lower()
    for m in _AUTH_MARKERS:
        if m.lower() in low:
            return "auth", text
    for m in _SELFHEAL_MARKERS:
        if m.lower() in low:
            return "selfheal", text
    return "unknown", text


def check_list_sources(db, settings=None) -> list[dict]:
    """④ 公众号**列表源逐个验活** —— 光看"配了没"是假绿(2026-10-06 补)。

    ⚠️ 与 10-05 给微信读书补验活是**同一个病**:`MultiSourceClient` 把某个源的失效
    当成"换下一个源"的 WARNING 吞掉,链路照跑 ⇒ **降级是静默的**。
    实测代价:`wemp`(自研兜底,本该是"WeRSS 挂了"的保险)自 **2026-10-02 14:00** 起
    每轮报 `200003 会话失效`,**连着 4 天没人知道**;而每天 09:30 推管理群的那份体检报告上,
    **它连一行都没有**。保险失效而无人察觉,比没有保险更糟 —— 人会以为自己有兜底。

    ⚠️ **被动记日志救不了它**:WeRSS 答上了的号,`MultiSourceClient` **命中即返回**,
    `wemp` 根本不会被调用 —— 它的死在日志里**也不会出现**。所以只能**主动**拿一个真号
    逐个去问(每源一次请求,日级频率可忽略)。

    ⚠️ 本函数**只读**(GET 拉一页列表),且**绝不抛**:体检里一个探针崩掉不该毁掉整份报告
    (与 `_weread_alive` 同一条纪律)。
    """
    from sqlalchemy import select

    from app.db.models import WechatBenchmark
    from config.settings import get_settings

    settings = settings or get_settings()
    try:
        from app.services import wechat_monitor as _wm   # 走门面:子模块直 import 会循环导入

        backends = _wm.configured_backends(settings, session=db, user_id=1)
    except Exception as exc:  # noqa: BLE001 - 体检不吃异常
        return [{"name": "公众号·列表源", "level": YELLOW,
                 "detail": f"取不到源清单({type(exc).__name__}: {str(exc)[:80]}),本轮跳过验活"}]
    if not backends:
        return [{"name": "公众号·列表源", "level": RED,
                 "detail": "**一个列表源都没配**(WeRSS / wemp 凭据 / 读书平台全缺) —— "
                           "公众号采集没有任何免费全量列表来源"}]

    # 验活要拿**真号**去问:没有对标号就没法验,明说,别给假绿。
    bm = db.scalar(select(WechatBenchmark).where(
        WechatBenchmark.user_id == 1, WechatBenchmark.biz != "").limit(1))
    feed = _wm.feed_biz(bm) if bm is not None else ""
    if not feed:
        return [{"name": f"公众号·列表源({len(backends)} 个已配)", "level": YELLOW,
                 "detail": "库里没有可试的对标号(biz 不是 MP_WXS_* 形态),本轮**跳过**验活 —— "
                           "**跳过不等于通过**,别把这行当绿灯"}]

    out: list[dict] = []
    alive = 0
    for name, be in backends:
        try:
            items = be.mp_articles(feed, page=1, limit=1)
        except Exception as exc:  # noqa: BLE001 - 一个源失败不能毁掉整份报告
            kind, why = _classify_source_error(exc)
            if kind == "auth":
                out.append({"name": f"公众号·列表源·{name}", "level": RED,
                            "detail": f"**凭据已失效,等多久都不会自己好** —— {why[:110]}。"
                                      f"修法:浏览器登录 mp.weixin.qq.com → F12 复制整条 Cookie + "
                                      f"地址栏里的 token → `python scripts/wemp_cred.py "
                                      f'--cookie "..." --token "..."`'})
            else:
                out.append({"name": f"公众号·列表源·{name}", "level": YELLOW,
                            "detail": f"失败,但**看起来会自愈**(限流/网络):{why[:110]} —— "
                                      f"不报红:假红会让人忽略整份报告"})
        else:
            alive += 1
            # ⚠️ 「答了但是 0 条」**算活** —— 不能判黄:WeRSS 空转时每轮都是 0 条(它至今
            #    一篇文章没抓到过),那会变成**恒黄**,而恒黄的项等于没有。
            out.append({"name": f"公众号·列表源·{name}", "level": GREEN,
                        "detail": f"验活 ✓(问「{bm.nickname}」答了 {len(items)} 条;"
                                  f"0 条也可能是该源没收录这个号,不代表它坏了)"})
    # ★ **单点风险单独说**:只剩一个能答的源时降级链实际没有备胎,而"还有源在答"会让
    #    所有产出指标照常绿 —— 只有这一行看得出来。
    if alive <= 1:
        out.append({"name": "公众号·列表源兜底", "level": RED if alive == 0 else YELLOW,
                    "detail": f"**能答的源只剩 {alive} 个** ⇒ `MultiSourceClient` 的降级链"
                              f"实际上没有备胎。" + (
                                  "链路已经完全取不到列表了。" if alive == 0 else
                                  "挂掉的那个源**不影响产出指标**(它答不上时会被静默跳过),"
                                  "所以只能靠这一行看出来。")})
    return out


def check_chains(db, days: int = 3) -> list[dict]:
    """③ 产出层:每条链最近几次运行的产出(判据取自运行记录的 detail)。"""
    import re

    out: list[dict] = []
    for entry in CHAINS:
        label, kind, metric = entry[0], entry[1], entry[2]
        side = entry[4] if len(entry) > 4 else "local"
        rows = _rows(db, kind)
        if not rows:
            # ⚠️ **远程侧的链不能按本地标准判红**:两库独立,本机查不到它的运行记录是**正常的** ——
            # 报红就是误报,而"一条永远红的项会训练人忽略整份报告"(与"闸门失准"同一条教训)。
            if side == "remote":
                out.append({"name": label, "level": YELLOW,
                            "detail": "跑在**远程**,本机库看不到它的运行记录(属正常;"
                                      "要查它得登录远程)"})
            else:
                out.append({"name": label, "level": RED, "detail": "没有任何运行记录"})
            continue
        last = rows[0]
        age_h = (datetime.now() - last.started_at).total_seconds() / 3600
        detail = str(last.detail or "")[:70]
        # 产出值:能从 detail 里解析出就用它,解析不出**不下结论**(可能只是格式变了)
        val = None
        if metric:
            for r in rows:
                m = re.search(rf"(?:^|\s){re.escape(metric)}=(\d+)", str(r.detail or ""))
                if m:
                    val = int(m.group(1))
                    break
        stale_h = age_h > 48
        if last.status not in ("success", "partial"):
            lvl = RED
        elif stale_h:
            lvl = YELLOW
        elif metric and val == 0 and all(
                re.search(rf"(?:^|\s){re.escape(metric)}=0", str(r.detail or "")) for r in rows):
            lvl = YELLOW          # 连着几次都是 0
        else:
            lvl = GREEN
        bits = [f"最近 {str(last.started_at)[:16]}({age_h:.0f}h 前)", last.status]
        if metric:
            bits.append(f"{metric}={val if val is not None else '解析不出'}")
        out.append({"name": label, "level": lvl, "detail": " · ".join(bits) + f" · {detail}"})
    return out


def render(sections: list[tuple[str, list[dict]]]) -> str:
    lines: list[str] = [f"🔗 链路全检(只读 · {datetime.now():%m-%d %H:%M})", ""]
    for title, rows in sections:
        lines.append(f"【{title}】")
        for r in rows:
            lines.append(f"  {r['level']} {r['name']} —— {r['detail']}")
        lines.append("")
    bad = [r for _t, rs in sections for r in rs if r["level"] == RED]
    warn = [r for _t, rs in sections for r in rs if r["level"] == YELLOW]
    lines.append(f"小结:🔴 {len(bad)} · 🟡 {len(warn)} · "
                 f"🟢 {sum(len(rs) for _t, rs in sections) - len(bad) - len(warn)}")
    if bad:
        lines.append("先修红的:" + "、".join(r["name"] for r in bad))
    return chr(10).join(lines)


def check_job_liveness(db) -> list[dict]:
    """⑤ **作业落实性**:注册的作业 vs 真实心跳 —— "该跑没跑"要自己说出来(2026-10-07)。

    ⚠️ **为什么必须加这一条**:2026-10-07 实测,公众号监听轮**从 10-06 20:02 起 30 小时
    一轮没跑**(应用全程在线、别的作业照跑),而**没有任何一处告警** ——
    唯一的守卫 `scripts/job_liveness.py` ①**只有人手动跑** ②它把
    `hour='4,8,14,20'` 这种多定点判成"算不出间隔"直接放过,**监听作业恰好被排除在外**。
    代价是"阅读数又断了"要靠用户来问。⇒ 判活逻辑已提到 `app.services.job_liveness`
    (服务层,脚本只能是薄壳),并且**接进每天这份体检**。

    分级:`从没执行过` 报红(注册了就该跑);`间隔远超预期` 超过期望 6 倍报红,否则黄
    (留出"晚一轮"的余地 —— 告警变噪音就没人看了)。
    """
    from app.services.job_liveness import STALE_FACTOR, audit

    try:
        r = audit(db)
    except Exception as exc:  # noqa: BLE001 - 对账自己挂了也要说出来,别让整份体检断掉
        logger.exception("作业落实性对账失败")
        return [{"name": "作业落实性", "level": YELLOW,
                 "detail": f"对账本身失败:{type(exc).__name__}: {str(exc)[:120]}"}]

    n_jobs = len(r["jobs"])
    if r["baseline"] is None:
        # 心跳机制刚上线/新库:没有基线时"没记录"分不出真假,别报红(那会是一次假问题)
        return [{"name": "作业落实性", "level": YELLOW,
                 "detail": f"共 {n_jobs} 个作业注册;**心跳表还没有基线**(机制刚上线或新库),"
                           f"本次不判'从没跑过'"}]

    # **最慢的几个作业** —— 排期错峰时要的是"谁真的占着库",不是"谁看着重"。
    # ⚠️ 这条在 2026-10-07 加时长采集之前**根本量不出来**(全仓没有作业时长,
    # 见 `RunRecord.finished_at` 的说明),当时只能靠时刻聚类猜。
    slow = sorted(((b.last_duration_ms, jid) for jid, b in r["beats"].items()
                   if getattr(b, "last_duration_ms", None)),
                  reverse=True)[:3]
    slow_txt = ("" if not slow else
                ";最慢:" + "、".join(f"{jid} {ms / 1000:.1f}s" for ms, jid in slow))

    if not r["never"] and not r["stale"]:
        return [{"name": "作业落实性", "level": GREEN,
                 "detail": f"共 {n_jobs} 个作业,全部有心跳且间隔正常{slow_txt}"}]

    items: list[dict] = []
    if slow_txt:
        items.append({"name": "作业落实性", "level": GREEN, "detail": slow_txt.lstrip(";")})
    if r["never"]:
        items.append({"name": "作业落实性", "level": RED,
                      "detail": f"**注册了却从没执行过**({len(r['never'])} 个):"
                                f"{'、'.join(r['never'])} —— 注册成功 ≠ 跑过,"
                                f"查 trigger / 是否被角色挡下 / 是否每次都提前 return"})
    for s in r["stale"]:
        over_h = s["over_s"] / 3600
        expect_h = s["expect_s"] / 3600
        level = RED if s["over_s"] > s["expect_s"] * STALE_FACTOR * 2 else YELLOW
        items.append({"name": "作业落实性", "level": level,
                      "detail": f"**{s['job_id']}** 已 {over_h:.1f}h 没跑"
                                f"(期望最大空档 {expect_h:.1f}h,超 {STALE_FACTOR}× 即报);"
                                f"上次 {s['last_run_at']:%m-%d %H:%M}"})
    return items


def check_read_num_coverage(db, days: int = 3) -> list[dict]:
    """④ **阅读数覆盖** —— 只看**库里的真值**,不看运行记录字符串。

    ⚠️ **为什么必须单列这一条**(2026-10-05 的真实事故):`check_chains` 判"监听采文"
    看的是 detail 里的 `new=N` —— **采到文章就绿**。而精确阅读数在 detail 里只是个 `ok=0`。
    于是 2026-09-29 ~ 10-05 网页路被账号级拦了**整整一周**:近 3 天入库 162 篇、
    **有阅读数的 0 篇**,而每一层监控都在报绿/黄。

    ⇒ **"采到文章"与"采到阅读数"是两件事,得分开盯。** 这条是对着"它会再坏一次"加的,
    不是对着"这次坏了"。

    判据用**落库数据**而不是 detail 格式:格式会飘(这仓飘过),而库里的 `read_num` 是事实。
    """
    from sqlalchemy import func, select

    from app.db.models import WechatArticle

    since = datetime.now() - timedelta(days=days)
    try:
        total = db.scalar(select(func.count(WechatArticle.id))
                          .where(WechatArticle.created_at >= since)) or 0
        withnum = db.scalar(select(func.count(WechatArticle.id))
                            .where(WechatArticle.created_at >= since,
                                   WechatArticle.read_num > 0)) or 0
    except Exception as exc:  # noqa: BLE001 - 查不到就说不知道,别报成"死了"
        return [{"name": "公众号·精确阅读数", "level": YELLOW,
                 "detail": f"查不到({type(exc).__name__})"}]

    if total == 0:
        return [{"name": "公众号·精确阅读数", "level": YELLOW,
                 "detail": f"近 {days} 天没有新文,无从判断"}]
    ratio = withnum / total
    detail = f"近 {days} 天入库 {total} 篇,其中有阅读数的 **{withnum}** 篇"
    broken = ("查两条路:① 网页 `/web/mp/articles` —— 账号级 `-2041` 时**换新会话也没用**"
              "(2026-10-06 实测:刚续期、验证通过,第 1 个号就被挡);"
              "② App `i.weread.qq.com/book/articles` —— token 过期时「重取」拿回来的是**同一个值**"
              "(模拟器里微信读书登录态失效:在雷电里打开 / 重新登录微信读书 App,"
              "或跑 `scripts/weread_app_login.py` 重取)。")
    # ⚠️⚠️ **"几乎全丢"必须按红灯报**(2026-10-06 修)。此前只有"**恰好一篇都没有**"才判红,
    # 于是 163 篇里有 2 篇侥幸漏进来就掉到 🟡「偏低」—— 而 🟡 是最容易被忽略的那一档。
    # **实测代价**:阅读数从 09-28 起实际上全断,报告上一直写着"🟡 偏低",**8 天没人发现**,
    # 直到用户自己来问"为什么不带阅读数了"。**一个会长期挂在黄档的指标,等于没有指标。**
    # 5% 这条线的依据:轮转窗口正常时覆盖率是三成以上(窗口外的号本来就该显示"—"),
    # 低到 5% 以下时,"还能侥幸漏进来两篇"与"全断"在处置上无从区分。
    if ratio < 0.05:
        return [{"name": "公众号·精确阅读数", "level": RED,
                 "detail": f"{detail}(覆盖率 {ratio:.0%})⇒ **精确阅读数实质上全丢**。{broken}"}]
    if ratio < 0.2:
        return [{"name": "公众号·精确阅读数", "level": YELLOW,
                 "detail": detail + f"(覆盖率 {ratio:.0%},偏低 —— 通常说明列表额度/兜底有问题。" + broken + ")"}]
    return [{"name": "公众号·精确阅读数", "level": GREEN,
             "detail": detail + f"(覆盖率 {ratio:.0%})"}]


def collect_sections(db) -> list[tuple[str, list[dict]]]:
    """跑完三层检查,返回 [(小标题, 结果列表)]。**只读**。调用方负责渲染/推送。

    ⚠️ ② 凭证层**不只是列 Cookie**,还有 `check_list_sources`(逐个列表源验活)——
    10-02 起 `wemp` 失效 4 天、报告上却一行都没有,就是缺了这一段。
    """
    return [("依赖(容器/库/venv/档案)", check_dependencies() + check_leaked_browsers()),
            ("凭证(Cookie 在不在 / 源活不活)", check_credentials(db) + check_list_sources(db)),
            ("产出(最近几次真跑出来的东西)",
             check_chains(db) + check_read_num_coverage(db)),
            # ⑤ **"该跑没跑"** —— 与"跑了但没产出"是两个层次的事,分开报(2026-10-07)。
            ("作业落实性(注册的 vs 真跑的)", check_job_liveness(db))]


def chain_report_tick(settings=None) -> int:
    """**把链路体检推到管理群**(2026-10-05 用户口径:「没有推送小红书、B站、知乎、贴吧、
    多平台的运行情况啊,管理器里面」)。

    ⚠️ 之前只有**公众号**有周期性的总结(`wechat_digest`),**其余几条链(小红书/B站/知乎/贴吧/
    抖音/迅雷/闲鱼)全靠人手动跑脚本看** —— 等于**没有主动通报**。
    这里把体检结果推给**管理群**(不是客户群):它是"要人干活的",与 `wechat_digest` 同一去向。

    ⚠️ **只在有红项时才推**? —— 不。**每天固定推一次**,哪怕是全绿:
    "今天全绿"本身就是运维要知道的信息,而且**只有每天都来,人才会注意到"今天没来"**
    (与 `health_push` 的"该来没来本身就是信号"同一条)。
    """
    from config.settings import get_settings
    from app.db import get_session_local

    settings = settings or get_settings()
    from app.services.feishu_client import FeishuClient, webhook_for
    hook = webhook_for(settings, "admin")
    if not hook:
        return 0
    db = get_session_local()()
    try:
        text = render(collect_sections(db))
    finally:
        db.close()
    ok = FeishuClient(hook, settings.feishu_secret).send(
        "🔗 链路体检(多平台)\n" + text[:3000])
    return 1 if ok else 0


def main(argv: list[str] | None = None) -> int:
    """CLI 入口(脚本侧 `scripts/chain_health.py` 只是薄壳)。"""
    args = argv if argv is not None else sys.argv[1:]
    from app.db.database import get_session_local

    db = get_session_local()()
    try:
        sections = collect_sections(db)
    finally:
        db.close()
    if "--json" in args:
        print(json.dumps({t: rs for t, rs in sections}, ensure_ascii=False, indent=2,
                         default=str))
    elif "--push" in args:
        print("已推送:", chain_report_tick())
    else:
        print(render(sections))
    return 0


if __name__ == "__main__":       # 直接 `python -m app.services.chain_health` 也能跑
    raise SystemExit(main())
