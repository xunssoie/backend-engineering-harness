# PostgreSQL 측정 스택

`perf.db` = `postgresql`일 때 phase 파일이 가리키는 절이다. MySQL의 관측 방법을 옮겨 쓰지 마라.

| 목적 | 수단 |
|---|---|
| 쿼리별 통계 | `pg_stat_statements` (`perf_collect_stats`) |
| 통계 리셋 | `SELECT pg_stat_statements_reset()` (`perf_reset_stats`) |
| 실행계획 (실측) | `EXPLAIN (ANALYZE, BUFFERS, VERBOSE)`, `BEGIN` / `ROLLBACK`으로 감싼다 |
| 읽은 페이지 | 계획의 `Buffers: shared hit / read`, 통계의 `shared_blks_hit` / `shared_blks_read` |
| 읽고 버린 행 | 계획의 `Rows Removed by Filter` |
| 플래너 통계 갱신, dead tuple 회수 | `VACUUM ANALYZE` (`perf_analyze`) |
| 컬럼 카디널리티 | `pg_stats.n_distinct` |

MySQL과 달라서 측정에 영향을 주는 것:

- 시간 칼럼(`total_exec_time`, `mean_exec_time`)은 이미 ms다. 이 칼럼명은 PostgreSQL 13 이상 기준이다. 12 이하는 `total_time`, `mean_time`이라 `perf_query_stats`의 쿼리가 실패한다.
- 쿼리 통계에 읽은 행 수가 없다. 대신 호출당 블록(`shared_blks_hit + read`)을 본다. 가공본의 `블록/호출` 칼럼이다.
- 쓰기의 되돌리기 `DELETE`는 dead tuple을 남긴다. `VACUUM`으로 회수하지 않으면 다음 측정의 시작 상태가 달라진다.
- `shared_buffers`와 OS 페이지 캐시는 재기동 없이 비우기 어렵다. 애플리케이션 캐시를 비워도 이 둘은 그대로다. 둘을 묶어 cold라고 적지 마라.

## 접속

`perf-env.sh`의 `psqlp`(= `dbq`)가 아래를 넣는다. 명령 블록에 옵션을 다시 적지 마라.

| 요소 | 이유 |
|---|---|
| `docker exec` (`perf.dbContainer`가 있을 때) | 호스트에 psql이 없어도 된다. 대신 파일은 표준입력으로 넘긴다. `-f`와 `\i`는 컨테이너 안 경로를 본다 |
| `-X` | 개인 `.psqlrc`가 출력 형식을 바꾸면 변환기가 못 읽는다 |
| `-v ON_ERROR_STOP=1` | 시드가 중간 오류를 넘기고 검증까지 가지 않게 한다 |
| `PGPASSWORD` | 명령줄 비밀번호 노출을 피한다 |

## 접속 점검

Phase 2의 0). 비ASCII 값이 있는 컬럼 하나를 읽는다. 깨지면 클라이언트 인코딩(`SHOW client_encoding;`)을 본다.

```bash
psqlp -c "SELECT {비ASCII 값이 있는 컬럼} FROM {테이블} LIMIT 1;"
```

## Phase 2 점검

Phase 2의 4~7).

```bash
# 4) pg_stat_statements가 살아 있는가
psqlp -c "SELECT count(*) FROM pg_stat_statements;"

# 5) 통계가 채워지는가. 대상 API를 한 번 호출한 뒤 0보다 커야 한다
psqlp -c "SELECT count(*) FROM pg_stat_statements WHERE dbid = (SELECT oid FROM pg_database WHERE datname = current_database());"

# 6) 원문 길이 상한과 추적 범위 (track_utility가 off면 BEGIN/COMMIT이 빠져 트랜잭션 제어 수를 셀 수 없다)
psqlp -c "SHOW track_activity_query_size;" -c "SHOW pg_stat_statements.track;" -c "SHOW pg_stat_statements.track_utility;"

# 7) shared_buffers. 데이터가 이보다 크면 OS 캐시나 디스크 읽기가 측정에 섞인다
psqlp -c "SHOW shared_buffers;"
```

