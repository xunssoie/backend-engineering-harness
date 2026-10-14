# 시드 모듈 (동시성 측정용)

동시성 측정에 필요한 것은 **많은 데이터가 아니라 하나의 뜨거운 자원과 그것을 다툴 사용자들**이다.
규모를 키우는 것이 목적인 optimize-performance의 시드와 목적이 다르다.

> **optimize-performance의 시드 모듈을 가져다 쓰지 마라.** 규모 중심이라 경합 조건(사용자 수 = VU,
> 자원 하나)을 보장하지 않고, 스키마가 바뀐 뒤 고쳐지지 않았을 수 있다.

## 모듈은 프로젝트가 가진다

시드 모듈은 도메인 스키마에 묶여 있어 이 스킬이 제공하지 않는다.
Phase 3에서 `.claude/resources/concurrency/seeds/`에 모듈이 없으면 이 문서의 규칙대로 만든다.
한 번 만든 모듈은 이후 이슈가 함께 쓴다. 이슈마다 복사하지 마라.

```
.claude/resources/concurrency/
├── seeds/                      # 프로젝트 공용 시드 모듈 (이 규칙으로 만든다)
│   ├── users.sql               # 예: 경합에 참여할 사용자
│   └── contention-resource.sql # 예: 다툼 대상 자원 하나
└── {이슈번호}/
    └── seeds.sql               # 변수 블록만
```

모듈을 만들 때 확인할 것:

| 확인 | 근거 |
|---|---|
| 테이블과 칼럼 | 마이그레이션 디렉토리(`migration.dir`)의 최신 스키마. 엔티티만 보고 쓰지 마라 |
| NOT NULL, 기본값, 열거형 값 | 애플리케이션이 열거형을 이름으로 파싱하면 없는 값이 저장은 되고 조회 시점에 500이 난다 |
| 대상 검증 외의 검증 | 요청이 경합 지점에 도달하려면 다른 검증(중복, 한도, 상태)을 전부 통과해야 한다 |

## 쓰는 법

`.claude/resources/concurrency/{이슈번호}/seeds.sql`은 **변수 블록만** 담는다.
**모듈 본문을 복사하지 마라.** 모듈은 실행 시점에 이어 붙인다.

MySQL은 사용자 변수, PostgreSQL은 임시 테이블 한 행으로 변수를 넘긴다.

```sql
-- 예시 (MySQL): CONC-{이슈번호} 시드
-- 대상: {측정할 임계 구역}

-- 재귀 CTE 기본 깊이 상한은 1000이다. VU가 1000을 넘으면 올린다.
SET SESSION cte_max_recursion_depth = 100000;

-- 사용자: VU 수와 반드시 같아야 한다.
SET @user_start  = 900001;
SET @user_count  = 500;

-- 경합 대상 자원: 딱 하나만 만든다.
SET @target_id       = 990001;
SET @target_capacity = 100;
```

```sql
-- 예시 (PostgreSQL): 같은 세션의 임시 테이블로 변수를 넘긴다. 모듈은 (SELECT user_start FROM seed_vars)로 읽는다
CREATE TEMP TABLE seed_vars AS
SELECT 900001 AS user_start, 500 AS user_count, 990001 AS target_id, 100 AS target_capacity;
```

실행은 호출자가 레포 루트에서 한다. 변수 블록과 모듈을 `cat`으로 이어 붙여
**한 세션으로** 흘려보낸다. 세션이 하나라 변수가 모듈까지 그대로 이어진다.

```bash
eval "$(.claude/scripts/harness conc-shell --issue {이슈번호})"
cat "$CONC_DIR/seeds.sql" \
    .claude/resources/concurrency/seeds/users.sql \
    .claude/resources/concurrency/seeds/contention-resource.sql \
  | dbc
```

> **`SOURCE`(MySQL), `\i`(psql)를 쓰지 마라.** 클라이언트가 직접 파일을 여는 명령이라
> 경로를 **클라이언트가 있는 곳** 기준으로 찾는다. 클라이언트가 컨테이너 안에 있으면
> 호스트 경로를 볼 수 없고, 그 줄이 조용히 실패해 변수만 들어간 채 0건이 적재된다.

## 변수

모듈은 변수 블록이 정한 값만 읽는다. 정의하지 않은 변수는 `NULL`이 되고,
`NOT NULL` 칼럼에 들어가면서 에러로 죽는다. 조용히 0건이 들어가지 않도록 NOT NULL 칼럼에 변수를 쓴다.

| 변수 (예) | 뜻 |
|---|---|
| `user_start` | 시드 사용자 id 시작값. `mint-tokens.sh`의 `--start`와 같아야 한다 |
| `user_count` | 시드 사용자 수. **VU 수와 반드시 같다** |
| `target_id` | 경합 대상 자원 id. 앱 시드와 겹치지 않게 크게 잡는다 |
| `target_capacity` | 대상 자원의 허용 상한. VU의 1/3 ~ 1/5로 잡는다 |

## 설계 규칙

- **사용자 수 = VU 수.** 어긋나면 같은 사용자가 두 번 요청하게 되고, 그 실패가 위반 건수를 왜곡한다.
- **경합 대상은 자원 하나다.** 여러 개로 흩으면 행마다 경합이 옅어져 결함이 재현되지 않는다.
- **사용자는 대상 검증 외의 한도에 걸리지 않게 만든다.** 사용자별 한도(개수, 금액, 학점 등)가 있으면
  시드 값으로 넉넉히 연다. 어느 한도가 있는지는 Phase 3-A의 검증 목록이 기준이다.
- **대상 자원은 요청을 받을 수 있는 상태로 둔다.** 상태 칼럼(활성, 판매 중 등)과 유형별 별도 상한을 확인한다.
- **열거형 칼럼에는 실재하는 상수명을 넣어라.** DB가 `VARCHAR`면 저장은 통과하고 조회 시점에 터진다.
- 모든 모듈은 **재실행해도 중복이 쌓이지 않아야 한다.** MySQL `INSERT IGNORE`, PostgreSQL
  `ON CONFLICT DO NOTHING`과 UNIQUE 제약이 그 장치다.
- 검증 쿼리는 모듈 말미에 둔다. 행 수와 함께 실제로 들어간 값을 뽑는다.

## 시드를 지울 때

시드 사용자와 자원은 id를 명시 삽입하므로 범위로 지울 수 있다. FK의 `ON DELETE` 동작을 먼저 확인한다.
CASCADE가 아니면 자식 테이블부터 지운다.

```sql
-- 예시 (MySQL)
DELETE FROM {사용자 테이블} WHERE id BETWEEN @user_start AND @user_start + @user_count - 1;
DELETE FROM {자원 테이블} WHERE id = @target_id;
```

앱 시드와 id가 겹치지 않도록 범위를 충분히 크게 잡는다. 겹치면 위 DELETE가 실제 데이터를 지운다.
