"""闲鱼**自动过滑块**(`app/services/xianyu_verify.py`)的回归测试(2026-11-26)。

重点钉住三件事,它们各自都对应一次真实踩坑:

1. **判据只认真搜索**:`punish` 端点回 `{"code":0,"dt":"success","ec":200}` ——
   通过和不通过都长这样。用 `"success" in body` 判,会把「被拦」读成「恢复」
   (2026-11-18 第一版监测脚本就这么假报过一次)。
2. **滑块在 iframe 里**:主文档一个可疑节点都没有,只查主文档会判成「没有滑块」。
3. **失败不许伪装成成功**:识别不到手柄 / 拖完仍被挡,都必须如实返回原因,
   并把验证框文案带出来(否则下次还是瞎猜选择器)。

⚠️ 全程**不开浏览器、不发请求**:所有页面/元素都是替身。
"""
from __future__ import annotations

import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.database import Base
from app.db import models  # noqa: F401
from app.db.models import RunRecord, User
from app.services import xianyu_verify as xv

_API = "mtop.taobao.idlemtopsearch.pc.search"


# --------------------------------------------------------------------- 替身
class _El:
    def __init__(self, box: dict | None, visible: bool = True, text: str = "") -> None:
        self._box, self._visible, self._text = box, visible, text

    def is_visible(self) -> bool:
        return self._visible

    def bounding_box(self) -> dict | None:
        return self._box

    def evaluate(self, js, arg=None):
        return self._text


class _Locator:
    def __init__(self, items: list[_El]) -> None:
        self._items = items

    def count(self) -> int:
        return len(self._items)

    def nth(self, i: int) -> _El:
        return self._items[i]


class _Frame:
    """够用即可:`locator` 按选择器回元素,`evaluate` 认 innerWidth / innerText。"""

    def __init__(self, url: str = "https://x/_____tmd_____/punish?x5secdata=abc",
                 name: str = "baxia-dialog-content",
                 elements: dict | None = None, inner_width: int = 300,
                 text: str = "请按住滑块,拖动到最右边") -> None:
        self.url, self.name = url, name
        self._els = elements or {}
        self._vw, self._text = inner_width, text

    def locator(self, sel: str) -> _Locator:
        return _Locator(self._els.get(sel, []))

    def evaluate(self, js, arg=None):
        if "innerWidth" in js:
            return self._vw
        return self._text


class _Mouse:
    def __init__(self) -> None:
        self.moves: list[tuple] = []
        self.down_calls = 0
        self.up_calls = 0

    def move(self, x, y):
        self.moves.append((round(x, 1), round(y, 1)))

    def down(self):
        self.down_calls += 1

    def up(self):
        self.up_calls += 1


class _Page:
    """页面替身:`on/remove_listener/goto` 扮演事件发射器(与 test_xianyu_browser 同风格)。"""

    def __init__(self, frames: list | None = None, responses: list | None = None) -> None:
        self.frames = frames or []
        self._responses = responses or []      #: (url, body) —— goto 时逐个投递
        self._handlers: dict = {}
        self.mouse = _Mouse()
        self.goto_url = ""
        self_waited = {"ms": 0}
        self._waited = self_waited

    def on(self, event, fn):
        self._handlers.setdefault(event, []).append(fn)

    def remove_listener(self, event, fn):
        if fn in self._handlers.get(event, []):
            self._handlers[event].remove(fn)

    def goto(self, url, **kw):
        self.goto_url = url
        for u, body in self._responses:
            for fn in list(self._handlers.get("response", [])):
                fn(_FakeResponse(u, body))

    def wait_for_timeout(self, ms):
        self._waited["ms"] += ms

    def evaluate(self, js, arg=None):
        return "[]"


class _FakeResponse:
    def __init__(self, url: str, body: str) -> None:
        self.url, self._body = url, body

    def text(self) -> str:
        return self._body


def _search_ok(items: int = 3) -> str:
    return json.dumps({"api": _API, "ret": ["SUCCESS::调用成功"],
                       "data": {"resultList": [{"i": i} for i in range(items)]}})


def _blocked() -> str:
    return json.dumps({"ret": ["FAIL_SYS_USER_VALIDATE", "RGV587_ERROR::SM::哎哟喂,被挤爆啦"],
                       "data": {"url": "https://x/_____tmd_____/punish?x5secdata=abc"}})


@pytest.fixture(autouse=True)
def _reset_solved():
    """进程内「刚过完验证」的状态必须每个用例清零 —— 否则先跑的用例会替后面的放行。"""
    xv._LAST_SOLVED_AT = None
    yield
    xv._LAST_SOLVED_AT = None


# ------------------------------------------------------------------ 判据
def test_judge_body_never_treats_punish_success_as_ok() -> None:
    """★ 滑块 punish 端点回 `{"code":0,"dt":"success","ec":200}` —— **不许**当恢复。"""
    punish = json.dumps({"code": 0, "dt": "success", "ec": 200,
                         "result": {"code": 0, "sig": "from bx"}})
    assert xv.judge_body(punish) == "other"


