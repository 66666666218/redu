#!/usr/bin/env sh
# 数据库备份(按 DATABASE_URL 自动选择 SQLite / MySQL)
#
# 用法:
#   sh scripts/backup.sh                    → 输出到 ./backup/
#   BACKUP_DIR=/data/backup sh scripts/backup.sh
#
# SQLite 复用 `app.db.maintenance.snapshot_sqlite()`——与每日 04:00 自动快照**同一实现**,
# 走 SQLite 在线备份 API:库正被写入时也能拿到一致副本。
# 直接 cp/gzip 库文件在写入过程中可能拷到撕裂的中间状态,故不采用。
set -e
cd "$(dirname "$0")/.." || exit 1

BACKUP_DIR="${BACKUP_DIR:-./backup}"
mkdir -p "$BACKUP_DIR"
STAMP=$(date +%Y%m%d_%H%M%S)

# ---- SQLite(本地部署)----
if echo "${DATABASE_URL:-}" | grep -q "sqlite"; then
  echo "备份 SQLite → ${BACKUP_DIR}"
  # Python 只输出 ASCII 路径:Windows 控制台默认 GBK,直接 print emoji 会 UnicodeEncodeError
  OUT=$(PYTHONIOENCODING=utf-8 ${PYTHON:-python} - "$BACKUP_DIR" <<'PY'
import os
import shutil
import sys
from datetime import datetime

from app.db.maintenance import snapshot_sqlite
from config.settings import get_settings

out_dir = sys.argv[1]
info = snapshot_sqlite(get_settings().database_url)  # 内部已校验非空 + quick_check
dest = os.path.join(out_dir, f"platform_{datetime.now():%Y%m%d_%H%M%S}.db")
shutil.copy2(info["path"], dest)
print(f"{dest} ({os.path.getsize(dest) / 1024:.1f} KB)")
PY
)
  echo "✅ 完成: ${OUT}"
  exit 0
fi

# ---- MySQL ----
DB_HOST="${DB_HOST:-mysql}"
DB_PORT="${DB_PORT:-3306}"
DB_USER="${DB_USER:-redu}"
DB_PASS="${DB_PASS:-redu}"
DB_NAME="${DB_NAME:-redu}"

FILE="$BACKUP_DIR/${DB_NAME}_${STAMP}.sql.gz"
echo "备份 ${DB_NAME} → ${FILE}"

if command -v docker >/dev/null 2>&1 && docker ps --format '{{.Names}}' | grep -q redu-mysql; then
  docker exec redu-mysql sh -c "mysqldump -u'$DB_USER' -p'$DB_PASS' '$DB_NAME' | gzip" > "$FILE"
else
  mysqldump -h"$DB_HOST" -P"$DB_PORT" -u"$DB_USER" -p"$DB_PASS" "$DB_NAME" | gzip > "$FILE"
fi
echo "✅ 完成: $(du -h "$FILE" | cut -f1)"
