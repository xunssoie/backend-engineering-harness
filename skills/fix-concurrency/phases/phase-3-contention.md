## Phase 3. 경합 조건 설계

### 목적
결함이 드러날 만큼 센 경합을 만든다. 시드, 토큰, 폭발 부하 스크립트, 되돌리기 SQL을 준비한다.

경합이 약하면 결함은 재현되지 않는다. **재현되지 않은 결함은 존재하지 않는 것과 구분되지 않는다.**

### 선행 조건
- Phase 2 완료

### 참조 파일
- `.claude/skills/fix-concurrency/template/seeds/README.md`
- `.claude/scripts/perf/mint-tokens.sh` (인자는 스크립트 머리말)
- `.claude/skills/fix-concurrency/template/k6-burst-template.js`
- `.claude/skills/fix-concurrency/template/reset.sql`

### 절차

#### 3-A. 경합 조건 확정

1. **경합 조건을 호출자와 확정한다.** 아래 세 값이 이후 모든 측정에서 고정된다.

   | 값 | 정하는 기준 |
   |---|---|
   | 다투는 자원의 수 | **1개로 좁힌다.** 여러 행에 흩으면 행마다 경합이 옅어져 결함이 안 드러난다 |
   | 허용 상한 | 그 자원이 받아줄 수 있는 최대치 (예: 정원, 재고, 좌석 수) |
   | 동시 요청 수 (VU) | **허용 상한보다 충분히 크게.** 초과분이 곧 위반 후보다 |

   - 권장 출발점은 **허용 상한의 3~5배**다. 상한 100이면 VU 300~500.
   - VU는 Phase 2에서 확인한 커넥션 풀 크기를 넘기지 마라. 넘기면 락 대기가 아니라 커넥션 대기를 재게 된다.
     풀을 못 올리면 상한을 낮춰 비율을 맞춘다. (풀 100이면 상한 20에 VU 100)
   - 감소 경로(취소, 반납)는 상한 초과 개념이 없다. 이때 허용 상한은 **시드가 채워둔 점유 수**이며
     성공 수가 이 값과 정확히 같아야 한다. 출발 상태(만석)를 함께 정한다.
   - 정한 값을 `record.md`의 **경합 조건**에 적는다.

2. **요청 하나가 대상 검증만 통과하도록 조건을 정리한다.**
   다른 검증에 먼저 걸리면 경합 지점까지 도달하지 못한 요청을 재게 된다.

   대상 경로의 검증을 코드에서 모두 찾아 표로 만들고, 각각의 통과 조건을 시드로 보장한다.

   | 검증 | 통과 조건 |
   |---|---|
   | {대상 자원의 상태 검증} | {예: 자원 상태가 활성} |
   | {중복 요청 검증, UNIQUE 제약} | **VU마다 서로 다른 사용자.** 사용자 수 = VU 수 |
   | {사용자별 한도 검증} | {예: 시드는 점유 0건에서 시작, 한도를 넉넉히} |
   | {유형별 별도 상한} | {예: 별도 상한이 없는 유형으로 둔다} |

   - 사용자 수가 VU보다 적으면 중복 요청으로 실패한 요청이 섞여 위반 건수가 왜곡된다. **반드시 일치시킨다.**

#### 3-B. 시드

3. **시드 모듈을 확인한다.** 주입된 `seedModules`가 비어 있거나 이번 대상에 필요한 테이블을 채우는 모듈이 없으면
   `template/seeds/README.md`의 규칙대로 `.claude/resources/concurrency/seeds/`에 모듈을 만든다.
   - 스키마는 마이그레이션 디렉토리의 최신 상태를 Read해 맞춘다. 엔티티만 보고 쓰지 마라.
   - 만든 모듈은 다음 이슈도 쓴다. 이슈 디렉토리에 복사하지 마라.

4. `.claude/resources/concurrency/{이슈번호}/seeds.sql`을 만든다. **변수 블록만** 담는다.
   이슈 공용 파일이 이미 있으면(`issueShared.seeds.sql`) 변수가 이번 경합 조건과 맞는지만 확인한다.

5. 시드를 적재하도록 제시하고 결과를 받는다. 실행은 호출자가 레포 루트에서 한다.

   ```bash
   eval "$(.claude/scripts/harness conc-shell --issue {이슈번호})"
   cat "$CONC_DIR/seeds.sql" \
       .claude/resources/concurrency/seeds/{모듈1}.sql \
       .claude/resources/concurrency/seeds/{모듈2}.sql \
     | dbc
   ```

   - 각 모듈 말미의 검증 쿼리 결과로 행 수와 대상 자원의 상한을 확인한다.
   - 사용자 수가 3-A에서 정한 VU와 다르면 여기서 멈춘다.

#### 3-C. 토큰

