---
name: implement
description: |
  계획서(PLAN-{번호})대로 기능을 구현한다.
  Trigger: "구현해줘", "PLAN-{번호} 구현해줘", "계획서대로 만들어줘", "이 계획 구현 시작"
  Do NOT use for: 계획 수립(→ write-plan), 테스트 작성(→ write-test), PR 생성(→ open-pr)
  Boundary: 계획서대로 코드를 구현한다. 계획을 새로 짜지 않고, 테스트 코드는 쓰지 않는다.
allowed-tools: Read, Grep, Glob, Edit, Write, Bash(*/.claude/scripts/harness *), Bash(.claude/scripts/harness *)
model: sonnet
effort: high
---

# 기능 구현 (계획서 기반)

## 주입된 맥락

!`"${CLAUDE_PROJECT_DIR}/.claude/scripts/harness" skill-context implement`

- `planText`: 계획서 전문. 다시 Read하지 않는다 (`planTruncated`가 true일 때만 나머지를 Read)
- `plan.uncheckedDecisions`: 미결정 항목 수

## 시작 조건

- `planStageEnabled`가 false면 계획서 없이 `issueDetail`의 작업 항목을 기준으로 구현한다. 아래 계획서 관련 절차(계획 이탈 기록, Phase 2)는 건너뛰고, 정해지지 않은 사항은 사용자에게 묻는다
- `plan.exists`가 false면 "계획서가 없습니다. 먼저 write-plan을 실행하세요"를 알리고 중단한다
- `plan.uncheckedDecisions`가 0보다 크면 그 항목을 사용자에게 확정받아 계획서에 반영하기 전까지 시작하지 않는다

## 계획 이탈 처리

계획서는 검토를 마친 기준이다. 구현 중 문제가 생기면 심각도로 나눈다.

| 심각도 | 경우 | 처리 |
|---|---|---|
| critical | 계획에 논리 오류가 있다, 정해지지 않은 사항이 나왔다 | 멈추고 사용자에게 확정받은 뒤 진행 |
| trivial | 로직은 맞지만 더 나은 방법이 있다 | 계획서 `Deviation Log`에 `- {파일}: {바꾼 것} - 이유: {이유}`로 남기고 진행 |

## Phase 1: 구현

1. 계획서 "구현 계획"의 레이어 순서대로 구현한다
2. 파일을 만들기 전에 Glob으로 존재 여부를 확인하고, 있으면 Edit으로 고친다
3. 계획서의 클래스, 메서드 시그니처, 경로를 그대로 따른다
4. 인증이 필요 없는 엔드포인트를 추가하거나 바꾸면 인증 화이트리스트와 인증 필터를 함께 확인한다
5. 서비스 정책(성공과 실패 조건, 제한 수치, 산정 기준, 검증 순서, 유효 기간, 권한 범위)이 바뀌면
   `.claude/spec/service-policy/`의 해당 도메인 파일을 같은 작업에서 고친다

## Phase 2: 계획 대비 확인

```bash
harness lint plan --file {plan.path} --against-diff
```

`errors`에 "변경되지 않은 파일"이 있으면 빠뜨린 것인지, 계획 이탈인지 판단한다. 빠뜨렸으면 구현하고, 이탈이면 Deviation Log에 남긴다.

> 다음 Phase 조건: 빠진 파일이 없거나 모두 Deviation Log에 설명됐을 때

## Phase 3: 결과 보고

```bash
harness render-report implement --data '{"issue": {번호}, "items": [{"target": "{클래스.메서드()}", "mark": "✅"}], "policy": "{갱신한 정책 파일과 바뀐 규칙 또는 변경 없음}", "deviation": "{Deviation Log 요약 또는 없음}"}'
```

- `mark`: ✅ 계획대로 구현, ⚠️ trivial 이탈을 적용했거나 확인이 필요함
- 출력을 그대로 보여준다
