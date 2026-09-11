"""密钥泄露扫描:检查项目文件中是否含真实密钥。

用法: python scripts/check_secrets.py
退出码: 0=干净;1=发现疑似密钥(禁止提交)

检测模式:DeepSeek sk-* / dajiala JZL* / 微信读书 wr_skey / 夸克 __puus|__pus
占位值白名单:含 test/placeholder/your/xxx/已泄露 等词的行跳过。
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

PATTERNS = [
    (re.compile(r"sk-[a-f0-9]{20,}"), "DeepSeek API key"),
    (re.compile(r"JZL[a-f0-9]{16,}"), "dajiala API key"),
    (re.compile(r"wr_skey=[A-Za-z0-9]{8,}"), "微信读书 wr_skey"),
    (re.compile(r"__puus=[A-Za-z0-9+/=]{30,}"), "夸克 __puus"),
    (re.compile(r"__pus=[A-Za-z0-9+/=]{30,}"), "夸克 __pus"),
]

WHITELIST = ("test", "placeholder", "your", "xxx", "<", "已泄露", "重新复制", "重新登录", "查询")

SCAN_DIRS = ["app", "frontend/src", "doc", "scripts", "config"]
SCAN_EXTS = {".py", ".md", ".js", ".vue", ".ts", ".json", ".sh", ".yml", ".example"}


def scan_file(fp):
    hits = []
    try:
        lines = fp.read_text(encoding="utf-8").splitlines()
    except (UnicodeDecodeError, PermissionError):
        return hits
    for i, line in enumerate(lines, 1):
        for pat, desc in PATTERNS:
            for m in pat.finditer(line):
                value = m.group(0)
                val_part = value.split("=", 1)[-1] if "=" in value else value
                if any(w in val_part.lower() for w in WHITELIST):
                    continue
                hits.append((i, desc, line.strip()[:80]))
    return hits


def main():
    issues = []
    for d in SCAN_DIRS:
        base = ROOT / d
        if not base.exists():
            continue
        for fp in base.rglob("*"):
            if fp.suffix in SCAN_EXTS and fp.is_file():
                for lineno, desc, line in scan_file(fp):
                    issues.append((str(fp.relative_to(ROOT)), lineno, desc, line))

    if issues:
        print(f"发现 {len(issues)} 处疑似密钥泄露,禁止提交:")
        for fp, lineno, desc, line in issues:
            print(f"  {fp}:{lineno}  [{desc}]  {line[:80]}")
        return 1
    print("OK 未发现密钥泄露")
    return 0


if __name__ == "__main__":
    sys.exit(main())
