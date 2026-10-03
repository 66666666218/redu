"""密钥泄露扫描(2026-10-03 重写):工作区 + **git 历史**。

用法:
    python scripts/check_secrets.py              # 只扫工作区(提交前跑,退出码 1 = 有泄漏)
    python scripts/check_secrets.py --history    # **同时扫 git 历史**(慢,但那是真风险所在)

**为什么重写**(2026-10-03 全项目审查):旧版给出的是**虚假安全感** ——
  ① **只有 5 个模式**,不认**飞书 webhook**。而当天真实查到的事故恰恰是:
     线上在用的飞书告警机器人 UUID 被写进 `doc/deploy-checklist.md`,而仓库是**公开**的,
     旧脚本跑完照样打印 "OK 未发现密钥泄露"。
  ② **只扫 5 个目录**,漏掉根目录(`.env.example` 就在根上)与 `data/`、`*.bat`、`*.txt`、`*.yaml`。
  ③ **不扫 git 历史** —— 而删掉文件并不能把密钥从历史里拿掉(那份 webhook 至今仍在历史里)。
  ④ 白名单按"值里含 test 就跳过":对十六进制/UUID 类模式尚可(hex 里不可能出现 t/s),
     但对 base64/自由文本类模式会漏。现改为"值或整行含占位标记"才跳过。

**成熟方案**:真要长期防,用 [`gitleaks`](https://github.com/gitleaks/gitleaks)(单文件 Go 二进制,
扫描+熵分析+几百条规则,可挂 pre-commit/CI)。本脚本是**零依赖兜底**:覆盖本项目已知的凭据形态,
且能扫历史。两者不冲突,可并存。
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
PATTERNS: list[tuple[re.Pattern, str]] = [
    # ---- 本项目实际用到的凭据 ----
    (re.compile(rf"open\.feishu\.cn/open-apis/bot/v2/hook/{_UUID}"), "飞书机器人 webhook(整条 URL 即凭据)"),
    (re.compile(r"sk-[A-Za-z0-9]{20,}"), "DeepSeek/OpenAI API key"),
    (re.compile(r"JZL[a-f0-9]{16,}"), "dajiala API key"),
    (re.compile(r"wr_skey=[A-Za-z0-9]{8,}"), "微信读书 wr_skey"),
    (re.compile(r"__puus=[A-Za-z0-9+/=]{30,}"), "夸克 __puus"),
    (re.compile(r"__pus=[A-Za-z0-9+/=]{30,}"), "夸克 __pus"),
    (re.compile(r"(SESSDATA|BDUSS|STOKEN)=[A-Za-z0-9_\-%]{16,}"), "B站/百度登录态"),
    # ---- 通用形态 ----
    (re.compile(r"gh[pousr]_[A-Za-z0-9]{36,}"), "GitHub token"),
    (re.compile(r"github_pat_[A-Za-z0-9_]{40,}"), "GitHub 细粒度 token"),
    (re.compile(r"AKIA[0-9A-Z]{16}"), "AWS Access Key"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "私钥"),
    (re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"), "Slack token"),
]

# 值里出现这些词 = 占位符,不是真凭据。⚠️ 十六进制/UUID 里不可能出现 t/s 等字母,
# 所以「test/placeholder」这类词对 hex 类模式是安全的白名单;自由文本类靠行级标记兜。
VALUE_MARKERS = ("test", "placeholder", "your", "xxx", "example", "已泄露", "重新复制", "重新登录")
LINE_MARKERS = ("<", "已泄露", "PLACEHOLDER", "占位", "示例")

# 用**排除法**而不是目录白名单:漏一个目录就等于没扫(旧版就是这么漏掉根目录的)。
EXCLUDE_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", ".pytest_cache",
                "tools/MediaCrawler", "data/backups"}
EXCLUDE_SUFFIX = {".db", ".png", ".jpg", ".jpeg", ".gif", ".zip", ".gz", ".whl", ".pyc",
                  ".ico", ".woff", ".woff2", ".ttf", ".lock"}
MAX_BYTES = 2_000_000          # 单文件上限:跳过巨型产物,别把扫描卡死

_BARE_UUID = re.compile(_UUID)
# 裸 UUID 只有**同行出现这些词**时才算凭据(否则 UUID 到处都是,噪音会淹没真信号)
_FEISHU_CONTEXT = ("feishu", "飞书", "webhook", "hook/", "bot/v2")


def _mask(text: str, keep: int = 10) -> str:
    """把疑似密钥打码(留前 keep 个字符便于定位是哪一条)。"""
    out = text
    for pat, _ in PATTERNS:
        out = pat.sub(lambda m: m.group(0)[:keep] + "…<已打码>", out)
    return _BARE_UUID.sub(lambda m: m.group(0)[:8] + "…<已打码>", out)


def _hits_in_text(text: str) -> list[tuple[int, str, str]]:
    out: list[tuple[int, str, str]] = []
    for i, line in enumerate(text.splitlines(), 1):
        for pat, desc in PATTERNS:
            for m in pat.finditer(line):
                value = m.group(0)
                if any(w in value.lower() for w in VALUE_MARKERS):
                    continue
                if any(w in line for w in LINE_MARKERS):
                    continue
                out.append((i, desc, line.strip()[:90]))
        # ⚠️ **裸 UUID + 上下文**:2026-10-03 实测踩到的缺口 —— 文档里写的是
        # `` `2a294b76-…` ``(**光一个 UUID,不带 URL 前缀**),而飞书 webhook 的凭据
        # **就是那个 UUID**,所以只匹配完整 URL 会**正好漏掉这一种最常见的写法**
        # (旧脚本连飞书都没认,新版第一版又漏了裸 UUID 形态)。
        # 裸 UUID 单独看噪音太大(UUID 到处都是),所以**必须同行出现飞书语境词**才算。
        if _BARE_UUID.search(line):
            low = line.lower()
            if any(w in low for w in _FEISHU_CONTEXT) and not any(w in line for w in LINE_MARKERS):
                out.append((i, "飞书 webhook UUID(裸值 + 飞书语境)", line.strip()[:90]))
    return out


def _skip(path: Path) -> bool:
    rel = path.relative_to(ROOT).as_posix()
    if any(rel == d or rel.startswith(d + "/") for d in EXCLUDE_DIRS):
        return True
    if path.suffix.lower() in EXCLUDE_SUFFIX:
        return True
    try:
        return path.stat().st_size > MAX_BYTES
    except OSError:
        return True


def _tracked_files() -> list[Path]:
    """**会被提交的文件** = git 已跟踪的 + 未跟踪但**没被忽略**的。

    ⚠️ 这层语义很关键(2026-10-03):扫 `.env` 只会制造噪音 —— 它**本来就该有密钥**,
    而且被 `.gitignore` 挡着、根本进不了仓库。扫描器的职责是"**禁止提交**",
    所以判据必须是"这个文件会不会进仓库",而不是"这个文件在哪"。
    (`git ls-files --others --exclude-standard` 正好给出"准备新增、尚未忽略"的那批 ——
    也就是`git add .` 之后真的会被提上去的文件。)
    """
    out: list[Path] = []
    for args in (["ls-files"], ["ls-files", "--others", "--exclude-standard"]):
        try:
            res = subprocess.run(["git", *args], cwd=str(ROOT), capture_output=True,
                                 text=True, encoding="utf-8", errors="ignore")
        except OSError:
            return []
        for rel in (res.stdout or "").splitlines():
            if rel.strip():
                out.append(ROOT / rel.strip())
    return out


def scan_worktree() -> list[tuple[str, int, str, str]]:
    """扫**会被提交的文件**(已跟踪 + 未跟踪未忽略)。"""
    files = _tracked_files()
    if not files:                              # 极端情况(非 git 环境):退回全盘扫
        files = [p for p in ROOT.rglob("*") if p.is_file()]
    issues: list[tuple[str, int, str, str]] = []
    for fp in files:
        if not fp.is_file() or _skip(fp):
            continue
        try:
            text = fp.read_text(encoding="utf-8")
        except (UnicodeDecodeError, PermissionError, OSError):
            continue                       # 二进制/读不了就跳过
        for lineno, desc, line in _hits_in_text(text):
            try:
                where = fp.relative_to(ROOT).as_posix()
            except ValueError:
                where = str(fp)
            issues.append((where, lineno, desc, line))
    return issues


def scan_history(limit_commits: int = 0) -> list[tuple[str, int, str, str]]:
    """扫 **git 历史**(删文件 ≠ 从历史里拿掉)。

    只扫**文本类**路径 —— `.git` 有 1.7G(vendored 的 MediaCrawler/APK chunk 等),
    全量 `git log -p` 会把内存和时间打爆;而凭据只可能出现在文本里。
    """
    paths = ["*.py", "*.md", "*.js", "*.vue", "*.ts", "*.json", "*.yml", "*.yaml",
             "*.sh", "*.bat", "*.txt", "*.example", "*.env", ".env*", "*.cfg", "*.ini"]
    cmd = ["git", "log", "-p", "--all", "--no-color", "--", *paths]
    if limit_commits:
        cmd.insert(3, f"-{limit_commits}")
    issues: list[tuple[str, int, str, str]] = []
    try:
        proc = subprocess.Popen(cmd, cwd=str(ROOT), stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, text=True,
                                encoding="utf-8", errors="ignore")
    except OSError as exc:
        print(f"(git 不可用,跳过历史扫描:{exc})")
        return issues
    commit = "?"
    lineno = 0
    assert proc.stdout is not None
    for line in proc.stdout:
        if line.startswith("commit "):
            commit = line.split()[1][:8]
        lineno += 1
        # 只看**新增行**:`-` 开头的是删除行(说明当时就被删了,不是"现在泄漏")
        if not line.startswith("+"):
            continue
        for _ln, desc, text in _hits_in_text(line):
            issues.append((f"commit:{commit}", lineno, desc, text))
    proc.wait()
    return issues


def main() -> int:
    issues = scan_worktree()
    print(f"工作区:扫完,命中 {len(issues)} 处")
    if "--history" in sys.argv:
        print("git 历史:扫描中(1.7G 仓库,可能需要一两分钟)…")
        hist = scan_history()
        print(f"git 历史:命中 {len(hist)} 处")
        issues += hist

    if issues:
        print(f"\n发现 {len(issues)} 处疑似密钥泄露:")
        for where, lineno, desc, line in issues[:60]:
            # ⚠️ **输出里给密钥打码**:扫描日志会被贴进 issue/聊天/CI 输出 ——
            # 报"这里泄漏了"的同时把密钥再抄一遍,等于二次泄漏。
            print(f"  {where}:{lineno}  [{desc}]  {_mask(line)}")
        if len(issues) > 60:
            print(f"  …还有 {len(issues) - 60} 处")
        print("\n⚠️ 注意:**删文件不会把密钥从 git 历史里拿掉** —— 真泄漏了要"
              "① 吊销/重建该凭据 ② 再谈清历史。")
        return 1
    print("\nOK 未发现密钥泄露" + ("(含 git 历史)" if "--history" in sys.argv else
                                  "(仅工作区;要连历史一起扫加 --history)"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
