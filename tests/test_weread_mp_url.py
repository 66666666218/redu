"""`build_mp_url` 的 `~` → `_` 归一化(2026-10-16)。

**为什么单独钉一条**:这一处曾让库里 **26%(531/2041)的公众号原文链点不开**,
而症状在报告上长得像"卡片没做好" —— 用户原话是
「公众号和快手这些虽然有链接了但是**没办法看对标的作品**」「打开一个链接会显示**参数错误**」。

**判据不是猜的**(真浏览器做的控制变量):
  · 库里那条 `…/s/rGQaA4S~OmBCyR0kJcj0qw` ⇒ **微信的「参数错误」页**;
  · 同一个位置换成 `_`                      ⇒ **真实文章**(标题「国考、省考——27公考天晓…」,**5/5 条全部如此**);
  · 换成 `-`                                ⇒ 又是参数错误(所以不是"随便换一个就行")。
⇒ 规则**无歧义**:微信短链 id 是 **base64url**(字母表只有 `A-Za-z0-9-_`),
**`~` 根本不在里面** ⇒ 上游串里出现的 `~` 只能是 `_` 被**按另一个字母表**编码过。
全库扫过:**唯一的非 base64url 字符就是 `~`**(618 处),没有第二种需要映射的。

⚠️ 这组测试同时是**防回退守卫**:旧实现是 `quote(token, safe='~')` —— **原样保留 `~`**,
注释还写着「token 可能含 `~`(**合法字符**)」。谁要是"顺手"把 `safe='~'` 加回去,
或者把 `.replace('~', '_')` 删掉,这里会红。
"""
import os

os.environ.setdefault("JWT_SECRET", "test_secret_0123456789abcdef0123456789abcdef")
os.environ.setdefault("DATABASE_URL", "sqlite://")

from app.services.weread_client import build_mp_url, review_to_url  # noqa: E402


def test_波浪号要换回下划线() -> None:
    """★ 上游 `original_id` 里带 `~` ⇒ 生成的链**必须**把 `~` 换成 `_`。"""
    assert build_mp_url("rGQaA4S~OmBCyR0kJcj0qw") == (
        "https://mp.weixin.qq.com/s/rGQaA4S_OmBCyR0kJcj0qw")


def test_下划线原样保留() -> None:
    """本来就是 `_` 的不许再动 —— 别把这条规则做成"双向替换"。"""
    assert build_mp_url("rGQaA4S_OmBCyR0kJcj0qw") == (
        "https://mp.weixin.qq.com/s/rGQaA4S_OmBCyR0kJcj0qw")


def test_生成出来的链里永远没有波浪号() -> None:
    """**不变式**:base64url 里没有 `~` ⇒ 我们产出的短链里也不该出现它。"""
    for tok in ("a~b", "~~~", "x~y_z-1", "正常中文~尾巴"):
        assert "~" not in build_mp_url(tok), tok
    assert build_mp_url("") == ""          # 空串照旧回空,别造出半条链


def test_review_to_url_也走同一处() -> None:
    """`reviewId`(`MP_WXS_<bookId>_<token>`)那条路必须**同一规则**,不许各写一份。"""
    out = review_to_url("MP_WXS_3902714095_rGQaA4S~OmBCyR0kJcj0qw", "MP_WXS_3902714095")
    assert out == "https://mp.weixin.qq.com/s/rGQaA4S_OmBCyR0kJcj0qw"


# ---------------------------------------------------------------------------
# 原文链**能不能给客户点**(2026-11-25 事故:App 路的 `originalId` 拼出死链)
# ---------------------------------------------------------------------------

def test_短_token_算可用() -> None:
    from app.services.weread_client import is_usable_mp_url

    assert is_usable_mp_url("https://mp.weixin.qq.com/s/FZd_K4FKBiDA3u1Q9XIrsg")


def test_App路长_token_判不可用() -> None:
    """★ 这就是那次事故的形态:75 字符 `WR1…`,实测 6/6 是「参数错误」页。

    **这是止血闸**:库里的行照留(读数/去重/存在性都有意义),
    但客户点的那三处出口(标题回落、「原文」列、爆点苗头)一律不许给。
    """
    from app.services.weread_client import is_usable_mp_url

    bad = ("https://mp.weixin.qq.com/s/WR16GyCQy62oBe2AS48zp8tVCvw6f1CTs34iBLv8bB9BmEAz4aoG"
           "MY7baBKaCPwGQj7Gs6QA344")
    assert not is_usable_mp_url(bad)


def test_未来出现的新长度也算不可用() -> None:
    """判据是「**不是 22 字符**」不是「是 75 字符」——

    后者只挡已知的一种;前者对任何新形态一律保守(宁可少给一个链接)。
    """
    from app.services.weread_client import is_usable_mp_url

    for tok in ("abc", "x" * 30, "x" * 128):
        assert not is_usable_mp_url(f"https://mp.weixin.qq.com/s/{tok}"), tok


def test_别的域名不许被这条规则波及() -> None:
    """我方盘链 / 其他链必须原样放行 —— 这个判据只管 mp.weixin 的 `/s/`。"""
    from app.services.weread_client import is_usable_mp_url

    assert is_usable_mp_url("https://pan.quark.cn/s/0123456789ab")
    assert is_usable_mp_url("https://pan.baidu.com/s/1abcdefg")


def test_空串与查询串() -> None:
    from app.services.weread_client import is_usable_mp_url

    assert not is_usable_mp_url("")
    assert not is_usable_mp_url(None)
    # 查询串/锚点不参与长度判定
    assert is_usable_mp_url("https://mp.weixin.qq.com/s/FZd_K4FKBiDA3u1Q9XIrsg?p=1#x")
