#!/usr/bin/env bash
# 부하 측정 중 애플리케이션의 JVM, 커넥션 풀, 캐시, Redis 상태를 Prometheus 엔드포인트에서 주기적으로 긁어 가공본을 만든다.
#
# 왜 주기 샘플링인가:
#   Micrometer의 *_max 계열(gc_pause_seconds_max, acquire_seconds_max, requests_active_seconds_max)은
#   최근 창(기본 2분)에서 감쇠하는 값이라 측정이 끝난 뒤 한 번 긁으면 0이다. 부하 중에 긁어야 잡힌다.
#   heap 피크, 커넥션 대기 피크 같은 순간값도 전후 스냅샷으로는 보이지 않는다.
#
# 두 종류의 원본을 남긴다:
#   {out}              게이지 시계열 CSV. 매 회 한 행
#   {out}.first.prom   첫 스크랩 원문. 누적 카운터 증분의 기준점
#   {out}.last.prom    마지막 스크랩 원문. 매 회 덮어쓴다
#   라벨이 가변인 카운터(캐시명별, Redis 명령별, 리포지토리 메서드별)는 CSV 칼럼이 아니라 first/last 증분으로 본다.
#   새 지표군이 생겨도 칼럼을 늘리지 않아도 된다.
#
# 사용:
#   bash jvm-sampler.sh check     [--url URL]                                 지표 노출 점검. 게이트 7개 중 하나라도 없으면 exit 1
#   bash jvm-sampler.sh sample    --out FILE [--url URL] [--interval SEC]     종료 신호(TERM, INT)를 받을 때까지 긁는다
#   bash jvm-sampler.sh summarize --in FILE --out FILE [--requests N]        CSV와 first/last 원문을 읽어 md 가공본을 쓴다
#
# 설정 (앞에 있는 것이 우선. harness 설정은 이 스크립트 옆의 ../harness get-config로 읽는다):
#   URL          --url > PERF_METRICS_URL > {perf.managementUrl}/prometheus > http://localhost:8080/actuator/prometheus
#   SEC          --interval > PERF_SAMPLE_INTERVAL > 5
#   지표 이름    PERF_METRIC_POOL, PERF_METRIC_HTTP_ACTIVE, PERF_OLD_GEN_POOL, PERF_METRIC_CACHE, PERF_METRIC_REDIS,
#                PERF_METRIC_REPOSITORY > perf.metrics.{pool,httpActive,oldGenPool,cache,redis,repository} > Micrometer 기본값
#
# 예 (optimize-performance commands.md의 A 블록):
#   bash .claude/scripts/perf/jvm-sampler.sh sample --out "$TARGET_DIR/jvm-samples-0.csv" &
#   SAMPLER_PID=$!
#   k6 run ...
#   kill $SAMPLER_PID; wait $SAMPLER_PID 2>/dev/null
#   bash .claude/scripts/perf/jvm-sampler.sh summarize --in "$TARGET_DIR/jvm-samples-0.csv" --out "$TARGET_DIR/jvm-metrics-0.md" --requests "$REQS"
#
# 가공본 규칙:
#   - 캐시, Redis, 리포지토리 구획은 측정 구간 증분이 있을 때만 표로 나타난다. 증분이 전부 0이면 한 줄로 접는다.
#     구획이 없다는 것은 그 대상이 거기 닿지 않았다는 관측이다.
#   - 응답에 없는 지표군은 CSV에서 빈 칼럼, 요약 표에서 `미수집`이다. 0으로 채우지 않는다.
#   - 소수 자릿수: MB와 ms는 1자리, CPU와 overhead는 3자리, 요청당은 3자리, 적중률은 % 1자리.
#   - 표의 행 이름(`HikariCP pending` 등)은 harness perf-delta가 읽는다. 지표 이름을 바꿔도 행 이름은 바꾸지 마라.

set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
HARNESS="$SCRIPT_DIR/../harness"
CSV_HEADER=offset_s,heap_used_mb,heap_max_mb,old_after_gc_pct,gc_count,gc_pause_ms,gc_pause_max_ms,gc_overhead,threads_live,threads_blocked,hikari_active,hikari_pending,hikari_timeout_total,hikari_acquire_max_ms,http_active,http_active_max_ms,process_cpu,system_cpu

