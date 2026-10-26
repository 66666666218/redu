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


def _endpoint_up(port: int = PORT) -> bool:
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=2)
        return True
    except Exception:  # noqa: BLE001 - 没起来是预期
        return False


def _existing_port() -> int | None:
    """同一档案**已经**有一份带调试端口的 Edge 在跑吗?有就返回它的端口。

    ⚠️ 2026-10-26 实测踩到的:档案被上一次的浏览器占着(比如跑在 9336),
    `subprocess.Popen` 起的新实例会**把启动交给老实例然后自己退出** ——
    于是我们死等 9335 永远等不到。**Edge 是按档案单实例的**,所以这里必须先复用。
    """
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "(Get-CimInstance Win32_Process -Filter \"Name='msedge.exe'\" "
             "| Select-Object -ExpandProperty CommandLine) -join \"`n\""],
            capture_output=True, timeout=20)
        text = (out.stdout or b"").decode("utf-8", "ignore")
    except Exception:  # noqa: BLE001 - 查不到就当没有
        return None
    key = str(PROFILE).lower()
    for line in text.splitlines():
        low = line.lower()
        if key not in low or "--remote-debugging-port=" not in low:
            continue
        try:
            port = int(low.split("--remote-debugging-port=")[1].split()[0].strip('"'))
        except (IndexError, ValueError):
            continue
        if _endpoint_up(port):
            return port
    return None


def _profile_pids() -> list[int]:
    """正在使用**我们这个档案**的 Edge 进程 PID(不管它有没有调试端口)。"""
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "(Get-CimInstance Win32_Process -Filter \"Name='msedge.exe'\" "
             "| Where-Object { $_.CommandLine -like '*" + PROFILE.name + "*' } "
             "| Select-Object -ExpandProperty ProcessId) -join ','"],
            capture_output=True, timeout=20)
        text = (out.stdout or b"").decode("utf-8", "ignore").strip()
    except Exception:  # noqa: BLE001 - 查不到就当没有
        return []
    return [int(x) for x in text.split(",") if x.strip().isdigit()]


def _kill_profile_browsers() -> int:
    """杀掉占用这个档案的 Edge —— **否则新启动会被"接管"而永远开不出调试端口**。

    ⚠️ 2026-10-26 实测:Edge 启动时发现同档案已有实例在跑,会打印
    「正在现有浏览器会话中打开」然后**自己退出**;而那个老实例可能根本
    **没有**带 `--remote-debugging-port`(比如上一次被别的方式起的)⇒ 死等端口必失败。
    这个档案是**我们自己的自动化档案**,没有人在里面工作,杀掉是安全的。
    """
    pids = _profile_pids()
    for pid in pids:
        try:
            subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                           capture_output=True, timeout=10)
        except Exception:  # noqa: BLE001 - 杀不掉就继续试下一个
            pass
    if pids:
        time.sleep(2)
    return len(pids)


def _ensure_browser() -> int:
    """确保有可用的 CDP 端点,**返回端口**(可能是复用别人的)。"""
    port = _existing_port()
    if port:
        print(f"… 复用已在运行的浏览器(端口 {port})", flush=True)
        return port
    if _endpoint_up(PORT):
        return PORT
    killed = _kill_profile_browsers()
    if killed:
        print(f"… 清理了 {killed} 个占用该档案、但没开调试端口的 Edge", flush=True)
    PROFILE.mkdir(parents=True, exist_ok=True)
    #: ⚠️ **不要用 `stdout/stderr=DEVNULL`**(2026-10-26 实测):那样起的 Edge 会**直接退出**,
    #: 端口永远不就绪(同一命令改成重定向到文件就秒起)。到底为什么不确定,但现象稳定复现 ——
    #: 而且重定向到文件还有个好处:浏览器自己报的错能留着看。
    log = Path("data/_cdp_browser.log")
    log.parent.mkdir(parents=True, exist_ok=True)
    sink = open(log, "ab")
    subprocess.Popen([_edge(), f"--remote-debugging-port={PORT}", f"--user-data-dir={PROFILE}",
                      "--no-first-run", "--no-default-browser-check", "--start-minimized",
                      "about:blank"],
                     stdout=sink, stderr=sink)
    for _ in range(60):
        time.sleep(0.5)
        if _endpoint_up(PORT):
            return PORT
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


def _same_doc(existing: str, wanted: str) -> bool:
    """盘里这个名字算不算"同一份文件"?

    ⚠️ 夸克遇到同名会存成 `名字(1).pdf`(实测 2026-10-26),只按原名精确匹配会
    认不出刚传上去的那份 ⇒ 会误报"没传上去",然后**再传一份**(越传越多)。
    """
    existing = str(existing or "").strip()
    if existing == wanted:
        return True
    if not wanted or "." not in wanted:
        return False
    stem, _, ext = wanted.rpartition(".")
    #: `名字(1).pdf` / `名字(12).pdf`
    for n in range(1, 20):
        if existing == f"{stem}({n}).{ext}":
            return True
    return False


