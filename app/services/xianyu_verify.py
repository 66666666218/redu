"""闲鱼人机验证(阿里 baxia 滑块)**自动求解**(2026-11-26)。

## 它解决的是什么

协议路被风控时回
`{"ret":["FAIL_SYS_USER_VALIDATE","RGV587_ERROR::SM::哎哟喂,被挤爆啦"],
  "data":{"url":"…/_____tmd_____/punish?x5secdata=…"}}`,
浏览器路则表现为搜索回包里出现 `USER_VALIDATE/RGV587/SESSION_EXPIRED`。
两种情况的**修法只有一条:把那道滑块拖过去**(换号/换出口都已证伪,见
`doc/闲鱼纯协议-链路拆解.md`)。

以前这一步是**人工**的(`scripts/xianyu_login.py` 弹窗让人拖)。本模块把它自动化:
在**同一个持久档案**的同一个页面里找到验证框 → 模拟人手拖动 → **用一次真实搜索验收**。

## ⚠️ 滑块藏在 iframe 里

主文档里**一个可疑节点都没有**(2026-11-18 实测,正则 `captcha|slider|nc_` 全空):
它在 `baxia-dialog-content` 这个 iframe 内,src 指向 `_____tmd_____/punish?x5secdata=…`。
⇒ 找滑块**必须遍历 `page.frames`**,只看主文档会得出「页面没有滑块」的错结论。

## ★★ 验收判据:只认「真发一次搜索」,其它一律不算

`punish` 端点自己的回包是 `{"code":0,"dt":"success","ec":200,…}` ——
**通过和不通过都长这样**。所以:

* ❌ 不许用 `"success" in body` 判恢复(2026-11-18 第一版监测脚本据此假报过一次);
* ❌ 不许用「验证框消失了」当恢复(它也会因超时自己关掉);
* ✅ 只认:搜索回包 `ret` 含 `SUCCESS` **且** `data.resultList` 非空。

所以 `solve_challenge()` **自己就发一次真搜索**来验收(`search_probe`),
不存在「只看弹窗关没关」的弱判据分支 —— 那条路必然会把失败读成成功。

## 边界(没做到的部分,别当成已覆盖)

* 只处理**「按住滑块拖到最右边」**这一类。若平台换成**拼图对齐/点选**类验证,
  `_locate_slider` 找不到手柄 ⇒ 返回 `reason="handle_not_found"` 并把验证框文案带出来
  (供下一次按真实 DOM 补选择器),**绝不假装通过**。
* 拖动轨迹是「加速—匀速—减速 + 纵向抖动 + 停顿」的近似,不是对人手轨迹的精确建模。
"""
from __future__ import annotations

import json
import random
import time
import urllib.parse
from datetime import datetime
from typing import Any

from app.utils import get_logger

logger = get_logger(__name__)

SEARCH_URL = "https://www.goofish.com/search?q="
#: 搜索接口。**按 URL 定位回包**,不要求回包里带 `api`(风控回包没有 `api`,见模块头)
API = "mtop.taobao.idlemtopsearch.pc.search"

#: 验证框 iframe 的特征(URL 或 name 里任一命中即可)
FRAME_MARKS = ("punish", "_____tmd_____", "baxia-dialog", "baxia")

#: 滑块手柄候选(阿里 nc 各代的 id/class 都收进来;顺序=优先级)
HANDLE_SELECTORS = (
    "#nc_1_n1z",              # nc 经典版按钮
    ".btn_slide",             # nc 经典版按钮 class
    "[class*='btn_slide']",
    "#nc_1_n1",               # 少数版本里 n1 是按钮、n1t 是轨道
    "[class*='slide-btn']",
    "[class*='slider-btn']",
)
#: 轨道候选(用来算「还要拖多远」)
TRACK_SELECTORS = (
    "#nc_1_n1t",
    "#nc_1_wrapper",
    ".nc_scale",
    "[class*='nc_scale']",
    "[class*='scale']",
    "[class*='track']",
)

#: 「最后一次真的过完验证」的**墙钟时刻**。`tenant_base.verify_cooldown_active` 拿它跟
#: 最新一条风控 marker 的 `started_at` 比:比它新 ⇒ 冷却已经过时,该放行。
_LAST_SOLVED_AT: datetime | None = None


def mark_solved() -> None:
    """记下「刚刚过了一次验证」的墙钟时刻。"""
    global _LAST_SOLVED_AT
    _LAST_SOLVED_AT = datetime.now()


