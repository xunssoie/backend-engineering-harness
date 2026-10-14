#!/usr/bin/env bash
# 측정 셸 환경. 레포 루트에서, 새 터미널마다 한 번 source한다.
# 실행(bash perf-env.sh)이 아니라 source여야 함수와 변수가 현재 셸에 남는다. bash, zsh 모두 된다.
#
#   source .claude/skills/optimize-performance/template/perf-env.sh {이슈번호} {슬러그}
#
# 값은 전부 .claude/harness.json의 perf 절에서 읽는다(harness get-config). 여기에 프로젝트 값을 적지 마라.
#
# 정의하는 것
#   HARNESS           harness CLI 경로
#   PERF_DIR          이슈 디렉토리   {perf.resultsDir}/{이슈번호}
#   TARGET_DIR        대상 디렉토리   $PERF_DIR/{슬러그}
#   SEEDS             프로젝트 시드 모듈 디렉토리 (perf.seedsDir)
#   PERF_SCRIPTS      jvm-sampler.sh, mint-tokens.sh 위치
#   PERF_DB, PERF_DB_NAME, PERF_APP_URL, PERF_METRICS_URL, PERF_HEALTH_URL
#   PERF_JWT_SECRET   토큰 서명키 (perf.tokens.secretEnv 변수가 비어 있으면 secretFile에서 읽는다)
#   dbq               DB 접속 함수. perf.db가 mysql이면 mysqlp, postgresql이면 psqlp와 같다
#                     dbq -e "SELECT 1;" (mysql) / dbq -c "SELECT 1;" (psql) / dbq < file.sql
#   perf_reset_stats       쿼리 통계 리셋
#   perf_collect_stats F   쿼리 통계 원본을 F에 쓴다 (harness perf-query-stats가 읽는 형식)
#   perf_analyze T...      옵티마이저 통계 갱신 (인자 없으면 DB 전체)
#   perf_capture_plan F L  표준입력의 쿼리 하나로 실행계획과 작업량을 F에 덧붙인다. L은 라벨(Q1 등)
#   perf_requests F        k6-test-summary의 요청 수
# SQL의 이유와 함정은 stacks/{perf.db}.md에 있다. 함수 본문만 고치고 설명을 여기 옮기지 마라.

