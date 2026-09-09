#!/usr/bin/env sh
# MySQL 备份(在 Docker 部署下使用;也可用于本机)
# 用法: sh scripts/backup.sh  (输出到 ./backup/ 目录)
set -e
BACKUP_DIR="${BACKUP_DIR:-./backup}"
mkdir -p "$BACKUP_DIR"
STAMP=$(date +%Y%m%d_%H%M%S)
DB_HOST="${DB_HOST:-mysql}"
DB_PORT="${DB_PORT:-3306}"
DB_USER="${DB_USER:-redu}"
DB_PASS="${DB_PASS:-redu}"
DB_NAME="${DB_NAME:-redu}"

FILE="$BACKUP_DIR/${DB_NAME}_${STAMP}.sql.gz"
echo "备份 ${DB_NAME} → ${FILE}"

# ---- SQLite 备份(本地部署,DATABASE_URL 为 sqlite:/// 时) ----
SQLITE_DB="${SQLITE_DB:-./data/platform.db}"
if echo "${DATABASE_URL:-}" | grep -q "sqlite"; then
  SQLITE_FILE="$BACKUP_DIR/platform_${STAMP}.db.gz"
  echo "备份 SQLite → $SQLITE_FILE"
  # WAL 模式下先 checkpoint 保证一致性
  sqlite3 "$SQLITE_DB" "PRAGMA wal_checkpoint(TRUNCATE);" 2>/dev/null || true
  gzip -c "$SQLITE_DB" > "$SQLITE_FILE"
fi
if command -v docker >/dev/null 2>&1 && docker ps --format '{{.Names}}' | grep -q redu-mysql; then
  docker exec redu-mysql sh -c "mysqldump -u'$DB_USER' -p'$DB_PASS' '$DB_NAME' | gzip" > "$FILE"
else
  mysqldump -h"$DB_HOST" -P"$DB_PORT" -u"$DB_USER" -p"$DB_PASS" "$DB_NAME" | gzip > "$FILE"
fi
echo "✅ 完成: $(du -h "$FILE" | cut -f1)"
