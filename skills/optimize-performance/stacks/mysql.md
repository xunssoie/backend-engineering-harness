# MySQL 측정 스택

`perf.db` = `mysql`일 때 phase 파일이 가리키는 절이다. MySQL 8.0 / InnoDB 기준. PostgreSQL의 관측 방법을 옮겨 쓰지 마라.

| 목적 | 수단 |
|---|---|
| 쿼리별 통계 | `performance_schema.events_statements_summary_by_digest` (`perf_collect_stats`) |
| 통계 리셋 | `TRUNCATE TABLE performance_schema.events_statements_summary_by_digest` (`perf_reset_stats`) |
| 실행계획 (실측) | `EXPLAIN ANALYZE` (SELECT만) |
| 실행계획 (추정) | `EXPLAIN FORMAT=JSON` |
| 접근 방식별 실제 작업량 | `FLUSH STATUS` → 쿼리 → `SHOW SESSION STATUS` (`Handler_%`, `Sort_%`) |
| 옵티마이저 통계 갱신 | `ANALYZE TABLE` (`perf_analyze`) |
| 인덱스 카디널리티 | `SHOW INDEX FROM` |

PostgreSQL과 달라서 측정에 영향을 주는 것:

- `TIMER_WAIT` 계열은 **피코초**다. ms는 `/1e9`. 원본은 피코초 그대로 수집하고 `perf-query-stats`가 바꾼다.
- `EXPLAIN ANALYZE`에 `BUFFERS`가 없다. 읽은 페이지 대신 `Handler_%` 카운터로 **읽은 행 수**를 본다.
- `VACUUM`이 없다. 갱신할 것은 옵티마이저 통계(`ANALYZE TABLE`)뿐이다.
- InnoDB 버퍼 풀은 재기동 없이 비울 수 없다. 측정은 **warm으로 통일**하고 매번 같은 워밍업으로 상태를 맞춘다.

## 접속

`perf-env.sh`의 `mysqlp`(= `dbq`)가 아래를 모두 넣는다. 명령 블록에 옵션을 다시 적지 마라.

| 요소 | 이유 |
|---|---|
| `docker exec` (`perf.dbContainer`가 있을 때) | 호스트에 mysql 클라이언트가 없어도 된다. 대신 파일은 표준입력으로 넘긴다. `SOURCE`는 컨테이너 안에서 돌아 호스트 경로를 못 본다 |
| 함수 | zsh는 따옴표 없는 변수 확장에 단어 분리를 하지 않아 `"$MYSQL -e"` 형식이 깨진다 |
| `--default-character-set` | 없으면 latin1로 붙어 쿼리 안의 한글 리터럴이 `?`가 되고 결과가 조용히 틀린다 |
| `--init-command="SET NAMES ... COLLATE ..."` | charset만 맞추면 collation이 서버 기본(`utf8mb4_0900_ai_ci`)으로 남아 다른 collation의 컬럼과 사용자 변수 비교가 ERROR 1267로 죽는다. 값은 `perf.mysqlCollation`. 컬럼 collation과 맞춘다 |
| `MYSQL_PWD` | 명령줄 비밀번호 경고와 셸 기록 노출을 피한다 |

## 접속 점검

Phase 2의 0). 비ASCII 값이 있는 컬럼 하나를 읽는다. `?`로 보이면 `perf-env.sh`가 source되지 않았거나 charset 설정이 다르다.

```bash
mysqlp -e "SELECT {비ASCII 값이 있는 컬럼} FROM {테이블} LIMIT 1;"
```

## Phase 2 점검

Phase 2의 4~7).

```bash
# 4) performance_schema와 digest 소비자 (셋 다 ON / YES)
mysqlp -e "
SELECT @@performance_schema AS ps;
SELECT NAME, ENABLED FROM performance_schema.setup_consumers
WHERE NAME IN ('statements_digest', 'events_statements_current');"

# 5) digest 테이블이 채워지는가 (0보다 커야 한다)
mysqlp -e "
SELECT count(*) FROM performance_schema.events_statements_summary_by_digest
WHERE SCHEMA_NAME = '$PERF_DB_NAME';"

# 6) digest 원문이 잘리는가. 가장 긴 digest가 max_digest_length에 닿으면 잘리는 것이다
mysqlp -e "
SELECT @@performance_schema_max_digest_length AS max_len;
SELECT LENGTH(DIGEST_TEXT) AS len, LEFT(DIGEST_TEXT, 80) AS head
FROM performance_schema.events_statements_summary_by_digest
WHERE SCHEMA_NAME = '$PERF_DB_NAME' ORDER BY len DESC LIMIT 3;"

# 7) 버퍼 풀 크기. 데이터가 이보다 크면 디스크 I/O가 측정에 섞인다
mysqlp -e "SELECT @@innodb_buffer_pool_size / 1024 / 1024 AS buffer_pool_mib;"
```

