"""抖音签名移植(`app/services/douyin_sign/`)的**离线**正确性守卫(2026-10-07)。

## 它能证明什么、**不能**证明什么(别把这两件事混了)
**能**:我们那份拷贝的行为与上游 f2 在 pin 住的提交(见 `NOTICE.md`)逐字节一致 ——
凭据是**上游自己公布的硬编码期望值**(`XBogus` 那三条),那是**独立于我们这份拷贝的
外部锚点**,不是"我拿自己的输出给自己对账"。
**不能**:证明抖音**今天还认**这个签名。**签名算得对 ≠ 接口没改版**,后者只能靠真实
发一次请求(见 `doc/抖音纯协议-链路拆解.md`)。

## 为什么必须冻住时间与随机种子
两个算法都掺 `time.time()`;`a_bogus` 还掺 `random.random()`(混淆字节)。
不冻住,输出每次都不同,就只能断言"长度像那么回事" —— 那是**最弱的一档断言**,
**把常量换成桩也照样绿**(本仓反复栽在假绿上)。冻死这两者,才换来一个**逐字符可比**
的金标。

⚠️ 因此 `a_bogus` 的金标**只在 `seed=42` 下有意义** —— 换种子就不是这个串了,
这不是算法错了,是那串本来就有随机成分。
"""
import hashlib
import random
from pathlib import Path

import pytest

from app.services.douyin_sign import (
    ABogus,
    BrowserFingerprintGenerator,
    XBogus,
)
from app.services.douyin_sign import abogus as abogus_module
from app.services.douyin_sign import xbogus as xbogus_module

FIXED_TIME = 1700000000.0
#: `test_xbogus.py` 用的**就是这个** UA。⚠️ UA 是进签名的量(它被 RC4+base64 后参与运算),
#: 换 UA 换签名 —— 我第一次拿 `test_abogus.py` 那个 Chrome/130 的 UA 来对下面的期望值,
#: 三条全不中,差点误判成"移植错了"。
UA_XB = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
         "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36")
UA_AB = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
         "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36 Edg/130.0.0.0")
#: 固定指纹。**不要改成 `generate_fingerprint()` 的实时结果** —— 它内部用 `random`,
#: 每次都不一样,金标会变成薛定谔的。
FP_AB = "1355|845|1385|921|0|0|0|0|1864|1042|1376|987|1355|845|24|24|Win32"
PARAMS_AB = ("device_platform=webapp&aid=6383&channel=channel_pc_web"
             "&aweme_id=7380308675841297704&update_version_code=170400&pc_client_type=1"
             "&version_code=190500&version_name=19.5.0&cookie_enabled=true"
             "&screen_width=1920&screen_height=1080&browser_language=zh-CN"
             "&browser_platform=Win32&browser_name=Edge&browser_version=125.0.0.0"
             "&browser_online=true&engine_name=Blink&engine_version=125.0.0.0"
             "&os_name=Windows&os_version=10&cpu_core_num=12&device_memory=8"
             "&platform=PC&downlink=10&effective_type=4g&round_trip_time=50"
             "&webid=7376294349792396827")
#: 金标:seed=42 / t=FIXED_TIME / FP_AB / PARAMS_AB 下的 a_bogus
GOLDEN_ABOGUS = (
    "O7m0/QzVkVxPhESY56KLfY3q61l3YQxI0SVkMD2fgVfPqL39HMYD9exoIBGvXY8jwG/-IeYjy4hbYrC2"
    "rQcy8ZwfHSiq/2AhmfSkKl5Q5xSSs1XaC60grUkq-wsASMq8svH1iAi8qhQCSYmhlxAJ5kIlO62-zo0/9lW="
)
PKG = Path(__file__).resolve().parent.parent / "app" / "services" / "douyin_sign"
_BANNER_RULE = "# " + "=" * 77


class _FrozenTime:
    """只替换模块级 `time` 对象 —— **不动全局 `time` 模块**。

    上游的测试写法是 `monkeypatch.setattr(xbogus_module.time, "time", ...)`,
    而 `xbogus_module.time` **就是全局 time 模块本身**,等于把整个进程的时间冻住了;
    虽说 monkeypatch 会还原,但没必要牵连别的代码。
    """

    @staticmethod
    def time() -> float:
        return FIXED_TIME


@pytest.fixture
def xb(monkeypatch):
    """冻住时间后返回 `XBogus` 类。"""
    monkeypatch.setattr(xbogus_module, "time", _FrozenTime)
    return XBogus


