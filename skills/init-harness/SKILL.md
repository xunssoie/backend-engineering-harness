---
name: init-harness
description: |
  .claude로 클론한 하네스를 현재 프로젝트에 맞게 설정한다. 프로젝트를 감지하고, 취향만 묻고, 설정과 컨벤션, 정책 문서를 만들고, 설치를 점검한다.
  Trigger: "/init-harness", "하네스 초기화", "하네스 설정 다시 하자"
  Do NOT use for: 하네스 업데이트(→ update-harness), 파이프라인 작업(→ dev-pipeline)
  Boundary: 하네스 설정, 문서 초안, 점검까지 한다. 프로덕션 코드는 고치지 않는다.
allowed-tools: Read, Write, Edit, Glob, Grep, AskUserQuestion, Agent, Bash(*/.claude/scripts/harness *), Bash(.claude/scripts/harness *), Bash(gh label *)
model: opus
effort: high
---

# 하네스 초기화

설치는 `git clone {하네스 저장소} .claude`로 이미 끝났다. 이 스킬은 그 하네스를 프로젝트에 맞춘다.
아래 `harness`는 `.claude/scripts/harness`다.

## 주입된 맥락

!`"${CLAUDE_PROJECT_DIR}/.claude/scripts/harness" --project "${CLAUDE_PROJECT_DIR}" detect-project || true`

감지 결과는 Phase 2, 3, 4에서 그대로 쓴다. 같은 내용을 다시 조사하지 않는다.

## Phase 1: 하네스 파일 전환

```bash
harness adopt
```

- 클론된 버전, 커밋, 원격 주소, 파일별 해시를 `.claude/harness-manifest.json`에 남기고, `.claude/.git`과 저장소용 파일(README, LICENSE, VERSION, CHANGELOG)을 지운다. 이후 `.claude`는 프로젝트의 일반 파일이다
- `alreadyAdopted`가 true면 이미 초기화한 프로젝트다. 재설정으로 진행한다
- `warnings`가 있으면 보고에 남긴다

## Phase 2: 질문

감지 결과로 정해지는 것(언어, 빌드, DB, 마이그레이션 경로, 브랜치 전략, 라벨, PR 섹션, Docker 필요 여부)은 묻지 않는다.
감지가 애매한 항목만 근거와 함께 확인받는다. 아래는 AskUserQuestion으로 묻는다. 권장안을 첫 옵션에 둔다.

**질문 1 (한 번에 묻는다)**

| 질문 | 옵션 | answers 키 |
|---|---|---|
| 파이프라인 구성 | 전체: 이슈, 계획, 구현, 테스트, 커밋, PR, 리뷰 반영 (권장) / 리뷰 봇 없이 / 계획 단계 없이 / 직접 고르기 | `stages`, `reviewBot` |
| 사용자 확인을 받을 지점 (복수) | 이슈 생성 전 / 계획 확정 / 커밋 전 / PR 생성 전 (모두 권장) | `gates` (`issue`, `plan`, `commit`, `pr`) |
| PR 본문 스타일 | 문제와 해결을 서술로 남기는 이력서형 (권장) / 간결형 / 템플릿만 채움 | `prStyle` |
| 커밋 attribution | Co-Authored-By 붙임 (권장) / 붙이지 않음 | `attribution` (붙이지 않으면 빈 문자열) |

**질문 2 (해당할 때만)**

| 조건 | 질문 | answers 키 |
|---|---|---|
| `build.deps`에 DB가 있다 | 성능 최적화, 동시성 스킬 사용 (사용 안 함 / 성능만 / 둘 다) | `perfSkills` (`none`, `perf`, `both`) |
| `perfSkills`가 `none`이 아니다 | 부하 테스트 도구 (k6 권장. 지금은 k6만 지원) | `loadTool` |
| `sources.domains`가 있다 | 서비스 정책 문서 초안을 지금 만들지 (지금 생성 권장 / 나중에) | `policyDrafts` |
| 계획서 | 계획서를 커밋할지 (커밋하지 않음 권장 / 커밋) | `plansTracked` |

