# 변경 기록

릴리스할 때 `VERSION`을 올리고 이 파일 맨 위에 절을 추가한 뒤 `v{버전}` 태그를 단다.
`/update-harness`가 설치된 버전과 새 버전 사이의 절을 읽어 보여준다.

```
## {버전} - {YYYY-MM-DD}
### 바뀐 것
- {스킬이나 파일}: {무엇이 왜 바뀌었는지}
### 영향 받는 파일
- {경로}
### 필요한 조치
- 없음 (또는: 사용자가 해야 할 일. 설정 형식 변환은 harness_update.MIGRATIONS에 넣고 여기에 자동 변환됨이라고 적는다)
```

## 0.1.0 - 2026-10-14
### 바뀐 것
- 첫 릴리스. 파이프라인 스킬 7개와 dev-pipeline, optimize-performance, fix-concurrency, conversation-feedback
- init-harness, update-harness
- harness CLI, 안전장치 훅, 하네스 업데이트 확인 훅
### 영향 받는 파일
- 전체
### 필요한 조치
- 없음
