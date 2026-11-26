"""闲鱼滑块(阿里 baxia / punish)**自动化过验证**的工具与探针(2026-11-26)。

## 背景:这个滑块长什么样

被风控时,mtop 回
`{"ret":["FAIL_SYS_USER_VALIDATE","RGV587_ERROR::SM::哎哟喂,被挤爆啦"],"data":{"url":"…/_____tmd_____/punish?x5secdata=…"}}`
—— 页面随即弹出验证框。**它不在主文档里**:在 iframe `baxia-dialog-content` 内,
src 指向 `_____tmd_____/punish?x5secdata=…`(2026-11-18 实测,扫主文档会得出
「页面没有滑块」的错结论)。

历史做法是**人手动拖一下**(2026-11-18 就是用户拖完当场恢复的)。本工具把它自动化:
在同一个持久档案里打开页面 → 识别验证框 → 模拟人手拖动 → **用一次真实搜索**判定到底过没过。

## ⚠️ 判定只用「服务端原话」,不许看 UI 文案

滑块 `punish` 端点自己的回包是 `{"code":0,"dt":"success","ec":200,...}` ——
**通过和不通过都长得像成功**。拿 `"success" in body` 判恢复,会把"被拦"读成"恢复"
(2026-11-18 第一版监测脚本就这么假报过一次)。
⇒ 唯一判据:**真发一次搜索**,`ret` 含 `SUCCESS` 且 `data.resultList` 非空。

## 两种跑法

    python scripts/xianyu_slider.py --probe      # 只取证:抓 DOM + 截图,不动手
    python scripts/xianyu_slider.py              # 自动过验证,并用真实搜索验收
    python scripts/xianyu_slider.py --probe -k 素材

`--probe` 会把诊断写到 `data/_xianyu_slider_probe/`(截图 + `summary.json`)。

⚠️ **不是无头**:实测无头模式下闲鱼 mtop 回 `TIMEOUT::接口超时`(有头才正常)。
⚠️ **档案必须是生产那个** `tools/xianyu_profile` —— 换个档案过验证,采集照样被拦。
⚠️ 一次运行 = 一次真实搜索。被风控期间**别连跑**:
每撞一次都是一次风控输入(本仓 `stress-test-costs-the-account` 那条)。
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PROFILE = ROOT / "tools" / "xianyu_profile"
OUT_DIR = ROOT / "data" / "_xianyu_slider_probe"
EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
SEARCH = "https://www.goofish.com/search?q={q}"

ARGS = [a for a in sys.argv[1:] if not a.startswith("-")]
PROBE_ONLY = "--probe" in sys.argv
KEYWORD = ARGS[0] if ARGS else "网盘资源"

#: 验证框所在的 iframe —— 两个特征都要认:URL 里有 `punish` / `_____tmd_____`(阿里 TMD 通用路径),
#: 以及 `baxia-dialog-content`(2026-11-18 实测到的那个 id/name)。
_FRAME_MARKS = ("punish", "_____tmd_____", "baxia")


def _dump_frame(frame) -> dict:
    """把一个 frame 的**可操作线索**全抓下来:候选选择器、尺寸、outerHTML 片段。

    ⚠️ 只抓 `outerHTML` 前 1200 字 —— 太长会淹没关键属性,而且控制台会截断。
    """
    info: dict = {"url": frame.url, "name": frame.name, "found": {}}
    candidates = {
        "nc_handle": "#nc_1_n1z, .btn_slide, [class*='btn_slide'], [class*='slider'], [class*='slide-btn']",
        "nc_track": "#nc_1_n1, [class*='nc_scale'], [class*='track'], [class*='scale']",
        "canvas": "canvas",
        "imgs": "img",
        "baxia": "[class*='baxia'], #baxia-dialog, .baxia-dialog-content",
    }
    for key, sel in candidates.items():
        try:
            loc = frame.locator(sel)
            n = loc.count()
            rows = []
            for i in range(min(n, 6)):
                el = loc.nth(i)
                try:
                    box = el.bounding_box()
                except Exception:  # noqa: BLE001 - 不可见元素取不到 box
                    box = None
                try:
                    html = el.evaluate("e => e.outerHTML.slice(0, 1200)")
                except Exception as exc:  # noqa: BLE001
                    html = f"<取不到:{type(exc).__name__}>"
                rows.append({"box": box, "html": html})
            info["found"][key] = {"count": n, "items": rows}
        except Exception as exc:  # noqa: BLE001 - 单个选择器失败不影响其余
            info["found"][key] = {"error": f"{type(exc).__name__}: {str(exc)[:80]}"}
    return info


def _is_challenge_frame(frame) -> bool:
    u = (frame.url or "").lower()
    return any(m in u for m in _FRAME_MARKS)


def main() -> int:
    from playwright.sync_api import sync_playwright

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    print(f"=== 档案:{PROFILE} ===", flush=True)
    print(f"=== 关键词:{KEYWORD} | 模式:{'只取证' if PROBE_ONLY else '自动过验证'} ===",
          flush=True)

    responses: list[dict] = []
    with sync_playwright() as p:
        kwargs = {"user_data_dir": str(PROFILE), "headless": False, "no_viewport": True}
        try:
            ctx = p.chromium.launch_persistent_context(
                channel="msedge", executable_path=EDGE, **kwargs)
        except Exception as exc:  # noqa: BLE001
            print(f"Edge 起不来({str(exc)[:80]}),退回自带 Chromium", flush=True)
            ctx = p.chromium.launch_persistent_context(**kwargs)

        pg = ctx.pages[0] if ctx.pages else ctx.new_page()

        def _on_response(r) -> None:
            """把 punish / 搜索的回包留档 —— 判定和排障都靠它,不靠 UI 文案。"""
            u = r.url
            if "punish" not in u and "idlemtopsearch" not in u:
                return
            try:
                body = r.text()[:600]
            except Exception:  # noqa: BLE001
                body = "<取不到 body>"
            responses.append({"url": u[:200], "status": r.status, "body": body})

        pg.on("response", _on_response)
        try:
            pg.goto(SEARCH.format(q=KEYWORD), wait_until="domcontentloaded", timeout=60000)
        except Exception as exc:  # noqa: BLE001
            print(f"(导航异常,继续观察):{str(exc)[:100]}", flush=True)

        # 等验证框出现(或确认没有):punish 是异步请求后才弹的,轮询 frame 树
        challenge_frames = []
        for i in range(25):
            pg.wait_for_timeout(1000)
            try:
                challenge_frames = [f for f in pg.frames if _is_challenge_frame(f)]
            except Exception:  # noqa: BLE001 - 页面在跳转(已知竞态)
                challenge_frames = []
            if challenge_frames:
                print(f"✓ 第 {i + 1}s 出现验证框 frame:{challenge_frames[0].url[:120]}",
                      flush=True)
                break
            if responses:
                print(f"  ...已收到 {len(responses)} 条相关回包,继续等验证框", flush=True)

        cookies_before = [c.get("name") for c in ctx.cookies()]
        summary: dict = {
            "keyword": KEYWORD,
            "page_url": pg.url,
            "frame_urls": [f.url for f in pg.frames],
            "challenge_frames": len(challenge_frames),
            "responses": responses,
            "cookies_has_x5sec": [n for n in cookies_before if n and "x5sec" in n],
        }

        # 截图:整页 + 每个验证 frame 的元素截图
        try:
            pg.screenshot(path=str(OUT_DIR / "page.png"))
        except Exception as exc:  # noqa: BLE001
            print(f"(整页截图失败:{str(exc)[:60]})", flush=True)

        if challenge_frames:
            summary["frames"] = [_dump_frame(f) for f in challenge_frames]
            iframe = pg.query_selector(
                "iframe[src*='punish'], iframe[src*='_____tmd_____'], "
                "iframe[name*='baxia'], iframe[id*='baxia']")
            if iframe is not None:
                try:
                    iframe.screenshot(path=str(OUT_DIR / "challenge.png"))
                except Exception as exc:  # noqa: BLE001
                    print(f"(验证框截图失败:{str(exc)[:60]})", flush=True)

        if not PROBE_ONLY and challenge_frames:
            summary["solve"] = _try_solve(pg, challenge_frames[0])

        (OUT_DIR / "summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n=== 诊断已写入 {OUT_DIR} ===", flush=True)
        print(f"  验证框:{len(challenge_frames)} 个 | 相关回包:{len(responses)} 条", flush=True)
        for r in responses[-6:]:
            print(f"  [{r['status']}] {r['body'][:220]}", flush=True)
        time.sleep(2)
        ctx.close()
    return 0


def _try_solve(pg, frame) -> dict:
    """自动过验证(占位:真实实现见 `app/services/xianyu_verify.py`,先取证再抄参数)。"""
    from app.services.xianyu_verify import solve_challenge

    return solve_challenge(pg, frame)


if __name__ == "__main__":
    raise SystemExit(main())
