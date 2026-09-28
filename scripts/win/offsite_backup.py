"""异地备份(git 版):每日把最新 sqlite 快照提交并推送到 GitHub 私有仓库 redu-backup。

为什么选 git 而不是 scp-to-VPS:零新增凭证(复用本机已有 GitHub 推送密钥)、
GitHub 天然异地、自带全部历史版本、VPS 端不需要任何配置。
计划任务(RedianMonitor_Backup)每小时调起本脚本,脚本自带「每天只推一次」节流。
前置:GitHub 上存在私有仓库 66666666218/redu-backup(与本机推送密钥同账号)。
"""
import datetime as dt
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]           # D:\code\redian(脚本在 scripts/win/ 下,两层)
BACKUP_DIR = ROOT / "data" / "backups"
REPO = Path(r"D:\code\redu-backup")
REMOTE = "git@github.com:66666666218/redu-backup.git"
MARK = ROOT / "data" / ".offsite_backup_done"


def git(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=REPO, capture_output=True,
                          text=True, timeout=300)


def main() -> int:
    today = dt.date.today().isoformat()
    if MARK.exists() and MARK.read_text(encoding="utf-8").strip() == today:
        print("today already pushed, skip")
        return 0
    snaps = sorted(BACKUP_DIR.glob("platform_*.db"),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    if not snaps:
        print("no snapshot yet (app generates one daily at 04:00)")
        return 0
    snap = snaps[0]
    REPO.mkdir(parents=True, exist_ok=True)
    if not (REPO / ".git").exists():
        if git(["init", "-b", "main"]).returncode != 0:
            print("git init failed")
            return 1
        git(["remote", "add", "origin", REMOTE])
        # 本机可能没有全局 git 身份:备份仓库自带,不依赖全局配置
        git(["config", "user.name", "zhouwanpeng"])
        git(["config", "user.email", "2391957313@qq.com"])
    # 固定文件名覆盖式写入:历史版本由 git 承载
    (REPO / "platform_latest.db").write_bytes(snap.read_bytes())
    git(["add", "platform_latest.db"])
    git(["commit", "-m", f"backup {today} ({snap.name}, {snap.stat().st_size // 1024} KB)"])
    p = git(["push", "-u", "origin", "main"])
    if p.returncode != 0:
        print("push failed:", p.stderr[:300])
        return 1
    MARK.write_text(today, encoding="utf-8")
    print(f"pushed {snap.name} ({snap.stat().st_size // 1024} KB) to redu-backup")
    return 0


if __name__ == "__main__":
    sys.exit(main())
