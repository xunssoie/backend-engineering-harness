---
name: dev-pipeline
description: |
  이슈 발의부터 리뷰 반영까지 파이프라인을 현재 단계부터 이어서 진행한다. 어디까지 했는지는 산출물(브랜치, 계획서, 테스트 기록, PR)로 판정하므로 새 세션에서도 이어 갈 수 있다.
  Trigger: "작업 이어서 해줘", "파이프라인 진행해줘", "다음 단계 진행", "PR까지 쭉 진행해줘", "커밋까지 진행해줘"
  Do NOT use for: 단계 하나만 실행할 때(해당 스킬을 직접 부른다)
argument-hint: "[~까지 | 작업 설명]"
allowed-tools: Skill, Bash(*/.claude/scripts/harness *), Bash(.claude/scripts/harness *)
model: sonnet
effort: low
---

# 개발 파이프라인

## 주입된 맥락

!`"${CLAUDE_PROJECT_DIR}/.claude/scripts/harness" skill-context dev-pipeline`

- `stage`: 지금 할 단계, `reason`: 그렇게 판정한 근거, `done`: 끝난 단계
- `stages`: 이 프로젝트에서 켜진 단계와 순서
- `gates`: 단계 스킬이 사용자 확인을 받는 지점

## 진행 규칙

1. **멈출 단계**를 정한다. $ARGUMENTS에 "커밋까지", "PR까지"처럼 단계가 있으면 그 단계까지, 없으면 `stages`의 끝까지다
2. `stage`의 스킬을 Skill 도구로 부른다. $ARGUMENTS에 작업 설명이 있으면 open-issue에 넘긴다
3. 단계 스킬이 끝나면 현재 단계를 다시 판정한다:
   ```bash
   harness pipeline-stage
   ```
4. 아래 중 하나면 멈추고, 지금 단계와 다음 할 일을 한 줄로 알린다
   - 멈출 단계를 마쳤다
   - `stage`가 `done`이다
   - 단계 스킬이 사용자 입력을 기다린다 (결정 필요, 커밋 승인, 피드백 번호 선택). 사용자가 답하면 그 스킬이 이어서 진행하고, 끝나면 3번부터 다시 한다
   - 같은 단계가 두 번 연속 나왔다. 진행이 안 된 것이니 `reason`과 함께 보고한다
   - 단계 스킬이 실패를 보고했다
5. 그 밖에는 2번으로 돌아가 다음 단계를 진행한다

## 하지 않는 것

- 단계 스킬의 일을 대신하지 않는다. 판정, 작성, 실행은 각 스킬이 한다
- `stages`에 없는 단계를 부르지 않는다
- 단계 사이에 진행 상황을 길게 요약하지 않는다. 각 스킬의 보고서로 충분하다
