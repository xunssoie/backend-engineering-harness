---
name: fix-concurrency
description: |
  동시 요청에서 깨지는 불변식을 재현하고, 동시성 제어 기법을 비교 검증해 하나를 채택한다. 기법 선택과 부하 실행은 호출자가 한다.
  Trigger: "/fix-concurrency {대상}", "동시성 문제 해결하자", "초과 판매 막자", "정원 초과 막자", "락 걸어서 정합성 맞추자", "lost update 고치자"
  Do NOT use for: 응답시간, 처리량이 목표인 성능 개선(→ optimize-performance), 구현 계획 수립(→ write-plan)
  Boundary: 불변식 정의, 경합 재현, 기법 제시, 후보별 적용과 검증, 채택 결정 기록까지 수행한다. 부하 테스트와 데이터베이스 조회 실행은 호출자가 직접 한다. 어떤 기법을 채택할지는 스킬이 단독으로 정하지 않는다.
argument-hint: "[대상 임계 구역 | 슬러그]"
allowed-tools: Read, Grep, Glob, Edit, Write, Skill, Bash(git *), Bash(gh *), Bash(*/.claude/scripts/harness *), Bash(.claude/scripts/harness *)
model: opus
effort: xhigh
---

# 동시성 결함 해결

각 단계에서 확인할 항목을 호출자에게 안내하고, 관측된 사실을 정리해 제시한다.
무엇을 불변식으로 볼지, 어떤 기법을 채택할지, 어떤 트레이드오프를 감수할지는 호출자가 판단한다.

애플리케이션 기동, k6 부하 테스트, 데이터베이스 쿼리는 호출자가 직접 실행하며, 명령어와 입력 형식을 제시하고 결과를 받는다.
측정 원본의 가공(요약, 증가분, 비교표, 진행 상태 갱신)은 스킬이 `harness conc-*` 명령으로 한다. 손으로 계산하지 마라.

대상: $ARGUMENTS

## 주입된 맥락

!`"${CLAUDE_PROJECT_DIR}/.claude/scripts/harness" skill-context fix-concurrency`

- `cli`: harness 경로. 본문의 `harness`는 이 경로로 부른다
- `branch`, `issue`: 현재 브랜치와 이슈 번호
- `targets`: 이 이슈의 진행 중인 대상(`slug`, `record`, `resume`). `resume`이 재개 지점이다
- `db`, `stack`: DB 종류와 측정 명령 파일. `stack`이 null이면 `.claude/harness.json`의 `perf` 절부터 채우게 한다
- `concurrency`: 측정 설정(프로파일, 기동 명령, 헬스 URL, 표본 수집). 인증 헤더는 `perf.tokens`에 있다
- `dbFunction`: `dbc` 함수 정의의 형태. 비밀번호 자리는 `***`로 가려져 있으니 그대로 붙여넣지 말고, 호출자 블록 첫 줄에는 `eval "$(harness conc-shell ...)"`을 쓴다
- `seedModules`, `issueShared`, `profileFileExists`, `tools`: 시드 모듈, 이슈 공용 파일, 측정 프로파일, k6/docker/jq/lsof 유무

## optimize-performance와의 경계

두 스킬은 **1급 지표가 다르다.** 골격은 닮았지만 판정 기준이 정반대라 섞어 쓰면 안 된다.

| | optimize-performance | fix-concurrency |
|---|---|---|
| 1급 지표 | 응답시간, 처리량 | **불변식 위반 건수** |
| 2급 지표 | 읽은 행 수, 실행계획 | 처리량, 락 대기, 데드락, 재시도 |
| 부하 형태 | ramp-up 후 일정 부하 유지 | **순간 동시 폭발** |
| 개선 방향 | 빨라지면 성공 | **느려져도 정합하면 성공** |
| 사이클 성격 | 기법을 **누적** 적용 | 기법을 **하나씩 갈아끼워 비교** |
| 종료 조건 | 개선이 멈추면 종료 | 후보를 다 재고 하나를 채택하면 종료 |
| `{n}` | 누적 상태 | 후보 번호 |
| 커넥션 풀 | 측정 안정성 우선 | **VU 이상** (락 경합을 재기 위해) |