usage() {
    sed -n '2,40p' "$0" | sed 's/^# \{0,1\}//' >&2
    exit 1
}

# harness 설정 한 키. 런처가 없거나 값이 비면 기본값
cfg() {
    local v=""
    if [ -x "$HARNESS" ]; then
        v=$("$HARNESS" get-config "$1" 2>/dev/null || true)
    fi
    if [ -n "$v" ]; then printf '%s' "$v"; else printf '%s' "$2"; fi
}

MODE="${1:-}"
[ -n "$MODE" ] && shift || usage

URL="${PERF_METRICS_URL:-}"
INTERVAL="${PERF_SAMPLE_INTERVAL:-5}"
OUT=""
IN=""
REQUESTS=""

while [ $# -gt 0 ]; do
    case "$1" in
        --url)      URL="$2";      shift 2 ;;
        --out)      OUT="$2";      shift 2 ;;
        --in)       IN="$2";       shift 2 ;;
        --interval) INTERVAL="$2"; shift 2 ;;
        --requests) REQUESTS="$2"; shift 2 ;;
        *) echo "알 수 없는 인자: $1" >&2; exit 1 ;;
    esac
done

if [ -z "$URL" ]; then
    URL="$(cfg perf.managementUrl http://localhost:8080/actuator)/prometheus"
fi
POOL="${PERF_METRIC_POOL:-$(cfg perf.metrics.pool hikaricp_connections)}"
HTTPA="${PERF_METRIC_HTTP_ACTIVE:-$(cfg perf.metrics.httpActive http_server_requests_active_seconds)}"
OLDGEN="${PERF_OLD_GEN_POOL:-$(cfg perf.metrics.oldGenPool long-lived)}"
CACHE="${PERF_METRIC_CACHE:-$(cfg perf.metrics.cache cache)}"
REDIS="${PERF_METRIC_REDIS:-$(cfg perf.metrics.redis lettuce_seconds)}"
REPO="${PERF_METRIC_REPOSITORY:-$(cfg perf.metrics.repository spring_data_repository_invocations_seconds)}"

scrape() {
    curl -sf -m 3 "$URL"
}

