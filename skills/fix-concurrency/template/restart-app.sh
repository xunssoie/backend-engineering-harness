#!/usr/bin/env bash
# 측정 대상 애플리케이션을 측정용 프로파일로 재기동한다.
#
# 실행 (레포 루트 기준 전체 경로로 부른다. cd 하지 않는다):
#   bash .claude/skills/fix-concurrency/template/restart-app.sh
#
# 이 스크립트가 있는 이유:
#   기존 인스턴스를 내리지 않고 기동 명령을 다시 돌리면 포트 충돌로 새 프로세스만 죽고
#   **옛 코드가 계속 응답한다.** 그 상태로 Phase 7을 재면 후보를 적용하지 않은 채 "효과 없음"으로 오판한다.
#   포트를 비우는 것과 기동을 확인하는 것을 한 번에 묶어 그 실수를 막는다.
#
# 설정 (.claude/harness.json):
#   concurrency.appStartCommand  기동 명령. {profile} 자리에 concurrency.profile이 들어간다
#   concurrency.appPort          비울 포트
#   concurrency.healthUrl        기동 확인 URL, concurrency.healthUpPattern 응답에 있어야 하는 문자열
#   concurrency.bootLog          기동 로그, concurrency.bootTimeoutSec 기동 대기 상한
#   concurrency.poolMinIdle      커넥션 풀 충전 대기 기준. 0이면 건너뛴다
#
# 어느 디렉토리에서 실행해도 되도록 스스로 레포 루트로 이동한다.

set -uo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
ROOT=$(cd "$SCRIPT_DIR/../../../.." && pwd)
cd "$ROOT" || exit 1
H="$ROOT/.claude/scripts/harness"

cfg() { "$H" get-config "$1"; }

PROFILE=$(cfg concurrency.profile)
START_CMD=$(cfg concurrency.appStartCommand)
APP_PORT=$(cfg concurrency.appPort)
HEALTH_URL=$(cfg concurrency.healthUrl)
UP_PATTERN=$(cfg concurrency.healthUpPattern)
BOOT_LOG=$(cfg concurrency.bootLog)
TIMEOUT=$(cfg concurrency.bootTimeoutSec)
POOL_MIN=$(cfg concurrency.poolMinIdle)
DB=$(cfg perf.db)

PROFILE=${PROFILE:-conc}
APP_PORT=${APP_PORT:-8080}
UP_PATTERN=${UP_PATTERN:-'"status":"UP"'}
BOOT_LOG=${BOOT_LOG:-build/conc-boot.log}
TIMEOUT=${TIMEOUT:-120}
POOL_MIN=${POOL_MIN:-0}

if [ -z "$START_CMD" ] || [ -z "$HEALTH_URL" ]; then
  echo "!! concurrency.appStartCommand, concurrency.healthUrl을 .claude/harness.json에 채운다"
  exit 1
fi
START_CMD=${START_CMD//\{profile\}/$PROFILE}

echo "레포 루트: $ROOT"
echo

echo "===== 1. $APP_PORT 점유 프로세스 종료 ====="
PIDS=$(lsof -ti:"$APP_PORT" || true)
if [ -n "$PIDS" ]; then
  echo "종료 대상 PID: $PIDS"
  echo "$PIDS" | xargs kill 2>/dev/null || true
else
  echo "점유 프로세스 없음"
fi

i=0
while [ $i -lt 20 ]; do
  i=$((i + 1))
  [ -z "$(lsof -ti:"$APP_PORT" || true)" ] && { echo "포트 해제 확인 (${i}초)"; break; }
  sleep 1
done
if [ -n "$(lsof -ti:"$APP_PORT" || true)" ]; then
  echo "!! 20초 후에도 $APP_PORT 가 점유 중이다. 확인: lsof -i:$APP_PORT"
  exit 1
fi

echo; echo "===== 2. $PROFILE 프로파일로 기동 ====="
mkdir -p "$(dirname "$BOOT_LOG")"
nohup bash -c "$START_CMD" > "$BOOT_LOG" 2>&1 &
echo "기동 명령: $START_CMD"
echo "로그: $BOOT_LOG"

echo; echo "===== 3. 기동 대기 (최대 ${TIMEOUT}초) ====="
UP=no
ELAPSED=0
while [ "$ELAPSED" -lt "$TIMEOUT" ]; do
  if curl -s "$HEALTH_URL" 2>/dev/null | grep -qF "$UP_PATTERN"; then
    echo "UP 확인 (${ELAPSED}초)"; UP=yes; break
  fi
  if grep -q "APPLICATION FAILED TO START\|BUILD FAILED" "$BOOT_LOG" 2>/dev/null; then
    echo "!! 기동 실패. 로그 마지막 20줄:"; tail -20 "$BOOT_LOG"; exit 1
  fi
  sleep 2
  ELAPSED=$((ELAPSED + 2))
done

if [ "$UP" != "yes" ]; then
  echo "!! ${TIMEOUT}초 내에 기동되지 않았다. 로그 마지막 20줄:"; tail -20 "$BOOT_LOG"; exit 1
fi

if [ "$POOL_MIN" -gt 0 ]; then
  echo; echo "===== 4. 커넥션 풀 충전 대기 (DB 접속 수 >= $POOL_MIN) ====="
  # 풀이 minimum-idle을 채우는 데 기동 후 수 초가 걸린다.
  # 덜 찬 상태로 부하를 주면 커넥션 생성 지연이 락 대기에 섞인다.
  PRELUDE=$("$H" conc-shell) || { echo "!! conc-shell 실패. perf 절을 확인하라"; exit 1; }
  eval "$PRELUDE"
  i=0
  while [ $i -lt 30 ]; do
    i=$((i + 1))
    case "$DB" in
      mysql) TC=$(dbc -N -B -e "SHOW STATUS LIKE 'Threads_connected';" 2>/dev/null | awk '{print $2}') ;;
      postgresql) TC=$(dbc --csv -t -c "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database();" 2>/dev/null) ;;
      *) TC="" ;;
    esac
    echo "  접속 수=$TC"
    [ -n "$TC" ] && [ "$TC" -ge "$POOL_MIN" ] && { echo "풀 충전 완료"; break; }
    sleep 2
  done
fi

echo; echo "재기동 완료."
