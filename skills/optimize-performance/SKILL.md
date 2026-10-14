---
name: optimize-performance
description: |
  지정한 API의 성능을 측정, 진단하고 개선 기법을 근거와 함께 제시한다. 기법 선택과 부하 테스트 실행은 호출자가 한다.
  Trigger: "/optimize-performance {엔드포인트}", "이 API 성능 개선하자", "느린 API 최적화하자"
  Do NOT use for: 구현 계획 수립(→ write-plan), 계획 기반 구현(→ implement), 동시성 문제 재현(→ fix-concurrency)
  Boundary: 측정 설계, 관측 결과 정리, 기법 제시, 호출자와의 설계 협의, 확정된 설계의 적용, 기록까지 수행한다. 부하 테스트와 DB 조회 실행은 호출자가 직접 한다. 무엇이 병목인지, 어떤 기법을 쓸지, 어떻게 설계할지는 스킬이 단독으로 정하지 않는다.
argument-hint: "{HTTP 메서드} {경로} [, 다음 대상...]"
allowed-tools: Read, Grep, Glob, Edit, Write, Skill, Bash(*/.claude/scripts/harness *), Bash(.claude/scripts/harness *)
model: opus
effort: xhigh
---

# 성능 최적화

각 단계에서 확인할 항목을 호출자에게 안내하고, 관측된 사실을 정리해 제시한다.
무엇이 병목인지, 어떤 기법을 채택할지, 어떤 트레이드오프를 감수할지는 호출자가 판단한다.
확정된 설계를 코드에 반영하는 것은 스킬이 한다.

애플리케이션 기동, k6 부하 테스트, 데이터베이스 쿼리는 호출자가 직접 실행한다. 스킬은 명령어와 입력 형식을 제시하고 결과 파일을 읽는다.
측정 원본을 가공본으로 바꾸는 일, 전후 델타, 진행 상태 갱신은 `harness`의 `perf-*` 명령이 한다. 손으로 옮겨 쓰지 마라.

대상 API: $ARGUMENTS

## 주입된 맥락

!`"${CLAUDE_PROJECT_DIR}/.claude/scripts/harness" skill-context optimize-performance`

- `cli`: harness 경로. 본문과 phase 파일의 `harness`는 이 경로로 부른다
- `branch`, `issue`: 현재 브랜치와 이슈 번호. `issue`가 null이면 Phase 1의 1에서 확보한다
- `perf`: `.claude/harness.json`의 `perf` 절 (DB 종류, 컨테이너, 접속, 앱 URL, 프로파일, 토큰, 지표 이름)
- `perfConfigured`: false면 `perf` 절이 채워지지 않은 것이다. Phase 2 전에 호출자와 채운다. `perfErrors`에 사유가 있으면 먼저 고친다
- `perfDir`, `targets`: 이 이슈의 측정 디렉토리와 대상별 `record.md`, 진행 상태(`progress.next`가 재개 지점)
- `shared`: 이슈 공용 `seeds.sql`, `tokens.json`(건수, 만료 시각, 만료 여부)
- `seedModules`, `seedsReadme`: 프로젝트 시드 모듈 (`perf.seedsDir`)
- `stackGuide`: `perf.db`에 해당하는 stacks 파일. DB별 명령과 함정은 여기에만 있다. null이면 `perf.db`가 비었거나 지원하지 않는 값이다
- `perfEnv`, `scripts`: 측정 셸 환경과 공용 스크립트(`jvm-sampler.sh`, `mint-tokens.sh`) 경로

## 역할 경계

경계는 **사실이냐 판단이냐**다.

| 스킬이 한다 (사실) | 호출자가 한다 (판단) |
|---|---|
| 수치 재구성 (요청당 호출 수, 총 시간 비중, 전후 델타). 계산은 `perf-*` 명령에 맡긴다 | 병목이 어디인지 |
| 쿼리 원문을 리포지토리 메서드에 매핑 | 그 쿼리가 왜 느린지 |
| 실행계획 노드별 수치를 표로 정리 | 어느 노드가 문제인지 |
| 기법 선택지와 각각의 비용을 나열 | 어떤 기법을 쓸지 |
| 확정된 설계를 코드에 반영 | 설계의 각 항목을 무엇으로 정할지 |

