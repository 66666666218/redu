"""类目 → 话题 与**按类目轮换出词**单测(2026-10-04,用户口径)。

用户原话:"你脑子里另有一张表(比如"影视"下面挂"短剧/漫剧/解说","资料"下面挂"四六级/考公/模板"…),
**轮换类目出词**:这轮搜"资料"、下轮搜"影视",而不是永远同一类,
你抖音搜索就跟着群里面的资源名字走结合资源库里面的名称"。

**它解决什么**:那条链原来每轮都用同一批词 → 反复命中同一批群
(实测 27 条线索里 10 条解析成群、群只从 9→10,自循环)。广度由**轮换**保证。
"""
import os

os.environ.setdefault("JWT_SECRET", "test_secret_0123456789abcdef0123456789abcdef")
os.environ.setdefault("DATABASE_URL", "sqlite://")

import pytest  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app.db.database import Base  # noqa: E402
from app.db import models  # noqa: F401,E402
from app.services import category_topics as ct  # noqa: E402


@pytest.fixture()
def session():
    eng = create_engine("sqlite://")
    Base.metadata.create_all(eng)
    db = sessionmaker(bind=eng)()
    yield db
    db.close()


def test_classify_maps_titles_to_categories() -> None:
    assert ct.classify("四级真题 网盘") == "资料"
    assert ct.classify("挑丨情丑闻【韩剧】") == "影视"
    # ⚠️ 我一度把"模板"从资料里摘掉,**被用户否掉**:"并不用收窄,你因为类目很宽所以可以泛化很多"
    assert ct.classify("某某设计模板") == "资料"
    # ⚠️ 注意:`某某问卷模板` 会落到**资料**(资料命中"模板"1、问卷命中"问卷"1 → 平票按表序)
    # —— 这是关键词分类的**边界模糊**,不是 bug;词表怎么调由用户定(见交接说明)。
    assert ct.classify("某某软件安装包") == "软件"


def test_unknown_returns_empty_not_a_guess() -> None:
    """认不出就返回空 —— **不猜**。猜错会把这个词出到错的类目里去。"""
    assert ct.classify("三岁分享") == ""
    assert ct.classify("") == ""


def test_pick_prefers_resource_names_of_that_category() -> None:
    """**优先永远是"资源名称"**(用户口径)—— 该类目有货就先出它。"""
    words = ["四级真题 网盘", "韩剧全集", "高数课件"]
    assert ct.pick(words, "资料", 2) == ["四级真题 网盘", "高数课件"]


def test_pick_falls_back_to_that_categorys_own_topics() -> None:
    """⚠️ **该类目没现成资源名时,用**它自己的话题词**兜底 —— 不是拿别的类目的词充数。

    实测(2026-10-04):候选池小时,轮到「问卷/软件/大瓜」这类没货的类目,
    若拿别的类目的词凑数,**轮换就形同虚设**(每个类目出的其实是同一批)。
    而"主动往这个方向扩"正是要的"创新"。
    """
    words = ["四级真题 网盘", "韩剧全集"]           # 只有资料类、影视类
    got = ct.pick(words, "问卷", 2)
    assert all(ct.classify(w) == "问卷" for w in got), f"兜底词必须是本类目的:{got}"
    assert "四级真题 网盘" not in got, "不能拿别的类目的资源名充数"


def test_pick_zero_or_negative_is_empty() -> None:
    assert ct.pick(["a"], "资料", 0) == []


def test_rotation_advances_and_wraps(session) -> None:
    """轮换:每轮前进一个类目,**走完一圈回到开头**。"""
    order = list(ct.DEFAULT_CATEGORIES)
    first = ct.current_category(session)
    assert first == order[0], "没设过游标时应从第一个开始"
    seen = [first]
    for _ in range(len(order) - 1):
        seen.append(ct.advance_category(session))
    assert seen == order, f"轮换顺序不对:{seen}"
    assert ct.advance_category(session) == order[0], "走完一圈应回到开头"