조치:

- 4)가 `relation "pg_stat_statements" does not exist`로 실패 → DB 컨테이너 `command`(compose 파일)에 아래를 넣고 재기동한 뒤 확장을 만든다.

  ```yaml
  command: postgres -c shared_preload_libraries=pg_stat_statements -c pg_stat_statements.track=all
  ```

  ```bash
  psqlp -c "CREATE EXTENSION IF NOT EXISTS pg_stat_statements;"
  ```

- 5)가 0 → 대상 API를 한 번 호출한 뒤 다시 센다. `perf.dbName`이 앱이 붙는 DB와 같은지 본다.
- 6)의 `track_utility`가 off → `-c pg_stat_statements.track_utility=on`을 `command`에 넣는다. 원문 길이가 잘리면 `track_activity_query_size`를 올린다.

## 스키마 드리프트

Phase 2의 10). 마이그레이션이 선언한 인덱스 중 DB에 없는 것만 출력한다. 아무것도 출력되지 않아야 한다.
`{마이그레이션 디렉토리}`는 harness 설정 `migration.dir`이다. 스키마가 `public`이 아니면 바꾼다.

```bash
MIG={마이그레이션 디렉토리}
comm -23 \
  <(comm -23 \
      <(grep -rhoiE 'CREATE (UNIQUE )?INDEX (CONCURRENTLY )?(IF NOT EXISTS )?[a-z0-9_]+' "$MIG" | awk '{print $NF}' | sort -u) \
      <(grep -rhoiE 'DROP INDEX (CONCURRENTLY )?(IF EXISTS )?[a-z0-9_]+' "$MIG" | awk '{print $NF}' | sort -u)) \
  <(psqlp -Atc "SELECT indexname FROM pg_indexes WHERE schemaname = 'public'" | sort)
```

출력이 있으면 그 인덱스를 **만드는 버전**이 이력에 success로 남았는지 본다. 최근 몇 건만 보면 초기 버전이 걸리지 않는다.

```bash
psqlp -c "
SELECT version, description, success, installed_on
FROM flyway_schema_history WHERE version IN ({버전들}) ORDER BY installed_rank;"
```

success인데 인덱스가 없으면 마이그레이션 도구 밖에서 지워진 것이다. 복구 뒤 `ANALYZE {테이블}`.

## 행 수

**`pg_stat_user_tables.n_live_tup`을 행 수로 믿지 마라.** 통계 수집기가 증감으로 누적하는 추정치이고,
`VACUUM`이나 `ANALYZE`가 돈 적 없는 테이블에서는 통계 리셋 이후의 변화량만 담는다. 수십만 행 테이블이 수천이나 0으로 나오기도 한다.

```bash
psqlp -c "
SELECT '{테이블}' AS t, count(*) FROM {테이블}
UNION ALL SELECT '{테이블2}', count(*) FROM {테이블2};"
```

## 통계 갱신

`perf_analyze`는 인자가 없으면 `VACUUM ANALYZE;`, 테이블을 주면 그 테이블만 돈다. DB가 커서 오래 걸리면 대상 테이블로 좁힌다.

- **`VACUUM`을 빼지 마라.** 쓰기 엔드포인트는 측정마다 행을 넣고, 되돌리기 `DELETE`는 dead tuple을 남긴다.
  autovacuum은 `autovacuum_vacuum_threshold + autovacuum_vacuum_scale_factor × reltuples`(기본 `50 + 0.2 × reltuples`)를 넘어야 돌아서
  큰 테이블에서는 dead tuple이 수십만 개 쌓여도 돌지 않는다. dead tuple이 남은 상태와 회수된 상태는 같은 INSERT의 단가를 2배 이상 벌린다.
- 읽기 전용 엔드포인트라도 `ANALYZE`가 플래너 통계를 맞추므로 그대로 실행한다.

시작 상태 확인 (dead tuple이 0 근처이고 `last_vacuum`이 방금이어야 한다):

