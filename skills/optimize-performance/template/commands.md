# 측정 명령 블록

Phase 4, 6, 8이 호출자에게 제시하는 명령이다. `{n}`과 `{…}` 자리를 채워 **블록 그대로** 제시한다.
전제: `perf-env.sh`를 source한 터미널 (`$HARNESS`, `$PERF_DIR`, `$TARGET_DIR`, `$PERF_SCRIPTS`, `$PERF_APP_URL`, `dbq`와 `perf_*` 함수).
DB별 SQL은 `perf_*` 함수 안에 있다. 이유와 함정은 `stacks/{perf.db}.md`에 있다.

## A. 부하 측정

Phase 4는 `{n}` = 0, Phase 8은 `{n}` = 이번 사이클 번호.

```bash
# 1) 토큰. 이슈 공용이라 이미 있고 만료 전이면 건너뛴다. 로그인 API를 태우지 않는다. 인증 없는 대상이면 없음
bash "$PERF_SCRIPTS/mint-tokens.sh" --start {ID_START} --count {USER_COUNT} --out "$PERF_DIR/tokens.json"

# 2) 워밍업 (JIT, 커넥션 풀, DB 버퍼, 애플리케이션 캐시). 이 실행의 결과는 쓰지 않는다
k6 run -e PHASE=warmup -e BASE_URL="$PERF_APP_URL" "$TARGET_DIR/test-script.js"

# 3) 되돌리기. 쓰기 엔드포인트면 record.md의 되돌리기 SQL을 여기서 실행한다 (dbq). 읽기면 없음

# 4) 통계 갱신 (PostgreSQL은 dead tuple 회수 포함). 시작 상태 확인은 stacks 파일 **통계 갱신** 절
perf_analyze {대상 쿼리가 읽고 쓰는 테이블...}

# 5) 애플리케이션 캐시 비우기. Phase 3-B에서 cold를 택한 경우에만. Phase 4와 8이 같아야 한다
{perf.cacheFlushCommand}

# 6) 쿼리 통계 리셋
perf_reset_stats

# 7) JVM 샘플러. 측정과 함께 돌고 9)에서 멈춘다
bash "$PERF_SCRIPTS/jvm-sampler.sh" sample --out "$TARGET_DIR/jvm-samples-{n}.csv" &
SAMPLER_PID=$!

# 8) 측정. handleSummary가 k6 원본을 쓴다
k6 run -e PHASE=measure -e BASE_URL="$PERF_APP_URL" -e SUMMARY_OUT="$TARGET_DIR/k6-raw-{n}.json" "$TARGET_DIR/test-script.js"

# 9) 샘플러 정지, 쿼리 통계 원본 수집, 가공. 가공이 성공하면 1차 출력을 지운다
kill $SAMPLER_PID; wait $SAMPLER_PID 2>/dev/null
perf_collect_stats "$TARGET_DIR/query-stats-raw-{n}.txt"
"$HARNESS" perf-summarize-k6 "$TARGET_DIR/k6-raw-{n}.json" --out "$TARGET_DIR/k6-test-summary-{n}.json" \
  && rm "$TARGET_DIR/k6-raw-{n}.json"
REQS=$(perf_requests "$TARGET_DIR/k6-test-summary-{n}.json")
if ! [ "$REQS" -gt 0 ] 2>/dev/null; then
  echo "요청 수가 '$REQS'다. 측정이 실패했으므로 가공하지 않는다. 원인을 확인하고 재측정하라."
else
  bash "$PERF_SCRIPTS/jvm-sampler.sh" summarize \
    --in "$TARGET_DIR/jvm-samples-{n}.csv" --out "$TARGET_DIR/jvm-metrics-{n}.md" --requests "$REQS" \
    && rm "$TARGET_DIR/jvm-samples-{n}.csv" "$TARGET_DIR/jvm-samples-{n}.csv.first.prom" "$TARGET_DIR/jvm-samples-{n}.csv.last.prom"
  "$HARNESS" perf-query-stats "$TARGET_DIR/query-stats-raw-{n}.txt" \
    --requests-from "$TARGET_DIR/k6-test-summary-{n}.json" --out "$TARGET_DIR/query-stats-summary-{n}.md" {--technique "{기법}"} \
    && rm "$TARGET_DIR/query-stats-raw-{n}.txt"
fi
```

