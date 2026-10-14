#!/bin/bash
# PreToolUse(Edit|Write) 훅: 하네스 설정 자기보호
# 훅, 권한 설정, 하네스 설정을 고치려는 시도를 사용자 확인으로 올린다.
# 이 훅이 없으면 세션 중 Edit 한 번으로 다른 안전장치가 모두 무력화될 수 있다.

command -v jq >/dev/null 2>&1 || { echo "차단됨: jq가 없어 안전 검사를 수행할 수 없습니다." >&2; exit 2; }

FILE_PATH=$(jq -r '.tool_input.file_path // empty')
[ -z "$FILE_PATH" ] && exit 0

if printf '%s' "$FILE_PATH" | grep -qE '\.claude/(hooks|scripts)/|\.claude/settings(\.local)?\.json$|\.claude/harness\.json$'; then
  jq -n '{hookSpecificOutput: {hookEventName: "PreToolUse", permissionDecision: "ask", permissionDecisionReason: "훅, 권한, 하네스 설정 변경은 사용자 승인이 필요합니다."}}'
fi

exit 0