```bash
psqlp -c "
SELECT relname, n_dead_tup, last_vacuum, last_analyze
FROM pg_stat_user_tables WHERE relname IN ({대상 쿼리가 읽고 쓰는 테이블}) ORDER BY relname;"
```

## 쿼리 통계 수집

`perf_collect_stats`가 실행하는 쿼리다. 계산은 `perf-query-stats`가 하므로 원본 칼럼만 뽑는다.

```sql
SELECT calls, total_exec_time AS total_ms, rows, shared_blks_hit, shared_blks_read, query
FROM pg_stat_statements
WHERE dbid = (SELECT oid FROM pg_database WHERE datname = current_database())
  AND query NOT LIKE '%pg_stat_statements%'
ORDER BY total_exec_time DESC;
```

| 요소 | 이유 |
|---|---|
| `--csv` | 정렬 출력은 쿼리 원문을 잘라 리포지토리 메서드를 식별할 수 없게 한다. 여러 줄 원문도 따옴표로 보존된다 |
| `LIMIT` 없음 | 비중의 분모는 전체 쿼리다. 표에 싣는 상위 건수는 변환기가 정한다 |
| `dbid` 조건 | 같은 인스턴스의 다른 DB 쿼리가 섞이지 않는다 |

## 실행계획 캡처

`perf_capture_plan`이 `BEGIN; EXPLAIN (ANALYZE, BUFFERS, VERBOSE) {쿼리}; ROLLBACK;`을 버리는 1회 뒤에 한 번 더 실행해 덧붙인다.
`EXPLAIN ANALYZE`는 쿼리를 실제로 실행한다. `BEGIN`과 `ROLLBACK`을 빼지 마라. 시퀀스 증가와 트리거의 외부 호출은 롤백되지 않는다.

## 실행계획 읽기

표 위의 `쿼리 전체`는 `Execution Time`이다.

| 노드 | 스캔 방식 / 인덱스 | actual time | 추정 rows | 실측 rows | loops | Rows Removed by Filter | shared hit / read |
|---|---|---|---|---|---|---|---|

표 아래에 붙이는 칼럼 설명 (그 계획의 실제 수치로 예를 든다):

| 칼럼 | 설명 |
|---|---|
| actual time=A..B | A는 첫 행까지, B는 마지막 행까지(ms). **자식 노드의 시간을 포함**하고 loops당 값이다 |
| 추정 rows | 플래너가 실행 전에 `pg_stats`로 계산한 예상 행 수. 스캔과 조인 방식은 이 값으로 정해진다 |
| 실측 rows | 그 노드가 실제로 내보낸 행 수. **loops당 평균**이므로 총량은 loops를 곱한다. 추정과 10배 이상 벌어지면 계획이 잘못 골라졌을 수 있다 |
| loops | 노드가 실행된 횟수. 병렬 실행이면 리더 + 워커 수, Nested Loop 안쪽이면 바깥 행 수만큼 커진다 |
| Rows Removed by Filter | 읽었지만 조건에 맞지 않아 버린 행 수(loops당). **버려진 행이 곧 낭비된 작업량**이라 인덱스 필요성을 가장 직접 보여준다 |
| shared hit / read | 만진 버퍼(8KB 페이지) 수. `hit`은 `shared_buffers`에서 바로 꺼낸 것, `read`는 OS에 요청한 것(OS 캐시 또는 디스크). 자식 포함 누적치라 최상단 노드 값이 쿼리 전체다. × 8KB로 바이트를 함께 제시한다 |

`perf-delta`는 라벨마다 `Execution Time`, 최상단 `shared hit + read`, `Rows Removed` × loops의 합을 비교한다.

## 위험 신호

Phase 6의 공통 표에 더해 본다.

| 지표 | 위험 신호 |
|---|---|
| OLTP 경로의 `Seq Scan` | 1만 행 이상 테이블이면 신호. 그 전에 스키마 드리프트 점검을 통과했는지 본다 |
| `Rows Removed by Filter` × loops가 반환 행보다 훨씬 큼 | 읽고 버리는 행이 많음 |
| `shared read`가 `hit`보다 큼 (warm 측정에서) | 데이터가 `shared_buffers`보다 크다 |

