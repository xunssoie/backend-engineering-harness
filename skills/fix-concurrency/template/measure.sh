#!/usr/bin/env bash
# 측정 한 회. Phase 4(n=0), Phase 7(n=후보 번호), Phase 8(n=final)이 같은 스크립트를 쓴다.
#
# 실행 (레포 루트 기준 전체 경로로 부른다. cd 하지 않는다):
#   bash .claude/skills/fix-concurrency/template/measure.sh {이슈번호} {슬러그} {n}
#
# 순서:
#   1) 되돌리기       $TARGET_DIR/reset.sql. 마지막 SELECT의 reset_ok가 1이 아니면 멈춘다
#   2) 락 카운터 BEFORE
#   3) 폭발 부하       k6 원본 요약은 k6-burst-raw-{n}.json. 부하 중 대기 락 수를 표본 수집한다
#   4) 락 카운터 AFTER, 측정 구간 서버 로그 집계(PostgreSQL), 최근 데드락 원문
#   5) 불변식 검증     invariant-{n}.txt
#
# 산출물은 원본 그대로 남긴다. 가공(요약, 증가분, 비교표)은 스킬이 harness conc-* 명령으로 한다.
# 같은 n의 산출물이 이미 있으면 멈춘다. 앞선 측정을 덮어쓰지 않는다 (다시 재려면 FORCE=1).
#
# 어느 디렉토리에서 실행해도 되도록 스스로 레포 루트로 이동한다.

set -uo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
ROOT=$(cd "$SCRIPT_DIR/../../../.." && pwd)
cd "$ROOT" || exit 1
H="$ROOT/.claude/scripts/harness"

if [ $# -ne 3 ]; then
  echo "사용: bash .claude/skills/fix-concurrency/template/measure.sh {이슈번호} {슬러그} {n}"
  exit 2
fi
ISSUE=$1
SLUG=$2
N=$3

cfg() { "$H" get-config "$1"; }

PRELUDE=$("$H" conc-shell --issue "$ISSUE" --slug "$SLUG") || { echo "!! conc-shell 실패. .claude/harness.json의 perf 절을 확인하라"; exit 1; }
eval "$PRELUDE"

DB=$(cfg perf.db)
CONTAINER=$(cfg perf.dbContainer)
BASE_URL=$(cfg perf.appBaseUrl)
AUTH_HEADER=$(cfg perf.tokens.header)
AUTH_PREFIX=$(cfg perf.tokens.headerPrefix)
SAMPLE=$(cfg concurrency.sampleLockWaits)
INTERVAL=$(cfg concurrency.sampleIntervalSec)
PG_WAIT=$(cfg concurrency.pgStatsFlushWaitSec)
BASE_URL=${BASE_URL:-http://localhost:8080}
SAMPLE=${SAMPLE:-true}
INTERVAL=${INTERVAL:-0.5}
PG_WAIT=${PG_WAIT:-11}

LOCK="$TARGET_DIR/lock-stats-$N.txt"
RAW="$TARGET_DIR/k6-burst-raw-$N.json"
INV="$TARGET_DIR/invariant-$N.txt"

for f in burst-script.js invariant-check.sql reset.sql; do
  [ -f "$TARGET_DIR/$f" ] || { echo "!! $TARGET_DIR/$f 가 없다. Phase 1, 3의 산출물을 먼저 만든다"; exit 1; }
done
if [ "${FORCE:-0}" != "1" ]; then
  for f in "$LOCK" "$RAW" "$INV"; do
    [ -e "$f" ] && { echo "!! $f 가 이미 있다. 앞선 측정을 덮어쓰지 않는다. 다시 재려면 FORCE=1"; exit 1; }
  done
fi
command -v k6 >/dev/null 2>&1 || { echo "!! k6가 없다"; exit 1; }

counters() {
  case "$DB" in
    mysql)
      dbc -B -N -e "SHOW GLOBAL STATUS LIKE 'Innodb_row_lock%';
SELECT NAME, COUNT FROM information_schema.INNODB_METRICS
WHERE NAME IN ('lock_deadlocks','lock_timeouts','lock_row_lock_waits');" ;;
    postgresql)
      dbc --csv -t -c "SELECT 'deadlocks', deadlocks FROM pg_stat_database WHERE datname = current_database()
UNION ALL SELECT 'xact_commit', xact_commit FROM pg_stat_database WHERE datname = current_database()
UNION ALL SELECT 'xact_rollback', xact_rollback FROM pg_stat_database WHERE datname = current_database()
UNION ALL SELECT 'conflicts', conflicts FROM pg_stat_database WHERE datname = current_database();" ;;
  esac
}

waiting() {
  case "$DB" in
    mysql) dbc -B -N -e "SELECT COUNT(*) FROM performance_schema.data_lock_waits;" 2>/dev/null ;;
    postgresql) dbc --csv -t -c "SELECT count(*) FROM pg_locks WHERE NOT granted;" 2>/dev/null ;;
  esac
}

