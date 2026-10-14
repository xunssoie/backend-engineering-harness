-- 되돌리기 템플릿.
-- Phase 3에서 대상에 맞게 고쳐 .claude/resources/concurrency/{이슈번호}/{슬러그}/reset.sql 로 저장한다.
-- measure.sh가 매 측정 직전에 실행한다. 후보 코드를 되돌린 뒤에도 Phase 7에서 다시 실행한다.
--
-- ── 작성 규칙 ──────────────────────────────────────────────
-- 1. 측정이 바꾼 데이터만 **초기 상태로** 되돌린다. 초기 상태는 대상마다 다르다.
--    증가 경로(신청, 예약)는 "비우기"가 초기 상태고, 감소 경로(취소, 반납)는 "다시 채우기"가 초기 상태다.
-- 2. **마지막 SELECT는 reset_ok 칼럼 하나를 1 또는 0으로 낸다.** measure.sh가 이 값이 1이 아니면 측정을 멈춘다.
--    확인 칼럼(rows_left, counter 등)을 함께 내면 기록에 남는다.
-- 3. 다른 데이터를 지우지 마라. WHERE에 대상 id를 반드시 건다.
-- 4. MySQL과 PostgreSQL 공통 문법만 쓴다. 불리언 대신 CASE ... THEN 1 ELSE 0.
-- ──────────────────────────────────────────────────────────

-- 예시: 증가 경로. 대상 자원의 점유 행을 모두 지우고 집계를 0으로 되돌린다
DELETE FROM {점유 테이블} WHERE {자원 FK} = {대상 id};
UPDATE {자원 테이블} SET {집계 칼럼} = 0 WHERE id = {대상 id};

-- 되돌아갔는지 확인. 기대값은 초기 상태에 맞춘다 (감소 경로면 시드가 채운 수)
SELECT (SELECT COUNT(*) FROM {점유 테이블} WHERE {자원 FK} = {대상 id})  AS rows_left,
       (SELECT {집계 칼럼} FROM {자원 테이블} WHERE id = {대상 id})       AS counter,
       CASE WHEN (SELECT COUNT(*) FROM {점유 테이블} WHERE {자원 FK} = {대상 id}) = 0
             AND (SELECT {집계 칼럼} FROM {자원 테이블} WHERE id = {대상 id}) = 0
            THEN 1 ELSE 0 END                                              AS reset_ok;