## 락 현황

```bash
psqlp -c "
SELECT pid, wait_event_type, wait_event, state, left(query, 80) AS query
FROM pg_stat_activity WHERE wait_event_type = 'Lock';"
psqlp -c "SELECT locktype, relation::regclass, mode, granted, pid FROM pg_locks WHERE NOT granted;"
```

## 설계 재료

파일로 남기지 않는다.

```bash
psqlp -c "\d {테이블}"
psqlp -c "SELECT attname, n_distinct, null_frac FROM pg_stats WHERE tablename = '{테이블}';"
```

`n_distinct`가 음수면 행 수 대비 비율이다(-1은 전부 다름). 실제 `count(DISTINCT)`와 크게 어긋나면 `ANALYZE`가 필요한 상태다.

## 설계 제약

- 부분 인덱스는 쿼리의 `WHERE`가 인덱스 조건을 함의할 때만 쓰인다. 파라미터로 바뀌는 조건에는 걸리지 않는다.
- 인덱스 컬럼에 함수를 씌우면 그 식으로 만든 표현식 인덱스만 쓰인다. 타입이 다른 비교도 인덱스를 막는다.
- `ORDER BY`가 인덱스 순서와 어긋나면 Sort 노드가 붙는다. 등호 조건 뒤에 정렬 컬럼을 둔다.
- 운영 테이블의 인덱스 추가는 `CREATE INDEX CONCURRENTLY`를 검토한다. 트랜잭션 안에서 돌릴 수 없으니 마이그레이션 도구 설정과 함께 본다.

## 적용 확인

Phase 7의 3. 새 인덱스가 계획에 잡히는지 본다. 쓰기 쿼리면 `BEGIN` / `ROLLBACK`으로 감싼다.

```bash
psqlp -c "EXPLAIN {대상 쿼리};" | grep -E 'Index|Scan|Sort'
```

## 시드

- 모듈은 psql 변수(`\set 이름 값`, 본문에서 `:이름`)만 읽는다. 변수가 없으면 `:이름`이 그대로 넘어가 구문 오류로 죽는다. 오류 메시지에 변수명이 찍힌다.
- 행 생성은 `generate_series(:start, :start + :count - 1)`로 한다. 재실행해도 중복이 쌓이지 않게 `ON CONFLICT DO NOTHING`을 쓴다.
- id를 명시 삽입했으면 모듈 끝에서 시퀀스를 맞춘다. 안 맞추면 앱의 다음 INSERT가 PK 충돌로 죽는다.

  ```sql
  SELECT setval(pg_get_serial_sequence('{테이블}', 'id'), (SELECT COALESCE(max(id), 1) FROM {테이블}));
  ```

- 카디널리티는 `(i % :변수)`로 통제한다.
- 실행은 `cat "$PERF_DIR/seeds.sql" "$SEEDS/{모듈}.sql" | dbq`. 컨테이너 접속이면 `\i`가 호스트 경로를 못 보므로 `cat`으로 잇는다. `cat` 순서가 FK 의존 순서다.
- 모듈을 만들거나 고치기 전에 NOT NULL, 기본값 없는 컬럼을 뽑아 INSERT 컬럼 목록과 맞춘다.

  ```bash
  psqlp -c "
  SELECT table_name, string_agg(column_name, ', ' ORDER BY ordinal_position) AS required_columns
  FROM information_schema.columns
  WHERE table_schema = 'public' AND table_name IN ({테이블들})
    AND is_nullable = 'NO' AND column_default IS NULL
  GROUP BY table_name;"
  ```

- 대량 적재(수십만 행 이상)는 모듈마다 한 트랜잭션(`BEGIN` ... `COMMIT`)으로 묶고, 끝에 `ANALYZE {테이블}`을 둔다.
  적재 전후로 인덱스가 사라진 사례가 있으니 적재 뒤 Phase 2의 스키마 드리프트 점검을 다시 돌린다.