동시성 제어는 거의 항상 처리량을 깎는다. **처리량이 떨어졌다는 이유로 실패 판정을 내리지 마라.**
정합성을 얻는 대가가 얼마인지를 재는 것이 이 스킬의 목적이다.

## 동시성 개선의 틀

이 스킬의 Phase는 아래 네 단계에서 나왔다. 순서를 바꾸지 마라.

1. **불변식을 문장으로 못 박는다.** 무엇이 참이어야 하는지 정의하지 않으면 무엇이 깨졌는지도 셀 수 없다.
   "빠르게"는 정도의 문제지만 "상한을 넘지 않는다"는 참 아니면 거짓이다. (Phase 1)
2. **결함을 실제로 터뜨린다.** 재현하지 못한 결함은 고쳤는지도 확인할 수 없다.
   여기서 재현에 실패하면 그 뒤 전부가 무의미하므로 게이트로 둔다. (Phase 2, 3, 4)
3. **기법을 하나씩 갈아끼워 같은 조건으로 잰다.** 동시성 제어 기법은 서로 배타적이라 누적되지 않는다.
   비관적 락 위에 낙관적 락을 얹지 않는다. (Phase 5, 6, 7)
4. **비교표를 놓고 하나를 채택한다.** 채택 근거가 남아야 개선이 완결된다. (Phase 8)

## 지표 체계

**정합성 지표 (1급).** 하드웨어와 무관하다. 이 값들로만 성공을 판정한다.

| 지표 | 정의 | 성공 기준 |
|---|---|---|
| 불변식 위반 건수 | Phase 1에서 정의한 각 불변식이 깨진 횟수 | 0 |
| 카운터 정합 오차 | 집계 칼럼 값 - 실제 행 수 | 0 |
| 응답 코드 분포 | 성공 수와 실패 코드별 건수 | 성공 수 = 허용 상한, 실패는 의도한 코드 |

카운터 정합 오차는 lost update의 **직접 증거**다. 위반 건수가 0이어도 이 값이 0이 아니면 갱신이 유실된 것이다.

**경합 지표 (2급).** 정합성을 얻은 대가를 잰다. 채택 근거가 되지만 성공 판정 기준은 아니다.
구체 수단은 `stack` 파일의 **측정 구간 락 카운터** 절에 있다.

| 지표 | MySQL | PostgreSQL |
|---|---|---|
| 처리량, 지연 | k6 요약의 RPS, p95, p99 | 같음 |
| 락 대기 | `Innodb_row_lock%` 증가분 | 서버 로그 `still waiting for` 건수 (`log_lock_waits`) |
| 데드락 | `INNODB_METRICS`의 `lock_deadlocks` 증가분 | `pg_stat_database.deadlocks` 증가분 |
| 락 타임아웃 | `INNODB_METRICS`의 `lock_timeouts` 증가분 | 서버 로그 `lock timeout` 건수 |
| 대기 중인 락 (실시간) | `performance_schema.data_lock_waits` 표본 | `pg_locks WHERE NOT granted` 표본 |
| 최근 데드락 원문 | `SHOW ENGINE INNODB STATUS`의 `LATEST DETECTED DEADLOCK` | 서버 로그 `deadlock detected` |
| 재시도 횟수 | 낙관적 락일 때만. 애플리케이션 카운터로 센다 | 같음 |

**동시성 측정에서만 걸리는 것** (DB별 세부는 `stack` 파일의 같은 이름 절)

- **격리 수준이 결과를 바꾼다.** 기본값이 MySQL은 `REPEATABLE READ`, PostgreSQL은 `READ COMMITTED`다.
  낙관적 락의 재시도 빈도, `FOR UPDATE`의 범위, 동시 UPDATE의 재평가 방식이 달라지므로 측정 시작 전에 값을 확인해 기록한다.
- **락 카운터는 인스턴스 전역 누적값이다.** 부하 직전과 직후에 각각 읽어 차이를 낸다. 리셋 명령에 의존하지 마라.
- **커넥션 풀이 VU보다 작으면 락 경합이 아니라 커넥션 대기를 재게 된다.** 동시성 측정에서는
  풀 크기를 VU 이상으로 올린다. optimize-performance의 풀 크기 규칙을 그대로 가져오지 마라.
- **DB 버퍼 캐시는 재기동 없이 비울 수 없다.** 캐시 상태는 warm으로 통일한다.

