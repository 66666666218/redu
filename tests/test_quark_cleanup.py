"""夸克清理计划(`classify`)的守卫(2026-10-08)。全部离线。

`classify` 是**纯函数**(不碰网络、不碰盘),所以这里能把每条判据钉死。
它决定的是**要删什么** —— 本仓唯一一个"判错了就真删了"的地方,所以每条都要有守卫。

## 三层,一层比一层宽,但都有明确的反面用例
| 判据 | 删? | 反面(不删)由谁守 |
|---|---|---|
| **强模式**(名字就是网盘/加群指令) | 删,单家也删 | — |
| **弱模式** + 横跨 ≥2 个资源包 | 删 | `test_弱模式只出现在一个包里只报告不删` |
| **跨目录重复**(名字+大小一样) | 删旧的、留新的 | `test_小体积重复不参与` |
| **保护名单**(分享链 first_fid / 简介.doc) | **永不删** | 两条专属用例 |

⚠️ 最要紧的一条是**没扫完**:`build_plan` 会把 `complete=False` 带出来,而
`quark_dup_apply.py` 默认**拒绝执行** —— 因为"只出现在一个包里"这个判断
在没扫完时**是假的**(没扫到的家里可能也有一份)。
"""
from __future__ import annotations

import pytest

from app.services.quark_dup import (
    DUP_MIN_BYTES,
    classify,
)

#: ⚠️ 三个月**都在 10 月** —— 用 `_0929` 当"旧的那个"会踩到范围闸(9 月不在范围内),
#: 于是"跨目录重复"这条**看着在测、其实什么都没测**。测试当场抓到了这个。
ORDER = ["redian监听_1007", "redian监听_1004", "redian监听_1001"]
SEP_HOME = "redian监听_0929"


def _f(name: str, fid: str, size: int, home: str, path: str = "x") -> dict:
    return {"home": home, "path": path, "name": name, "size": size, "fid": fid}


def _run(files, prot_fids=frozenset(), prot_names=frozenset(), prefix="10"):
    return classify(files, set(prot_fids), set(prot_names),
                    mmdd_prefix=prefix, home_order=ORDER)


def _names(plan, key="delete") -> set[str]:
    return {d["name"] for d in plan[key]}


# ---------------------------------------------------------------------------
# 引流:强模式 / 弱模式
# ---------------------------------------------------------------------------


def test_强模式单家也删() -> None:
    """★ 真样本:`切勿卸载网盘,后续资源会同步更新到本网盘.txt`(60 字节)——
    这种名字本身就是一条**网盘使用指令**,资源本体不可能叫这个。"""
    plan = _run([_f("切勿卸载网盘，后续资源会同步更新到本网盘.txt", "A", 60, ORDER[0])])
    assert _names(plan) == {"切勿卸载网盘，后续资源会同步更新到本网盘.txt"}


def test_另一个真样本_先保存在观看更高清() -> None:
    """真样本(目录名,但归类逻辑与文件一致):`先保存在观看更高清`。"""
    plan = _run([_f("先保存在观看更高清", "A", 0, ORDER[0])])
    assert _names(plan) == {"先保存在观看更高清"}


def test_弱模式一律只报告_哪怕横跨多个包() -> None:
    """★★ **这条改过一次,记下来免得改回去**(2026-10-08 真数据上当场发现)。

    原来的规则是「弱模式 + 横跨 ≥2 个包 ⇒ 删」。它在**这份盘上逻辑失效**:
    同一份资源本来就被重复保存到多个家(这正是本次要清的东西),
    于是**包里几乎每个文件都"横跨多个家"** ⇒ 那条判据退化成
    「名字里带 必看/教程/注意/截图 就删」。实测误判到的真名字:
      `高中英语151组最容易拼错的单词，考场上一定要注意！.docx`(真学习资料,只因含「注意」)
      `视频教程(1).mp4` / `编年史教程1(1).zip` / `道格测试入口以及教程.rar`

    而这类总共只有 269 MiB —— **省不到空间,却会误删资源**。⇒ 一律只报告。
    """
    plan = _run([_f("下载说明.txt", "A", 100, ORDER[0]), _f("下载说明.txt", "B", 100, ORDER[1])])
    assert plan["delete"] == [], "弱模式不许自动删(横跨多个包也不许)"
    assert len(plan["review"]) == 1, "但要进待确认清单给人看"


