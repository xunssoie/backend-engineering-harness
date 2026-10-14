---
name: update-harness
description: |
  하네스를 새 버전으로 업데이트한다. 변경 기록을 보여주고, 프로젝트가 고친 파일은 병합하고, 설정 형식 변환을 적용한다.
  Trigger: "/update-harness", "하네스 업데이트해줘", "새 버전으로 올려줘"
  Do NOT use for: 첫 설정, 재설정(→ init-harness)
  Boundary: 하네스 파일과 설정만 바꾼다. 커밋은 하지 않는다.
argument-hint: "[버전]"
allowed-tools: Read, Edit, AskUserQuestion, Bash(*/.claude/scripts/harness *), Bash(.claude/scripts/harness *), Bash(python3 */.claude/.state/update-src/scripts/harness *)
model: sonnet
effort: medium
---

# 하네스 업데이트

아래 `harness`는 `.claude/scripts/harness`(설치된 버전), `NEW_CLI`는 받아 둔 새 버전의 CLI다.
업데이트는 새 버전의 CLI로 실행한다. 설치된 버전은 새 버전의 병합 규칙과 설정 변환을 모른다.

## Phase 1: 새 버전 받기

```bash
harness prepare-update [--version {$ARGUMENTS의 버전}]
```

- `installed`와 `target`이 같으면 이미 최신이라고 알리고 끝낸다
- `changelog`의 버전별 "바뀐 것"을 요약해 보여준다. `actionRequired`에 든 버전은 "필요한 조치"를 따로 강조한다

## Phase 2: 미리보기와 확인

```bash
python3 {newCli} --project "${CLAUDE_PROJECT_DIR}" apply-update --src {srcDir} --dry-run
```

아래를 보여주고 진행할지 묻는다. 미루면 `harness skip-update --version {target}`을 실행해 다음 버전까지 묻지 않게 한다.

- 교체(`updated`), 추가(`added`), 병합(`merged`), 프로젝트 수정본 유지(`kept`), 하네스에서 지워져 함께 지울 파일(`deletedUpstream`)
- 충돌(`conflicts`): 프로젝트가 고친 곳과 새 버전이 같은 곳을 고쳤다
- 다시 만들지 않는 파일(`skippedRemoved`): 프로젝트가 뺀 스킬

## Phase 3: 업데이트

```bash
python3 {newCli} --project "${CLAUDE_PROJECT_DIR}" apply-update --src {srcDir}
```

- `migrations`가 있으면 설정이 어떻게 바뀌었는지 보여준다
- `conflicts`의 파일마다 프로젝트 파일과 `.claude/.state/update/{경로}`의 새 버전을 비교해 보여주고, 사용자와 합친 결과로 프로젝트 파일을 고친다
- 끝나면 `harness verify-install`로 점검한다

## Phase 4: 결과 보고

- 버전: `{installed}` → `{target}`
- 교체, 병합, 충돌 해소한 파일 수와 설정 변환
- "필요한 조치"가 있었다면 그 처리 결과
- 다음 할 일: 바뀐 `.claude`를 확인하고 커밋한다
