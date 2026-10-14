# MySQL (InnoDB) 측정 명령

phase 파일이 절 이름으로 이 파일을 가리킨다. 명령은 모두 `dbc` 함수를 쓴다.

## 접속

`dbc`는 `.claude/harness.json`의 `perf` 절(`dbContainer`, `dbName`, `dbUser`, `dbPassword`)로 만든 함수다.
직접 쓰지 말고 CLI가 만든 정의를 블록 첫머리에 넣는다.

```bash
eval "$(.claude/scripts/harness conc-shell --issue {이슈번호} --slug {슬러그})"
```

`CONC_DIR`, `TARGET_DIR`과 함께 아래 형태의 함수가 정의된다.

```bash
dbc() { docker exec -i -e MYSQL_PWD=... {perf.dbContainer} mysql -u{perf.dbUser} --default-character-set=utf8mb4 '--init-command=SET NAMES utf8mb4 [COLLATE {concurrency.mysqlCollation}]' {perf.dbName} "$@"; }
```

**문자열 변수로 만들지 마라.** `DB="mysql -h 127.0.0.1 -P 3307 -u root app_db"` 형태는 두 가지 이유로 실패한다.

- 호스트에 `mysql` 클라이언트가 없는 경우가 많다. 컨테이너 안의 클라이언트를 거쳐야 한다.
- zsh는 파라미터 확장 결과를 단어 분할하지 않아 `$DB -e "..."`가 통째로 명령어 하나로 해석되어 `command not found`가 난다.

함수는 두 문제를 한 번에 피한다. `docker exec`의 `-i`는 `dbc < 파일.sql` 형태를 위해 필요하다.

charset과 collation 옵션도 빼지 마라. 컨테이너 안의 클라이언트는 로케일이 없어 `latin1`로 붙는다.

- `--default-character-set=utf8mb4`가 없으면 쿼리 안의 한글 리터럴이 `?`가 되어 결과가 조용히 틀린다.
- 연결 collation이 칼럼 collation과 다르면 칼럼과 사용자 변수(또는 리터럴)의 비교가 `ERROR 1267 (Illegal mix of collations)`로 죽는다.
  칼럼 collation을 확인해 서버 기본과 다르면 `concurrency.mysqlCollation`에 적는다.

```bash
dbc -e "
SELECT @@collation_server AS server_default;
SELECT DISTINCT COLLATION_NAME FROM information_schema.COLUMNS
WHERE TABLE_SCHEMA = DATABASE() AND COLLATION_NAME IS NOT NULL;"
```

## 격리 수준과 타임아웃

```bash
dbc -e "
SELECT @@global.transaction_isolation  AS global_iso,
       @@session.transaction_isolation AS session_iso,
       @@innodb_lock_wait_timeout      AS lock_wait_timeout_sec,
       @@innodb_deadlock_detect        AS deadlock_detect,
       @@max_connections               AS max_connections,
       @@autocommit                    AS autocommit;"
```

- 기본값은 `REPEATABLE-READ`, 락 대기 타임아웃 50초다.
- **락 대기 타임아웃이 부하 지속시간보다 길면** 비관적 락 후보에서 요청이 타임아웃 없이 끝까지 밀린다.
  그 상태로도 측정은 되지만, 실패가 관측되지 않는 이유가 되므로 값을 기록해둔다.
- `max_connections`가 커넥션 풀 크기보다 작으면 풀이 차지 않는다. 올릴 때는 `SET PERSIST max_connections = {값};`,
  되돌릴 때는 `RESET PERSIST max_connections;`. 바꿨으면 record.md의 측정 환경에 남긴다.

## 락 관측 수단 점검

```bash
# 행 락 누적 카운터 (항상 사용 가능)
dbc -e "SHOW GLOBAL STATUS LIKE 'Innodb_row_lock%';"

# 데드락, 락 타임아웃 누적 카운터
dbc -e "
SELECT NAME, COUNT, STATUS
FROM information_schema.INNODB_METRICS
WHERE NAME IN ('lock_deadlocks','lock_timeouts','lock_row_lock_waits');"

# 실시간 락 대기 관측 테이블 (8.0. 부하 중에만 행이 보인다)
dbc -e "SELECT COUNT(*) AS data_locks_readable FROM performance_schema.data_locks;"
```

- `INNODB_METRICS`의 `STATUS`가 `disabled`인 지표가 있으면 켠다. 꺼진 지표는 COUNT가 0으로 남아 "데드락 없음"으로 오판된다.

  ```bash
  dbc -e "SET GLOBAL innodb_monitor_enable='lock_deadlocks'; SET GLOBAL innodb_monitor_enable='lock_timeouts';"
  ```

- `data_locks` 조회가 권한 오류로 실패하면 그 사실을 기록하고 `concurrency.sampleLockWaits`를 false로 둔다.
  누적 카운터만으로도 후보 비교는 성립한다.

## 측정 구간 락 카운터

`measure.sh`가 부하 직전(`=== BEFORE ===`)과 직후(`=== AFTER ===`)에 아래를 `-B -N`(탭 구분, 머리 행 없음)으로 기록한다.

```sql
SHOW GLOBAL STATUS LIKE 'Innodb_row_lock%';
SELECT NAME, COUNT FROM information_schema.INNODB_METRICS
WHERE NAME IN ('lock_deadlocks','lock_timeouts','lock_row_lock_waits');
```

