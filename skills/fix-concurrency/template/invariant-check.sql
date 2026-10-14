-- 불변식 검증 템플릿.
-- Phase 1에서 대상에 맞게 고쳐 .claude/resources/concurrency/{이슈번호}/{슬러그}/invariant-check.sql 로 저장한다.
-- Phase 4, 7, 8에서 **그대로** 재사용한다. 후보마다 바꾸지 마라. 바꾸면 전후 비교가 성립하지 않는다.
--
-- 실행: measure.sh가 부른다 (MySQL은 dbc -t, PostgreSQL은 dbc -P footer=off).
-- 출력은 harness conc-compare가 읽는다. 아래 칼럼 이름을 바꾸지 마라.
--
-- ── 작성 규칙 ──────────────────────────────────────────────
-- 1. 불변식 하나가 결과 한 행으로 나온다. 한 쿼리에 여러 불변식을 섞지 마라.
-- 2. 모든 불변식 행이 같은 칼럼 구조를 갖는다: invariant / expected / actual / violations.
--    구조가 다르면 Phase 4와 7에서 같은 방식으로 읽을 수 없다.
-- 3. invariant 칸은 `I{번호}. {불변식 문장}`으로 시작한다. conc-compare가 `I{번호}`로 후보 간 행을 맞춘다.
-- 4. **violations는 0이 통과다.** 불리언이 아니라 개수로 만든다.
--    "몇 건 어긋났는가"가 lost update의 규모를 보여준다.
-- 5. 집계 값과 실제 행 수를 대조하는 불변식을 반드시 넣는다(아래 I2).
--    상한을 안 넘겨도 갱신이 유실됐을 수 있고, 그건 이 불변식으로만 잡힌다.
-- 6. 역방향 동작(취소, 감소, 반납)에 걸린 불변식을 빼먹지 마라(아래 I3).
-- 7. 참고 자료는 metric 칼럼으로 시작하는 별도 결과로 낸다. 판정에는 쓰지 않는다.
-- 8. 값을 반올림하거나 가공하지 마라. 원본 그대로 파일에 남긴다.
-- 9. MySQL과 PostgreSQL 공통 문법만 쓴다. IF() 대신 CASE, 사용자 변수 대신 리터럴.
--    {대상 id} 같은 자리는 Phase 3에서 확정한 값으로 직접 바꿔 넣는다.
-- ──────────────────────────────────────────────────────────
--
-- 아래 이름은 자리표시다. 프로젝트 스키마로 바꾼다.
--   {자원 테이블}  다투는 자원 행이 있는 테이블 (예: 재고, 좌석, 정원이 있는 상품)
--   {상한 칼럼}    허용 상한 (예: 정원, 재고 총량)
--   {집계 칼럼}    애플리케이션이 증감시키는 집계 값 (예: 현재 인원, 남은 수량)
--   {점유 테이블}  요청 하나가 성공할 때마다 생기는 행 (예: 예약, 신청, 주문 항목)
--   {자원 FK}      점유 행이 자원을 가리키는 칼럼
--   {대상 id}      Phase 3에서 확정한 다투는 자원의 id

SELECT '=== 불변식 검증 ===' AS section;

-- I1. 점유 행 수는 상한을 넘지 않는다.
--     violations = 상한을 초과해 들어간 행 수.
SELECT 'I1. COUNT({점유 테이블}) <= {상한 칼럼}'                   AS invariant,
       r.{상한 칼럼}                                                AS expected,
       (SELECT COUNT(*) FROM {점유 테이블} o
         WHERE o.{자원 FK} = r.id)                                  AS actual,
       GREATEST(0, (SELECT COUNT(*) FROM {점유 테이블} o
                     WHERE o.{자원 FK} = r.id) - r.{상한 칼럼})       AS violations
FROM {자원 테이블} r
WHERE r.id = {대상 id};

-- I2. 집계 칼럼이 실제 점유 행 수와 일치한다.
--     violations != 0 이면 갱신이 유실된 것이다. lost update의 직접 증거다.
--     I1이 0이어도 이 값이 0이 아닐 수 있다. 반드시 함께 본다.
SELECT 'I2. {집계 칼럼} = COUNT({점유 테이블})'                    AS invariant,
       (SELECT COUNT(*) FROM {점유 테이블} o
         WHERE o.{자원 FK} = r.id)                                  AS expected,
       r.{집계 칼럼}                                                AS actual,
       ABS(r.{집계 칼럼} - (SELECT COUNT(*) FROM {점유 테이블} o
                             WHERE o.{자원 FK} = r.id))             AS violations
FROM {자원 테이블} r
WHERE r.id = {대상 id};

-- I3. 집계 칼럼은 음수가 되지 않는다.
--     역방향 경로에 같은 제어를 걸지 않으면 여기서 드러난다.
SELECT 'I3. {집계 칼럼} >= 0'                                      AS invariant,
       '>= 0'                                                      AS expected,
       r.{집계 칼럼}                                                AS actual,
       CASE WHEN r.{집계 칼럼} < 0 THEN 1 ELSE 0 END               AS violations
FROM {자원 테이블} r
WHERE r.id = {대상 id};

-- 참고 자료. 불변식은 아니지만 판정에 쓴다.
SELECT '=== 참고 ===' AS section;

-- 같은 사용자의 중복 점유가 실제로 들어갔는지. UNIQUE 제약이 있으면 항상 1 이하여야 한다.
-- 1을 넘으면 제약이 없거나 깨진 것이다.
SELECT 'ref. 사용자당 최대 점유 수'                                 AS metric,
       COALESCE(MAX(cnt), 0)                                       AS max_per_user
FROM (
    SELECT COUNT(*) AS cnt
    FROM {점유 테이블}
    WHERE {자원 FK} = {대상 id}
    GROUP BY {사용자 FK}
) g;

-- 전체 자원으로 넓혀 집계가 어긋난 행이 또 있는지 본다.
-- 대상 자원만 보다가 다른 곳의 오염을 놓치지 않기 위한 것이다.
SELECT 'ref. 집계가 어긋난 자원 수'                                 AS metric,
       COUNT(*)                                                    AS resources_with_drift
FROM {자원 테이블} r
WHERE r.{집계 칼럼} <> (SELECT COUNT(*) FROM {점유 테이블} o WHERE o.{자원 FK} = r.id);