재설정이면 현재 `.claude/harness.json` 값을 기본값으로 보여주고 바꿀 것만 묻는다.

## Phase 3: 설정 적용

1. 감지 결과를 스크래치패드 파일로 저장하고 설정을 쓴다. 계획서 커밋 여부는 `.claude/.gitignore`에 함께 반영된다

   ```bash
   harness build-config --detected {감지 결과 파일} --answers '{질문 답 JSON}' --write
   ```

2. 쓰지 않는 스킬을 지운다. 파이프라인 단계에서 뺀 스킬, `perfSkills`에 따라 optimize-performance와 fix-concurrency.
   설정의 단계 목록에서도 빠지고, 업데이트 때 다시 생기지 않는다

   ```bash
   harness remove-skill --name {스킬}
   ```

3. `prStyle`이 이력서형이 아니면 `${CLAUDE_SKILL_DIR}/variants/pr-writing-{concise|template}.md`로 `.claude/spec/pr-writing.md`를 바꾼다
4. `missingLabels`가 있으면 목록을 보여주고 만들지 묻는다. 승인하면 `gh label create {이름}`
5. 두 측정 스킬을 쓰면 `perf` 절에는 DB 종류, 부하 도구, compose 파일만 채워진다. 나머지(컨테이너, DB 이름, 토큰)는 스킬을 처음 쓸 때 채운다

## Phase 4: 문서 생성

컨벤션과 정책 문서는 코드에서 관찰한 규칙만 담는다. 작성 기준은 `${CLAUDE_SKILL_DIR}/guides/convention-writing.md`다.
재설정이면 이미 있는 문서는 건드리지 않고 없는 것만 만든다. 다시 만들지는 사용자에게 묻는다.

문서 작성은 서브에이전트(general-purpose)에 나눠 맡기고 한 메시지에 병렬로 띄운다. 각 에이전트에 넘길 것:
작성 기준 파일 경로, 감지 결과 파일 경로, 맡은 문서 목록과 출력 경로, "코드에서 근거를 찾지 못한 규칙은 쓰지 말고 확인 필요 목록으로 반환하라"

| 묶음 | 문서 |
|---|---|
| 구조, 코드 | `.claude/rules/project-structure.md`, `.claude/rules/code-convention/common.md`, 감지된 레이어마다 `.claude/rules/code-convention/{레이어}.md` |
| DB, Git | `.claude/rules/migration.md` (마이그레이션이 있을 때), `.claude/spec/git-convention.md` |
| 테스트, API 문서 | `.claude/spec/test-convention.md`, `.claude/skills/write-test/template/test-code-template.md`, `springdoc`이 있으면 `.claude/spec/api-docs-convention.md` |
| 서비스 정책 (`policyDrafts`가 지금 생성일 때) | `.claude/spec/service-policy/README.md`와 도메인 파일. 도메인이 많으면 8개씩 나눠 에이전트를 여러 개 띄운다 |

에이전트가 끝나면 `${CLAUDE_SKILL_DIR}/templates/CLAUDE.md.tmpl`로 `.claude/CLAUDE.md`를 만든다. 프로젝트 루트에 `CLAUDE.md`가 이미 있으면 덮어쓰지 않고, 템플릿의 "하네스" 절만 추가할지 묻는다.

## Phase 5: 점검

```bash
harness verify-install
```

`failed` 항목을 고친다. 고칠 수 없는 항목(gh 인증 등)은 방법과 함께 보고에 남긴다.

## Phase 6: 결과 보고

아래만 짧게 보고한다.

- 하네스 버전, 지운 스킬
- 설정 요약: base 브랜치, 활성 단계, 게이트, 테스트 명령, Docker 필요 여부
- 만든 문서 목록
- **확인 필요**: 감지가 애매했던 항목, 에이전트가 반환한 근거 없는 규칙, 점검 실패 항목
- 다음 할 일: `.claude`를 커밋한다. 이후 `/dev-pipeline {작업 설명}` 또는 단계별 스킬로 시작한다