- **둘 다 인스턴스 전역 누적값이다.** 측정 구간의 증가분은 BEFORE와 AFTER의 차이로 낸다. 리셋 명령에 의존하지 마라.
- `Innodb_row_lock_time_avg`는 기동 이후 누적 평균이라 **뺄셈으로 구간 값을 낼 수 없다.**
  구간 평균은 `Innodb_row_lock_time` 증가분 / `Innodb_row_lock_waits` 증가분이다.
  `Innodb_row_lock_time_max`도 기동 이후 최댓값이라 구간 값이 아니다.
- 계산은 `harness conc-lock-delta --db mysql $TARGET_DIR/lock-stats-{n}.txt`가 한다. 손으로 빼지 마라.

| conc-lock-delta 지표 | 출처 |
|---|---|
| `lock_waits` | `Innodb_row_lock_waits` 증가분 |
| `row_lock_time_ms` | `Innodb_row_lock_time` 증가분 |
| `row_lock_time_avg_ms` | 위 둘의 비 (구간 평균) |
| `deadlocks` | `lock_deadlocks` 증가분 |
| `lock_timeouts` | `lock_timeouts` 증가분 |
| `waiting_samples` | 부하 중 `performance_schema.data_lock_waits` 행 수 표본 |

## 실시간 락 대기

부하 중에 다른 터미널에서 본다. `measure.sh`는 행 수만 표본으로 남긴다.

```bash
dbc -e "
SELECT w.REQUESTING_ENGINE_TRANSACTION_ID AS waiting_trx,
       w.BLOCKING_ENGINE_TRANSACTION_ID   AS blocking_trx,
       l.OBJECT_NAME, l.INDEX_NAME, l.LOCK_TYPE, l.LOCK_MODE, l.LOCK_DATA
FROM performance_schema.data_lock_waits w
JOIN performance_schema.data_locks l
  ON l.ENGINE_LOCK_ID = w.REQUESTING_ENGINE_LOCK_ID
LIMIT 20;"
```

## 최근 데드락 원문

```bash
dbc -e "SHOW ENGINE INNODB STATUS\G" | sed -n '/LATEST DETECTED DEADLOCK/,/^TRANSACTIONS$/p'
```

- 끝 표지를 `/^---/`로 잡지 마라. 머리 바로 밑의 밑줄에서 범위가 끝나 본문이 비어 보인다.
- 이 절은 **인스턴스 기동 이후 마지막 데드락 하나**다. 이번 구간의 것인지는 `deadlocks` 증가분으로 가른다.
  증가분이 0인데 원문이 있으면 앞선 측정의 것이다.
- 원문의 `lock mode S` → `lock_mode X ... waiting` 쌍은 공유 락을 쥔 채 배타 락으로 승격하려다 서로 막힌 것이다.
  FK 검사가 부모 행에 S 락을 거는 경우가 흔하다.

## 제약과 인덱스

```bash
dbc -e "SHOW CREATE TABLE {테이블}\G"
```

## 발행 문장 확인

Phase 6에서 제어가 실제로 발행되는지 본다. 통계를 비우고 요청 한 건만 보낸 뒤 digest를 읽는다.

```bash
dbc -e "TRUNCATE TABLE performance_schema.events_statements_summary_by_digest;"

# (요청 한 건)

dbc -e "
SELECT COUNT_STAR AS calls, DIGEST_TEXT
FROM performance_schema.events_statements_summary_by_digest
WHERE SCHEMA_NAME = DATABASE()
  AND DIGEST_TEXT NOT LIKE '%performance_schema%'
ORDER BY LAST_SEEN DESC LIMIT 20\G"
```

| 기법 | 문장에 나타나야 하는 것 |
|---|---|
| 원자적 조건부 UPDATE | `UPDATE ... SET col = col + ? WHERE ... AND col < ?` 형태. 절대값 대입이 아니어야 한다 |
| 비관적 락 | 대상 조회에 `FOR UPDATE`가 붙어 있어야 한다 |
| 낙관적 락 | `UPDATE ... WHERE id = ? AND version = ?` 형태 |
| 분산 락 (`GET_LOCK`) | `SELECT GET_LOCK(?, ?)`와 `RELEASE_LOCK` |

## 동시성 측정에서만 걸리는 것

- **격리 수준이 결과를 바꾼다.** `REPEATABLE READ`에서 일반 `SELECT`는 트랜잭션 첫 읽기 시점의 스냅샷을 읽는다(non-locking consistent read).
  갱신 직전 확인이 필요하면 `FOR UPDATE`나 `FOR SHARE`로 현재 값을 읽어야 한다.
  `FOR UPDATE`의 갭 락 범위도 격리 수준에 따라 달라진다. 측정 시작 전에 값을 확인해 기록한다.
- **락은 쓰기 순서를 정해줄 뿐, 읽은 값이 낡았다는 사실은 고쳐주지 못한다.** 절대값 대입 UPDATE는 X 락을 차례로 받아도
  각자 손에 든 낡은 값을 쓴다. `lock_waits`가 높은데 카운터 오차가 크면 이 양상이다.
- **InnoDB 버퍼 풀은 재기동 없이 비울 수 없다.** 캐시 상태는 warm으로 통일한다.
- 값이 바뀌지 않은 칼럼의 세컨더리 인덱스 갱신은 건너뛰지만, ORM이 전 칼럼을 싣는 UPDATE는 row image가 커서 락 보유 시간이 길어진다.
