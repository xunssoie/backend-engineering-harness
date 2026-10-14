## 커밋, 푸시 완료

| 커밋 | 메시지 |
|------|--------|
{{#done}}| `{{hash}}` | `{{message}}` |
{{/done}}

{{#pushed}}- **푸시**: `{{branch}}` → `origin/{{branch}}` ({{pushMode}})
{{/pushed}}{{^pushed}}- **푸시**: 하지 않음
{{/pushed}}{{#leftover}}- **남은 변경**: `{{.}}`
{{/leftover}}- **다음**: `{{next}}`