def last_solved_at() -> datetime | None:
    """最后一次真的过完验证的时刻(没解过 = `None`)。

    两个消费方:① `tenant_base.verify_cooldown_active` 拿它压过**更早**的风控 marker;
    ② `tenant._persist_jar_after_autosolve` 拿它做"这次解过、但还没回写过"的去重键。
    ⚠️ **只在进程内**:重启即丢。丢了的后果只是「多等一轮冷却」,方向是安全的。
    """
    return _LAST_SOLVED_AT


# --------------------------------------------------------------------- 取证/定位
def is_challenge_frame(frame: Any) -> bool:
    """这个 frame 是不是滑块验证框?(URL 或 name 命中特征)"""
    text = f"{getattr(frame, 'url', '') or ''} {getattr(frame, 'name', '') or ''}".lower()
    return any(m in text for m in FRAME_MARKS)


def find_challenge_frame(pg: Any, timeout_s: float = 8.0, poll_ms: int = 500) -> Any | None:
    """轮询 `page.frames`,等验证框出现。

    ⚠️ **验证框是异步弹的**:风控回包先到,iframe 之后才建。只查一次必然抓空
    (2026-11-18 就是因此判成「没有滑块」)。所以这里**默认等 8 秒**。
    """
    deadline = time.monotonic() + max(0.0, timeout_s)
    while True:
        try:
            frames = list(pg.frames)
        except AttributeError:
            #: 这个对象**根本没有 frame 树**(非页面/替身)⇒ 无从查起,立即返回。
            #: ⚠️ 别把这条并进下面的 `except Exception`:那会让它变成**12 秒空转**,
            #: 一个单元测试就卡 12 秒(2026-11-26 实测)。
            return None
        except Exception:  # noqa: BLE001 - 页面正在跳转(已知竞态),下一轮再试
            frames = []
        for f in frames:
            if is_challenge_frame(f):
                return f
        if time.monotonic() >= deadline:
            return None
        try:
            pg.wait_for_timeout(poll_ms)
        except Exception:  # noqa: BLE001
            return None


def frame_text(frame: Any, limit: int = 200) -> str:
    """取验证框里的可见文案 —— 失败时**必须留下它**,否则下次还是瞎猜选择器。"""
    try:
        txt = frame.evaluate(
            "() => (document.body && document.body.innerText || '').replace(/\\s+/g, ' ').trim()")
        return str(txt or "")[:limit]
    except Exception as exc:  # noqa: BLE001
        return f"<取不到:{type(exc).__name__}>"


def _visible_box(locator: Any) -> dict | None:
    """locator 的第 i 个可元素里挑第一个**可见且够大**的盒子。"""
    try:
        n = locator.count()
    except Exception:  # noqa: BLE001
        return None
    for i in range(min(int(n), 4)):
        el = locator.nth(i)
        try:
            if not el.is_visible():
                continue
            box = el.bounding_box()
        except Exception:  # noqa: BLE001 - 不可见/已脱离文档
            continue
        if box and box.get("width", 0) >= 8 and box.get("height", 0) >= 8:
            return dict(box)
    return None


def _locate_slider(frame: Any) -> tuple[dict, dict | None, str] | None:
    """定位手柄(必需)与轨道(可选)。返回 `(handle_box, track_box, selector)`。"""
    handle = None
    used = ""
    for sel in HANDLE_SELECTORS:
        try:
            box = _visible_box(frame.locator(sel))
        except Exception:  # noqa: BLE001 - 单个选择器不被支持不该中断
            box = None
        if box:
            handle, used = box, sel
            break
    if handle is None:
        return None
    track = None
    for sel in TRACK_SELECTORS:
        try:
            box = _visible_box(frame.locator(sel))
        except Exception:  # noqa: BLE001
            box = None
        #: 轨道必须**明显比手柄宽**,否则大概率是同名的小装饰节点
        if box and box.get("width", 0) > handle.get("width", 0) * 1.5:
            track = box
            break
    return handle, track, used


def _drag_distance(handle: dict, track: dict | None, frame: Any) -> float:
    """手柄要走多远才到最右边。

    优先用轨道:``轨道右缘 − 手柄右缘``;没有轨道就退到**验证框视口宽度**(留 12px 余量)——
    阿里这套 UI 的轨道几乎总是铺满对话框,退路够用但**不如轨道精确**。
    """
    if track:
        return max(0.0, (track["x"] + track["width"]) - (handle["x"] + handle["width"]) - 2.0)
    try:
        vw = float(frame.evaluate("() => window.innerWidth") or 0)
    except Exception:  # noqa: BLE001
        vw = 0.0
    if vw > 0:
        return max(0.0, vw - (handle["x"] + handle["width"]) - 12.0)
    return 260.0          # 最后兜底:阿里滑块轨道通常 ~260px