## 후보 진행 규칙

**기법 후보는 한 번에 하나만 코드에 올린다.** 후보 하나가 Phase 7까지 끝나면 **반드시 되돌린 뒤** 다음 후보의 Phase 5로 간다.

| 하지 마라 | 이유 |
|---|---|
| 두 기법을 함께 적용해서 재는 것 | 어느 쪽 효과인지 분리되지 않는다 |
| 앞 후보를 되돌리지 않고 다음 후보를 얹는 것 | 비교 기준이 원본이 아니게 된다 |
| 후보마다 부하 조건을 바꾸는 것 | 후보 간 비교가 성립하지 않는다 |
| 데이터를 되돌리지 않고 재측정하는 것 | 앞 측정이 남긴 행과 카운터가 그대로 섞인다 |

**되돌리기는 코드와 데이터 둘 다다.** 하나만 되돌리면 다음 후보의 측정이 오염된다.

| 대상 | 되돌리는 법 | 확인 |
|---|---|---|
| 코드 | `git stash` 또는 후보 적용 전 커밋으로 복원 | `git status`가 깨끗한가 |
| 데이터 | Phase 3에서 확정한 `reset.sql` | 마지막 SELECT의 `reset_ok`가 1인가 |

**모든 후보의 비교 대상은 항상 `-0`이다.** `harness conc-compare`가 이 규칙대로 표를 만든다.
조건(VU, 상한, 대상)이 `-0`과 다르면 `warnings`로 짚는다. 경고가 있으면 해석 전에 호출자에게 알린다.

## 명령 전달

호출자가 실행할 명령은 **레포 루트에서 붙여넣어 바로 도는 형태**로 준다.

| 하지 마라 | 이유 |
|---|---|
| 세션 스크래치패드나 `/tmp`에 스크립트를 만들고 그리로 `cd` 시키는 것 | 경로가 길어 붙여넣기에서 잘리고, 스크립트 안의 상대 경로가 실행 위치를 따라가 전부 깨진다 |
| 실행 위치를 전제한 상대 경로 | 호출자가 어느 디렉토리에 있는지 스킬은 모른다 |
| 여러 줄로 쪼개 순서대로 치게 하는 것 | 중간에 하나가 빠져도 어디서 어긋났는지 드러나지 않는다 |
| 접속 명령을 문자열 변수로 만드는 것 | zsh가 단어 분할을 하지 않아 `command not found`가 난다. 함수(`dbc`)를 쓴다 |

- 명령은 **한 블록**으로 준다. DB를 만지는 블록은 첫 줄에 접속 함수와 변수를 정의한다.

  ```bash
  eval "$(.claude/scripts/harness conc-shell --issue {이슈번호} --slug {슬러그})"
  ```

- 파일 경로는 **레포 루트 기준 상대 경로를 전체로** 적는다
  (`.claude/resources/concurrency/{이슈번호}/{슬러그}/burst-script.js`).
- 여러 단계를 묶어야 하면 `template/`의 스크립트를 **전체 경로로 실행**하게 한다.
  스크립트는 첫머리에서 스스로 레포 루트로 이동해 실행 위치에 무관하다
  (`template/measure.sh`, `template/restart-app.sh`).
- 측정 산출물은 이슈 디렉토리에 남긴다. 세션 임시 디렉토리에 두지 마라. **기록은 세션보다 오래 남아야 한다.**

## 산출물 규약

이슈 하나당 디렉토리 하나, 대상 임계 구역 하나당 그 아래 하위 디렉토리 하나를 쓴다.
시드와 토큰은 이슈 전체가 공유하고, 시드 모듈은 프로젝트 전체가 공유한다.

```
.claude/resources/concurrency/
├── seeds/                          # 프로젝트 공용 시드 모듈
└── {이슈번호}/
    ├── seeds.sql                   # 이슈 공용 (변수 블록)
    ├── tokens.json                 # 이슈 공용 (gitignore 대상)
    └── {대상-슬러그}/
        ├── record.md
        ├── invariant-check.sql
        ├── reset.sql
        ├── burst-script.js
        ├── k6-burst-raw-{n}.json
        ├── k6-burst-summary-{n}.json
        ├── invariant-{n}.txt
        └── lock-stats-{n}.txt
```