def test_cursor_survives_new_session(session) -> None:
    """游标落在 `system_config` —— **重启不丢**,否则每次重启都从头开始、永远只搜第一类。"""
    ct.advance_category(session)                       # 推进一格
    expected = ct.current_category(session)
    session.expunge_all()                              # 模拟"新会话读同一库"
    assert ct.current_category(session) == expected
    assert expected != list(ct.DEFAULT_CATEGORIES)[0]


class TestCategoriesAreExtensible:
    """⚠️ **类目不该被写死**(用户 2026-10-04:"上面那些类目只是举例但是**并不是全部**,
    **不要仅仅局限这几个**")—— 所以真正生效的表读 `.env` 的 `LEAD_CATEGORIES`,
    **加类目/加话题不用改代码**。代码里那份只是**草稿种子**。
    """

    def test_parse_format(self) -> None:
        got = ct.parse_categories("资料:真题,考公|影视:短剧,漫剧|新类目:话题A")
        assert got == {"资料": ("真题", "考公"), "影视": ("短剧", "漫剧"), "新类目": ("话题A",)}

    def test_parse_ignores_junk_and_allows_fallback(self) -> None:
        """脏输入**不产生半截表** —— 返回空,由 `categories()` 回落种子。"""
        assert ct.parse_categories("") == {}
        assert ct.parse_categories("没冒号的一坨") == {}
        assert ct.parse_categories("类目没有话题:") == {}

    def test_env_overrides_seed(self, monkeypatch) -> None:
        """`.env` 设了就**以它为准**(可以完全换掉种子里的类目)。"""
        import config.settings as cs
        monkeypatch.setattr(cs, "get_settings",
                            lambda: type("S", (), {"lead_categories": "自定义类:甲,乙"})())
        assert ct.categories() == {"自定义类": ("甲", "乙")}
        assert ct.classify("一个甲资源") == "自定义类"

    def test_seed_used_when_env_empty(self, monkeypatch) -> None:
        import config.settings as cs
        monkeypatch.setattr(cs, "get_settings",
                            lambda: type("S", (), {"lead_categories": ""})())
        assert ct.categories() == ct.DEFAULT_CATEGORIES


class TestUserRealExamples:
    """⚠️ **用户给的真实例子**(2026-10-04)—— 这批比我自己编的样例有价值得多:

        超人模拟器|入口                                   → 软件
        2026性格测试｜七宗罪&七美德测试入口+完整版操作教程   → 问卷
        孙宇晨小作文                                      → 大瓜

    **三条里两条当场把我打回原形**:
      ① "…测试…+完整版操作教程" 含资料类的"教程",而原来的判定是"**第一个命中的类目就赢**"
         + 资料排在表头 → **被误判成资料**。改成**按命中次数计分**后:问卷"测试"×2 > 资料"教程"×1 ✅
      ② "小作文"这种**实际说法**原来不在大瓜词表里 → **认不出**。已补(顺带补了塌房/道歉/内讧等)。

    同时它也说明了**为什么"入口"不能当判据**:软件类(超人模拟器|入口)和问卷类(测试入口)
    里都有它 —— **本身不携带类目信息**,放进去只会制造误判。
    """

    def test_software(self) -> None:
        assert ct.classify("超人模拟器|入口") == "软件"

    def test_questionnaire_beats_the_generic_tutorial_word(self) -> None:
        """⚠️ 这条是核心回归:命中"教程"(资料)也不能盖过两次"测试"(问卷)。"""
        assert ct.classify("2026性格测试｜七宗罪&七美德测试入口+完整版操作教程") == "问卷"

    def test_gossip(self) -> None:
        assert ct.classify("孙宇晨小作文") == "大瓜"

    def test_the_two_obvious_ones(self) -> None:
        assert ct.classify("四级真题 网盘") == "资料"
        assert ct.classify("挑丨情丑闻【韩剧】") == "影视"