6. **측정용 토큰을 서명키로 직접 만든다.** 로그인 API를 쓰지 마라.
   시드가 넣는 비밀번호는 해시가 아닌 더미 값이라 로그인 대조를 통과하지 못한다.

   ```bash
   bash .claude/scripts/perf/mint-tokens.sh \
     --start {시드 사용자 id 시작값} \
     --count {VU 수} \
     --out .claude/resources/concurrency/{이슈번호}/tokens.json
   jq 'length' .claude/resources/concurrency/{이슈번호}/tokens.json
   ```

   - 서명키는 `perf.tokens`의 `secretEnv`나 `secretFile`에서 읽는다. 값을 명령줄에 넘기지 마라. 셸 기록에 남는다
   - 인자 이름은 `mint-tokens.sh` 머리말이 기준이다. 위와 다르면 머리말을 따른다.
   - `tokens.json`은 gitignore 대상이다. 커밋하지 마라.

7. **토큰이 실제로 통하는지 한 번 확인한다.** 여기서 401이 나면 부하 전체가 401로 끝난다.

   ```bash
   TOKEN=$(jq -r '.[0].accessToken' .claude/resources/concurrency/{이슈번호}/tokens.json)
   curl -s -o /dev/null -w '%{http_code}\n' \
     -H "{perf.tokens.header}: {perf.tokens.headerPrefix}$TOKEN" \
     {perf.appBaseUrl}{인증이 필요한 조회 경로}
   ```

   - 200이 아니면 서명키가 측정 프로파일의 값과 같은지, 사용자 id가 실재하는지, 헤더 이름과 접두어가 맞는지 확인한다.

#### 3-D. 부하 스크립트와 되돌리기

8. `template/k6-burst-template.js`를 Read해 `.claude/resources/concurrency/{이슈번호}/{슬러그}/burst-script.js`로 만든다.
   템플릿 상단의 작성 규칙을 따른다.

   - **executor는 `per-vu-iterations`, `iterations: 1`이다.** VU마다 요청을 정확히 한 번 보내
     "N명이 동시에 버튼을 누른다"를 만든다. ramp 구간을 두지 마라. 램프를 두면 경합이 시간축으로 흩어진다.
   - 토큰은 `__VU`로 고른다. VU와 사용자가 1대1로 묶여야 중복 요청이 섞이지 않는다.
     optimize-performance 템플릿의 `iterationInTest` 방식을 가져오지 마라.

9. **되돌리기 SQL을 확정해 `reset.sql`로 저장한다.** `measure.sh`가 매 측정 전에 이걸 돌린다.
   `template/reset.sql`을 Read해 구조를 따른다.

   - 마지막 SELECT가 `reset_ok`를 1 또는 0으로 내야 한다. 1이 아니면 `measure.sh`가 측정하지 않는다.
   - **되돌리기를 건너뛰면 상한이 이미 찬 상태에서 재게 되어 전 요청이 상한 초과로 실패한다.**
     그 결과는 "정합성이 지켜졌다"로 보이지만 아무것도 검증하지 못한 것이다.
   - 감소 경로의 초기 상태는 "비우기"가 아니라 "다시 채우기"다. 기대값을 초기 상태에 맞춘다.
   - 한 번 돌려 `reset_ok`가 1인지 확인한다.

     ```bash
     eval "$(.claude/scripts/harness conc-shell --issue {이슈번호} --slug {슬러그})"
     dbc < "$TARGET_DIR/reset.sql"
     ```

10. `invariant-check.sql`의 `{대상 id}` 자리를 확정한 자원 id로 채운다. 불변식 문장과 칼럼은 그대로 둔다.

11. 확정한 조건과 산출물 경로를 `record.md`의 **경합 조건**에 채우고 진행 상태를 갱신한다.

    ```bash
    harness conc-progress {record.md 경로} --phase 3 --mark ✅
    ```

### 출력
- `.claude/resources/concurrency/seeds/` 모듈 (없었으면 생성)
- `.claude/resources/concurrency/{이슈번호}/seeds.sql` 생성, 적재 완료
- `.claude/resources/concurrency/{이슈번호}/tokens.json` 생성, 인증 확인 완료
- `.claude/resources/concurrency/{이슈번호}/{슬러그}/burst-script.js`, `reset.sql` 생성
- `record.md`의 **경합 조건**에 자원, 허용 상한, VU, 출발 상태, 되돌리기가 기록
- 진행 상태의 Phase 3이 ✅

### 실패 처리

| 증상 | 처리 |
|---|---|
| 시드 사용자 수 ≠ VU | 시드를 다시 적재한다. 그대로 진행하지 마라 |
| 토큰 확인이 401 | 서명키 불일치, 사용자 id 부재, 헤더 불일치. 부하로 넘어가지 마라 |
| VU를 풀 크기 이상으로 잡아야 함 | 허용 상한을 낮춰 비율을 맞춘다 |
| `reset_ok`가 0 | 되돌리기 SQL이나 기대값이 틀렸다. 고친 뒤 다시 확인한다 |

> 다음 Phase 조건: 시드, 토큰, 스크립트, 되돌리기 SQL이 모두 준비되고 토큰 인증이 확인되었을 때 → Phase 4

> Skip 조건: 2회차 이상이고 시드, 토큰, 스크립트, `reset.sql`이 이미 준비되어 있으면 → Phase 4
> (되돌리기는 `measure.sh`가 매번 실행한다. 준비물이 있다는 것과 데이터가 초기 상태라는 것은 다르다)
