# PostgreSQL 측정 명령

phase 파일이 절 이름으로 이 파일을 가리킨다. 명령은 모두 `dbc` 함수를 쓴다.

## 접속

`dbc`는 `.claude/harness.json`의 `perf` 절(`dbContainer`, `dbName`, `dbUser`, `dbPassword`)로 만든 함수다.
직접 쓰지 말고 CLI가 만든 정의를 블록 첫머리에 넣는다.

```bash
eval "$(.claude/scripts/harness conc-shell --issue {이슈번호} --slug {슬러그})"
```

`CONC_DIR`, `TARGET_DIR`과 함께 아래 형태의 함수가 정의된다.

```bash
dbc() { docker exec -i -e PGPASSWORD=... -e PGCLIENTENCODING=UTF8 {perf.dbContainer} psql -X -q -v ON_ERROR_STOP=1 -U {perf.dbUser} -d {perf.dbName} "$@"; }
```

- **문자열 변수로 만들지 마라.** zsh는 파라미터 확장 결과를 단어 분할하지 않아 `$DB -c "..."`가 명령어 하나로 해석된다.
- `-X`는 `~/.psqlrc`를 읽지 않는다. 개인 설정(출력 형식, `\timing`)이 산출물 형식을 바꾸지 않게 한다.
- `-v ON_ERROR_STOP=1`이 없으면 파일 중간의 오류를 넘기고 계속 실행해, 일부만 적용된 시드나 되돌리기가 성공처럼 보인다.
- `PGCLIENTENCODING=UTF8`이 없으면 컨테이너 로케일에 따라 한글 리터럴이 깨질 수 있다.
- `--csv`(머리 행 포함)와 `--csv -t`(머리 행 없음)는 psql 12 이상이다.

## 격리 수준과 타임아웃

```bash
dbc -c "
SELECT current_setting('default_transaction_isolation') AS isolation,
       current_setting('lock_timeout')                  AS lock_timeout,
       current_setting('deadlock_timeout')              AS deadlock_timeout,
       current_setting('statement_timeout')             AS statement_timeout,
       current_setting('log_lock_waits')                AS log_lock_waits,
       current_setting('max_connections')               AS max_connections,
       version();"
```

- 기본값은 `read committed`, `lock_timeout` 0(무제한), `deadlock_timeout` 1초다.
- **`lock_timeout`이 0이면** 비관적 락 후보에서 요청이 타임아웃 없이 끝까지 밀린다. 실패가 관측되지 않는 이유가 되므로 기록해둔다.
  애플리케이션이 세션 단위로 `SET lock_timeout`을 거는지도 확인한다.
- `max_connections`가 커넥션 풀 크기보다 작으면 풀이 차지 않는다. 올리려면 `ALTER SYSTEM SET max_connections = {값};` 뒤 **재기동**이 필요하다.

## 락 관측 수단 점검

PostgreSQL에는 **락 대기 누적 카운터가 없다.** 데드락만 누적된다. 나머지는 서버 로그와 부하 중 표본으로 본다.

```bash
# 데드락, 커밋, 롤백 누적 카운터
dbc -c "
SELECT deadlocks, xact_commit, xact_rollback, conflicts
FROM pg_stat_database WHERE datname = current_database();"

# 실시간 락 대기 (부하 중에만 행이 보인다)
dbc -c "SELECT count(*) AS waiting FROM pg_locks WHERE NOT granted;"

# 락 대기를 서버 로그에 남기는지
dbc -c "SHOW log_lock_waits;"
```

- `log_lock_waits`가 `off`면 켠다. `deadlock_timeout`보다 오래 기다린 락마다 `still waiting for` 로그가 남고,
  `measure.sh`가 측정 구간의 로그 건수를 센다. 이것이 PostgreSQL의 락 대기 지표다.

  ```bash
  dbc -c "ALTER SYSTEM SET log_lock_waits = on;" -c "SELECT pg_reload_conf();"
  ```

  되돌릴 때는 `ALTER SYSTEM RESET log_lock_waits;` 뒤 `pg_reload_conf()`. 바꿨으면 record.md의 측정 환경에 남긴다.
- 서버 로그를 읽으려면 `perf.dbContainer`가 있어야 한다(`docker logs`). 호스트 설치면 로그 파일 경로를 확인해 수동으로 센다.
- `pg_locks` 조회는 권한 없이도 된다. 표본을 끄려면 `concurrency.sampleLockWaits`를 false로 둔다.

## 측정 구간 락 카운터

`measure.sh`가 부하 직전(`=== BEFORE ===`)과 직후(`=== AFTER ===`)에 아래를 `--csv -t`(쉼표 구분, 머리 행 없음)로 기록한다.

```sql
SELECT 'deadlocks', deadlocks FROM pg_stat_database WHERE datname = current_database()
UNION ALL SELECT 'xact_commit', xact_commit FROM pg_stat_database WHERE datname = current_database()
UNION ALL SELECT 'xact_rollback', xact_rollback FROM pg_stat_database WHERE datname = current_database()
UNION ALL SELECT 'conflicts', conflicts FROM pg_stat_database WHERE datname = current_database();
```

- **누적 통계는 즉시 반영되지 않는다.** 15 이상은 유휴 백엔드가 최대 10초 뒤에 통계를 내보낸다.
  `measure.sh`는 AFTER 전에 `concurrency.pgStatsFlushWaitSec`(기본 11초)만큼 기다린다. 줄이지 마라. 데드락 증가분이 0으로 찍힌다.
- 측정 구간의 서버 로그 건수는 `=== WINDOW ===` 구간에 이미 증가분으로 남는다.
- 계산은 `harness conc-lock-delta --db postgresql $TARGET_DIR/lock-stats-{n}.txt`가 한다. 손으로 빼지 마라.