def test_教程类名字不会被自动删() -> None:
    """实测误判的原样复现 —— 这两个都**不能**自动删。"""
    plan = _run([_f("高中英语151组最容易拼错的单词，考场上一定要注意！.docx", "A", 117_000, ORDER[0]),
                 _f("高中英语151组最容易拼错的单词，考场上一定要注意！.docx", "B", 117_000, ORDER[1]),
                 _f("视频教程(1).mp4", "C", 900_000, ORDER[0]),
                 _f("视频教程(1).mp4", "D", 900_000, ORDER[2])])
    assert plan["delete"] == [], f"教程/注意类名字被判成引流了:{plan['delete']}"


def test_弱模式只出现在一个包里只报告不删() -> None:
    """⚠️ 这条**必须**不删:一个包里孤零零的 `使用说明.pdf` 极可能就是资源本体的一部分。
    宁可漏删(用户看得见清单),不可误删(不可逆)。"""
    plan = _run([_f("字体安装方法说明.docx", "A", 14905, ORDER[0])])
    assert plan["delete"] == []
    assert _names(plan, "review") == {"字体安装方法说明.docx"}


def test_名字像广告但不在任何模式里的不动() -> None:
    """反面对照 —— 不然规则会越滚越宽,最后把资源吃掉。"""
    plan = _run([_f("高性价比人生指南-HowToLiveBetter-现代-338页.pdf", "A", 6_285_115, ORDER[0])])
    assert plan["delete"] == [] and plan["review"] == []


# ---------------------------------------------------------------------------
# 跨目录重复
# ---------------------------------------------------------------------------


def test_跨目录重复删旧的留新的() -> None:
    """★ 用户报的那件事:`高性价比人生指南-...pdf` 在 `_0929` 与 `_1004` 各一份。

    留**最近在用的那个家**(`ORDER` 里越靠前越新 ⇒ 留 `_1004`)。
    ⚠️ "留哪份"**必须显式传 `home_order`** —— 写死成按名字排序会看着对但没依据。
    """
    big = DUP_MIN_BYTES + 1
    plan = _run([_f("资源包.pdf", "OLD", big, ORDER[2]), _f("资源包.pdf", "NEW", big, ORDER[1])])
    assert [d["fid"] for d in plan["delete"]] == ["OLD"], f"该删旧家的那份,实际 {plan['delete']}"
    assert "redian监听_1004" in plan["delete"][0]["why"]


def test_小体积重复不参与() -> None:
    """删小的重复省不到空间,却一样承担误删风险 ⇒ 不碰(引流另有一条规则)。"""
    plan = _run([_f("某文件.dat", "A", 1024, ORDER[0]), _f("某文件.dat", "B", 1024, ORDER[1])])
    assert plan["delete"] == []


def test_同一个家里同名同大小不算重复() -> None:
    """父目录不同才算跨目录重复;同一个家里出现两次是另一类问题,不在这里处理。"""
    big = DUP_MIN_BYTES + 1
    plan = _run([_f("资源包.pdf", "A", big, ORDER[0]), _f("资源包.pdf", "B", big, ORDER[0])])
    assert plan["delete"] == []


def test_大小不同不算同一份() -> None:
    big = DUP_MIN_BYTES + 1
    plan = _run([_f("资源包.pdf", "A", big, ORDER[0]), _f("资源包.pdf", "B", big + 7, ORDER[1])])
    assert plan["delete"] == []


# ---------------------------------------------------------------------------
# 保护(一票否决)
# ---------------------------------------------------------------------------


def test_被分享链指着的副本永不删() -> None:
    """★★ 最要紧的保护:删掉它 = **已经发给别人的链接变「已失效」**,发出去就收不回。
    这里强模式也得让路 —— 宁可留一个引流文件,不可弄死一条已发出的链。"""
    plan = _run([_f("切勿卸载网盘，后续资源会同步更新.txt", "SHARED", 60, ORDER[0])],
                prot_fids={"SHARED"})
    assert plan["delete"] == []
    assert plan["protected_left"] and "分享链" in plan["protected_left"][0]["why"]


def test_部分副本被保护时只删没被保护的那些() -> None:
    plan = _run([_f("下载前必看.docx", "SHARED", 10671, ORDER[0]),
                 _f("下载前必看.docx", "FREE", 10671, ORDER[1])],
                prot_fids={"SHARED"})
    assert [d["fid"] for d in plan["delete"]] == ["FREE"]