조치:

- 4)의 `ps`가 0 → DB 컨테이너의 `command`(compose 파일)에 `--performance-schema=ON`을 추가하고 재기동한다.
- 소비자가 `NO` → 재기동 없이 켠다.

  ```bash
  mysqlp -e "
  UPDATE performance_schema.setup_consumers SET ENABLED = 'YES'
  WHERE NAME IN ('statements_digest', 'events_statements_current');"
  ```

- 5)가 0 → 대상 API를 한 번 호출하게 한 뒤 다시 센다. 그래도 0이면 4)로 돌아간다. `perf.dbName`이 실제 스키마 이름과 같은지도 본다.
- 6)에서 잘림 → `--performance-schema-max-digest-length=4096`과 `--performance-schema-max-sql-text-length=4096`을 `command`에 넣고 재기동한다.
  MySQL 8.0.28+는 digest에서 `IN` 목록을 `IN (...)`로 접으므로 batch size의 자리표시자 1000개도 100자 미만이다.
  실측 길이가 닿지 않으면 올리지 마라.

## 스키마 드리프트

Phase 2의 10). 마이그레이션이 선언한 인덱스 이름 중 DB에 없는 것만 출력한다. 아무것도 출력되지 않아야 한다.
`{마이그레이션 디렉토리}`는 harness 설정 `migration.dir`이다. 이름 추출은 정규식이라 `CREATE INDEX`, `ADD INDEX`, `ADD UNIQUE`, `ADD KEY` 형태만 잡는다.

```bash
MIG={마이그레이션 디렉토리}
comm -23 \
  <(comm -23 \
      <(grep -rhoiE '(CREATE (UNIQUE |FULLTEXT )?INDEX|ADD (UNIQUE |FULLTEXT )?(INDEX|KEY)) `?[A-Za-z0-9_]+' "$MIG" \
          | awk '{print $NF}' | tr -d '`' | sort -u) \
      <(grep -rhoiE 'DROP (INDEX|KEY) `?[A-Za-z0-9_]+' "$MIG" | awk '{print $NF}' | tr -d '`' | sort -u)) \
  <(mysqlp -N -B -e "SELECT DISTINCT INDEX_NAME FROM information_schema.STATISTICS WHERE TABLE_SCHEMA = '$PERF_DB_NAME';" | sort -u)
```

출력이 있으면 `flyway_schema_history`(또는 프로젝트의 마이그레이션 이력 테이블)에서 **그 인덱스를 만드는 버전**이 success인지 본다.
최근 몇 건만 보면 초기 버전이 걸리지 않는다. success인데 인덱스가 없으면 마이그레이션 도구 밖에서 지워진 것이다.

```bash
mysqlp -e "SELECT version, description, success, installed_on FROM flyway_schema_history WHERE version IN ({버전들}) ORDER BY installed_rank;"
```

## 행 수

`information_schema.TABLES.TABLE_ROWS`는 샘플링 추정치라 배 단위로 어긋난다. 실제 카운트를 쓴다.

```bash
mysqlp -e "
SELECT '{테이블}' AS t, count(*) AS n FROM {테이블}
UNION ALL SELECT '{테이블2}', count(*) FROM {테이블2};"
```

## 통계 갱신

`perf_analyze {테이블...}`이 `ANALYZE TABLE`을 실행한다. 테이블을 반드시 지정한다.
측정 직전의 시작 상태(행 수, 쓰기 대상의 집계 컬럼)는 같은 자리에서 확인한다. 되돌리기가 제대로 됐는지의 근거다.

```bash
mysqlp -e "SELECT '{쓰기 테이블}' AS t, count(*) AS n FROM {쓰기 테이블};"
```

## 쿼리 통계 수집

`perf_collect_stats`가 실행하는 쿼리다. 계산은 `perf-query-stats`가 하므로 원본 칼럼만 뽑는다.

```sql
SELECT COUNT_STAR AS calls, SUM_TIMER_WAIT AS total_ps, SUM_ROWS_SENT AS rows_sent,
       SUM_ROWS_EXAMINED AS rows_examined, DIGEST_TEXT AS query
FROM performance_schema.events_statements_summary_by_digest
WHERE SCHEMA_NAME = '{perf.dbName}'
  AND DIGEST_TEXT NOT LIKE '%performance_schema%'
  AND DIGEST_TEXT NOT LIKE 'SET NAMES%'
ORDER BY SUM_TIMER_WAIT DESC;
```