관측 자료를 해석하는 Phase(1, 4, 6, 8)와 설계를 정하는 Phase(5)는 아래 순서를 지킨다.

1. 관측 자료를 먼저 전부 펼친다. 자료에 해석을 섞지 마라.
2. 호출자에게 해석(또는 설계)을 묻고 **답을 기다린다.** 결론을 먼저 말하지 마라.
3. 답이 오면 타당성을 판정한다. 타당하면 왜 타당한지 한 줄로 확인하고, 아니면 관측값으로 반례를 든다.
4. 호출자가 모르겠다고 하거나 답이 막히면, 그때 답과 근거를 제시한다.

## 측정 스택

DB별 관측 수단(쿼리 통계, 실행계획, 작업량 카운터, 통계 갱신, 접속 옵션)과 함정은 `stackGuide` 파일에 있다.
다른 DB의 관측 방법을 옮겨 쓰지 마라. phase 파일은 "stacks 파일의 X 절"로만 가리킨다.

공통 수단:

| 목적 | 수단 |
|---|---|
| 부하 | k6 (`template/k6-script-template.js`) |
| 쿼리 통계 가공 | `harness perf-query-stats` |
| k6 요약 가공, 직전 대비 델타 | `harness perf-summarize-k6` |
| 전후 비교 표 | `harness perf-delta` |
| JVM, 커넥션 풀, 캐시, Redis | Actuator Prometheus 주기 샘플링 (`.claude/scripts/perf/jvm-sampler.sh`) |
| 측정용 토큰 | 서명키로 직접 발급 (`.claude/scripts/perf/mint-tokens.sh`) |

**셸 환경.** 호출자가 새 터미널마다 한 번 source한다. 경로 변수, 서명키, DB 접속 함수(`dbq`)와 측정 함수가 여기서 정의된다.
값은 전부 `perf` 설정에서 읽는다. 명령 블록에 접속 정보나 정의를 다시 적지 마라.

```bash
source .claude/skills/optimize-performance/template/perf-env.sh {이슈번호} {슬러그}
```

Phase 4, 6, 8이 제시하는 명령은 `template/commands.md`에 있다. phase 파일에 옮겨 적지 않는다.

## 진입 지시

1. 주입된 `issue`와 `targets`를 본다. `issue`가 null이면 Phase 1부터 시작한다.
2. `targets`가 여러 개면 목록(슬러그, `progress.next`)을 보고하고 어느 대상을 이어서 할지 묻는다. `$ARGUMENTS`가 그중 하나를 가리키면 그 대상이다.
   - 대상을 정했으면 `progress.next.file`을 Read해 그 Phase부터 재개한다. `record.md` 전체를 읽지 마라.
   - `progress.memo`가 `없음`이 아니면 `record.md`의 **재개 메모** 절만 Read(`limit: 40`)한다.
   - `progress.next.choices`가 있으면 마지막 사이클이 끝난 상태다. 그 사이클 **판정**을 읽고 Phase 5(다음 사이클)와 Phase 9 중 무엇으로 갈지 호출자에게 확인한다.
   - 대상이 없거나 `$ARGUMENTS`가 새 대상이면 `phases/phase-1-target.md`부터 시작한다.
3. Phase 간 이동은 현재 phase 파일의 **다음 Phase 조건**을 따른다.
4. 각 Phase를 마치면 진행 상태를 갱신한다. 이 표가 유일한 상태 저장소다. 별도 state 파일을 두지 마라.

   ```bash
   harness perf-progress {record.md 경로} --phase {N} --mark ✅      # 건너뛰면 ⏭️, Phase 5~8은 --cycle {n}
   ```

   재개할 때 먼저 알아야 할 사실(환경 변화, 철거된 시드, 미결 사항)은 **재개 메모**에 적는다 (`--memo`, 여러 줄이면 Edit).

