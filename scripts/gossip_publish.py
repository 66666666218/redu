"""吃瓜资料 → 夸克网盘 → 分享链 → 发管理群(**测试版**,2026-10-25)。

    python scripts/gossip_publish.py                 # 发最新生成的那份
    python scripts/gossip_publish.py --pdf X.pdf     # 指定文件
    python scripts/gossip_publish.py --no-notify     # 只上传+建链,不发群

## 为什么上传要开浏览器

夸克**没有公开的上传接口**(读/写接口都是纯 Cookie 认证,但上传不在其中)。
而网页版本身是登录着的 ⇒ 用**普通命令行启动的 Edge** + CDP 接管,
把本地文件塞进页面上的 `input[type=file]`,等价于人手点「上传文件」。
建分享**不用浏览器**:`QuarkTransfer.share_fids()` 是现成的(纯 Cookie 接口)。

## 三条纪律

1. **上传完必须自己确认文件真在盘里**(`search_files` 轮询到 fid 才算数)——
   "点了上传"不等于"传上去了"。
2. **建链失败要如实抛**,不许拿一个猜出来的链接发出去。
3. 发群是**对外动作**:默认发**管理群**;要发客户群显式 `--to <名字>`
   (`feishu_webhook_<名字>` 的那个名字,如 `douyin_peer`)。**文案里必须带 AI 创作标识**。
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

OUT_DIR = Path("data/_gossip_test")
PROFILE = Path("data/_quark_profile")
PORT = 9335
_EDGE = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
)


class PublishError(RuntimeError):
    """上传/建链/通知失败。**不静默**。"""


def _edge() -> str:
    for p in _EDGE:
        if Path(p).exists():
            return p
    raise PublishError("找不到 Edge 可执行文件")


def _endpoint_up() -> bool:
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/version", timeout=2)
        return True
    except Exception:  # noqa: BLE001 - 没起来是预期
        return False


def _ensure_browser() -> None:
    """端点不通就用**普通命令行**拉起 Edge(不是 Playwright launch)。"""
    if _endpoint_up():
        return
    PROFILE.mkdir(parents=True, exist_ok=True)
    subprocess.Popen([_edge(), f"--remote-debugging-port={PORT}", f"--user-data-dir={PROFILE}",
                      "--no-first-run", "--no-default-browser-check", "--start-minimized",
                      "about:blank"],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(60):
        time.sleep(0.5)
        if _endpoint_up():
            return
    raise PublishError(
        f"浏览器已启动但 CDP 端口 {PORT} 没就绪 —— 常见原因是**同一档案已有 Edge 在跑**"
        f"(Edge 会把新启动交给老实例然后自己退出),或档案被锁。"
        f"手动确认:关掉所有用 `{PROFILE}` 的窗口后重试")


def _quark_cookie(session, user_id: int = 1) -> str:
    from app.services import cookie_store

    ck = (cookie_store.get_cookie(session, user_id, "quark") or "").strip()
    if not ck:
        raise PublishError("库里没有夸克 Cookie")
    return ck


def _find_file(client, name: str) -> dict | None:
    """在盘里找同名文件 → `{name, fid}`。

    ⚠️ **搜索索引有延迟**(2026-10-25 实测:文件已经传上去了,`search_files` 120 秒都没命,
    而 `list_recent` 立刻就看到了)⇒ 两条路都要查,别只信一个。
    """
    try:
        for f in client.search_files(name, size=20):
            if str(f.get("file_name") or "") == name:
                return {"name": name, "fid": str(f.get("fid"))}
    except Exception:  # noqa: BLE001 - 搜索失败就用最近列表兜
        pass
    try:
        for f in client.list_recent(size=30):
            if str(f.get("file_name") or "") == name and not f.get("dir"):
                return {"name": name, "fid": str(f.get("fid"))}
    except Exception:  # noqa: BLE001
        pass
    return None


def upload(pdf: Path, session, user_id: int = 1, wait_s: int = 120) -> dict:
    """把本地文件传进夸克根目录 → `{name, fid}`。**确认真的在盘里才返回。**"""
    from app.services.quark_transfer import QuarkTransfer
    from playwright.sync_api import sync_playwright

    if not pdf.exists():
        raise PublishError(f"文件不存在:{pdf}")
    ck = _quark_cookie(session, user_id)
    client = QuarkTransfer(ck)
    #: ★ **已经在盘里就别重复传**(同名重复传会让盘里出现 `文件(1).pdf`)
    hit = _find_file(client, pdf.name)
    if hit:
        print(f"✓ 盘里已有同名文件,跳过上传:{pdf.name}", flush=True)
        return hit

    _ensure_browser()
    jars = []
    for part in ck.split(";"):
        part = part.strip()
        if "=" not in part:
            continue
        k, v = part.split("=", 1)
        jars.append({"name": k.strip(), "value": v.strip(),
                     "domain": ".quark.cn", "path": "/"})

    with sync_playwright() as p:
        br = p.chromium.connect_over_cdp(f"http://127.0.0.1:{PORT}")
        ctx = br.contexts[0]
        ctx.add_cookies(jars)
        pg = ctx.new_page()
        try:
            pg.goto("https://pan.quark.cn/list#/list/all", wait_until="domcontentloaded",
                    timeout=60000)
            time.sleep(5)
            inp = pg.query_selector("input[type=file]")
            if inp is None:
                raise PublishError("夸克网盘页面里找不到上传输入框(可能未登录/改版)")
            inp.set_input_files(str(pdf))
            print(f"… 已投递浏览器上传:{pdf.name}", flush=True)
        finally:
            try:
                pg.close()
            except Exception:  # noqa: BLE001 - 关标签失败不影响下面校验
                pass

    #: ★ **必须自己确认文件在盘里**(点了上传 ≠ 传上去了)
    deadline = time.time() + wait_s
    while time.time() < deadline:
        hit = _find_file(client, pdf.name)
        if hit:
            print(f"✓ 已在网盘里:{pdf.name}(fid={hit['fid']})", flush=True)
            return hit
        time.sleep(3)
    raise PublishError(f"等了 {wait_s}s 文件仍未出现在网盘里:{pdf.name}")


def share(name: str, fid: str, session, user_id: int = 1) -> dict:
    """给盘里的文件建分享链 → `{share_url, password}`。"""
    from app.services.quark_transfer import QuarkTransfer

    res = QuarkTransfer(_quark_cookie(session, user_id)).share_fids(
        [fid], title=Path(name).stem[:80])
    if not res.get("share_url"):
        raise PublishError("夸克未返回分享链接")
    return res


def _hook_of(settings, to: str) -> str:
    """目标群 → webhook。`admin` 是管理群,其余按 `feishu_webhook_<名字>` 取。"""
    if to in ("", "admin"):
        return str(getattr(settings, "feishu_webhook_admin", "") or "").strip()
    return str(getattr(settings, f"feishu_webhook_{to}", "") or "").strip()


def notify(settings, title: str, share_url: str, password: str, fname: str,
           to: str = "admin") -> bool:
    """发到指定群(默认**管理群**)。

    ⚠️ **必须带 AI 创作标识**(2026-10-26 用户口径):资料是模型整理的,收的人
    有权知道 —— 这句话写在文案里,**不是**靠模型自己在正文里写。
    """
    from app.services.feishu_client import FeishuClient
    from app.services.gossip_doc import AI_NOTICE

    hook = _hook_of(settings, to)
    if not hook:
        raise PublishError(f"没有配置 {to} 群的 webhook")
    pwd = f"(提取码 `{password}`)" if password else ""
    text = (f"**【吃瓜资料】{title}**\n"
            f"网盘:夸克 · 文件:{fname}\n"
            f"链接:{share_url} {pwd}\n"
            f"⚠️ {AI_NOTICE};未经证实的内容已在文中标注,请自行判断。")
    ok = FeishuClient(hook, getattr(settings, "feishu_secret", "")).send(text)
    if not ok:
        raise PublishError("飞书发送失败")
    return ok


def main() -> int:
    ap = argparse.ArgumentParser(description="吃瓜资料 → 夸克 → 分享链 → 管理群")
    ap.add_argument("--pdf", default="", help="要发的 PDF(默认取最新的一份)")
    ap.add_argument("--no-notify", action="store_true", help="只上传+建链,不发群")
    ap.add_argument("--to", default="admin",
                    help="发到哪个群:admin(默认)或 feishu_webhook_<名字> 的 <名字>")
    ap.add_argument("--user", type=int, default=1)
    args = ap.parse_args()

    from app.db import get_session_local
    from config.settings import get_settings

    pdf = Path(args.pdf) if args.pdf else None
    if pdf is None:
        cands = sorted(OUT_DIR.glob("*.pdf"), key=lambda p: p.stat().st_mtime)
        if not cands:
            print(f"✗ {OUT_DIR} 里没有 PDF,先生成一份:python scripts/gossip_doc_test.py ...")
            return 2
        pdf = cands[-1]

    db = get_session_local()()
    try:
        print(f"→ 目标文件:{pdf}", flush=True)
        got = upload(pdf, db, args.user)
        res = share(got["name"], got["fid"], db, args.user)
        print(f"✓ 分享链:{res['share_url']}" + (f" 提取码 {res['password']}" if res.get('password') else ""))
        if not args.no_notify:
            notify(get_settings(), pdf.stem, res["share_url"], res.get("password", ""),
                   got["name"], to=args.to)
            print(f"✓ 已发到「{args.to}」群")
    except PublishError as exc:
        print(f"✗ {exc}")
        return 1
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