def test_judge_body_requires_result_list_for_success() -> None:
    """`ret` 是 SUCCESS 但 `resultList` 空 ⇒ **不算**搜到(空结果与"恢复"是两件事)。"""
    empty = json.dumps({"api": _API, "ret": ["SUCCESS::调用成功"], "data": {"resultList": []}})
    assert xv.judge_body(empty) == "other"
    assert xv.judge_body(_search_ok()) == "ok"


def test_judge_body_recognises_risk_without_api_field() -> None:
    """⚠️ 真风控回包**没有 `api`** —— 拿 `api` 当过滤器的版本会把风控整条丢掉。"""
    assert xv.judge_body(_blocked()) == "verify"


def test_judge_body_treats_session_expired_as_verify() -> None:
    """`SESSION_EXPIRED` 与 RGV587 同批出现(平台把未过风控的请求当匿名)⇒ 仍归类为滑块。"""
    body = json.dumps({"ret": ["FAIL_SYS_SESSION_EXPIRED::Session过期"]})
    assert xv.judge_body(body) == "verify"


def test_should_watch_skips_punish_activate_shade() -> None:
    assert xv.should_watch(f"https://h5api.m.goofish.com/h5/{_API}/1.0/?x=1")
    for bad in ("punish", "activate", "shade"):
        assert not xv.should_watch(f"https://h5api.m.goofish.com/h5/{_API}/{bad}")


# ---------------------------------------------------------------- 定位
def test_is_challenge_frame_by_url_and_name() -> None:
    assert xv.is_challenge_frame(_Frame(url="https://x/a", name="baxia-dialog-content"))
    assert xv.is_challenge_frame(_Frame(url="https://x/_____tmd_____/punish?x5secdata=1",
                                        name=""))
    assert not xv.is_challenge_frame(_Frame(url="https://www.goofish.com/search?q=abc", name=""))


def test_find_challenge_frame_polls_until_frame_appears() -> None:
    """验证框是**异步弹**的:第一次查为空不算结论 —— 必须继续轮询。"""
    frame = _Frame()

    class _PageLater(_Page):
        def __init__(self):                      # noqa: D107 - 测试替身
            super().__init__(frames=[])

        def wait_for_timeout(self, ms):
            if not self.frames:
                self.frames = [frame]

    got = xv.find_challenge_frame(_PageLater(), timeout_s=3.0)
    assert got is frame


def test_find_challenge_frame_returns_none_on_timeout() -> None:
    assert xv.find_challenge_frame(_Page(), timeout_s=0) is None


def test_drag_distance_uses_track_and_clamps_at_zero() -> None:
    handle = {"x": 10.0, "y": 5.0, "width": 40.0, "height": 30.0}
    track = {"x": 0.0, "y": 5.0, "width": 300.0, "height": 30.0}
    assert xv._drag_distance(handle, track, _Page()) == pytest.approx(248.0)
    #: 手柄已经贴右(异常形态)⇒ 不许出现负距离
    far = {"x": 290.0, "y": 5.0, "width": 40.0, "height": 30.0}
    assert xv._drag_distance(far, track, _Page()) == 0.0


def test_drag_distance_falls_back_to_viewport_width() -> None:
    """没有轨道时退到视口宽度 —— 但必须**留余量**,别让手柄撞出边界。"""
    handle = {"x": 10.0, "y": 5.0, "width": 40.0, "height": 30.0}
    got = xv._drag_distance(handle, None, _Frame(inner_width=320))
    assert got == pytest.approx(320 - 50 - 12)


# -------------------------------------------------------------- 人手轨迹
def test_trajectory_is_monotonic_and_ends_at_distance() -> None:
    pts = xv._trajectory(240.0)
    assert pts[-1] == pytest.approx(240.0)
    assert all(b > a for a, b in zip(pts, pts[1:]))