## 대상 진행 규칙

엔드포인트가 여러 개 주어져도 **한 번에 한 API씩** 진행한다. 한 대상이 Phase 9까지 끝난 뒤 다음 대상의 Phase 1로 간다.
측정 자원이 전부 하나뿐이라서다.

| 자원 | 동시에 진행하면 생기는 일 |
|---|---|
| 쿼리 통계 (인스턴스 전역) | 리셋 없이 다음 대상을 재면 섞인다. `per_req` 분모가 그 대상의 요청 수이므로 요청당 쿼리 수가 틀린다 |
| 애플리케이션 인스턴스 | 한 대상에 기법을 적용해 재기동하면, 아직 재지 않은 대상의 `-0`이 원본이 아니게 된다 |
| 커넥션 풀, DB, 버퍼 | 부하가 겹치면 경합과 워밍업 상태가 섞인다 |

하지 않는 것: 여러 대상의 디렉토리와 `record.md`를 한꺼번에 만드는 것, 한 측정 구간에 여러 대상의 부하를 잇는 것,
한 대상의 Phase 7을 적용한 뒤 기준선 없는 다른 대상을 재는 것, 결과를 모아 한 번에 보고하는 것.

이슈 공용 산출물(`seeds.sql`, `tokens.json`)만 예외로 공유한다.
남은 대상 목록은 호출자가 쥔다. Phase 1에서 "나머지는 이 대상이 끝난 뒤"라고 알리고 Phase 9에서 다시 확인한다.

## 산출물 규약

이슈 하나당 디렉토리 하나, 대상 엔드포인트 하나당 그 아래 하위 디렉토리 하나. 루트는 `perf.resultsDir`(기본 `.claude/resources/perf`)다.

```
.claude/resources/perf/
├── seeds/                           # 프로젝트 시드 모듈 (perf.seedsDir, 이슈를 넘어 재사용)
└── {이슈번호}/
    ├── seeds.sql                    # 이슈 공용 (변수 블록만)
    ├── tokens.json                  # 이슈 공용 (gitignore 대상)
    └── {엔드포인트-슬러그}/
        ├── record.md
        ├── test-script.js
        ├── k6-test-summary-{n}.json
        ├── jvm-metrics-{n}.md
        ├── query-stats-summary-{n}.md
        └── query-plan-{n}.txt
```

| 파일 | 만드는 Phase | 만드는 주체 |
|---|---|---|
| `seeds/*.sql` | 3-A (모듈이 없을 때만) | 스킬, `template/seeds/README.md` 규칙 |
| `seeds.sql` | 3-A (시드가 필요한 경우만) | 스킬, 모듈 변수 블록 |
| `tokens.json` | 4 | `mint-tokens.sh` |
| `record.md` | 1 | 스킬, `template/PERF-template.md` |
| `test-script.js` | 3-B | 스킬, `template/k6-script-template.js` |
| `k6-test-summary-{n}.json` | 4, 8 | `harness perf-summarize-k6` |
| `jvm-metrics-{n}.md` | 4, 8 | `jvm-sampler.sh summarize` |
| `query-stats-summary-{n}.md` | 4, 8 | `harness perf-query-stats` + 스킬이 출처, 하는 일 칸만 채움 (`template/query-stats-template.md`) |
| `query-plan-{n}.txt` | 6, 8 | `perf_capture_plan` 원본 그대로 |

템플릿 상단의 **작성 규칙**이 그 산출물의 작성 기준이다. phase 파일에 규칙을 중복해 적지 마라.
Read와 Write의 대상 경로에는 셸 변수가 통하지 않는다. 전체 경로를 쓴다.