| 파일 | 위치 | 만드는 Phase | 만드는 주체 | 템플릿 |
|---|---|---|---|---|
| `seeds/*.sql` | 프로젝트 | 3 (없을 때만) | 스킬 | `template/seeds/README.md` |
| `seeds.sql` | 이슈 | 3 | 스킬 | `template/seeds/README.md` |
| `tokens.json` | 이슈 | 3 | 호출자 | `.claude/scripts/perf/mint-tokens.sh` |
| `record.md` | 대상 | 1 | 스킬 | `template/CONCURRENCY-template.md` |
| `invariant-check.sql` | 대상 | 1 | 스킬 | `template/invariant-check.sql` |
| `reset.sql` | 대상 | 3 | 스킬 | `template/reset.sql` |
| `burst-script.js` | 대상 | 3 | 스킬 | `template/k6-burst-template.js` |
| `k6-burst-raw-{n}.json` | 대상 | 4, 7, 8 | 호출자 (`measure.sh`) | - |
| `invariant-{n}.txt` | 대상 | 4, 7, 8 | 호출자 (`measure.sh`) | `invariant-check.sql`의 출력 |
| `lock-stats-{n}.txt` | 대상 | 4, 7, 8 | 호출자 (`measure.sh`) | - |
| `k6-burst-summary-{n}.json` | 대상 | 4, 7, 8 | 스킬 (`harness conc-burst-summary`) | - |

`invariant-check.sql`은 Phase 1에서 한 번 만들고 **이후 고치지 않는다.**
후보마다 검증 기준이 달라지면 전후 비교가 성립하지 않는다.

템플릿 상단의 **작성 규칙**이 해당 산출물의 작성 기준이다. phase 파일에 규칙을 중복해 적지 마라.

Read와 Write의 대상 경로에는 셸 변수가 통하지 않는다. 전체 경로를 쓴다.

### 파일명의 `{n}`

`{n}`은 **후보 번호**다. optimize-performance와 달리 누적 상태가 아니다.

| n | 상태 |
|---|---|
| 0 | 아무 제어도 없는 원본. 결함이 재현되는 상태 |
| 1 | 후보 1만 원본 위에 단독 적용한 상태 |
| 2 | 후보 1을 되돌리고 후보 2만 단독 적용한 상태 |
| final | 채택안을 되살린 뒤 최종 확인 |

**모든 후보의 비교 대상은 항상 `-0`이다.** `-{n-1}`이 아니다.

### 측정 산출물의 형태

- 비교 대상이 되는 측정 출력은 전부 파일로 남기고, 스킬은 그 파일을 Read하거나 `harness conc-*`로 읽는다.
  터미널 출력을 붙여넣게 하지 마라.
- `invariant-{n}.txt`, `lock-stats-{n}.txt`, `k6-burst-raw-{n}.json`은 **원본 그대로 둔다.** 요약으로 대신하지 마라.
- `k6-burst-summary-{n}.json`은 `harness conc-burst-summary`가 원본에서 만든다. 최상위 `delta_vs_base`도 이 명령이 붙인다.
- 증가분, 구간 평균, 비교표는 `harness conc-lock-delta`, `harness conc-compare`의 출력을 옮긴다. 손으로 빼거나 나누지 마라.
- 수치를 임의로 반올림하지 마라. 명령이 뽑아준 자릿수를 그대로 옮긴다.
- 앞선 후보의 파일을 덮어쓰지 마라. `measure.sh`는 같은 `n`의 파일이 있으면 멈춘다.
- `record.md`에는 원본을 옮겨 적지 않고 판정에 쓴 값과 해석만 적는다.

## 분석 주도 규칙

관측 자료의 해석은 호출자가 한다. 이 규칙은 Phase 1, 4, 5, 7, 8에 적용된다.

1. 관측 자료를 먼저 전부 펼친다. 자료에 해석을 섞지 마라.
2. 호출자에게 해석을 묻고 **답을 기다린다.** 결론을 먼저 말하지 마라.
3. 답이 오면 타당성을 판정한다. 타당하면 왜 타당한지 한 줄로 확인하고, 아니면 관측값으로 반례를 든다.
4. 호출자가 모르겠다고 하거나 답이 막히면, 그때 답과 근거를 제시한다.