def test_分享标题级保护_连名字一起护住() -> None:
    """`file_num>1` 的链只给得出 `first_fid`,其余项只能**按名字**保护(名字能连坐,比漏保护好)。"""
    plan = _run([_f("音乐合集.zip", "A", 2_000_000, ORDER[0]),
                 _f("音乐合集.zip", "B", 2_000_000, ORDER[1])],
                prot_names={"音乐合集.zip"})
    assert plan["delete"] == []


def test_我们自己的简介永不删() -> None:
    """刚放进每个资源里的宣传简介 —— 强模式也护不住它,得靠**专属**名单。"""
    plan = _run([_f("简介.doc", "A", 2_976_256, ORDER[0])])
    assert plan["delete"] == [] and plan["review"] == []


# ---------------------------------------------------------------------------
# 范围:只动 10 月的账
# ---------------------------------------------------------------------------


def test_只动指定月份的副本() -> None:
    """用户口径「先删 **10 月份之内**保存进来的」⇒ 9 月的家完全不动。"""
    plan = _run([_f("切勿卸载网盘，后续资源会同步更新.txt", "SEP", 60, SEP_HOME)],
                prefix="10")
    assert plan["delete"] == [], "9 月的家不该被这次清理碰到"
    plan10 = _run([_f("切勿卸载网盘，后续资源会同步更新.txt", "OCT", 60, "redian监听_1004")],
                  prefix="10")
    assert len(plan10["delete"]) == 1


# ---------------------------------------------------------------------------
# 执行器:只走回收站 + 一批失败不中断
# ---------------------------------------------------------------------------


def test_删除只走回收站_永不彻底删() -> None:
    """★ 「彻底删」和「回收站」的差别是**能不能捞回来**。
    判据错了还能从回收站捡回来,彻底删就真没了 —— 盘商回收站是现成的兜底,没有理由不用。"""
    from app.services.quark_dup import apply_plan

    seen: list = []

    class _Q:
        def delete_files(self, fids, *, to_recycle=True):
            seen.append((list(fids), to_recycle))
            return {}

    plan = {"delete": [{"fid": "A", "size": 10}, {"fid": "B", "size": 20}]}
    out = apply_plan(_Q(), plan)
    assert seen == [(["A", "B"], True)], f"必须带 to_recycle=True,实际 {seen}"
    assert out["deleted"] == 2 and out["bytes"] == 30 and out["failed"] == []


def test_一批删除失败要记下来并继续下一批() -> None:
    """⚠️ 静默吞掉失败 = "看起来删干净了"。失败的批必须进 `failed` 被报出来。"""
    from app.services.quark_dup import apply_plan

    class _Q:
        def __init__(self):
            self.n = 0

        def delete_files(self, fids, *, to_recycle=True):
            self.n += 1
            if self.n == 1:
                raise RuntimeError("夸克接口 500")
            return {}

    plan = {"delete": [{"fid": f"F{i}", "size": 1} for i in range(3)]}
    out = apply_plan(_Q(), plan, batch=1)
    assert out["deleted"] == 2, "后两批该继续执行"
    assert len(out["failed"]) == 1, "失败的那批必须被记下来"


# ---------------------------------------------------------------------------
# ★ 家的「新旧」不能按 updated_at 排(2026-10-08 实测踩到)
# ---------------------------------------------------------------------------


def test_家的排序按名字里的日期_不按_updated_at() -> None:
    """★ 实测形状:`redian监听_0929` 的 `updated_at` 被改成了**当天**(只是被动过),
    于是按它排序 `_0929` 会跑到 `_1007` 前面 —— 而 `_0929` 是 9 月 29 日建立的家。

    这个顺序**直接决定"跨目录重复留哪份"**:排错了就会留下旧家的那份、
    删掉当前家正在用的那份。名字里的日期是建立那天写死的,不会被别处触碰。
    """
    from app.services.quark_dup import list_homes

    class _Q:
        def search_files(self, kw, size=20):
            return [
                {"file_name": "redian监听_0929", "fid": "S", "dir": True,
                 "updated_at": 9_999_999_999},          # ← 被动过,时间戳最"新"
                {"file_name": "redian监听_1007", "fid": "N", "dir": True,
                 "updated_at": 1_000},
                {"file_name": "redian监听", "fid": "NODATE", "dir": True,
                 "updated_at": 9_999_999_998},          # ← 同样被碰过,但没日期
            ]

    assert [h["fid"] for h in list_homes(_Q())] == ["N", "S", "NODATE"], \
        "必须按名字里的 MMDD 倒序(1007 > 0929 > 无日期)"