if [ $# -lt 2 ]; then
    echo "사용법: source .claude/skills/optimize-performance/template/perf-env.sh {이슈번호} {슬러그}" >&2
    return 1 2>/dev/null || exit 1
fi

PERF_ROOT="${CLAUDE_PROJECT_DIR:-$(git rev-parse --show-toplevel 2>/dev/null || pwd)}"
export HARNESS="$PERF_ROOT/.claude/scripts/harness"
if [ ! -x "$HARNESS" ]; then
    echo "harness CLI가 없다: $HARNESS. 레포 루트에서 source했는지 확인하라." >&2
    return 1 2>/dev/null || exit 1
fi

_perf_cfg() { "$HARNESS" get-config "perf.$1" 2>/dev/null; }

export PERF_DB="$(_perf_cfg db)"
export PERF_DB_NAME="$(_perf_cfg dbName)"
PERF_DB_USER="$(_perf_cfg dbUser)"
PERF_DB_CONTAINER="$(_perf_cfg dbContainer)"
PERF_DB_HOST="$(_perf_cfg dbHost)"
PERF_DB_PORT="$(_perf_cfg dbPort)"
_perf_pw_env="$(_perf_cfg dbPasswordEnv)"
PERF_DB_PASSWORD=""
if [ -n "$_perf_pw_env" ]; then
    PERF_DB_PASSWORD="$(printenv "$_perf_pw_env" 2>/dev/null)"
fi
[ -n "$PERF_DB_PASSWORD" ] || PERF_DB_PASSWORD="$(_perf_cfg dbPassword)"
PERF_MYSQL_CHARSET="$(_perf_cfg mysqlCharset)"
PERF_MYSQL_COLLATION="$(_perf_cfg mysqlCollation)"

export PERF_DIR="$(_perf_cfg resultsDir)/$1"
export TARGET_DIR="$PERF_DIR/$2"
export SEEDS="$(_perf_cfg seedsDir)"
export PERF_SCRIPTS="$PERF_ROOT/.claude/scripts/perf"
export PERF_APP_URL="$(_perf_cfg appBaseUrl)"
_perf_mgmt="$(_perf_cfg managementUrl)"
export PERF_METRICS_URL="$_perf_mgmt/prometheus"
export PERF_HEALTH_URL="$_perf_mgmt/health"

# 토큰 서명키. 변수에 이미 있으면 그대로, 없으면 프로파일 설정 파일의 키 줄에서 읽는다 (mint-tokens.sh와 같은 규칙)
_perf_secret_env="$(_perf_cfg tokens.secretEnv)"
_perf_secret_env="${_perf_secret_env:-PERF_JWT_SECRET}"
PERF_JWT_SECRET="$(printenv "$_perf_secret_env" 2>/dev/null)"
if [ -z "$PERF_JWT_SECRET" ] && [ "$(_perf_cfg tokens.mode)" = "mint" ]; then
    _perf_secret_file="$(_perf_cfg tokens.secretFile)"
    _perf_secret_key="$(_perf_cfg tokens.secretKey)"
    if [ -f "$_perf_secret_file" ]; then
        PERF_JWT_SECRET="$(grep -E "^[[:space:]]*${_perf_secret_key}[[:space:]]*[:=]" "$_perf_secret_file" | head -n 1 \
            | sed -E "s/^[[:space:]]*${_perf_secret_key}[[:space:]]*[:=][[:space:]]*//; s/[[:space:]]+#.*$//; s/^[\"']//; s/[\"'][[:space:]]*$//")"
    fi
    if [ -z "$PERF_JWT_SECRET" ]; then
        echo "경고: 토큰 서명키를 읽지 못했다. ${_perf_secret_env} 변수나 ${_perf_secret_file:-perf.tokens.secretFile}의 ${_perf_secret_key} 줄을 확인하라." >&2
    fi
fi
export PERF_JWT_SECRET
[ "$_perf_secret_env" = "PERF_JWT_SECRET" ] || export "$_perf_secret_env=$PERF_JWT_SECRET"

# ── DB 접속 ──────────────────────────────────────────────────────────────────────
# 함수로 정의한다. zsh는 따옴표 없는 변수 확장에 단어 분리를 하지 않아 "$CMD -e" 형식이 깨진다.
# 컨테이너가 있으면 docker exec로 컨테이너 안의 클라이언트를 쓴다. 호스트에 클라이언트가 없어도 되고,
# 대신 파일은 경로가 아니라 표준입력으로 넘겨야 한다 (dbq < file.sql, cat a.sql b.sql | dbq).

# MySQL. 네 요소 모두 필요하다 (stacks/mysql.md의 접속 절)
#   --default-character-set   없으면 latin1로 붙어 쿼리 안의 한글 리터럴이 ?가 되고 결과가 조용히 틀린다
#   --init-command SET NAMES  charset만 맞추면 collation이 서버 기본으로 남아 컬럼과 사용자 변수 비교가 ERROR 1267로 죽는다
mysqlp() {
    if [ -n "$PERF_DB_CONTAINER" ]; then
        docker exec -i -e MYSQL_PWD="$PERF_DB_PASSWORD" "$PERF_DB_CONTAINER" \
            mysql -u "$PERF_DB_USER" \
                --default-character-set="$PERF_MYSQL_CHARSET" \
                --init-command="SET NAMES $PERF_MYSQL_CHARSET COLLATE $PERF_MYSQL_COLLATION" \
                "$PERF_DB_NAME" "$@"
    else
        MYSQL_PWD="$PERF_DB_PASSWORD" mysql -h "$PERF_DB_HOST" -P "${PERF_DB_PORT:-3306}" -u "$PERF_DB_USER" \
            --default-character-set="$PERF_MYSQL_CHARSET" \
            --init-command="SET NAMES $PERF_MYSQL_CHARSET COLLATE $PERF_MYSQL_COLLATION" \
            "$PERF_DB_NAME" "$@"
    fi
}

# PostgreSQL. -X: 개인 psqlrc를 읽지 않는다(출력 형식이 바뀌면 변환기가 못 읽는다), ON_ERROR_STOP: 시드가 중간 오류를 넘기지 않는다
# PGCLIENTENCODING=UTF8: 없으면 클라이언트 로캘에 따라 쿼리 안의 한글 리터럴이 깨질 수 있다 (fix-concurrency의 dbc와 같은 설정)
psqlp() {
    if [ -n "$PERF_DB_CONTAINER" ]; then
        docker exec -i -e PGPASSWORD="$PERF_DB_PASSWORD" -e PGCLIENTENCODING=UTF8 "$PERF_DB_CONTAINER" \
            psql -X -v ON_ERROR_STOP=1 -U "$PERF_DB_USER" -d "$PERF_DB_NAME" "$@"
    else
        PGPASSWORD="$PERF_DB_PASSWORD" PGCLIENTENCODING=UTF8 psql -X -v ON_ERROR_STOP=1 -h "$PERF_DB_HOST" -p "${PERF_DB_PORT:-5432}" \
            -U "$PERF_DB_USER" -d "$PERF_DB_NAME" "$@"
    fi
}

dbq() {
    if [ "$PERF_DB" = "postgresql" ]; then psqlp "$@"; else mysqlp "$@"; fi
}

# ── 측정 함수 ────────────────────────────────────────────────────────────────────
# 원본은 harness perf-query-stats가 읽는 형식으로 쓴다. MySQL은 -B(TSV), PostgreSQL은 --csv. 계산과 반올림은 변환기가 한다.

perf_reset_stats() {
    if [ "$PERF_DB" = "postgresql" ]; then
        psqlp -qAt -c "SELECT pg_stat_statements_reset();" > /dev/null
    else
        mysqlp -e "TRUNCATE TABLE performance_schema.events_statements_summary_by_digest;"
    fi
}

perf_collect_stats() {
    if [ -z "$1" ]; then echo "사용법: perf_collect_stats {출력 파일}" >&2; return 1; fi
    if [ "$PERF_DB" = "postgresql" ]; then
        psqlp --csv -c "
SELECT calls, total_exec_time AS total_ms, rows, shared_blks_hit, shared_blks_read, query
FROM pg_stat_statements
WHERE dbid = (SELECT oid FROM pg_database WHERE datname = current_database())
  AND query NOT LIKE '%pg_stat_statements%'
ORDER BY total_exec_time DESC;" > "$1"
    else
        mysqlp -B -e "
SELECT COUNT_STAR AS calls, SUM_TIMER_WAIT AS total_ps, SUM_ROWS_SENT AS rows_sent,
       SUM_ROWS_EXAMINED AS rows_examined, DIGEST_TEXT AS query
FROM performance_schema.events_statements_summary_by_digest
WHERE SCHEMA_NAME = '$PERF_DB_NAME'
  AND DIGEST_TEXT NOT LIKE '%performance_schema%'
  AND DIGEST_TEXT NOT LIKE 'SET NAMES%'
ORDER BY SUM_TIMER_WAIT DESC;" > "$1"
    fi
}

perf_analyze() {
    if [ "$PERF_DB" = "postgresql" ]; then
        # VACUUM은 되돌리기 DELETE가 남긴 dead tuple을 회수한다. 테이블을 주면 그 테이블만
        if [ $# -eq 0 ]; then psqlp -c "VACUUM ANALYZE;"; else psqlp -c "VACUUM ANALYZE $(printf '%s, ' "$@" | sed 's/, $//');"; fi
    else
        if [ $# -eq 0 ]; then echo "MySQL은 테이블을 지정한다: perf_analyze {테이블...}" >&2; return 1; fi
        mysqlp -e "ANALYZE TABLE $(printf '%s, ' "$@" | sed 's/, $//');"
    fi
}

perf_capture_plan() {
    if [ -z "$1" ] || [ -z "$2" ]; then
        echo "사용법: perf_capture_plan {출력 파일} {라벨} <<'SQL' ... SQL" >&2; return 1
    fi
    local q
    q="$(cat | sed -E 's/[[:space:]]*;[[:space:]]*$//')"
    if [ "$PERF_DB" = "postgresql" ]; then
        # EXPLAIN ANALYZE는 쿼리를 실제로 실행한다. BEGIN/ROLLBACK으로 감싼다. 첫 실행은 버퍼를 채우는 용도라 버린다
        printf 'BEGIN;\nEXPLAIN (ANALYZE, BUFFERS, VERBOSE) %s;\nROLLBACK;\n' "$q" | psqlp > /dev/null
        {
            printf '=== [%s] ===\n' "$2"
            printf 'BEGIN;\nEXPLAIN (ANALYZE, BUFFERS, VERBOSE) %s;\nROLLBACK;\n' "$q" | psqlp
        } | tee -a "$1"
    else
        case "$(printf '%s' "$q" | tr -d '\n' | sed -E 's/^[[:space:](]*//' | cut -c1-6 | tr '[:lower:]' '[:upper:]')" in
            SELECT|WITH*)
                mysqlp -e "EXPLAIN ANALYZE $q" > /dev/null
                {
                    printf '=== [%s] ===\n=== EXPLAIN ANALYZE ===\n' "$2"
                    mysqlp -e "EXPLAIN ANALYZE $q\G"
                    printf '=== EXPLAIN FORMAT=JSON ===\n'
                    mysqlp -e "EXPLAIN FORMAT=JSON $q\G"
                    printf '=== STATUS COUNTERS ===\n'
                    mysqlp -e "FLUSH STATUS; $q; SHOW SESSION STATUS WHERE (Variable_name LIKE 'Handler_%' OR Variable_name LIKE 'Sort_%') AND Value > 0;" \
                        | grep -E '^(Handler_|Sort_)'
                } | tee -a "$1"
                ;;
            *)
                # 쓰기 문은 EXPLAIN ANALYZE를 받지 않는다. 작업량은 트랜잭션 안에서 재고 되돌린다
                {
                    printf '=== [%s] ===\n=== EXPLAIN FORMAT=JSON ===\n' "$2"
                    mysqlp -e "EXPLAIN FORMAT=JSON $q\G"
                    printf '=== STATUS COUNTERS ===\n'
                    mysqlp -e "FLUSH STATUS; BEGIN; $q; SHOW SESSION STATUS WHERE (Variable_name LIKE 'Handler_%' OR Variable_name LIKE 'Sort_%') AND Value > 0; ROLLBACK;" \
                        | grep -E '^(Handler_|Sort_)'
                } | tee -a "$1"
                ;;
        esac
    fi
}

perf_requests() {
    python3 -c 'import json,sys; v=json.load(open(sys.argv[1])).get("requests"); print(int(v) if v else "")' "$1" 2>/dev/null
}

printf 'PERF_DB=%s  PERF_DIR=%s  TARGET_DIR=%s  dbq 정의됨\n' "$PERF_DB" "$PERF_DIR" "$TARGET_DIR"
if [ -z "$PERF_DB_NAME" ]; then
    echo "경고: perf.dbName이 비어 있다. .claude/harness.json의 perf 절을 채워라." >&2
fi