@pytest.fixture
def gen_ab(monkeypatch):
    """冻住时间、且**每次调用前都复位随机种子**的 a_bogus 生成器。

    ⚠️ 种子必须在**每次调用之前**复位 —— 我第一版写成"fixture 里 seed 一次、
    测试里连算两次做可重复性断言",结果第二次就把随机数推进了,拿到不同的串。
    那不是算法不稳定,是 `generate_random_bytes` 本来就**逐次取新随机数**:
    a_bogus 里那段混淆字节每次都得不一样,否则签名可预测,等于没有风控对抗。
    ⇒ 所以"可重复"的正确表述是:**同样的种子 + 同样的输入 ⇒ 同样的输出**。
    """
    def _gen(params=PARAMS_AB, fp=FP_AB, ua=UA_AB, seed=42, body="", t=FIXED_TIME):
        random.seed(seed)

        class _T:
            @staticmethod
            def time() -> float:
                return t

        monkeypatch.setattr(abogus_module, "time", _T)
        return ABogus(fp=fp, user_agent=ua).generate_abogus(params=params, body=body)

    return _gen


# ---------------------------------------------------------------------------
# X-Bogus —— 上游公布的硬编码期望值(**外部锚点**)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "query, expected",
    [
        (
            "aweme_id=7196239141472980280&aid=1128&version_name=23.5.0"
            "&device_platform=android&os_version=2333",
            "DFSzswVY7OiANnTJtmWx-e9WX7jo",
        ),
        ("aid=1988&count=20&cursor=0&x=1234", "DFSzswVYtfxANnTJtmWx-e9WX7rS"),
    ],
)
def test_xbogus_命中上游公布的期望值(xb, query, expected):
    assert xb(UA_XB).getXBogus(query)[1] == expected


def test_xbogus_请求体为空时签名不变(xb):
    """GET 的空 body 与不传 body 必须等价 —— 否则 GET 会被算成另一种签名。"""
    q = "aid=1988&count=20&cursor=0&x=1234"
    assert xb(UA_XB).getXBogus(q)[1] == xb(UA_XB).getXBogus(q, "")[1]


def test_xbogus_请求体参与签名(xb):
    """POST 的 body 必须进签名:上游修过"忽略 body"的 bug,这里钉住修好后的行为。"""
    q = "aid=1988&count=20&cursor=0&x=1234"
    with_body = xb(UA_XB).getXBogus(q, '{"aweme_id": "1"}')[1]
    assert with_body != xb(UA_XB).getXBogus(q, "")[1]


@pytest.mark.parametrize(
    "query",
    ["aid=1988", "aid=19881", "a", "", "abcdef0123456789",
     "0123456789abcdef0123456789abcdef"],
)
def test_xbogus_短查询串按原文计算而非当成十六进制(xb, query):
    """上游 #389:短查询串曾被误当成十六进制摘要去解析。

    判据用**双 MD5(原文)**:这正是算法该做的;若哪天又退回"按十六进制解析",
    这里会红。同时也覆盖了**空串**与"看起来像 hex 的普通串"两个易错边界。
    """
    expect = list(hashlib.md5(hashlib.md5(query.encode()).digest()).digest())
    assert xb(UA_XB).md5_encrypt(query) == expect


def test_xbogus_返回值形状与字符集(xb):
    """**3 元组**、签名 28 字符、且只由自定义字符表组成。

    形状断了(比如上游改成 dataclass)会让所有调用方静默拿到错的东西,
    所以在这里钉死,而不是等线上拼出 `X-Bogus=<tuple>`。
    """
    out = xb(UA_XB).getXBogus("aid=1988")
    assert len(out) == 3
    params, value, user_agent = out
    assert params == f"aid=1988&X-Bogus={value}"
    assert user_agent == UA_XB
    assert len(value) == 28
    assert set(value) <= set(XBogus().character)


def test_xbogus_超短UA也走同一条路(xb):
    """UA 经 RC4+base64 后不足 32 字符时,曾被当成十六进制 —— 上游修过。"""
    assert len(xb("f2").getXBogus("aid=1988")[1]) == 28


@pytest.mark.parametrize("digest", ["abc", "zz"])
def test_xbogus_非法十六进制串要报错(xb, digest):
    """**宁可报错也别猜**:`abc` 是奇长度、`zz` 不是十六进制,都不该被静默容错。"""
    with pytest.raises(ValueError):
        XBogus().md5_str_to_array(digest)


# ---------------------------------------------------------------------------
# a_bogus —— 金标(**对上我们自己那份拷贝的回归锚**,不是外部锚点)
# ---------------------------------------------------------------------------


def test_abogus_金标(gen_ab):
    assert gen_ab()[1] == GOLDEN_ABOGUS


def test_abogus_同种子同输入必得同签名(gen_ab):
    """可重复性 = **种子 + 输入**都相同。这正是金标能成立的前提。"""
    assert gen_ab()[1] == gen_ab()[1]


