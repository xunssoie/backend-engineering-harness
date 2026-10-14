---
name: write-plan
description: |
  현재 브랜치에 연결된 GitHub 이슈를 읽어 클래스, 메서드 단위의 구현 계획서를 작성하고 미결정 사항을 사용자와 확정한다.
  Trigger: "계획 세워줘", "작업 계획 만들어줘", "계획서 써줘", "이 이슈 어떻게 구현할지 계획"
  Do NOT use for: 이슈 발의(→ open-issue), 구현(→ implement)
  Boundary: 계획서 작성까지만 한다. 코드는 수정하지 않는다.
allowed-tools: Read, Grep, Glob, Write, Edit, AskUserQuestion, Agent, Bash(*/.claude/scripts/harness *), Bash(.claude/scripts/harness *)
model: opus
effort: high
---

# 작업 계획 수립

계획서는 사람이 검토하는 핵심 게이트다. 어떤 클래스의 어떤 메서드를 어떻게 추가, 수정할지까지 쓴다.

## 주입된 맥락

!`"${CLAUDE_PROJECT_DIR}/.claude/scripts/harness" skill-context write-plan`

- `issueDetail`: 이슈 제목, 본문, 라벨. 다시 조회하지 않는다
- `plan.path`: 계획서 경로. `plan.exists`가 true면 새로 쓰지 말고 이어서 고친다
- `conventionLayers`, `policyDomains`: 참조할 수 있는 컨벤션 레이어와 정책 도메인 목록

`issue`가 null이면 이슈 번호를 묻고 중단한다.

## Phase 1: 코드베이스 파악

1. 이슈와 관련된 도메인의 탐색은 Explore 서브에이전트에 맡긴다. 받을 것은 아래 요약뿐이다:
   손댈 파일 경로, 같은 도메인의 기존 패턴(Controller, Service, Repository의 시그니처), 관련 Entity의 필드와 연관관계
2. 요약으로 부족한 파일만 직접 Read한다. 디렉토리 전체나 파일 여러 개를 통째로 출력하지 않는다

> 다음 Phase 조건: 손댈 파일과 기존 패턴이 정해졌을 때

## Phase 2: 컨벤션, 정책 확인

손댈 레이어와 도메인만 한 번에 가져온다:

```bash
harness plan-references --layers {레이어,...} --domains {도메인,...} [--migration]
```

- DB 변경이 있으면 `--migration`을 붙인다
- 기존 정책과 어긋나는 구현을 계획하지 않는다. 어긋나야 하면 정책 변경임을 계획서에 드러내고, 해당 정책 파일을 "영향 범위 - 수정 파일"에 넣는다

> 다음 Phase 조건: 관련 컨벤션과 정책을 확인했을 때

## Phase 3: 계획서 작성

1. `planTemplate`을 Read해서 `plan.path`에 작성한다
2. 구현 계획에는 경로, 클래스, 메서드 시그니처, 로직 요지를 쓴다. **메서드 본문 코드는 쓰지 않는다.** 구현은 implement가 한다
3. 사용자가 정해야 할 것은 "결정 필요"에 `- [ ] {사항} - 옵션 A / 옵션 B` 형식으로 남긴다. 추천이 있으면 근거와 함께 적는다
4. Deviation Log는 비워 둔다

## Phase 4: 미결정 사항 확정

1. "결정 필요" 항목을 사용자에게 묻고, 답을 계획서에 반영한 뒤 `- [x]`로 체크한다
2. 미결정이 남았는지 확인한다: `harness lint plan --file {plan.path}`

> 다음 Phase 조건: lint 결과 `ok`가 true일 때
> Skip 조건: "결정 필요" 항목이 처음부터 없을 때

## Phase 5: 결과 보고

```bash
harness render-report write-plan --data '{"issue": {번호}, "path": "{plan.path}", "newCount": {신규 수}, "modCount": {수정 수}, "decisions": "{확정한 결정 요약 또는 없음}"}'
```

출력을 그대로 보여준다.