echo "===== 1. 되돌리기 ($TARGET_DIR/reset.sql) ====="
case "$DB" in
  mysql) RESET_OUT=$(dbc -B < "$TARGET_DIR/reset.sql") ;;
  postgresql) RESET_OUT=$(dbc --csv < "$TARGET_DIR/reset.sql") ;;
  *) echo "!! perf.db가 mysql 또는 postgresql이 아니다: '$DB'"; exit 1 ;;
esac
[ $? -eq 0 ] || { echo "!! 되돌리기 SQL 실패"; exit 1; }
echo "$RESET_OUT"
RESET_OK=$(printf '%s\n' "$RESET_OUT" | awk -F'[\t,]' '
  found { print $col; exit }
  { for (i = 1; i <= NF; i++) if ($i == "reset_ok") { col = i; found = 1 } }')
if [ "$RESET_OK" != "1" ]; then
  echo "!! reset_ok가 1이 아니다 ('$RESET_OK'). 데이터가 초기 상태가 아니면 측정하지 않는다"
  exit 1
fi

{
  echo "=== RESET ==="
  echo "$RESET_OUT"
  echo "=== BEFORE ==="
  counters
} > "$LOCK" || { echo "!! BEFORE 스냅샷 실패"; exit 1; }

echo; echo "===== 2. 폭발 부하 (n=$N) ====="
START_TS=$(date -u +%Y-%m-%dT%H:%M:%SZ)
SAMPLES=$(mktemp)
k6 run \
  -e SUMMARY_OUT="$RAW" \
  -e BASE_URL="$BASE_URL" \
  -e AUTH_HEADER="$AUTH_HEADER" \
  -e AUTH_PREFIX="$AUTH_PREFIX" \
  "$TARGET_DIR/burst-script.js" &
K6_PID=$!
if [ "$SAMPLE" = "true" ]; then
  while kill -0 "$K6_PID" 2>/dev/null; do
    V=$(waiting | tr -d '[:space:]')
    [ -n "$V" ] && printf 'waiting\t%s\n' "$V" >> "$SAMPLES"
    sleep "$INTERVAL"
  done
fi
wait "$K6_PID"
K6_RC=$?
[ -f "$RAW" ] || { echo "!! k6 원본 요약이 없다 ($RAW). k6 종료 코드 $K6_RC"; rm -f "$SAMPLES"; exit 1; }

echo; echo "===== 3. 락 카운터 AFTER ====="
if [ "$DB" = "postgresql" ] && [ "$PG_WAIT" != "0" ]; then
  echo "통계 반영 대기 ${PG_WAIT}초"
  sleep "$PG_WAIT"
fi
{
  echo "=== AFTER ==="
  counters
  if [ "$SAMPLE" = "true" ]; then
    echo "=== WAITING SAMPLES ==="
    cat "$SAMPLES"
  fi
  if [ "$DB" = "postgresql" ] && [ -n "$CONTAINER" ]; then
    PGLOG=$(docker logs --since "$START_TS" "$CONTAINER" 2>&1)
    echo "=== WINDOW ==="
    printf 'log_lock_waits\t%s\n' "$(printf '%s\n' "$PGLOG" | grep -c 'still waiting for')"
    printf 'log_lock_timeouts\t%s\n' "$(printf '%s\n' "$PGLOG" | grep -c 'canceling statement due to lock timeout')"
    printf 'log_deadlocks\t%s\n' "$(printf '%s\n' "$PGLOG" | grep -c 'deadlock detected')"
    echo "=== LATEST DEADLOCK ==="
    printf '%s\n' "$PGLOG" | grep -A 12 'deadlock detected' | tail -60
  fi
  if [ "$DB" = "mysql" ]; then
    echo "=== LATEST DEADLOCK ==="
    # 끝 표지를 ^---로 잡으면 머리 밑줄에서 바로 끝나 본문이 비는다. TRANSACTIONS 절 머리까지 자른다
    dbc -e "SHOW ENGINE INNODB STATUS\G" | sed -n '/LATEST DETECTED DEADLOCK/,/^TRANSACTIONS$/p'
  fi
} >> "$LOCK"
rm -f "$SAMPLES"

echo; echo "===== 4. 불변식 검증 ====="
case "$DB" in
  mysql) dbc -t < "$TARGET_DIR/invariant-check.sql" | tee "$INV" ;;
  postgresql) dbc -P footer=off < "$TARGET_DIR/invariant-check.sql" | tee "$INV" ;;
esac

echo
echo "측정 완료 (n=$N, k6 종료 코드 $K6_RC)"
echo "  $LOCK"
echo "  $RAW"
echo "  $INV"