def test_abogus_换种子签名就该变(gen_ab):
    """随机成分**真的进签名** —— 否则那串混淆字节等于白算。"""
    assert gen_ab(seed=43)[1] != gen_ab(seed=42)[1]


def test_abogus_形状与字符集(gen_ab):
    """**4 元组**、长度 ∈ {164,168,172}、字符全在自定义表内(char/char2 并集)。"""
    out = gen_ab()
    assert len(out) == 4
    value = out[1]
    assert len(value) in (164, 168, 172)
    a = ABogus()
    assert set(value) <= (set(a.character_list[0]) | set(a.character_list[1]) | {"="})


@pytest.mark.parametrize(
    "mutate, why",
    [
        (lambda: {"params": PARAMS_AB + "&x=1"}, "参数进了签名"),
        (lambda: {"fp": FP_AB.replace("1355", "1444", 1)}, "浏览器指纹进了签名"),
        (lambda: {"ua": UA_AB.replace("130.0", "131.0")}, "UA 进了签名"),
        (lambda: {"t": FIXED_TIME + 1000.0}, "时间戳进了签名"),
    ],
)
def test_abogus_输入变则签名变(gen_ab, mutate, why):
    """**灵敏度守卫** —— 防的是"签名其实是个常量/桩"(本仓最忌的假绿)。

    只断言"长度对"挡不住桩函数;这里要求:换掉**每一个**输入维度,输出都得变。
    """
    kw = mutate()
    assert gen_ab(**kw)[1] != gen_ab()[1], (
        f"改了「{why}」签名却没变 —— 这一步没真的参与运算"
    )


def test_指纹生成器给出的是浏览器形状的串():
    """`generate_fingerprint` 每次都不一样(内部随机),但形状必须固定。

    ⚠️ 它**不稳定是有意的** —— 真浏览器指纹本来就逐机不同;别拿它当金标。
    """
    a = BrowserFingerprintGenerator.generate_fingerprint("Chrome")
    b = BrowserFingerprintGenerator.generate_fingerprint("Chrome")
    assert a != b, "指纹变得可预测了?那正是风控想看到的"
    assert a.count("|") == 16 and a.endswith("Win32")


# ---------------------------------------------------------------------------
# 绊索:算法体不许被悄悄改动
# ---------------------------------------------------------------------------

#: 去掉横幅、换行归一化后的算法体 sha256(见 NOTICE.md)
_BODY_SHA256 = {
    "abogus.py": "461b0c093627abbd972a076ce52391f27c8d220dcbdd0434b0f621f342d06015",
    "xbogus.py": "2f1d033ee8db74e6e63e2fd0cbbb105ba2780ab690bc51c778751db68ff3f707",
}


@pytest.mark.parametrize("name", sorted(_BODY_SHA256))
def test_算法体未被改动(name):
    """**有意的绊索**:改了算法体就得在这里红一次。

    这不是"禁止改动",而是**逼改动被看见**。真需要改(抖音改版了)时:
    改完更新本测试与 `NOTICE.md` 里的哈希,并在 CHANGELOG 写清**为什么改**。

    ⚠️ 换行先归一化再算 —— 否则 Windows 上 git 的 CRLF 转换会让它无故变红。
    """
    text = (PKG / name).read_text(encoding="utf-8").replace("\r\n", "\n")
    assert text.startswith(_BANNER_RULE), f"{name} 顶部的来源横幅被动了?"
    # ⚠️ 必须连行尾的 `\n` 一起切掉 —— 只切 `# ====...` 会在算法体前面留一个空行,
    # 哈希就对不上了(我第一版正是这么错的,哈希差的就是那个换行)。
    body = text.split(_BANNER_RULE + "\n")[-1]
    got = hashlib.sha256(body.encode()).hexdigest()
    assert got == _BODY_SHA256[name], (
        f"{name} 的算法体被改过。\n  期望 {_BODY_SHA256[name]}\n  实得 {got}\n"
        f"  若是有意为之:更新本测试与 NOTICE.md 的哈希,并在 CHANGELOG 说明原因。"
    )


def test_归属声明与许可证随包分发():
    """Apache-2.0 §4 要求:分发时得带上许可证与改动声明,不能只留个网址。"""
    notice = (PKG / "NOTICE.md").read_text(encoding="utf-8")
    assert "Johnserf-Seed/f2" in notice
    assert "f6be8c0" in notice, "NOTICE 里必须钉住上游提交号,否则追溯不了来源"
    lic = (PKG / "LICENSE.f2.txt").read_text(encoding="utf-8")
    assert "Apache License" in lic and "Version 2.0" in lic