def _find_file(client, name: str) -> dict | None:
    """在盘里找同名文件 → `{name, fid}`(接受夸克加的 `(N)` 后缀)。

    ⚠️ **搜索索引有延迟**(2026-10-25 实测:文件已经传上去了,`search_files` 120 秒都没命,
    而 `list_recent` 立刻就看到了)⇒ 两条路都要查,别只信一个。
    """
    try:
        for f in client.search_files(name, size=20):
            if _same_doc(str(f.get("file_name") or ""), name):
                return {"name": str(f.get("file_name") or name), "fid": str(f.get("fid"))}
    except Exception:  # noqa: BLE001 - 搜索失败就用最近列表兜
        pass
    try:
        for f in client.list_recent(size=30):
            if not f.get("dir") and _same_doc(str(f.get("file_name") or ""), name):
                return {"name": str(f.get("file_name") or name), "fid": str(f.get("fid"))}
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

    port = _ensure_browser()
    jars = []
    for part in ck.split(";"):
        part = part.strip()
        if "=" not in part:
            continue
        k, v = part.split("=", 1)
        jars.append({"name": k.strip(), "value": v.strip(),
                     "domain": ".quark.cn", "path": "/"})

    with sync_playwright() as p:
        br = p.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
        ctx = br.contexts[0]
        ctx.add_cookies(jars)
        pg = ctx.new_page()
        try:
            pg.goto("https://pan.quark.cn/list#/list/all", wait_until="domcontentloaded",
                    timeout=60000)
            time.sleep(5)
            #: ⚠️ **必须走"文件选择器"**（2026-10-26 实测）：页面上那个隐藏的
            #: `input[type=file]` 用 `set_input_files` 塞进去**不会触发上传** ——
            #: 看起来投递成功，文件却永远不出现（我第一次就是这么"成功"了 120 秒）。
            #: 正确做法：点「上传文件」并拦截它弹出的文件选择器。
            try:
                with pg.expect_file_chooser(timeout=15000) as fc:
                    pg.get_by_text("上传文件", exact=True).first.click()
                fc.value.set_files(str(pdf))
            except Exception as exc:  # noqa: BLE001 - 选择器拦不到要如实报
                raise PublishError(
                    f"打不开上传文件的对话框({type(exc).__name__}):{str(exc)[:100]}"
                    f" —— 夸克网页可能改版了") from exc
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


#: 热搜来源 → 该发哪个群(**按平台路由**,2026-10-26 用户口径:
#: 「哪个平台的热点信息写的就发哪个群里」)。认不出来就走全平台群。
_PLATFORM_GROUP = {"微博": "weibo_peer", "抖音": "douyin_peer", "百度": "baidu"}


def _hook_of(settings, to: str) -> str:
    """目标群 → webhook。`admin` 是管理群,其余按 `feishu_webhook_<名字>` 取。"""
    if to in ("", "admin"):
        return str(getattr(settings, "feishu_webhook_admin", "") or "").strip()
    return str(getattr(settings, f"feishu_webhook_{to}", "") or "").strip()


def resolve_targets(to: list[str], platform: str = "") -> list[str]:
    """把 `--to` 摊成**实际要发的群列表**。

    · 空 ⇒ `admin`(管理群);
    · `auto` ⇒ 按 `platform` 路由(微博→weibo_peer / 抖音→douyin_peer / 百度→baidu),
      认不出来走 `multiplatform` —— **绝不静默丢弃**(发错群比不发更糟)。
    · 其余原样,去重保序。
    """
    out: list[str] = []
    for t in (to or []):
        t = (t or "").strip()
        if not t:
            continue
        names = ([_PLATFORM_GROUP.get(platform, "multiplatform")]
                 if t == "auto" else [x.strip() for x in t.split(",") if x.strip()])
        for n in names:
            if n not in out:
                out.append(n)
    return out or ["admin"]


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
    ap.add_argument("--to", action="append", default=[],
                    help="发到哪个群(可多次/逗号分隔):admin(默认)|auto|feishu_webhook_<名字> 的名字")
    ap.add_argument("--platform", default="",
                    help="配合 `--to auto`:热搜来源平台(微博/抖音/百度)")
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
            targets = resolve_targets(args.to, args.platform)
            for t in targets:
                notify(get_settings(), pdf.stem, res["share_url"], res.get("password", ""),
                       got["name"], to=t)
                print(f"✓ 已发到「{t}」群")
    except PublishError as exc:
        print(f"✗ {exc}")
        return 1
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