def test_trajectory_is_not_uniform() -> None:
    """★ 匀速直线是机器特征:起步和收尾的步长必须**明显小于**中段。"""
    pts = xv._trajectory(300.0, steps=40)
    steps = [b - a for a, b in zip([0.0] + pts[:-1], pts)]
    mid = steps[len(steps) // 2]
    assert steps[0] < mid * 0.75
    assert steps[-1] < mid * 0.75


def test_drag_slider_moves_right_and_presses_once() -> None:
    pg = _Page()
    handle = {"x": 10.0, "y": 5.0, "width": 40.0, "height": 30.0}
    info = xv.drag_slider(pg, handle, 200.0)
    assert pg.mouse.down_calls == 1 and pg.mouse.up_calls == 1
    xs = [p[0] for p in pg.mouse.moves]
    assert xs[-1] >= 10.0 + 20.0 + 200.0 - 0.5     # 手柄中心 + 位移
    assert info["distance"] == pytest.approx(200.0)


# --------------------------------------------------------------- 主入口
def _slider_frame(**kw) -> _Frame:
    """带手柄 + 轨道的验证框(轨道 300 宽、手柄 40 宽)。"""
    handle = _El({"x": 10.0, "y": 40.0, "width": 40.0, "height": 30.0})
    track = _El({"x": 0.0, "y": 40.0, "width": 300.0, "height": 30.0})
    els = {"#nc_1_n1z": [handle], "#nc_1_n1t": [track]}
    els.update(kw.pop("elements", {}))
    return _Frame(elements=els, **kw)


def test_solve_marks_ok_only_when_real_search_succeeds() -> None:
    """★ 核心:拖动本身**永远不算**通过,只有真搜索回来的 `resultList` 算。"""
    frame = _slider_frame()
    pg = _Page(frames=[frame], responses=[(
        f"https://h5api.m.goofish.com/h5/{_API}/1.0/", _search_ok(3))])
    res = xv.solve_challenge(pg, frame)
    assert res["ok"] is True and res["reason"] == "search_ok"
    assert res["verify"]["items"] == 3
    assert res["handle_selector"] == "#nc_1_n1z"
    assert res["track_found"] is True


def test_solve_reports_failure_when_search_still_blocked() -> None:
    frame = _slider_frame()
    pg = _Page(frames=[frame], responses=[(
        f"https://h5api.m.goofish.com/h5/{_API}/1.0/punish", _blocked())])
    res = xv.solve_challenge(pg, frame)
    assert res["ok"] is False
    assert res["reason"].startswith("search_blocked")


def test_solve_without_handle_does_not_drag_and_keeps_frame_text() -> None:
    """识别不到手柄 ⇒ **别猜也别瞎拖**,把验证框文案带出来供下次补选择器。"""
    frame = _Frame(elements={}, text="请依次点击图中的文字")
    pg = _Page(frames=[frame])
    res = xv.solve_challenge(pg, frame)
    assert res["ok"] is False and res["reason"] == "handle_not_found"
    assert "请依次点击" in res["frame_text"]
    assert pg.mouse.moves == [] and pg.mouse.down_calls == 0


def test_solve_without_challenge_frame_says_so() -> None:
    res = xv.solve_challenge(_Page(frames=[]), frame_wait_s=0)
    assert res["ok"] is False and res["reason"] == "no_challenge_frame"


def test_weak_mode_is_explicitly_labelled() -> None:
    """`verify=False` 的弱判据只给排障脚本用 —— 文案里必须写清楚它**不是**通过判据。"""
    frame = _slider_frame()
    pg = _Page(frames=[frame])

    class _Gone(_Page):
        def wait_for_timeout(self, ms):
            self.frames = []

    res = xv.solve_challenge(_Gone(frames=[frame]), frame, verify=False, close_wait_s=0)
    assert res["reason"] == "frame_closed(weak)"


# ------------------------------------------------------------ 冷却联动
@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    db.add(User(username="t", password_hash="x"))
    db.commit()
    yield db
    db.close()


def _settings(**kw):
    from config.settings import Settings

    return Settings(_env_file=None, **kw)


def _add_marker(db, minutes_ago: int) -> None:
    from datetime import datetime, timedelta

    db.add(RunRecord(user_id=1, run_id="r", kind="xianyu", status="failed",
                     started_at=datetime.now() - timedelta(minutes=minutes_ago),
                     detail="XianyuVerify: 闲鱼人机验证(滑块),全部关键词均未采集"))
    db.commit()


def test_cooldown_active_without_any_solve(session) -> None:
    from app.services.tenant_base import verify_cooldown_active

    _add_marker(session, 10)
    assert verify_cooldown_active(session, 1, _settings(xianyu_cooldown_minutes=30)) is True


def test_solved_after_marker_lifts_cooldown(session) -> None:
    """★ 滑块已经自动过去了 ⇒ 那条旧 marker 不该再把人晾 30(实测 120)分钟。"""
    from app.services.tenant_base import verify_cooldown_active

    _add_marker(session, 10)
    xv.mark_solved()
    assert verify_cooldown_active(session, 1, _settings(xianyu_cooldown_minutes=30)) is False


def test_new_hit_after_solve_still_cools_down(session) -> None:
    """⚠️ 反过来不行:过完验证**之后又撞上**风控,照旧冷却 —— 否则会变成连拖。"""
    from datetime import datetime, timedelta

    from app.services.tenant_base import verify_cooldown_active

    xv.mark_solved()
    xv._LAST_SOLVED_AT = datetime.now() - timedelta(minutes=5)
    _add_marker(session, 0)          # marker 比 solved 更晚(刚刚撞上)
    assert verify_cooldown_active(session, 1, _settings(xianyu_cooldown_minutes=30)) is True