def test_认领旧家时也按名字里的日期_不按_updated_at() -> None:
    """同一件事在 `_adopt_existing_dir` 上更致命:它决定**后续资源写进哪个家**。
    按 updated_at 选,会把新资源写进一个早已停用的旧目录,而查重**按目录**做 ⇒ 又一轮重复。"""
    from app.services.quark_transfer import QuarkTransfer

    qt = QuarkTransfer("ck")

    qt.search_files = lambda kw, size=20: [
        {"file_name": "redian监听_0929", "fid": "S", "dir": True, "updated_at": 9_999_999_999,
         "pdir_fid": "0"},
        {"file_name": "redian监听_1007", "fid": "N", "dir": True, "updated_at": 1,
         "pdir_fid": "0"},
    ]
    assert qt._adopt_existing_dir("0", "redian监听") == "N"


def test_分享名单拉不完时必须抛_不能返回半份(monkeypatch) -> None:
    """★★ 对着一次**真实踩到的**缺口加的:实测该账号分享总数**超过 2000 条**
    (第 45 页仍有 50 条、第 60 页空),而原来的翻页上限正好是 40 页 ⇒
    保护名单被**静默截断**,更早发出的链接背后的文件没进名单 ——
    而"没进名单"在这套判据里等于"可以删"。

    这条守卫的是:拉不满时**抛**,而不是返回一个"看着有内容"的半份。
    半份比空更难发现 —— 它有 2000 条,谁都会以为齐了。
    """
    from app.services.quark_dup import SHARE_PAGE_CAP, protected_fids

    class _Q:
        def _request(self, *a, **kw):
            return {"data": {"list": [{"first_fid": "F", "file_num": 1}] * 50}}

    with pytest.raises(RuntimeError):
        protected_fids(_Q())

    class _Short:
        def __init__(self):
            self.pages = 0

        def _request(self, *a, **kw):
            self.pages += 1
            lst = [{"first_fid": "A", "file_num": 1}] * 50 if self.pages < 3 else []
            return {"data": {"list": lst}}

    fids, names, n = protected_fids(_Short())
    assert fids == {"A"} and n == 100, "短页即到底,不该抛"

    assert SHARE_PAGE_CAP >= 400, "上限太小又会截断;实测分享已超 2000 条"


# ---------------------------------------------------------------------------
# ★ 第二次真数据误判:广告语可以**挂在资源名尾部**
# ---------------------------------------------------------------------------


def test_括号里的标注不算引流_不能删资源本体() -> None:
    """★★ 对着一次**差一点就删**的误判加的(dry-run 里当场看到)。

    实测名字:
      `课时19485_乘风破浪会有时-《乘风破浪会有时》ppt【公众号dc008免费分享】.pptx`  ← 真课件
      `黄一鸣曝王S聪聊天记录【先保存才能看】.rar`                                  ← 真资源 + 广告后缀
    它们的"广告语"在**括号标注**里,而**文件本身就是资源**。
    ⇒ 判据必须是"**剥掉括号内容之后**仍命中强模式",不是"名字里出现过"。
    """
    from app.services.quark_dup import is_promo_name

    assert not is_promo_name("课时19485_乘风破浪会有时-《乘风破浪会有时》ppt【公众号dc008免费分享】.pptx")
    assert not is_promo_name("黄一鸣曝王S聪聊天记录【先保存才能看】.rar")
    assert not is_promo_name("高中英语151组最容易拼错的单词（注意）.docx")
    # 而真正的引流文件(整名即广告)剥完一字不少,照样命中
    assert is_promo_name("切勿卸载网盘，后续资源会同步更新到本网盘.txt")
    assert is_promo_name("长期招抖音作品代发兼职，每天花2分钟发布一下就行.PNG")
    assert is_promo_name("需要可扫码打印纸质版.png")