`{--technique "{기법}"}`은 `{n}` ≥ 1일 때만 넣는다. 가공본 헤더의 상태 줄이 된다.

블록의 순서와 각 요소는 아래 이유로 고정이다. 바꾸거나 빼지 마라.

| 요소 | 이유 |
|---|---|
| 토큰 발급을 리셋 앞에 | 발급이 DB를 건드리는 방식이어도 통계에 섞이지 않는다 |
| 워밍업에 애플리케이션 캐시 | 캐시는 첫 요청이 채운다. 워밍업 없이 재면 miss 구간이 측정에 섞인다 |
| 워밍업 → 되돌리기 → 통계 갱신 순서 | 되돌리기 `DELETE` 뒤에 통계를 갱신해야 옵티마이저가 같은 계획을 고른다. Phase 4와 8의 계획이 달라지면 전후 비교가 아니다 |
| 리셋 뒤에 곧바로 측정 | 쿼리 통계는 인스턴스 전역이다. 사이에 다른 부하가 끼면 통계가 섞이고 `per_req`가 틀린다 |
| 샘플러를 리셋 뒤, 측정 앞에 띄움 | 워밍업의 GC와 heap이 섞이지 않는다. 샘플의 `offset_s` 0이 측정 시작이고 `.first.prom`이 증분의 기준점이다 |
| `kill` 뒤 `wait` | 마지막 행과 `.last.prom`을 다 쓴 뒤에 가공한다 |
| 통계 수집을 가공 앞으로 | 측정 직후의 통계만 담는다. 가공 명령은 DB를 건드리지 않는다 |
| `REQS`를 가공 앞으로 | 샘플러 가공본의 "요청당" 칼럼과 쿼리 통계의 `per_req`가 같은 분모를 쓴다 |
| `REQS` 가드 | 요청 0건이면 분모가 0이다. 변환기도 0건이면 쓰지 않고 실패한다 |
| 1차 출력 삭제는 `&&` 뒤 | 가공이 실패하면 원본이 남아 원인을 보고 다시 가공할 수 있다 |
| 반올림 없음 | 수집 단계에서 반올림하지 않는다. 변환기가 정한 고정 자릿수 외에는 대화의 표에서만 줄인다 |

## B. 실행계획 캡처

Phase 6은 `{n}` = 사이클 번호 - 1, Phase 8은 `{n}` = 사이클 번호.
`{대상 쿼리}`는 가공본 쿼리 원문의 자리표시자(`?`, `$1`)에 `record.md` **실행계획**에 적어둔 값을 대입한 것이다.
`{라벨}`은 `record.md`에 적어둔 라벨(`Q1`, `Q2`)이다. 쿼리가 여럿이면 라벨마다 한 번씩 부른다.

```bash
perf_capture_plan "$TARGET_DIR/query-plan-{n}.txt" {라벨} <<'SQL'
{대상 쿼리}
SQL
```

| 요소 | 이유 |
|---|---|
| 버리는 1회 실행 | 함수가 먼저 한 번 실행해 버퍼를 채운다. 그 출력은 쓰지 않는다 |
| `=== [라벨] ===` 머리줄 | `perf-delta`가 라벨로 상태 간 같은 쿼리를 짝짓는다 |
| `tee -a` | 같은 상태의 다른 쿼리 계획을 덮어쓰지 않는다 |
| 쓰기 쿼리 | 함수가 트랜잭션 안에서 재고 되돌린다. 시퀀스, AUTO_INCREMENT 증가와 트리거의 외부 호출은 되돌려지지 않는다. 대상이 쓰기면 그 사실을 호출자에게 알린다 |
| DB별 차이 | 무엇을 담는지(`BUFFERS` 또는 `Handler_%`)는 stacks 파일 **실행계획 캡처** 절 |