**`{n}`은 사이클 번호가 아니라 코드의 상태 번호다.** 0은 아무것도 적용하지 않은 원본, n은 사이클 n까지 적용한 상태.
사이클 n의 개선 전 자료는 `-{n-1}`, 개선 후 자료는 `-{n}`이다.

측정 산출물의 형태:

- 비교 대상이 되는 출력은 전부 파일로 남기고 스킬은 그 파일과 `perf-*` 명령의 JSON을 읽는다. 터미널 출력을 붙여넣게 하지 마라.
- `k6-test-summary-{n}.json`과 `query-stats-summary-{n}.md`는 **가공본**이다. 1차 출력(`k6-raw-{n}.json`, `query-stats-raw-{n}.*`)은 변환이 성공하면 지운다.
  원문 없이는 재현할 수 없는 것(쿼리 원문)은 가공본에 들어 있다. 변환이 실패하면 1차 출력이 남으니 원인을 본 뒤 다시 변환한다.
- `query-plan-{n}.txt`는 **원본 그대로** 둔다. 노드 트리 전체가 근거다. 가공은 대화의 표로만 한다.
- `jvm-metrics-{n}.md`는 스크립트가 만든 완성본이다. 스킬은 읽기만 하고 다시 쓰지 않는다.
  캐시, Redis, 리포지토리 구획은 측정 구간에 증분이 있을 때만 나타난다. 구획이 없다는 것은 그 대상이 거기 닿지 않았다는 관측이다.
- 수치를 임의로 반올림하지 마라. 앞선 상태의 파일을 덮어쓰지 마라.
- `record.md`에는 원본을 옮기지 않고 해석과 판정만 적는다. 표의 수치는 `perf-delta` 출력에서 가져온다.
- 일회성 조회(행 수 확인, 테이블 정의, 인덱스 목록)는 파일로 남기지 않는다.

## Phase 인덱스

| Phase | 파일 | 요약 |
|---|---|---|
| 1 | `phases/phase-1-target.md` | 이슈와 브랜치 확보, 엔드포인트 확정, 실행 경로와 예상 쿼리, `record.md` 생성 |
| 2 | `phases/phase-2-environment.md` | 측정 프로파일, 셸 환경, 히스토그램, 쿼리 통계, 캐시 제어, 스키마 드리프트 점검 **(게이트)** |
| 3 | `phases/phase-3-dataset.md` | 3-A 데이터 규모와 카디널리티, 시드 (이슈 공용) / 3-B 부하 조건, k6 스크립트 (대상별) |
| 4 | `phases/phase-4-baseline.md` | 기준선 측정 결과를 가공해 제시, 호출자가 병목을 판정 |
| 5 | `phases/phase-5-design.md` | 기법 제시 → 호출자 선택 → 설계 협의 **(게이트)** |
| 6 | `phases/phase-6-snapshot.md` | 개선 전 지표와 실행계획 캡처 |
| 7 | `phases/phase-7-apply.md` | 확정한 설계 그대로 적용 (기법 하나), 테스트, 재기동 |
| 8 | `phases/phase-8-verify.md` | 동일 조건 재측정, 종료 판정 |
| 9 | `phases/phase-9-report.md` | 결과 보고, 산출물 정리 |

분기:

- Phase 1~4는 대상당 1회, Phase 5~8은 사이클마다 반복한다.
- 3-A: 이번 대상의 쿼리가 읽는 **모든 테이블**이 목표 규모와 카디널리티를 충족하면 건너뛴다.
  다른 대상이 시드를 돌렸다는 사실만으로 건너뛰지 마라. 앞선 대상이 다루지 않은 테이블이 있으면 그 테이블만 추가로 채운다.
- 3-B: 2회차 이상이고 스크립트가 있으면 건너뛴다.
- Phase 8: 종료 판정이거나 호출자가 종료를 선택 → Phase 9. 계속 → Phase 5 (사이클 번호 +1).
- Phase 9 뒤 같은 이슈에 다른 대상이 남아 있으면 → Phase 1 (새 슬러그 디렉토리).