def test_过宽的强模式必须已去掉() -> None:
    """`公众号`/`推广`/裸 `扫码`/裸 `二维码` 实测会把真资源判成引流
    (`课时…【公众号dc008免费分享】.pptx`、`软件推广合作协议.docx`)⇒ 不许再进强模式表。"""
    from app.services.quark_dup import STRONG_PATTERNS

    for bad in ("公众号", "推广", "扫码", "二维码", "代发", "引流"):
        assert bad not in STRONG_PATTERNS, f"过宽的强模式「{bad}」又回来了"


def test_软件推广合作协议这类真文档不该被判引流() -> None:
    from app.services.quark_dup import is_promo_name

    assert not is_promo_name("软件推广合作协议.docx")


def test_去引流版不是引流_招代发才是() -> None:
    """`啊腾去引流版.apk` 的「**去**引流版」意思可能正好相反(去广告的干净版);
    `快递代发代收合作协议.docx` 是**真文档**。⇒ 裸 `引流`/`代发` 不许进强模式表,
    只保留只可能是广告的具体形态(`招代发`/`代发兼职`/`代发助手`)。"""
    from app.services.quark_dup import is_promo_name

    assert not is_promo_name("啊腾去引流版.apk")
    assert not is_promo_name("快递代发代收合作协议,.docx")
    assert is_promo_name("长期招抖音作品代发兼职，每天花2分钟发布一下就行.PNG")
    assert is_promo_name("找抖音代发助手，每天轻松赚奶茶钱.JPG")
    assert is_promo_name("长期招代发简单轻松赚💰.png")


# ---------------------------------------------------------------------------
# 简介保护:从**配置的简介目录**现取,不写死文件名(2026-10-13)
# ---------------------------------------------------------------------------
#
# 背景(全部实测,不是设想):
# 用户把 `简介.doc` **改名**成 `热点监控第一时间获取.doc`,而保护名单里写死的就是
# 前一个名字 ⇒ 那份保护**从改名那一刻起就静默失效了**,没有任何地方会报出来。
# 而简介恰恰是盘上**最容易被判死**的东西 —— 它会被复制进每一个资源,于是:
#   ① 广告图那种名字**命中强模式**(`代发日入`)⇒ 单家也删;
#   ② 简介本身在多个家里**同名同大小** ⇒ 判成"跨目录重复",只留最新那个家。
# 终态就是:资源里的简介被自己的夜间清理一个个吃掉,而日志一切正常。


class _IntroQt:
    """够 `build_plan` 跑通的**离线**假件(不碰网络)。"""

    def __init__(self, promo: tuple[str, ...] = ("/监听宣传",)) -> None:
        self.homes = [{"file_name": "redian监听_1007", "fid": "H1", "dir": True,
                       "updated_at": 1_700_000_200},
                      {"file_name": "redian监听_1004", "fid": "H2", "dir": True,
                       "updated_at": 1_700_000_100}]
        # 简介目录里的**两个真名、真大小**
        self.promo = [{"file_name": "热点监控第一时间获取.doc", "fid": "P1",
                       "size": 3_283_456, "dir": False},
                      {"file_name": "招兼职代发日入200-500.jpg", "fid": "P2",
                       "size": 1_181_997, "dir": False}]
        # 两份资源(落在两个不同的家)里都已经把简介复制进去了
        res = [{"file_name": "招兼职代发日入200-500.jpg", "fid": "AD", "size": 1_181_997,
                "dir": False},
               {"file_name": "热点监控第一时间获取.doc", "fid": "DOC", "size": 3_283_456,
                "dir": False}]
        self.contents = {
            "H1": [{"file_name": "资源A", "fid": "R1", "size": 0, "dir": True}],
            "H2": [{"file_name": "资源B", "fid": "R2", "size": 0, "dir": True}],
            "R1": [{**x, "fid": x["fid"] + "1"} for x in res],
            "R2": [{**x, "fid": x["fid"] + "2"} for x in res],
            "PROMO": list(self.promo),
        }
        self.dirs = promo

    def search_files(self, keyword, size=50):
        return list(self.homes)

    def list_dir(self, fid="0"):
        return list(self.contents.get(fid, []))

    def _list_dir(self, fid="0"):
        return list(self.contents.get(fid, []))

    def resolve_named_dir(self, name):
        return "PROMO" if str(name).strip("/") in ("监听宣传",) else ""

    def _request(self, method, path, **kw):
        assert "mypage/detail" in path, f"意外请求 {path}"
        return {"data": {"list": []}}          # 短页 ⇒ 保护名单取完


