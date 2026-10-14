---
name: open-issue
description: |
  작업을 대화로 구체화해 GitHub 이슈를 열고, 연결된 작업 브랜치를 만들어 체크아웃한다.
  Trigger: "이슈 열어줘", "이거 작업 시작하자", "새 작업 시작하자", "기능/버그/리팩토링 이슈 만들어줘"
  Do NOT use for: 작업 계획(→ write-plan), 구현(→ implement)
  Boundary: 이슈 생성과 브랜치 생성까지만 한다. 무엇을, 왜만 다루고 어떻게는 다루지 않는다.
argument-hint: "[작업 설명]"
allowed-tools: Read, Write, AskUserQuestion, Bash(*/.claude/scripts/harness *), Bash(.claude/scripts/harness *)
model: sonnet
effort: medium
---

# 작업 발의 (이슈 + 브랜치)

## 주입된 맥락

!`"${CLAUDE_PROJECT_DIR}/.claude/scripts/harness" skill-context open-issue`

- `cli`: 하네스 CLI 경로. 아래 `harness`는 이 경로를 뜻한다
- `types`: 작업 종류별 라벨과 이슈 템플릿 경로(없으면 null)
- `gates.issue`: true면 이슈를 열기 전에 사용자 확인을 받는다

## Phase 1: 작업 구체화 (대화)

$ARGUMENTS가 비었거나 모호하면 대화로 아래를 확정한다. 여러 작업이 섞여 있으면 이슈를 나눠 작업마다 Phase 1~3을 반복한다.

- **종류**: `types`의 키 중 하나
- **제목**: 명사형, `titleMaxLength`자 이내. 대상을 나열하지 말고 무엇을 해결하는지를 남긴다
- **설명**: 왜 필요한지 1~3문장
- **작업 항목**: 체크리스트로 쪼갠 하위 작업
- **slug**: 브랜치용 영문 kebab-case (예: `login-redirect`)

> 다음 Phase 조건: 다섯 요소가 정해졌을 때. `gates.issue`가 true면 사용자가 확인했을 때

## Phase 2: 본문 작성

1. 종류의 `template`이 있으면 Read해서 그 구획을 그대로 따른다. 없으면 `## 이슈 내용`, `## 작업 내용`, `## 첨부 파일` 세 구획으로 쓴다
   - 이슈 내용: 설명. 문제가 여러 개면 번호로 나눈다
   - 작업 내용: `- [ ] {작업}` 체크리스트. 갈래가 여러 개면 굵은 소제목으로 묶는다
   - 첨부 파일: 근거가 되는 경로, 로그, 링크. 없으면 구획 제목만 둔다
2. 템플릿의 HTML 주석은 넣지 않는다
3. 본문을 세션 스크래치패드에 파일로 저장한다

## Phase 3: 이슈, 브랜치 생성

```bash
harness create-issue --type {종류} --title "{제목}" --body-file {본문 파일} --slug {slug}
```

- hotfix처럼 base가 다를 수 있는 작업은 사용자에게 base를 묻고 `--base {브랜치}`를 붙인다
- 출력 JSON의 `errors`가 있으면 그 내용대로 제목이나 본문을 고쳐 다시 실행한다. 표기와 길이 규칙은 CLI가 검사한다
- 성공하면 출력 끝의 보고서를 그대로 보여준다. 덧붙이지 않는다