# ----------------------------------------------------------------------- 人手轨迹
def _trajectory(distance: float, steps: int = 36) -> list[float]:
    """把总位移摊成**加速—匀速—减速**的逐步增量(人手的形状,不是匀速直线)。

    返回**每一步的绝对位移**列表(最后一项 == distance),调用方据此算绝对坐标。
    """
    if distance <= 0:
        return [0.0]
    weights: list[float] = []
    for i in range(steps):
        p = i / max(1, steps - 1)
        if p < 0.25:                      # 起步:0.35 → 1.0
            w = 0.35 + (p / 0.25) * 0.65
        elif p < 0.80:                    # 中段:匀速
            w = 1.0
        else:                             # 收尾:1.0 → 0.25
            w = 1.0 - ((p - 0.80) / 0.20) * 0.75
        weights.append(max(0.05, w))
    total_w = sum(weights)
    out: list[float] = []
    acc = 0.0
    for w in weights:
        acc += distance * w / total_w
        out.append(acc)
    out[-1] = distance
    return out


def drag_slider(pg: Any, handle: dict, distance: float, *, rng: random.Random | None = None) -> dict:
    """在页面坐标系里把滑块从当前位置拖到最右边(分段 move + 抖动 + 停顿)。"""
    rng = rng or random.Random()
    x0 = handle["x"] + handle["width"] / 2.0
    y0 = handle["y"] + handle["height"] / 2.0
    steps = max(18, min(64, int(distance / 5)))
    pts = _trajectory(distance, steps)
    #: 总时长 ~0.9~1.9 秒;太快是明显的脚本特征,太慢又会撞上 baxia 的空闲判定
    total_s = min(1.9, max(0.9, 0.7 + distance / 700.0))
    per_s = total_s / max(1, len(pts))

    pg.mouse.move(x0, y0)
    pg.wait_for_timeout(int(rng.uniform(80, 200)))
    pg.mouse.down()
    pg.wait_for_timeout(int(rng.uniform(60, 140)))
    for i, off in enumerate(pts):
        #: 纵向抖动:大部分步为 0,偶尔 ±1px —— 纯直线是机器特征
        jitter = 0 if i % 3 else rng.choice([0, 1, -1])
        pg.mouse.move(x0 + off, y0 + jitter)
        if i % 7 == 0:                     # 每几段来一次短停顿(模拟手抖/犹豫)
            pg.wait_for_timeout(int(rng.uniform(12, 28)))
        else:
            pg.wait_for_timeout(int(max(6, per_s * 1000 * rng.uniform(0.75, 1.25))))
    pg.wait_for_timeout(int(rng.uniform(120, 260)))   # 到位后按住一下再松手
    pg.mouse.up()
    return {"from": [round(x0, 1), round(y0, 1)], "distance": round(distance, 1),
            "steps": len(pts), "seconds": round(total_s, 2)}


# ----------------------------------------------------------------------- 真搜索验收
def judge_body(body: str) -> str:
    """判一条回包:`ok`(真的搜到东西)/ `verify`(被风控)/ `other`。

    ⚠️ **不许要求 `api` 字段存在**(风控回包没有 `api`,2026-11-18 栽过);
    ⚠️ **不许用 `"success" in body`**(`punish` 端点通过与否都回 success)。
    """
    try:
        obj = json.loads(body)
    except (ValueError, TypeError):
        return "other"
    if not isinstance(obj, dict):
        return "other"
    api = obj.get("api")
    if api is not None and api != API:
        return "other"
    ret = " ".join(str(x) for x in (obj.get("ret") or []))
    if "SUCCESS" in ret:
        data = obj.get("data") or {}
        return "ok" if isinstance(data, dict) and data.get("resultList") else "other"
    if "USER_VALIDATE" in ret or "RGV587" in ret or "SESSION_EXPIRED" in ret:
        return "verify"
    return "other"


def should_watch(url: str) -> bool:
    """这条响应值不值得解析(按 URL 定位接口,顺手排掉同域的三个干扰端点)。"""
    return (API in url) and not any(k in url for k in ("punish", "activate", "shade"))


