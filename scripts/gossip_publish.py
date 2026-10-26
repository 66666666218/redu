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
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

OUT_DIR = Path("data/_gossip_test")
PROFILE = Path("data/_quark_profile").resolve()   #: **必须是绝对路径**(见 _ensure_browser 的注释)
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


#: ⚠️ 2026-10-26 的一整轮教训,别再走回头路(每条都踩过):
#:   ① **复用"别人已打开的浏览器"+ 杀残留** —— 一个坑接一个坑:Edge 按档案单实例,
#:      同档案已有实例时新启动会打印「正在现有浏览器会话中打开」然后自己退出,死等端口永远等不到;
#:      被 `taskkill /F` 杀掉之后,档案还会进入一段"谁都起不来"的怪状态。
#:   ② **`--user-data-dir` 用相对路径** —— Edge 不认时会**退回默认档案**,于是又变成①的症状。
#:      只换这一点做受控对照:**相对 ⇒ FAIL,绝对 ⇒ OK**。这是真正的根因,不是 DEVNULL、不是端口。
#:   ③ **每次全新临时档案** —— 起得来(1 秒),但**夸克首次初始化不完整,上传落不了地**
#:      (同一个 PDF:持久档案传得上去,临时档案传不上去)。
#:   ④ **`browser.close()` 关不掉浏览器** —— CDP 连接上它只是断开,端口照旧监听;
#:      要按端口找 PID 再 taskkill(见 `_close_browser`)。
#: ⇒ 最终方案:**持久档案 + 绝对路径 + 用完按端口关掉**。三条缺一不可。


def _close_browser(port: int) -> bool:
    """关掉**我们自己起的**那个浏览器(按端口找 PID)。

    ⚠️ 不能用 Playwright 的 `browser.close()`(2026-10-26 实测):用 `connect_over_cdp`
    连上去之后调 `close()` **只是断开连接**,浏览器照旧开着 —— 端口仍然在监听。
    所以这里按端口找 PID 直接 taskkill。
    """
    try:
        out = subprocess.run(["netstat", "-ano"], capture_output=True, timeout=15)
        text = (out.stdout or b"").decode("utf-8", "ignore")
    except Exception:  # noqa: BLE001 - 查不到就当没关掉
        return False
    pids = {line.split()[-1] for line in text.splitlines()
            if f":{port}" in line and "LISTENING" in line.upper()}
    killed = False
    for pid in pids:
        if pid.isdigit():
            subprocess.run(["taskkill", "/F", "/PID", pid], capture_output=True, timeout=10)
            killed = True
    return killed


def _ensure_browser() -> tuple[int, bool]:
    """确保有可用的 CDP 端点 → `(端口, 是不是我们刚起的)`。

    ★ 用**持久档案** `data/_quark_profile`(它已经登录过、页面状态是热的),
    但**路径必须是绝对路径**、且**用完就关浏览器**(`_close_browser`)⇒ 桌面上不留窗口。
    ⚠️ 别改成"每次全新的临时档案":起是起得来(1 秒),但**夸克首次初始化不完整、上传落不了地**
    (2026-10-26 实测:同一个 PDF,持久档案传得上去、临时档案传不上去)。
    """
    if _endpoint_up(PORT):
        return PORT, False           #: 正常情况下不会有(我们每次用完就关)
    PROFILE.mkdir(parents=True, exist_ok=True)
    log = Path("data/_cdp_browser.log")
    log.parent.mkdir(parents=True, exist_ok=True)
    sink = open(log, "ab")          #: 浏览器自己的报错留着看(别用 DEVNULL,出问题就抓瞎)
    #: ⚠️⚠️ **`--user-data-dir` 必须是绝对路径**(2026-10-26 受控对照):
    #: 同一个全新档案、同一个端口,只把路径从相对换成绝对 ——
    #: **相对 ⇒ FAIL,绝对 ⇒ OK**。原因是 Edge 不认相对路径时会**退回默认档案**,
    #: 而默认档案已有实例在跑 ⇒ 打印「正在现有浏览器会话中打开」然后自己退出,端口永远等不到。
    #: (我为此先后怀疑过 DEVNULL、档案单例、端口占用,全错;只有受控对照能定住它。)
    abs_profile = PROFILE.resolve()
    subprocess.Popen([_edge(), f"--remote-debugging-port={PORT}",
                      f"--user-data-dir={abs_profile}", "--no-first-run",
                      "--no-default-browser-check", "--start-minimized",
                      "about:blank"],
                     stdout=sink, stderr=sink)
    for _ in range(40):
        time.sleep(0.5)
        if _endpoint_up(PORT):
            return PORT, True
    raise PublishError(
        f"浏览器起来了但端口 {PORT} 没就绪 —— 看 `{log}` 里浏览器自己怎么说")


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


