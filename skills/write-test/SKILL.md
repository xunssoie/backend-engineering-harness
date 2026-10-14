---
name: write-test
description: |
  테스트 코드를 작성하고 실행한다. 대상 없이 부르면 실행만 한다.
  Trigger: "테스트 작성해줘", "테스트 코드 만들어줘", "XXXService 테스트", "테스트 추가해줘", "테스트 돌려줘", "테스트 실행해줘"
  Do NOT use for: 기존 테스트의 단순 수정(직접 Edit), 빌드 설정 변경
  Boundary: 테스트 대상(프로덕션) 코드의 버그는 고치지 않고 보고만 한다.
argument-hint: "[대상 클래스명 | 테스트 클래스명]"
allowed-tools: Read, Grep, Glob, Edit, Write, Bash(*/.claude/scripts/harness *), Bash(.claude/scripts/harness *)
model: opus
effort: high
---

# 테스트 작성과 실행

대상: $ARGUMENTS

## 주입된 맥락

!`"${CLAUDE_PROJECT_DIR}/.claude/scripts/harness" skill-context write-test`

- `testConvention`, `testTemplate`: 테스트 규칙과 코드 템플릿 경로
- `lastTest`: 마지막 실행 기록

**테스트는 항상 `harness run-tests`로 실행한다.** Docker 확인, 로그 파일 보관, 결과 집계를 한다. 빌드 도구로 직접 실행하면 훅이 막는다.

인자가 이미 있는 테스트 클래스명이거나 비어 있으면 Phase 4(실행)로 바로 간다.

## Phase 1: 대상 분석

1. 대상 클래스를 찾아 Read한다
2. public 메서드와 각 메서드의 분기(조건, 예외)를 목록으로 만든다
3. 생성자 의존성을 확인한다

## Phase 2: Fixture 준비

1. 대상 도메인의 기존 Fixture를 Glob으로 찾아 재사용한다
2. 없으면 `testTemplate`의 Fixture 절을 따라 만든다

> Skip 조건: 필요한 Fixture가 모두 있을 때

## Phase 3: 테스트 작성

1. `testConvention`과 `testTemplate`을 Read한다
2. Phase 1의 메서드마다 아래 케이스를 쓴다
   - 정상 동작
   - 실패 (존재하지 않음, 권한 없음, 중복 등). 커스텀 예외는 예외 코드까지 검증한다. 타입만 보면 다른 코드로 회귀해도 통과한다
   - 경계값, 빈 목록, null (해당할 때만)
3. Phase 1의 분기 목록과 대조해 빠진 케이스가 없는지 확인한다

## Phase 4: 실행

```bash
harness run-tests --tests "{패키지}.{테스트 클래스}"   # 인자가 없으면 --tests 없이 전체 실행
```

- 전체 실행은 오래 걸리므로 Bash의 `run_in_background`로 돌린다
- `status`별 처리:
  - `passed`: Phase 5로 간다
  - `docker-unavailable`: 사용자에게 Docker를 켜 달라고 알리고 멈춘다
  - `build-failed`: `buildErrors`의 줄을 보고 컴파일 오류를 고친다
  - `failed`: `failed` 목록(`테스트#메서드`, 첫 줄 메시지)으로 원인을 판단한다. 스택이 필요하면 `log`에서 해당 테스트 이름으로 grep해 그 구간만 읽는다
- 원인이 **이번에 작성한 테스트**면 고쳐서 다시 실행한다. 수정과 재실행은 최대 3회다
- 원인이 **프로덕션 코드의 버그**면 원인과 수정 방안만 보고한다. 사용자 확인 없이 고치지 않는다
- `note`에 skip이 있으면 이번에 작성한 테스트가 skip됐는지 확인한다

> 다음 Phase 조건: 전부 통과했거나 재실행 상한에 도달했을 때

## Phase 5: 결과 보고

```bash
harness render-report write-test --data '{"target": "{대상}", "written": "{작성한 파일 경로와 메서드 수 또는 실행만}", "failed": [{"test": "{테스트#메서드}", "cause": "{원인 1~2문장}"}], "next": "{실패가 남았으면 실패 해소 후 commit-push, 아니면 commit-push}"}'
```

실행 명령, 통과와 실패 수, 시간은 마지막 `run-tests` 기록에서 자동으로 채워진다. 실패가 없으면 `failed`를 빼고, 원인 분석이 없는 실패는 첫 줄 메시지가 그대로 들어간다.

출력을 그대로 보여준다.