class TestStudyMaterialOnly:
    """⚠️ **「资料」= 学习资料**(用户 2026-10-04:"资料基本都是学习资料,就你理解的四级、
    考公、**小学学习资料**等一些")。

    我原来把网撒得太大(`模板/表格/PPT/简历/素材/字体/笔刷/壁纸/头像/表情包/图标/报告/手册`
    全算资料)—— **那是设计素材和办公模板,不是学习资料**。已摘掉。
    """

    def test_study_materials(self) -> None:
        for t in ("四级真题", "考公资料", "小学学习资料", "人教版数学课件", "考研英语讲义"):
            assert ct.classify(t) == "资料", t

    def test_category_is_broad_on_purpose(self) -> None:
        """⚠️ **类目宽是有意的**(用户 2026-10-04:"并不用收窄,你因为类目很宽所以可以泛化很多")——
        我一度收窄成"只收学习类",被否掉。宁可宽,让更多资源名有类目可归。"""
        for t in ("某某字体包", "4K壁纸合集", "简历模板PPT", "人教版数学课件"):
            assert ct.classify(t) == "资料", t


class TestUnclassifiedIsNotDropped:
    """⚠️ 类目表是**收窄**的,大量资源名归不进任何类目(壁纸/字体/模板…)。

    若把它们一律丢掉,轮换到任何类目时都用不上 —— 等于**把用户自己的资源名扔了**,
    与"搜索词应该是资源名称"直接冲突。所以未分类的排在**类目内资源名之后、话题词之前**。
    """

    def test_unclassified_words_still_get_picked(self) -> None:
        # "某某冷门资源" 不命中任何类目的词 → 未分类
        got = ct.pick(["某某冷门资源", "四级真题"], "资料", 2)
        assert got == ["四级真题", "某某冷门资源"], f"未分类的不该被丢:{got}"

    def test_other_categories_words_are_excluded(self) -> None:
        """但**别的类目**的词仍然不要 —— 否则轮换没有意义。"""
        got = ct.pick(["韩剧全集", "四级真题"], "资料", 2)
        assert got[0] == "四级真题"
        assert "韩剧全集" not in got          # 影视类的,留给影视那一轮
        assert len(got) == 2                   # 空位由本类目话题词补上


class TestFourWayDistinction:
    """**四个类目的区分能力**(2026-10-04 用户直接问:"你现在是否能分清资料,问卷,软件,大瓜")。

    这张表就是当时的验证结果 —— 22 条**全判对**。其中两条是**改出来的**:

    | 输入 | 改前 | 原因 | 改后 |
    | --- | --- | --- | --- |
    | `问卷模板` | 资料 ❌ | 问卷"问卷"1票 vs 资料"模板"1票 → **平票**按表序落资料 | **问卷** ✅ |
    | `吃瓜合集` | 资料 ❌ | 大瓜"吃瓜"1票 vs 资料"合集"1票 → **平票** | **大瓜** ✅ |

    修法是给**通用后缀词**(合集/模板/表格/文档…)降权:具体话题词 2 分、通用词 1 分。
    """

    CASES = {
        "资料": ("四级真题", "考公资料", "小学学习资料", "人教版数学课件",
                 "壁纸", "字体包", "PPT模板"),
        "问卷": ("2026性格测试", "七宗罪&七美德测试入口", "心理测评量表",
                 "问卷模板", "MBTI测试"),
        "软件": ("超人模拟器|入口", "某某软件安装包", "PS绿色版", "源码"),
        "大瓜": ("孙宇晨小作文", "某明星塌房", "吃瓜合集", "热搜回应"),
        "影视": ("挑丨情丑闻【韩剧】", "短剧全集"),
    }

    def test_all_cases(self) -> None:
        wrong = [(w, ct.classify(w) or "(未分类)", want)
                 for want, ws in self.CASES.items() for w in ws
                 if ct.classify(w) != want]
        assert not wrong, f"判错的: {wrong}"

    def test_generic_suffix_does_not_win_a_tie(self) -> None:
        """⚠️ 核心回归:**通用后缀词(模板/合集)不得盖过具体话题词(问卷/吃瓜)**。"""
        assert ct.classify("问卷模板") == "问卷"
        assert ct.classify("吃瓜合集") == "大瓜"
