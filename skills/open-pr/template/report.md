## PR 생성 완료

- **PR**: [#{{number}}]({{url}}){{#existing}} (이미 열려 있던 PR){{/existing}}
- **대상**: `{{base}}` ← `{{branch}}`
- **라벨**: {{labelText}}
- **이슈 연결**: {{#closesIssue}}#{{issue}} 머지 시 자동 닫힘{{/closesIssue}}{{^closesIssue}}연결 안 됨 - 머지 후 직접 닫아야 함{{/closesIssue}}
- **다음**: `{{next}}`