#: 夸克**网页上传的默认落地目录**(2026-10-26 实测)。
#: ⚠️ 不查这里就会得出「没传上去」的**假结论**:文件其实在,只是不在根目录,
#: 而 `search_files` 的索引对它迟迟不命中、`list_recent` 又只列顶层目录。
_UPLOAD_DIR_NAME = "夸克上传文件"


def _upload_dir_fid(client) -> str:
    try:
        for f in client.list_recent(size=50):
            if f.get("dir") and str(f.get("file_name") or "") == _UPLOAD_DIR_NAME:
                return str(f.get("fid") or "")
    except Exception:  # noqa: BLE001 - 查不到就跳过这条路径
        pass
    return ""


def _find_file(client, name: str) -> dict | None:
    """在盘里找同名文件 → `{name, fid}`(接受夸克加的 `(N)` 后缀)。

    三条路都要查,缺一条就可能误判成"没传上去":
      ① `search_files`(搜索索引,**有延迟**);
      ② `list_recent`(根目录最近,只列顶层);
      ③ **「夸克上传文件」子目录**(网页上传的默认落地处)。
    """
    try:
        for f in client.search_files(name, size=20):
            if _same_doc(str(f.get("file_name") or ""), name):
                return {"name": str(f.get("file_name") or name), "fid": str(f.get("fid"))}
    except Exception:  # noqa: BLE001 - 搜索失败就用下面两条兜
        pass
    try:
        for f in client.list_recent(size=30):
            if not f.get("dir") and _same_doc(str(f.get("file_name") or ""), name):
                return {"name": str(f.get("file_name") or name), "fid": str(f.get("fid"))}
    except Exception:  # noqa: BLE001
        pass
    fid = _upload_dir_fid(client)
    if fid:
        try:
            for f in client.list_dir(fid):
                if not f.get("dir") and _same_doc(str(f.get("file_name") or ""), name):
                    return {"name": str(f.get("file_name") or name),
                            "fid": str(f.get("fid"))}
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

    port, started = _ensure_browser()
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
        try:
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
            #: ★ **必须自己确认文件在盘里**(点了上传 ≠ 传上去了)。
            #: ⚠️ 校验要放在浏览器**还活着**的时候做 —— 上传是页面驱动的,提前关浏览器会把它掐断。
            return _wait_for_file(client, pdf, wait_s)
        finally:
            #: ★ **只关我们自己起的那个**(2026-10-26 用户问「多余的窗口会不会自己关」)。
            #: 复用别人已经在跑的浏览器时**不许关** —— 那不是我们的。
            if started:
                if _close_browser(port):
                    print("… 已关闭本次启动的浏览器", flush=True)
                #: 档案**保留**(它是持久档案,下次还能用):只关浏览器,不删目录


def _wait_for_file(client, pdf: Path, wait_s: int) -> dict:
    """轮询"文件真的在盘里了吗",拿到 `{name, fid}` 才算数。"""
    deadline = time.time() + wait_s
    while time.time() < deadline:
        hit = _find_file(client, pdf.name)
        if hit:
            print(f"✓ 已在网盘里:{hit['name']}(fid={hit['fid']})", flush=True)
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
