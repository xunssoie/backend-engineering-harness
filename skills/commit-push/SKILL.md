---
name: commit-push
description: |
  변경 사항을 작업 성격별 커밋으로 묶어 컨벤션에 맞는 메시지로 커밋하고 push한다.
  Trigger: "커밋해줘", "커밋하고 푸시해줘", "푸시해줘", "커밋 올려줘", "커밋까지만 해줘"
  Do NOT use for: 이슈, 브랜치 생성(→ open-issue), PR 생성(→ open-pr)
  Boundary: 커밋과 push까지만 한다.
argument-hint: "[push 안 함 | 사용자 지시]"
allowed-tools: Read, Agent(commit-planner), AskUserQuestion, Bash(*/.claude/scripts/harness *), Bash(.claude/scripts/harness *)
model: sonnet
effort: low
---

# 커밋, 푸시

## 주입된 맥락

!`"${CLAUDE_PROJECT_DIR}/.claude/scripts/harness" skill-context commit-push`

## 시작 조건

- `protectedBranch`가 true면 "보호 브랜치에는 직접 커밋하지 않는다"를 알리고 중단한다
- `status.clean`이 true면 커밋할 변경이 없다고 알리고 중단한다
- `issue`가 null이면 이슈 번호를 묻는다. 없다고 하면 번호 없이 진행한다

## Phase 1: 커밋 계획 (commit-planner 위임)

diff는 메인 대화에서 읽지 않는다. `commit-planner`에게 맡긴다. 프롬프트에 넘길 것:

- 계획 JSON 경로: `{root}/{commitPlanPath}`
- 이슈 번호, `.claude/spec/git-convention.md`의 커밋 타입 표 경로
- push 여부: $ARGUMENTS에 "커밋까지만", "푸시 안 함"이 있으면 false
- 사용자가 대화에서 밝힌 제외 대상이나 묶음 지시

## Phase 2: 검증과 확인

```bash
harness apply-commits {계획 JSON 경로} --dry-run
```

- `errors`가 있으면 계획 JSON을 고쳐(Edit) 다시 검증한다. 메시지 규칙과 파일 목록은 CLI가 검사한다
- `gates.commit`이 true면 커밋 메시지, 파일 수, `leftover`(커밋하지 않을 변경), push 방식(`pushCommand`)을 보여주고 승인을 받는다

> 다음 Phase 조건: 검증을 통과했고, 게이트가 켜져 있으면 사용자가 승인했을 때

## Phase 3: 커밋, 푸시

```bash
harness apply-commits {계획 JSON 경로}
```

성공하면 출력 끝의 보고서를 그대로 보여준다. 실패하면 `errors`와 `done`(이미 만든 커밋)을 보고하고 멈춘다.
