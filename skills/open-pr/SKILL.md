---
name: open-pr
description: |
  PR을 생성한다. 본문은 PR 작성 규칙에 맞춰 쓰고, 이슈 라벨과 담당자를 지정하고, 이슈 연결을 확인한다.
  Trigger: "PR 날려줘", "PR 만들어줘", "PR 생성해줘", "PR 올려줘"
  Do NOT use for: 커밋(→ commit-push), 리뷰 피드백(→ review-feedback)
  Boundary: PR 생성까지만 한다. 머지와 리뷰 요청은 하지 않는다.
argument-hint: "[base 브랜치]"
allowed-tools: Read, Edit, Agent(pr-writer), AskUserQuestion, Bash(*/.claude/scripts/harness *), Bash(.claude/scripts/harness *)
model: sonnet
effort: low
---

# PR 생성

## 주입된 맥락

!`"${CLAUDE_PROJECT_DIR}/.claude/scripts/harness" skill-context open-pr`

## 시작 조건

- `existingPr`가 열려 있으면 그 PR 번호와 URL을 알리고 끝낸다
- `upstream.remoteExists`가 false이거나 `upstream.ahead`가 0보다 크면 push되지 않은 커밋이 있다고 알리고 commit-push를 권한 뒤 멈춘다
- base는 $ARGUMENTS가 있으면 그것, 없으면 `base`

## Phase 1: 본문 작성 (pr-writer 위임)

본문 작성은 `pr-writer`에게 맡긴다. 프롬프트에 넘길 것:

- 제목 파일 `{root}/{prDraftDir}/title.txt`, 본문 파일 `{root}/{prDraftDir}/body.md`
- 이슈 번호와 타입, base ref(`vsBase.ref`), 계획서 경로(`plan.path`), PR 템플릿(`prTemplate`), 작성 규칙(`prWritingGuide`)
- 메인 대화에서 나온 설계 결정 중 계획서에 없는 것 (있으면 요약해서)

## Phase 2: 검증과 확인

```bash
harness create-pr --title-file {제목 파일} --body-file {본문 파일} --base {base} --dry-run
```

- `errors`가 있으면 제목이나 본문 파일을 고쳐(Edit) 다시 검증한다. 섹션 구조, 제목 규칙, 표기, `close #번호`는 CLI가 검사한다
- `gates.pr`가 true면 제목과 소제목 목록, 라벨(`labels`)을 보여주고 승인을 받는다

## Phase 3: 생성

```bash
harness create-pr --title-file {제목 파일} --body-file {본문 파일} --base {base}
```

출력 끝의 보고서를 그대로 보여준다. 다시 실행해도 PR이 중복 생성되지 않는다.