| 요소 | 이유 |
|---|---|
| `-B` (탭 구분, 이스케이프) | 기본 박스 출력은 `DIGEST_TEXT`를 잘라 출처를 매핑할 수 없게 한다. 변환기가 이스케이프를 푼다 |
| `LIMIT` 없음 | 비중(`pct`)의 분모는 전체 digest다. 표에 싣는 상위 건수는 변환기가 정한다 |
| `rows_examined` | 읽은 행 대 돌려준 행. 하드웨어 독립 지표이고 인덱스 필요성을 가장 직접 보여준다 |
| `SET NAMES` 제외 | `mysqlp`의 init-command가 남기는 행이다. 측정 대상이 아니다 |

## 실행계획 캡처

`perf_capture_plan`이 한 파일에 `EXPLAIN ANALYZE`, `EXPLAIN FORMAT=JSON`, `STATUS COUNTERS` 세 블록을 이어 붙인다.

| 요소 | 이유 |
|---|---|
| `FLUSH STATUS`와 쿼리를 한 `-e` 안에 | 세션 카운터다. 명령을 나누면 세션이 갈려 0이 나온다 |
| `Sort_%` 포함 | `Sort_rows`, `Sort_scan`이 filesort 판정 근거다 |
| `Value > 0`만 | 0인 카운터는 빠진다. `perf-delta`는 카운터 블록이 있으면 빠진 항목을 0으로 읽는다 |
| `EXPLAIN ANALYZE`는 SELECT만 | MySQL 8.0은 쓰기 문을 받지 않는다. 쓰기 쿼리면 `EXPLAIN FORMAT=JSON`만 뜨고, 작업량은 `BEGIN; {쿼리}; ROLLBACK;` 안에서 잰다. AUTO_INCREMENT 증가는 롤백되지 않는다 |

## 실행계획 읽기

노드별 표와 카운터 표로 정리한다. 표 위의 `쿼리 전체`는 루트 노드 `actual time`의 뒤 값 × loops다.

| 노드 | 접근 방식 / 인덱스 | actual time | loops | 소요 ms | 추정 rows | 실측 rows | 비고 |
|---|---|---|---|---|---|---|---|

| 카운터 | 값 | 뜻 |
|---|---|---|

표 아래에 붙이는 칼럼 설명 (그 계획의 실제 수치로 예를 든다):

| 칼럼 | 설명 |
|---|---|
| actual time=A..B | A는 첫 행까지, B는 마지막 행까지(ms). **loops당 평균**이고 자식 노드 시간을 포함한다 |
| 소요 ms | `B × loops`. 그 노드가 자식까지 포함해 실제로 쓴 시간. 부모의 소요를 넘지 않는다. 루트의 소요가 쿼리 전체다 |
| 추정 rows | 옵티마이저가 실행 전에 인덱스 통계로 계산한 예상 행 수. 접근 방식과 조인 순서는 이 값으로 정해진다 |
| 실측 rows | 그 노드가 실제로 내보낸 행 수. loops당 평균이므로 총량은 loops를 곱한다. 추정과 10배 이상 벌어지면 통계가 낡았거나 계획이 잘못 골라진 것이다 |
| loops | 노드가 실행된 횟수. Nested Loop 안쪽이면 바깥 행 수만큼 커진다 |
| `Handler_read_rnd_next` | 테이블을 순차로 읽은 횟수. **풀스캔의 직접 증거.** 반환 행보다 훨씬 크면 그만큼 읽고 버린 것이다 |
| `Handler_read_key` | 인덱스로 행을 찾은 횟수. 크고 `rnd_next`가 작으면 인덱스가 일하고 있다 |
| `Handler_read_next` | 인덱스 순서로 다음 행을 읽은 횟수. 범위 스캔의 폭 |
| `Handler_read_rnd` | 인덱스로 찾은 위치에서 행을 다시 읽은 횟수. 커버링이 안 될 때 늘어난다 |
| `Sort_scan`, `Sort_rows` | filesort 발생 여부와 정렬한 행 수. 인덱스 순서로 정렬이 풀리면 0 |

PostgreSQL의 `BUFFERS`에 해당하는 항목은 없다. 읽은 페이지 대신 읽은 행으로 본다. 없는 지표를 있는 것처럼 적지 마라.
`쿼리 전체`는 결과 전송 시간을 뺀 서버 실행 시간이라 쿼리 통계의 `mean_ms`보다 작은 것이 정상이다. 둘의 차가 크면 전송량(응답 크기)을 본다.

## 위험 신호

Phase 6의 공통 표에 더해 본다.