# 스크랩 원문(stdin)을 CSV 한 행(offset 제외)으로. 지표군이 없으면 그 칼럼은 빈 값.
# 라벨은 application 이 맨 앞이라 이름 뒤 접두 매칭 대신 index() 로 라벨을 찾는다.
row_from_scrape() {
    awk -v pool="$POOL" -v httpa="$HTTPA" -v oldgen="$OLDGEN" '
    function f(v, has, d) { return has ? sprintf("%." d "f", v) : "" }
    /^#/ { next }
    /^jvm_memory_used_bytes\{/ && index($0, "area=\"heap\"")            { heap_used += $NF; has_heap = 1 }
    /^jvm_memory_max_bytes\{/  && index($0, "area=\"heap\"")            { if ($NF > 0) heap_max += $NF }
    /^jvm_memory_usage_after_gc\{/ && index($0, "pool=\"" oldgen "\"") { old_after = $NF * 100; has_old = 1 }
    /^jvm_gc_pause_seconds_count\{/                                     { gc_count += $NF; has_gc = 1 }
    /^jvm_gc_pause_seconds_sum\{/                                       { gc_sum += $NF * 1000 }
    /^jvm_gc_pause_seconds_max\{/                                       { v = $NF * 1000; if (v > gc_max) gc_max = v }
    /^jvm_gc_overhead[{ ]/                                              { gc_ovh = $NF; has_ovh = 1 }
    /^jvm_threads_live_threads[{ ]/                                     { threads = $NF; has_threads = 1 }
    /^jvm_threads_states_threads\{/ && index($0, "state=\"blocked\"")   { blocked = $NF; has_blocked = 1 }
    $0 ~ ("^" pool "_active\\{")                                       { h_active += $NF; has_hikari = 1 }
    $0 ~ ("^" pool "_pending\\{")                                      { h_pending += $NF }
    $0 ~ ("^" pool "_timeout_total\\{")                                { h_timeout += $NF }
    $0 ~ ("^" pool "_acquire_seconds_max\\{")                          { v = $NF * 1000; if (v > h_acq) h_acq = v }
    $0 ~ ("^" httpa "_gcount\\{")                                      { http_active += $NF; has_http = 1 }
    $0 ~ ("^" httpa "_max\\{")                                         { v = $NF * 1000; if (v > http_max) http_max = v }
    /^process_cpu_usage[{ ]/                                            { pcpu = $NF; has_pcpu = 1 }
    /^system_cpu_usage[{ ]/                                             { scpu = $NF; has_scpu = 1 }
    END {
        printf "%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s\n",
            f(heap_used / 1048576, has_heap, 1), f(heap_max / 1048576, has_heap, 1), f(old_after, has_old, 1),
            f(gc_count, has_gc, 0), f(gc_sum, has_gc, 1), f(gc_max, has_gc, 1), f(gc_ovh, has_ovh, 3),
            f(threads, has_threads, 0), f(blocked, has_blocked, 0),
            f(h_active, has_hikari, 0), f(h_pending, has_hikari, 0), f(h_timeout, has_hikari, 0), f(h_acq, has_hikari, 1),
            f(http_active, has_http, 0), f(http_max, has_http, 1),
            f(pcpu, has_pcpu, 3), f(scpu, has_scpu, 3)
    }'
}

# ── check ──────────────────────────────────────────────────────────────────────

do_check() {
    local body
    if ! body=$(scrape); then
        echo "앱이 $URL 에 떠 있지 않다. 측정 프로파일로 기동했는지, perf.managementUrl이 실제 관리 포트와 같은지 확인하라." >&2
        exit 1
    fi

    local missing=0
    check_family() {  # $1 표시명, $2 패턴, $3 게이트 여부(1/0), $4 없을 때 조치
        # grep -q 는 첫 매치에서 끝나 printf 가 SIGPIPE 를 받고 pipefail 이 실패로 판정한다. -c 로 끝까지 읽는다
        if [ "$(printf '%s\n' "$body" | grep -cE "$2")" -gt 0 ]; then
            printf '%-14s 있음\n' "$1"
        else
            printf '%-14s 없음  → %s\n' "$1" "$4"
            if [ "$3" = 1 ]; then
                missing=$((missing + 1))
            fi
        fi
        return 0
    }

    echo "게이트 (7개 모두 있어야 한다)"
    local profile_hint="측정 프로파일로 떴는지, Actuator의 prometheus 노출이 켜져 있는지 확인"
    check_family heap        '^jvm_memory_used_bytes\{.*area="heap"'  1 "$profile_hint"
    check_family gc          '^jvm_gc_pause_seconds_count\{'          1 "GC가 아직 한 번도 안 돈 것. 메인 포트로 요청을 몇 번 보내고 재시도"
    check_family threads     '^jvm_threads_live_threads'              1 "$profile_hint"
    check_family pool        "^${POOL}_active\\{"                    1 "메인 포트로 요청 1회 뒤 재시도. 풀이 HikariCP가 아니면 PERF_METRIC_POOL 또는 perf.metrics.pool"
    check_family http_active "^${HTTPA}_gcount\\{"                   1 "메인 포트로 요청 1회 뒤 재시도 (관리 포트 요청은 메인 서버 미터를 만들지 않는다)"
    check_family process_cpu '^process_cpu_usage'                     1 "$profile_hint"
    check_family system_cpu  '^system_cpu_usage'                      1 "$profile_hint"
    echo
    echo "참고 (대상에 따라 없을 수 있다. 게이트 아님)"
    check_family cache       "^${CACHE}_gets_total\\{"               0 "캐시가 등록되지 않았다. 캐싱 대상이 아니면 무관"
    check_family redis       "^${REDIS}_count\\{"                    0 "Redis 클라이언트가 아직 명령을 보내지 않았다"
    check_family repository  "^${REPO}_count\\{"                     0 "리포지토리 호출이 아직 없다. 요청 1회 뒤 생긴다"

    if [ "$missing" -gt 0 ]; then
        echo
        echo "게이트 ${missing}개 없음. 조치 뒤 다시 실행하라." >&2
        exit 1
    fi
}

# ── sample ─────────────────────────────────────────────────────────────────────

do_sample() {
    [ -n "$OUT" ] || { echo "--out 이 필요하다" >&2; exit 1; }
    [ -d "$(dirname "$OUT")" ] || { echo "디렉토리가 없다: $(dirname "$OUT")" >&2; exit 1; }

    if [ ! -f "$OUT" ]; then
        echo "$CSV_HEADER" > "$OUT"
    fi

    local start now body row sleep_pid=""
    start=$(date +%s)
    # 종료 신호가 sleep 을 기다리지 않게 sleep 을 백그라운드로 두고 wait 한다
    trap '[ -n "$sleep_pid" ] && kill "$sleep_pid" 2>/dev/null; exit 0' TERM INT

    while :; do
        now=$(date +%s)
        if body=$(scrape); then
            [ -f "$OUT.first.prom" ] || printf '%s\n' "$body" > "$OUT.first.prom"
            printf '%s\n' "$body" > "$OUT.last.prom"
            row=$(printf '%s\n' "$body" | row_from_scrape)
            echo "$((now - start)),$row" >> "$OUT"
        else
            echo "offset $((now - start))s: 스크랩 실패 ($URL). 이 회차는 기록하지 않는다." >&2
        fi
        sleep "$INTERVAL" &
        sleep_pid=$!
        wait "$sleep_pid" || true
        sleep_pid=""
    done
}

# ── summarize ──────────────────────────────────────────────────────────────────

do_summarize() {
    [ -n "$IN" ]  || { echo "--in 이 필요하다" >&2; exit 1; }
    [ -n "$OUT" ] || { echo "--out 이 필요하다" >&2; exit 1; }
    [ -f "$IN" ]  || { echo "CSV가 없다: $IN" >&2; exit 1; }
    [ -f "$IN.first.prom" ] && [ -f "$IN.last.prom" ] || { echo "원문이 없다: $IN.first.prom / $IN.last.prom" >&2; exit 1; }
    if [ -n "$REQUESTS" ] && ! [ "$REQUESTS" -gt 0 ] 2>/dev/null; then
        echo "--requests 는 양의 정수여야 한다: '$REQUESTS'" >&2; exit 1
    fi

    local base name
    base=$(basename "$OUT")
    case "$base" in
        jvm-metrics-*.md) name="jvm-metrics-${base#jvm-metrics-}"; name="${name%.md}" ;;
        *)                name="$base" ;;
    esac

    {
        echo "# $name"
        echo

        # 헤더와 게이지 표. 칼럼: 1 offset 2 heap_used 3 heap_max 4 old_after 5 gc_count 6 gc_pause 7 gc_max 8 gc_ovh
        #                       9 threads 10 blocked 11 h_active 12 h_pending 13 h_timeout 14 h_acq 15 http_active 16 http_max 17 pcpu 18 scpu
        awk -F, -v requests="$REQUESTS" '
        function mx(c, d) { return n[c] ? sprintf("%." d "f", m[c]) : "미수집" }
        function av(c, d) { return n[c] ? sprintf("%." d "f", s[c] / n[c]) : "미수집" }
        function at(c)    { return n[c] ? t[c] : "-" }
        NR == 1 { next }
        {
            rows++; last = $1
            for (c = 2; c <= 18; c++) {
                if ($c == "") continue
                n[c]++; s[c] += $c
                if (!(c in m) || $c > m[c]) { m[c] = $c; t[c] = $1 }
            }
            heap_max = ($3 != "") ? $3 : heap_max
        }
        END {
            interval = rows > 1 ? sprintf("%.0f", last / (rows - 1)) : "-"
            req = requests != "" ? requests "건" : "미지정"
            printf "샘플 %d건 / 간격 %ss / 구간 0~%ss / 요청 %s\n\n", rows, interval, last, req
            print "## 게이지 (샘플 중 최대, 평균)"
            print ""
            print "| 지표 | 최대 | 평균 | 최대 시점(s) | 기준 |"
            print "|---|---|---|---|---|"
            printf "| heap 사용 (MB) | %s | %s | %s | heap max %s MB |\n", mx(2,1), av(2,1), at(2), (heap_max != "" ? heap_max : "미수집")
            printf "| GC 후 old gen 점유 (%%) | %s | %s | %s | 바닥이 오르면 누수나 캐시 적재 |\n", mx(4,1), av(4,1), at(4)
            printf "| GC 최장 정지 (ms) | %s | - | %s | |\n", mx(7,1), at(7)
            printf "| GC overhead | %s | %s | %s | GC가 쓴 CPU 비율 (0~1) |\n", mx(8,3), av(8,3), at(8)
            printf "| 스레드 수 / blocked | %s / %s | %s / %s | %s | blocked > 0이면 락 경합 |\n", mx(9,0), mx(10,0), av(9,1), av(10,1), at(10)
            printf "| HikariCP active | %s | %s | %s | 풀 크기는 record.md 측정 환경 |\n", mx(11,0), av(11,1), at(11)
            printf "| HikariCP pending | %s | %s | %s | 0보다 크면 커넥션 대기 |\n", mx(12,0), av(12,1), at(12)
            printf "| HikariCP acquire max (ms) | %s | - | %s | |\n", mx(14,1), at(14)
            printf "| 처리 중 요청 수 / 최장 (ms) | %s / %s | %s / - | %s | 서버 안 동시 요청 |\n", mx(15,0), mx(16,1), av(15,1), at(15)
            printf "| process CPU | %s | %s | %s | 0~1 |\n", mx(17,3), av(17,3), at(17)
            printf "| system CPU | %s | %s | %s | 0~1 |\n", mx(18,3), av(18,3), at(18)
            print ""
        }' "$IN"

        # 누적 증분과 라벨 가변 구획. first.prom → last.prom
        awk -v requests="$REQUESTS" -v pool="$POOL" -v repo="$REPO" '
        function lab(s, name,    r) {
            if (match(s, name "=\"[^\"]*\"")) {
                r = substr(s, RSTART + length(name) + 2, RLENGTH - length(name) - 3)
                return r
            }
            return ""
        }
        function delta(k) { return (k in last) ? last[k] - ((k in first) ? first[k] : 0) : 0 }
        function per(v) { return requests != "" ? sprintf("%.3f", v / requests) : "-" }
        # 키는 값 앞까지 전부다. 라벨 안에 공백이 있어(action="end of minor GC") $1 로 자르면 행이 뭉개진다
        function key(line,    k) { k = line; sub(/ [^ ]*$/, "", k); return k }
        /^#/ { next }
        FNR == NR { first[key($0)] = $NF; next }
        { last[key($0)] = $NF }
        END {
            # 누적 표. 라벨이 있는 지표는 라벨 무시하고 합산한다
            for (k in last) {
                if (k ~ /^jvm_gc_pause_seconds_count\{/) { gc_c_l += last[k]; gc_c_f += (k in first ? first[k] : 0); has_gc = 1 }
                if (k ~ /^jvm_gc_pause_seconds_sum\{/)   { gc_s_l += last[k]; gc_s_f += (k in first ? first[k] : 0) }
                if (k ~ /^jvm_gc_memory_allocated_bytes_total/) { al_l += last[k]; al_f += (k in first ? first[k] : 0); has_al = 1 }
                if (k ~ /^jvm_gc_memory_promoted_bytes_total/)  { pr_l += last[k]; pr_f += (k in first ? first[k] : 0); has_pr = 1 }
                if (k ~ ("^" pool "_timeout_total\\{")) { to_l += last[k]; to_f += (k in first ? first[k] : 0); has_to = 1 }
                if (k ~ ("^" pool "_usage_seconds_sum\\{"))   { us_s += delta(k); has_us = 1 }
                if (k ~ ("^" pool "_usage_seconds_count\\{")) { us_c += delta(k) }
            }
            print "## 누적 (측정 구간 증분)"
            print ""
            print "| 지표 | 시작 | 끝 | 증분 | 요청당 |"
            print "|---|---|---|---|---|"
            if (has_gc) {
                printf "| GC 횟수 | %d | %d | %d | %s |\n", gc_c_f, gc_c_l, gc_c_l - gc_c_f, per(gc_c_l - gc_c_f)
                printf "| GC 일시정지 합 (ms) | %.1f | %.1f | %.1f | %s |\n", gc_s_f * 1000, gc_s_l * 1000, (gc_s_l - gc_s_f) * 1000, per((gc_s_l - gc_s_f) * 1000)
            } else {
                print "| GC 횟수 | 미수집 | 미수집 | 미수집 | - |"
                print "| GC 일시정지 합 (ms) | 미수집 | 미수집 | 미수집 | - |"
            }
            if (has_al) printf "| 할당량 (MB) | %.1f | %.1f | %.1f | %s |\n", al_f / 1048576, al_l / 1048576, (al_l - al_f) / 1048576, per((al_l - al_f) / 1048576)
            else        print "| 할당량 (MB) | 미수집 | 미수집 | 미수집 | - |"
            if (has_pr) printf "| old gen 승격량 (MB) | %.1f | %.1f | %.1f | %s |\n", pr_f / 1048576, pr_l / 1048576, (pr_l - pr_f) / 1048576, per((pr_l - pr_f) / 1048576)
            else        print "| old gen 승격량 (MB) | 미수집 | 미수집 | 미수집 | - |"
            if (has_to) printf "| HikariCP timeout | %d | %d | %d | %s |\n", to_f, to_l, to_l - to_f, per(to_l - to_f)
            else        print "| HikariCP timeout | 미수집 | 미수집 | 미수집 | - |"
            if (has_us) printf "| 커넥션 보유 평균 (ms) | - | - | %s | - |\n", (us_c > 0 ? sprintf("%.1f", us_s / us_c * 1000) : "호출 없음")
            else        print "| 커넥션 보유 평균 (ms) | - | - | 미수집 | - |"
            print ""

            # 리포지토리 호출. 증분 > 0인 메서드만, 증분 내림차순.
            # 정렬은 sort 파이프로 한다. 파이프 출력이 awk 의 print 버퍼보다 먼저 나가지 않게 fflush 한다.
            # 행은 ROW 접두로 흘려보내고 뒤 awk 가 표로 감싼다.
            print "## 리포지토리 호출"
            fflush()
            sortcmd = "sort -t\"\t\" -k2,2nr"
            for (k in last) {
                if (k !~ ("^" repo "_count\\{")) continue
                d = delta(k); if (d <= 0) continue
                ks = k; sub(/_count\{/, "_sum{", ks)
                mean = (ks in last) ? delta(ks) / d * 1000 : 0
                printf "ROW\t%d\t%s.%s\t%s\t%.1f\n", d, lab(k, "repository"), lab(k, "method"), per(d), mean | sortcmd
            }
            close(sortcmd)
            print "END_REPO"
        }' "$IN.first.prom" "$IN.last.prom" | awk '
        /^## 리포지토리 호출$/ { print; print ""; next }
        /^ROW\t/ {
            if (!hdr) { print "| repository.method | 호출 증분 | 요청당 | mean ms |"; print "|---|---|---|---|"; hdr = 1 }
            split($0, f, "\t")
            printf "| %s | %s | %s | %s |\n", f[3], f[2], f[4], f[5]
            next
        }
        /^END_REPO$/ { if (!hdr) print "리포지토리 호출: 측정 구간 호출 없음"; print ""; next }
        { print }'

        # 캐시와 Redis 구획. 증분이 있을 때만 표
        awk -v requests="$REQUESTS" -v cache="$CACHE" -v redis="$REDIS" '
        function lab(s, name,    r) {
            if (match(s, name "=\"[^\"]*\"")) return substr(s, RSTART + length(name) + 2, RLENGTH - length(name) - 3)
            return ""
        }
        function delta(k) { return (k in last) ? last[k] - ((k in first) ? first[k] : 0) : 0 }
        function per(v) { return requests != "" ? sprintf("%.3f", v / requests) : "-" }
        function key(line,    k) { k = line; sub(/ [^ ]*$/, "", k); return k }
        /^#/ { next }
        FNR == NR { first[key($0)] = $NF; next }
        { last[key($0)] = $NF }
        END {
            for (k in last) {
                if (k ~ ("^" cache "_gets_total\\{"))     { c = lab(k, "cache"); r = lab(k, "result"); gets[c, r] += delta(k); caches[c] = 1; cache_any += delta(k) }
                if (k ~ ("^" cache "_puts_total\\{"))     { c = lab(k, "cache"); puts[c] += delta(k); caches[c] = 1; cache_any += delta(k) }
                if (k ~ ("^" cache "_removals_total\\{")) { c = lab(k, "cache"); rems[c] += delta(k); caches[c] = 1; cache_any += delta(k) }
                if (k ~ ("^" redis "_count\\{")) {
                    op = lab(k, "db_operation"); d = delta(k)
                    if (d > 0) {
                        ks = k; sub(/_count\{/, "_sum{", ks)
                        km = k; sub(/_count\{/, "_max{", km)
                        rc[op] += d; rs[op] += (ks in last ? delta(ks) : 0)
                        mv = (km in last) ? last[km] * 1000 : 0; if (mv > rm[op]) rm[op] = mv
                        redis_any += d
                    }
                }
            }
            print "## 캐시"
            print ""
            if (cache_any > 0) {
                print "| 캐시 | hit | miss | pending | put | removal | 적중률 |"
                print "|---|---|---|---|---|---|---|"
                for (c in caches) {
                    h = gets[c, "hit"] + 0; mi = gets[c, "miss"] + 0
                    rate = (h + mi > 0) ? sprintf("%.1f%%", 100 * h / (h + mi)) : "-"
                    printf "| %s | %d | %d | %d | %d | %d | %s |\n", c, h, mi, gets[c, "pending"] + 0, puts[c] + 0, rems[c] + 0, rate
                }
            } else {
                print "캐시: 측정 구간 접근 없음"
            }
            print ""
            print "## Redis"
            print ""
            if (redis_any > 0) {
                print "| 명령 | 호출 증분 | 요청당 | mean ms | max ms |"
                print "|---|---|---|---|---|"
                fflush()
                sortcmd = "sort -t\"\t\" -k2,2nr"
                for (op in rc) printf "ROW\t%d\t%s\t%s\t%.2f\t%.1f\n", rc[op], op, per(rc[op]), rs[op] / rc[op] * 1000, rm[op] | sortcmd
                close(sortcmd)
            } else {
                print "Redis: 측정 구간 호출 없음"
            }
            print ""
        }' "$IN.first.prom" "$IN.last.prom" | awk '
        /^ROW\t/ { split($0, f, "\t"); printf "| %s | %s | %s | %s | %s |\n", f[3], f[2], f[4], f[5], f[6]; next }
        { print }'

        # 타임라인
        awk -F, '
        function v(c) { return $c == "" ? "-" : $c }
        NR == 1 {
            print "## 타임라인"
            print ""
            print "| offset_s | heap_used_mb | gc_pause_ms(증분) | gc_pause_max_ms | threads_blocked | hikari_active | hikari_pending | http_active | process_cpu |"
            print "|---|---|---|---|---|---|---|---|---|"
            next
        }
        {
            inc = ($6 == "" || prev == "") ? ($6 == "" ? "-" : "0.0") : sprintf("%.1f", $6 - prev)
            if ($6 != "") prev = $6
            printf "| %s | %s | %s | %s | %s | %s | %s | %s | %s |\n", $1, v(2), inc, v(7), v(10), v(11), v(12), v(15), v(17)
        }' "$IN"
    } > "$OUT"

    echo "가공본 작성: $OUT"
}

case "$MODE" in
    check)     do_check ;;
    sample)    do_sample ;;
    summarize) do_summarize ;;
    *)         usage ;;
esac
