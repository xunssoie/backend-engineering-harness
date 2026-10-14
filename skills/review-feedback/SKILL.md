---
name: review-feedback
description: |
  PR에 달린 리뷰 봇 피드백을 수집해 코드베이스 기준으로 타당성을 상중하로 판정하고, 사용자가 고른 항목만 고친다.
  Trigger: "코드래빗 피드백 봐줘", "리뷰 피드백 검토해줘", "PR 리뷰 확인해줘"
  Do NOT use for: PR 생성(→ open-pr), 계획 기반 구현(→ implement)
  Boundary: 판정, 선택 항목 수정, 반영 항목의 스레드 resolve까지 한다. 판정만으로 코드를 고치지 않는다.
argument-hint: "[PR 번호]"
allowed-tools: Read, Grep, Glob, Edit, Agent(feedback-judge), Bash(*/.claude/scripts/harness *), Bash(.claude/scripts/harness *), Bash(jq *)
model: opus
effort: high
---

# 리뷰 피드백 검토

리뷰 봇은 변경된 파일 주변만 보고 피드백을 남긴다. 그대로 수용하지 않고 코드를 직접 확인해 판정한다.

## 주입된 맥락

!`"${CLAUDE_PROJECT_DIR}/.claude/scripts/harness" skill-context review-feedback`

대상 PR은 $ARGUMENTS가 있으면 그 번호, 없으면 `pr.number`. 둘 다 없으면 "PR이 없습니다. 먼저 open-pr을 실행하세요"를 알리고 멈춘다.

## Phase 1: 수집

```bash
harness collect-review-comments --pr {번호}
```

- 원문은 `itemsFile`, `reviewsFile`에 있다. **원문 파일을 Read하지 않는다**
- `pending`(판정 대상 인덱스)과 `handledExcluded`(답글이 달려 제외한 수)만 쓴다
- `pending`이 비었고 `reviewsNonEmpty`가 false면 피드백이 없다고 알리고 끝낸다

## Phase 2: 판정 (feedback-judge 위임)

판정 디렉토리는 `{root}/{reviewDir}/verdicts-{번호}`다.

1. `pending` 항목마다 `feedback-judge`를 호출한다. 넘길 것: 항목 id, `itemsFile` 경로, 판정 디렉토리
   - 항목끼리 독립이므로 한 메시지에 병렬로, 한 번에 최대 10개까지 띄운다
2. `reviewsNonEmpty`가 true면 `feedback-judge`를 한 번 더 불러 `reviewsFile`과 판정 디렉토리를 넘긴다

## Phase 3: 판정 표

```bash
harness render-verdict-table --rows {판정 디렉토리}
```

- `malformed`가 있으면 그 항목만 feedback-judge를 다시 부른다
- `table`을 그대로 보여주고, 그 아래에 `상 {a}건 / 중 {b}건 / 하 {c}건, 처리 이력으로 제외 {handledExcluded}건`과 "수정할 번호를 골라주세요."를 붙인다
- **이 시점에 코드를 고치지 않는다.** 상 판정도 예외가 아니다

> 다음 Phase 조건: 사용자가 번호를 골랐을 때

## Phase 4: 선택 항목 수정

1. 고른 번호만 고친다. `rows`의 `fix`(수정 지침)를 기준으로 한다
2. 원문이 꼭 필요하면 그 항목만 뽑는다: `jq -r 'select(.id == {id}) | .body' {itemsFile}`
3. 리뷰 봇이 제안한 diff를 붙여넣지 말고 이 코드베이스의 컨벤션에 맞춰 다시 쓴다
4. 정책이 바뀌면 `.claude/spec/service-policy/`의 해당 파일도 고친다
5. 수정 범위가 계획서 한 건 수준으로 커지면 멈추고 write-plan을 권한다

> Skip 조건: 사용자가 아무 항목도 고르지 않았을 때

## Phase 5: 반영 항목 resolve

수정한 **인라인** 항목만 resolve한다. 판정만 하고 넘긴 항목은 그대로 둬서, 무엇이 남았는지 스레드 상태로 보이게 한다.

```bash
harness resolve-review-threads --pr {번호} {코멘트 id ...}
```

요약 리뷰 항목은 스레드가 없어 resolve 대상이 아니다.

> Skip 조건: Phase 4를 건너뛰었을 때

## Phase 6: 결과 보고

```bash
harness render-report review-feedback --data '{"pr": {번호}, "url": "{pr.url}", "total": {판정 수}, "counts": {render-verdict-table의 counts}, "handledExcluded": {수}, "fixed": "{번호와 한 줄 요약 또는 없음}", "resolvedText": "{resolve 수, 요약 리뷰 반영 수 또는 없음}", "policy": "{갱신한 정책 파일 또는 변경 없음}"}'
```

출력을 그대로 보여준다.