| 스킬이 한다 (사실) | 호출자가 한다 (판단) |
|---|---|
| 임계 구역과 공유 자원을 코드에서 짚어 제시 | 무엇을 불변식으로 삼을지 |
| 위반 건수, 카운터 오차, 락 지표를 표로 정리 | 그 위반이 어떤 인터리빙에서 났는지 |
| 기법 선택지와 각각의 비용을 나열 | 어떤 기법을 채택할지 |
| 후보 간 비교표 구성 | 어떤 트레이드오프를 감수할지 |

## 진입 지시

1. 주입된 `issue`를 쓴다. null이면 Phase 1의 1번(이슈 확보)부터 간다.
2. `targets`로 진행 중인 대상을 본다.
   - 대상이 여러 개면 목록(`slug`, `resume`)을 보고하고 어느 대상을 이어서 할지 호출자에게 묻는다.
     `$ARGUMENTS`가 그중 하나를 가리키면 그 대상으로 간다.
   - 대상을 정했으면 그 대상의 `resume`이 가리키는 Phase 파일을 Read해 그 지점부터 재개한다. `record.md` 전체를 읽지 마라.
     필요한 절만 Read한다.
     - `resume.phase`가 `"5|8"`이면 다음 후보(`resume.candidate`)를 잴지, 채택으로 갈지 호출자에게 묻는다.
     - `resume.done`이 true면 그 대상은 끝났다. 새 대상인지 확인한다.
   - 해당하는 대상이 없거나 `$ARGUMENTS`가 새 대상이면 `phases/phase-1-invariant.md`부터 시작한다.
3. Phase 간 이동은 항상 현재 phase 파일의 **다음 Phase 조건**을 따른다.
4. 각 Phase를 마치면 진행 상태를 갱신한다. 표를 Edit로 고치지 마라.

   ```bash
   harness conc-progress {record.md 경로} --phase {N} --mark ✅              # Phase 1~4, 8
   harness conc-progress {record.md 경로} --phase {N} --mark ✅ --candidate {n} [--technique "{기법}"]   # Phase 5~7
   ```

   이 표가 유일한 상태 저장소다. 별도 state 파일이나 인덱스 파일을 두지 마라.

## Phase 인덱스

| Phase | 파일 | 한 줄 요약 |
|---|---|---|
| 1 | `phases/phase-1-invariant.md` | 이슈와 브랜치 확보, 임계 구역 특정, **불변식 정의**, `record.md` 생성 |
| 2 | `phases/phase-2-environment.md` | 측정 프로파일, 커넥션 풀, 격리 수준, 락 관측 수단 점검 **(게이트)** |
| 3 | `phases/phase-3-contention.md` | 경합 조건 확정, 시드, 토큰, k6 폭발 스크립트, 되돌리기 SQL |
| 4 | `phases/phase-4-reproduce.md` | 결함 재현. 위반이 관측되지 않으면 진행 불가 **(게이트)** |
| 5 | `phases/phase-5-design.md` | 기법 후보 제시 → 호출자가 선택 → 설계 확정 **(게이트)** |
| 6 | `phases/phase-6-apply.md` | 후보 하나를 원본 위에 단독 적용, 제어가 실제로 걸렸는지 확인 |
| 7 | `phases/phase-7-verify.md` | 동일 조건 재측정, 정합성 1급, 경합 2급으로 판정, 코드와 데이터 되돌리기 |
| 8 | `phases/phase-8-adopt.md` | 후보 비교표, 채택 결정, 최종 반영, 보고 |

**분기 요약**

- Phase 1~4는 대상당 1회 수행한다. Phase 5~7은 후보마다 반복한다.
- Phase 3: 2회차 이상이고 시드와 스크립트가 준비되어 있으면 → **Phase 4** (건너뜀)
- Phase 4: 위반이 관측되지 않으면 → **Phase 3** (경합 조건을 세게 잡고 다시)
- Phase 7: 재고 싶은 후보가 남았고 호출자가 계속을 선택 → **Phase 5** (후보 번호 +1)
- Phase 7: 호출자가 비교를 끝내기로 선택 → **Phase 8**
- Phase 8 종료 후 같은 이슈에 다른 임계 구역이 남아 있으면 → **Phase 1** (새 슬러그 디렉토리)
