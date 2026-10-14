## 테스트 완료 - {{target}}

- **실행**: {{command}} - 통과 {{passed}}, 실패 {{failedCount}}, skip {{totals.skipped}} ({{durationSec}}초)
- **작성**: {{written}}
{{#failed}}- **실패**: `{{test}}` - {{cause}}
{{/failed}}{{^failed}}- **실패**: 없음
{{/failed}}{{#buildErrors.0}}- **빌드 오류**: 로그 `{{log}}`
{{/buildErrors.0}}- **다음**: `{{next}}`