def test_简介目录里的文件护得住_尤其那张名字像广告的() -> None:
    """★★ **改名之后保护必须还在**。两条断言分别对着上面 ① 和 ② 两个死法。"""
    from app.services.quark_dup import build_plan

    plan = build_plan(_IntroQt(), mmdd_prefix="10", protect_dirs=("/监听宣传",))
    assert plan["delete"] == [], f"简介被自己的清理判死了:{[d['name'] for d in plan['delete']]}"
    assert {x["name"] for x in plan["intro_protected"]} == {
        "热点监控第一时间获取.doc", "招兼职代发日入200-500.jpg"}
    assert plan["complete"] is True


def test_没有简介保护时这两个文件真的会被判死() -> None:
    """⚠️ **反面**:没有这条保护,上面那两个文件**确实**会被清掉 ——
    所以上一条断言不是空的(本仓吃过"看着在测其实什么都没测"的亏)。"""
    from app.services.quark_dup import build_plan

    plan = build_plan(_IntroQt(), mmdd_prefix="10", protect_dirs=())
    names = {d["name"] for d in plan["delete"]}
    assert "招兼职代发日入200-500.jpg" in names, "名字命中强模式 ⇒ 单家也会删"
    assert "热点监控第一时间获取.doc" in names, "两家同名同大小 ⇒ 判成跨目录重复"
    assert plan["intro_protected"] == []


def test_默认从配置取简介目录_不用调用方传(monkeypatch) -> None:
    """线上是 `pan_auto_clean` / 脚本各调一次 —— **不靠调用方记得传参数**才生效。"""
    from types import SimpleNamespace

    from app.services import quark_dup
    from app.services.quark_dup import build_plan

    monkeypatch.setattr("config.settings.get_settings",
                        lambda: SimpleNamespace(pan_intro_quark_dir="/监听宣传"))
    assert quark_dup.intro_protect_dirs() == ("/监听宣传",)
    fids, names, detail = quark_dup.collect_intro_protect(_IntroQt(), ("/监听宣传",))
    assert names == {"热点监控第一时间获取.doc", "招兼职代发日入200-500.jpg"}
    assert fids == {"P1", "P2"}, "原件要能按 fid 精确护住"
    assert len(detail) == 2 and all(x["size"] > 0 for x in detail)
    plan = build_plan(_IntroQt(), mmdd_prefix="10")       # ← 故意**不传** protect_dirs
    assert plan["delete"] == [] and len(plan["intro_protected"]) == 2


def test_简介目录取不到时不崩也不改判据() -> None:
    """⚠️ 保护名单只用于**少删**。取不到就回到**旧行为**,绝不许因此多删或整轮失败。"""
    from app.services.quark_dup import build_plan

    qt = _IntroQt()

    def _boom(name):
        raise RuntimeError("网络抖了")

    qt.resolve_named_dir = _boom
    plan = build_plan(qt, mmdd_prefix="10", protect_dirs=("/监听宣传",))
    assert plan["intro_protected"] == []
    assert plan["delete"], "取不到简介目录时,判据必须与旧版一致(该删的还是删)"


# ---------------------------------------------------------------------------
# ★★ 快照 TTL 与「多轮收敛」(2026-10-15)
#
# 实测事故:TTL 写死 8h、而作业**每天**才一轮 ⇒ 每轮开始时上一轮的快照**全已过期**
# ⇒ 每轮从零重扫同样那批家、在**同一处**撞预算、留下**同样几个**家
# ⇒ `complete` 恒 False ⇒ 护栏「没扫完就拒绝删」天天生效
# ⇒ **夸克连着三天一个文件都没删**,而运行记录里那三天**一字不差**
# (`已扫19个家,还差9个`)—— 一个设计成"会收敛"的循环,数字却纹丝不动。