| conc-lock-delta 지표 | 출처 |
|---|---|
| `lock_waits` | 구간 로그의 `still waiting for` 건수 (`deadlock_timeout` 초과 대기만) |
| `deadlocks` | `pg_stat_database.deadlocks` 증가분 |
| `deadlocks_logged` | 구간 로그의 `deadlock detected` 건수 |
| `lock_timeouts` | 구간 로그의 `canceling statement due to lock timeout` 건수 |
| `xact_rollback` | 롤백 증가분. 직렬화 실패(40001), 데드락, 애플리케이션 예외 롤백이 모두 섞인다 |
| `waiting_samples` | 부하 중 `pg_locks WHERE NOT granted` 행 수 표본 |

`lock_waits`는 `deadlock_timeout`(기본 1초)보다 짧은 대기를 세지 않는다. MySQL의 `Innodb_row_lock_waits`와 같은 값이 아니다.
DB 간 비교에 쓰지 말고 같은 DB의 후보 간 비교에만 쓴다.

## 실시간 락 대기

부하 중에 다른 터미널에서 본다.

```bash
dbc -c "
SELECT a.pid, a.wait_event_type, a.wait_event, pg_blocking_pids(a.pid) AS blocked_by,
       now() - a.query_start AS waiting_for, left(a.query, 120) AS query
FROM pg_stat_activity a
WHERE a.datname = current_database() AND a.wait_event_type = 'Lock'
ORDER BY a.query_start LIMIT 20;"
```

- `wait_event`가 `transactionid`면 다른 트랜잭션이 고친 행을 기다리는 것(행 락), `tuple`이면 같은 행을 기다리는 대기열 뒤쪽이다.

## 최근 데드락 원문

PostgreSQL은 데드락 원문을 뷰로 남기지 않는다. 서버 로그에만 있다. `measure.sh`가 측정 구간 로그에서 잘라
`=== LATEST DEADLOCK ===` 구간에 남긴다. 수동으로 볼 때는:

```bash
docker logs --since {측정 시작 시각, 예: 2026-01-01T00:00:00Z} {perf.dbContainer} 2>&1 | grep -A 12 'deadlock detected' | tail -60
```

- `Process A waits for ShareLock on transaction X; blocked by process B.` 쌍이 순환을 보여준다.
  FK 검사가 부모 행에 `FOR KEY SHARE`를 거는 경우가 흔하다.

## 제약과 인덱스

```bash
dbc -c "\d+ {테이블}"
```

## 발행 문장 확인

Phase 6에서 제어가 실제로 발행되는지 본다. `pg_stat_statements`가 있으면 통계를 비우고 요청 한 건만 보낸 뒤 읽는다.

```bash
dbc -c "SELECT pg_stat_statements_reset();"

# (요청 한 건)

dbc -c "
SELECT calls, left(query, 300) AS query
FROM pg_stat_statements
WHERE dbid = (SELECT oid FROM pg_database WHERE datname = current_database())
  AND query NOT LIKE '%pg_stat_statements%'
ORDER BY calls DESC LIMIT 20;"
```

`pg_stat_statements`가 없으면(확장 미설치, `shared_preload_libraries` 미등록) 구간 로그로 본다.

```bash
dbc -c "ALTER SYSTEM SET log_statement = 'all';" -c "SELECT pg_reload_conf();"
# (요청 한 건)
docker logs --since 1m {perf.dbContainer} 2>&1 | grep 'statement:' | tail -20
dbc -c "ALTER SYSTEM RESET log_statement;" -c "SELECT pg_reload_conf();"
```

| 기법 | 문장에 나타나야 하는 것 |
|---|---|
| 원자적 조건부 UPDATE | `UPDATE ... SET col = col + $1 WHERE ... AND col < $2` 형태. 절대값 대입이 아니어야 한다 |
| 비관적 락 | 대상 조회에 `FOR UPDATE`(또는 `FOR NO KEY UPDATE`)가 붙어 있어야 한다 |
| 낙관적 락 | `UPDATE ... WHERE id = $1 AND version = $2` 형태 |
| 분산 락 (권고 락) | `pg_advisory_xact_lock($1)` 또는 `pg_try_advisory_lock` |

## 동시성 측정에서만 걸리는 것

- **기본 격리 수준이 MySQL과 다르다.** `READ COMMITTED`에서는 문장마다 새 스냅샷을 읽는다.
  같은 행을 동시에 고친 UPDATE는 앞 트랜잭션 커밋을 기다린 뒤 **최신 행으로 WHERE를 다시 평가한다.**
  그래서 `SET col = col + 1 WHERE col < cap`은 READ COMMITTED에서 안전하지만, 읽은 값을 애플리케이션에서 계산해 넣는 절대값 대입은 여전히 유실된다.
- **`REPEATABLE READ` 이상이면 같은 행 동시 갱신이 `could not serialize access`(SQLSTATE 40001)로 실패한다.**
  이 실패는 유실을 막지만 재시도 없이 그대로 응답되면 5xx로 보인다. 격리 수준을 바꾼 후보는 재시도 설계를 함께 잰다.
- **`SELECT ... FOR UPDATE`는 FK가 걸린 자식 INSERT와 충돌하지 않게 `FOR NO KEY UPDATE`를 고려한다.**
  `FOR UPDATE`는 자식 행의 FK 검사(`FOR KEY SHARE`)까지 막는다.
- **공유 버퍼는 재기동 없이 비울 수 없다.** 캐시 상태는 warm으로 통일한다.
- `xact_rollback` 증가분에는 측정과 무관한 롤백(헬스 체크, 커넥션 검증)도 섞일 수 있다. 원본(`-0`)과의 차이로만 읽는다.