| 지표 | 위험 신호 |
|---|---|
| OLTP 경로의 `Handler_read_rnd_next` 급증 | 1만 행 이상 테이블이면 풀스캔 신호 |
| `Sort_rows`가 반환 행 수보다 큼 | 정렬을 인덱스로 못 풀고 있음 |

## 락 현황

```bash
mysqlp -e "SELECT * FROM performance_schema.data_lock_waits\G SHOW ENGINE INNODB STATUS\G" \
  | grep -A20 'LATEST DETECTED DEADLOCK\|TRANSACTIONS'
```

## 설계 재료

파일로 남기지 않는다.

```bash
mysqlp -e "SHOW CREATE TABLE {테이블}\G"
mysqlp -e "SHOW INDEX FROM {테이블};"
mysqlp -e "
SELECT count(*) AS total, count(DISTINCT {컬럼}) AS distinct_vals, max(c) AS max_group_size
FROM (SELECT {컬럼}, count(*) AS c FROM {테이블} GROUP BY {컬럼}) g;"
```

`SHOW INDEX`의 `Cardinality`는 샘플링 추정치다. `count(DISTINCT)`와 크게 어긋나면 `ANALYZE TABLE`이 필요한 상태다.

## 설계 제약

- InnoDB 보조 인덱스는 PK를 항상 포함한다. 커버링 인덱스에 PK를 다시 넣지 않는다.
- `ORDER BY`가 인덱스 순서와 어긋나면 `filesort`가 붙는다. 등호 조건 뒤에 정렬 컬럼을 둔다.
- `FULLTEXT`는 `MATCH ... AGAINST`에서만 쓰인다. `LIKE`로 바꾸면 인덱스를 못 쓴다.
- 인덱스 컬럼에 함수를 씌우면 인덱스를 못 쓴다. 타입이 다른 비교(문자열 컬럼에 숫자)도 같다.

## 적용 확인

Phase 7의 3. 새 인덱스가 `key`에 잡히는지 본다.

```bash
mysqlp -e "EXPLAIN {대상 쿼리}\G" | grep -E 'key|type|rows|Extra'
```

## 시드

- 모듈은 MySQL 사용자 변수(`@변수`)만 읽는다. 정의하지 않은 변수는 `NULL`이 되고 `NOT NULL` 컬럼에서 에러로 죽는다. 조용히 0건이 되지는 않는다.
- 행 생성은 재귀 CTE로 한다. `SET SESSION cte_max_recursion_depth = 10000000;`을 `seeds.sql` 변수 블록 맨 위에 둔다. 기본 상한 1000에서 끊긴다.
- 재실행해도 중복이 쌓이지 않게 `INSERT IGNORE`와 UNIQUE 제약, 또는 id 범위 명시 삽입을 쓴다.
- 카디널리티는 `ELT(1 + (n % @변수), ...)`로 통제한다. `ELT` 목록 길이가 상한이다. 넘기면 `NULL`이 들어가 죽는다.
- 실행은 `cat "$PERF_DIR/seeds.sql" "$SEEDS/{모듈}.sql" | dbq`. `cat` 순서가 FK 의존 순서다.
- 모듈을 만들거나 고치기 전에 NOT NULL, 기본값 없는 컬럼을 뽑아 INSERT 컬럼 목록과 맞춘다.

  ```bash
  mysqlp -e "
  SELECT TABLE_NAME, GROUP_CONCAT(COLUMN_NAME ORDER BY ORDINAL_POSITION) AS required_columns
  FROM information_schema.COLUMNS
  WHERE TABLE_SCHEMA = '$PERF_DB_NAME' AND TABLE_NAME IN ({테이블들})
    AND IS_NULLABLE = 'NO' AND COLUMN_DEFAULT IS NULL AND EXTRA NOT LIKE '%auto_increment%'
  GROUP BY TABLE_NAME;"
  ```

- 대량 적재(수십만 행 이상):
  - 적재 세션에서 `SET SESSION unique_checks = 0; SET SESSION foreign_key_checks = 0;`를 변수 블록에 넣으면 빨라진다.
    전역 설정(`innodb_flush_log_at_trx_commit`)은 건드리지 마라. 되돌리지 않으면 측정이 운영과 다른 내구성에서 돈다.
  - FULLTEXT 인덱스는 행마다 증분 유지돼 적재를 몇 배 느리게 한다. 떼고 넣고 다시 만든다. 인덱스가 없는 동안 `MATCH ... AGAINST`는 에러이므로 재생성 전에 대상 API를 호출하지 마라.
  - 대량 INSERT 직후 FULLTEXT에서 일부 행이 색인되지 않은 채 남은 사례가 있다. 검증 쿼리에서 색인 행 수와 적재 행 수를 비교하고, 어긋나면 인덱스를 다시 만든다.