class Test快照TTL与收敛:
    def _build(self, cache, ttl):
        from app.services.quark_dup import build_plan

        return build_plan(_IntroQt(), mmdd_prefix="10", cache=cache, ttl_hours=ttl,
                          protect_dirs=())

    def test_TTL_之内要复用_第二次不再重扫(self, tmp_path) -> None:
        from app.services.quark_dup_cache import ScanCache

        cache = ScanCache(tmp_path / "c.json")
        p1 = self._build(cache, 48)
        assert p1["complete"] is True and len(p1["scanned_now"]) == 2
        p2 = self._build(cache, 48)
        assert len(p2["reused_homes"]) == 2 and p2["cache_miss"] == {}, \
            "TTL 之内应当复用,不该再扫一遍"
        assert p2["calls"] == 0, "复用不该产生任何 list_dir 调用"

    def test_快照过期要报_stale_而不是默默重扫(self, tmp_path) -> None:
        """★ 这条就是那次事故的**判据**:以前过期与「从来没有」都只回 `None`,分不出来。"""
        from app.services.quark_dup_cache import ScanCache

        cache = ScanCache(tmp_path / "c.json")
        self._build(cache, 48)
        p = self._build(cache, -1)          #: 负 TTL = 必然过期(不依赖 sleep)
        assert p["cache_miss"] == {ScanCache.STALE: 2}, (
            f"过期必须报 `stale`(那是「该改 TTL」的信号);实际 {p['cache_miss']}")

    def test_没扫完要把重扫原因写进运行记录(self) -> None:
        from app.services.pan_auto_clean import _fmt

        line = _fmt({"pan": "夸克", "state": "incomplete", "scanned": 19,
                     "remaining_homes": 9, "planned": 3,
                     "cache_miss": {"stale": 19}, "calls": 2500, "ttl_hours": 8.0})
        assert "stale" in line and "TTL" in line and "2500" in line, line

    def test_默认_TTL_要撑得过作业间隔(self) -> None:
        """★★ **不变量守卫** —— TTL ≤ 作业间隔,缓存就一点用都没有。

        2026-10-15 那个 bug 就是「`build_plan` 默认 8h」对着「作业每天一轮」,
        而这件事在代码里**没有任何一处写着** ⇒ 只能靠这条守着。
        """
        from config.settings import Settings

        ttl = float(Settings.model_fields["pan_auto_clean_quark_ttl_hours"].default)
        assert ttl > 24 * 2 - 1, (
            f"TTL 默认 {ttl}h —— 至少要撑过「一天一轮 + 跳过一天」(>24h;取 48h 留余量)。"
            "短于作业间隔的 TTL 会让这套缓存**永远不起作用**")

    def test_作业真的把_TTL_传给了_build_plan(self) -> None:
        """配置写了没人传 = **空转**(本仓为这个栽过:`xhs_sign_x_rap` 空转三天)。

        ⚠️ 按 AST 查**真实调用** —— 子串查会被解释性注释打红(今天已栽两次)。
        """
        import ast
        from pathlib import Path

        from app.services import pan_auto_clean as pac

        src = Path(pac.__file__).read_text(encoding="utf-8")
        fn = next((n for n in ast.walk(ast.parse(src))
                   if isinstance(n, ast.FunctionDef) and n.name == "clean_quark"), None)
        assert fn is not None, "找不到 `clean_quark` —— 改名了?守卫要跟着改"
        kws = {k.arg for c in ast.walk(fn) if isinstance(c, ast.Call)
               for k in c.keywords if k.arg}
        assert "ttl_hours" in kws, "`clean_quark` 没把 TTL 传给 `build_plan` ⇒ 配置空转"


# ---------------------------------------------------------------------------
# ★★★ **整包重复**(2026-10-16,用户口径「相同的资源只需要保存一份」)
#
# 真占空间的重复长这样:夸克给**重名目录**加 `(1)` ⇒ 一条分享链被转存两次、
# **整个资源包多一份**(实测 36 组 / ~44 GiB)。⚠️ 现有的 `classify` **一条都抓不到** ——
# 那些文件的目录名不同,不在"同一目录/同一家"下。


class _PkgQT:
    """够 `build_package_plan` 跑通的假盘。"""

    def __init__(self, homes, dirs):
        self._homes, self._dirs = homes, dirs

    def search_files(self, kw, size=50):  # noqa: ARG002
        return list(self._homes)

    def list_dir(self, fid="0"):
        return list(self._dirs.get(fid, []))

    def delete_files(self, fids, to_recycle=True):  # noqa: ARG002
        self.deleted = list(fids)
        return {"failed": []}


