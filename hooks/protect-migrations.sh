#!/bin/bash
# PreToolUse(Edit|Write) 훅: 배포됐을 수 있는 Flyway 마이그레이션 수정 차단
# 설정의 migration.immutableRef(기본 origin/main)에 이미 있는 파일만 불변으로 본다.
# 이 브랜치에서 새로 만든 V파일은 자유롭게 고칠 수 있다.
# exit 0 = 허용, permissionDecision "deny" = 차단

command -v jq >/dev/null 2>&1 || { echo "차단됨: jq가 없어 안전 검사를 수행할 수 없습니다." >&2; exit 2; }

HARNESS="$(cd "$(dirname "${BASH_SOURCE[0]}")/../scripts" && pwd)/harness"

INPUT=$(cat)
TOOL_NAME=$(printf '%s' "$INPUT" | jq -r '.tool_name // empty')
FILE_PATH=$(printf '%s' "$INPUT" | jq -r '.tool_input.file_path // empty')

[ -z "$FILE_PATH" ] && exit 0
# .sql이 아니면 설정을 읽을 필요도 없다
case "$FILE_PATH" in *.sql) ;; *) exit 0 ;; esac

MIG_DIR=$(python3 "$HARNESS" get-config migration.dir 2>/dev/null)
MIG_PATTERN=$(python3 "$HARNESS" get-config migration.filePattern 2>/dev/null)
REF=$(python3 "$HARNESS" get-config migration.immutableRef 2>/dev/null)
: "${MIG_DIR:=src/main/resources/db/migration}" "${MIG_PATTERN:=V[0-9]+(_[0-9]+)*__.*\.sql}" "${REF:=origin/main}"

printf '%s' "$FILE_PATH" | grep -qF "$MIG_DIR/" || exit 0
basename "$FILE_PATH" | grep -qE "^${MIG_PATTERN}$" || exit 0

deny() {
  jq -n --arg reason "$1" \
    '{hookSpecificOutput: {hookEventName: "PreToolUse", permissionDecision: "deny", permissionDecisionReason: $reason}}'
  exit 0
}

# 기존 파일을 고치는 경우만 문제다. Write로 새 파일을 만드는 것은 허용한다
conservative() {
  [ "$TOOL_NAME" = "Edit" ] && deny "$1"
  [ "$TOOL_NAME" = "Write" ] && [ -f "$FILE_PATH" ] && deny "$1"
  exit 0
}

# CLAUDE_PROJECT_DIR가 비면 상대 경로 계산과 git 대상이 어긋나 그대로 통과(fail-open)할 수 있다
[ -z "$CLAUDE_PROJECT_DIR" ] && conservative "CLAUDE_PROJECT_DIR가 비어 있어 $REF 기준을 확인할 수 없어 보수적으로 차단합니다."

REL="${FILE_PATH#"$CLAUDE_PROJECT_DIR"/}"

git -C "$CLAUDE_PROJECT_DIR" rev-parse --verify -q "$REF" >/dev/null 2>&1 \
  || conservative "$REF 기준을 확인할 수 없어 기존 마이그레이션 수정을 보수적으로 차단합니다."

if git -C "$CLAUDE_PROJECT_DIR" cat-file -e "$REF:$REL" 2>/dev/null; then
  deny "이미 $REF에 있는(배포됐을 수 있는) Flyway 마이그레이션은 수정할 수 없습니다. 새 버전 파일을 추가하세요."
fi

exit 0