def search_probe(pg: Any, keyword: str, timeout_s: float = 25.0) -> dict:
    """**真发一次搜索**来验收:打开搜索页,按网络层回包判定。

    返回 `{"ok": bool, "items": int, "ret": str, "url": str}`。
    """
    seen: dict = {"ok": False, "items": 0, "ret": "", "url": ""}
    risks: list[str] = []

    def _on_response(r) -> None:
        try:
            if not should_watch(r.url):
                return
            kind = judge_body(r.text())
        except Exception:  # noqa: BLE001 - 取不到 body 就不是我们要的
            return
        if kind == "ok":
            try:
                obj = json.loads(r.text())
                seen["items"] = len((obj.get("data") or {}).get("resultList") or [])
            except Exception:  # noqa: BLE001
                seen["items"] = -1
            seen["ok"] = True
            seen["url"] = r.url[:160]
        elif kind == "verify":
            risks.append(seen["ret"] or "USER_VALIDATE")

    pg.on("response", _on_response)
    try:
        try:
            pg.goto(SEARCH_URL + urllib.parse.quote(keyword),
                    wait_until="domcontentloaded", timeout=60000)
        except Exception as exc:  # noqa: BLE001 - 导航抖动不该当成"验证失败"
            logger.debug("闲鱼验收搜索导航异常:%s", str(exc)[:120])
        for _ in range(max(1, int(timeout_s))):
            if seen["ok"] or risks:
                break
            pg.wait_for_timeout(1000)
    finally:
        try:
            pg.remove_listener("response", _on_response)
        except Exception:  # noqa: BLE001
            logger.debug("闲鱼验收监听移除失败", exc_info=True)
    if not seen["ok"] and risks:
        seen["ret"] = risks[0][:160]
    return seen


# --------------------------------------------------------------------------- 主入口
def solve_challenge(pg: Any, frame: Any = None, *, verify: bool = True,
                    keyword: str = "网盘资源", frame_wait_s: float = 8.0,
                    close_wait_s: float = 6.0) -> dict:
    """过滑块。返回诊断 dict,**`ok` 只在真搜索验收通过时为 True**。

    `verify=False` 只做「拖动 + 弹窗是否关闭」,仅供**取证/排障脚本**用 ——
    ⚠️ **生产路径不许传 `verify=False`**:弹窗自己会因超时关闭,那个判据会把失败读成成功
    (与「`"success" in body`」是同一族错)。
    """
    out: dict = {"ok": False, "reason": "", "drag": {}, "frame_text": "", "verify": {}}
    frame = frame or find_challenge_frame(pg, timeout_s=frame_wait_s)
    if frame is None:
        out["reason"] = "no_challenge_frame"
        return out
    found = _locate_slider(frame)
    if found is None:
        #: **别猜也别瞎拖**:把验证框文案带出去,下次按真实 DOM 补选择器
        out["reason"] = "handle_not_found"
        out["frame_text"] = frame_text(frame)
        logger.warning("闲鱼滑块:识别到验证框但找不到手柄,文案=%s", out["frame_text"][:120])
        return out
    handle, track, sel = found
    distance = _drag_distance(handle, track, frame)
    out["handle_selector"] = sel
    out["track_found"] = track is not None
    try:
        out["drag"] = drag_slider(pg, handle, distance)
    except Exception as exc:  # noqa: BLE001 - 拖动失败要如实报,不许当"大概过了"
        out["reason"] = f"drag_failed:{type(exc).__name__}:{str(exc)[:100]}"
        return out
    pg.wait_for_timeout(1500)             # 给 punish 的校验/收尾一点时间

    if verify:
        vr = search_probe(pg, keyword)
        out["verify"] = vr
        if vr.get("ok"):
            out["ok"] = True
            out["reason"] = "search_ok"
            mark_solved()
        else:
            out["reason"] = f"search_blocked:{vr.get('ret', '')[:90]}"
            out["frame_text"] = frame_text(frame)
            logger.warning("闲鱼滑块拖完仍未通过:%s", out["reason"][:140])
        return out

    #: ‼️ 弱判据,**只给排障脚本**用:弹窗消失 ≠ 通过(它会因超时自己关)。
    try:
        gone = find_challenge_frame(pg, timeout_s=close_wait_s) is None
    except Exception:  # noqa: BLE001
        gone = False
    out["ok"] = gone
    out["reason"] = "frame_closed(weak)" if gone else "frame_still_open"
    return out