def _pkgfile(name, size, fid="f"):
    return {"file_name": name, "size": size, "fid": fid, "dir": False}


def _mk(homes, dirs):
    """把 `build_package_plan` 的保护查询短路掉(那不是本条要测的)。"""
    from app.services import quark_dup as qd

    return qd, _PkgQT(homes, dirs)


def test_整包重复_留文件多的那份(monkeypatch) -> None:
    from app.services import quark_dup as qd

    monkeypatch.setattr(qd, "refresh_protect", lambda qt, dirs: (set(), set(), 0, []))
    qt = _PkgQT([{"file_name": "redian监听", "fid": "H", "dir": True}], {
        "H": [{"file_name": "资源A", "fid": "A", "dir": True},
              {"file_name": "资源A(1)", "fid": "B", "dir": True}],
        "A": [_pkgfile("1.mp4", 100), _pkgfile("2.mp4", 100)],      #: 2 文件
        "B": [_pkgfile("1.mp4", 100)],                        #: 1 文件 ⇒ 更不完整
    })
    plan = qd.build_package_plan(qt)
    assert plan["n_groups"] == 1 and plan["n_drop"] == 1
    g = plan["groups"][0]
    assert g["keep"]["fid"] == "A", "该留文件更多的那份"
    assert [d["fid"] for d in g["drop"]] == ["B"]


def test_完全打平时留不带后缀的那份(monkeypatch) -> None:
    """打平时留**第一次转存**的那份(其余都是它的副本)。"""
    from app.services import quark_dup as qd

    monkeypatch.setattr(qd, "refresh_protect", lambda qt, dirs: (set(), set(), 0, []))
    qt = _PkgQT([{"file_name": "redian监听", "fid": "H", "dir": True}], {
        "H": [{"file_name": "资源A", "fid": "A", "dir": True},
              {"file_name": "资源A(1)", "fid": "B", "dir": True}],
        "A": [_pkgfile("1.mp4", 100)],
        "B": [_pkgfile("1.mp4", 100)],
    })
    assert qd.build_package_plan(qt)["groups"][0]["keep"]["fid"] == "A"


def test_含我方分享链的那份永不删(monkeypatch) -> None:
    """★ **一票否决**:删掉它 = 已发出的链接立刻失效(本仓既定口径)。"""
    from app.services import quark_dup as qd

    monkeypatch.setattr(qd, "refresh_protect",
                        lambda qt, dirs: ({"PROT"}, set(), 1, []))
    qt = _PkgQT([{"file_name": "redian监听", "fid": "H", "dir": True}], {
        "H": [{"file_name": "资源A", "fid": "A", "dir": True},
              {"file_name": "资源A(1)", "fid": "B", "dir": True}],
        "A": [_pkgfile("1.mp4", 100), _pkgfile("2.mp4", 100)],      #: 文件更多
        "B": [_pkgfile("1.mp4", 100, fid="PROT")],            #: 但这份被分享链引用
    })
    g = qd.build_package_plan(qt)["groups"][0]
    assert g["keep"]["fid"] == "B", "被分享链引用的那份**必须留**(哪怕它更小)"
    assert [d["fid"] for d in g["drop"]] == ["A"]


def test_只有一个目录时不算重复(monkeypatch) -> None:
    from app.services import quark_dup as qd

    monkeypatch.setattr(qd, "refresh_protect", lambda qt, dirs: (set(), set(), 0, []))
    qt = _PkgQT([{"file_name": "redian监听", "fid": "H", "dir": True}], {
        "H": [{"file_name": "资源A", "fid": "A", "dir": True}], "A": [_pkgfile("1", 1)]})
    assert qd.build_package_plan(qt)["n_drop"] == 0


def test_名字里本来就有括号但无兄弟时不算(monkeypatch) -> None:
    """`X(1)` **单独存在**不算重复 —— 必须真的有 `X` 与它并存。"""
    from app.services import quark_dup as qd

    monkeypatch.setattr(qd, "refresh_protect", lambda qt, dirs: (set(), set(), 0, []))
    qt = _PkgQT([{"file_name": "redian监听", "fid": "H", "dir": True}], {
        "H": [{"file_name": "资源A(1)", "fid": "B", "dir": True}], "B": [_pkgfile("1", 1)]})
    assert qd.build_package_plan(qt)["n_drop"] == 0
