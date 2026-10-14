#!/bin/bash
# PreToolUse(Bash) 훅: 위험한 명령 차단, 확인 요청
#
#   DENY : 되돌릴 수 없이 파괴적인 명령 -> stderr 사유 + exit 2
#   ASK  : 맥락상 위험하지만 정당한 사용도 있는 명령 -> permissionDecision "ask"
#   exit 0(출력 없음) = 허용
#
# 재귀 삭제와 테스트 실행은 명령 문자열이 아니라 대상으로 판정해야 해서 `harness judge-command`에 맡긴다.
# settings.json의 deny 규칙은 훅의 판정과 무관하게 명령을 막는다(훅이 allow해도 뒤집히지 않는다).
# 그래서 rm 계열은 deny에 두지 않고 여기서 대상으로 판정한다.

command -v jq >/dev/null 2>&1 || { echo "차단됨: jq가 없어 안전 검사를 수행할 수 없습니다." >&2; exit 2; }

HARNESS="$(cd "$(dirname "${BASH_SOURCE[0]}")/../scripts" && pwd)/harness"

INPUT=$(cat)
COMMAND=$(printf '%s' "$INPUT" | jq -r '.tool_input.command // empty')

[ -z "$COMMAND" ] && exit 0

deny() {
  echo "차단됨: $1" >&2
  exit 2
}

ask() {
  jq -n --arg reason "$1" \
    '{hookSpecificOutput: {hookEventName: "PreToolUse", permissionDecision: "ask", permissionDecisionReason: $reason}}'
  exit 0
}

# 공백을 정규화해 "DROP   DATABASE" 같은 다중 공백 우회를 막는다
NORM=$(printf '%s' "$COMMAND" | tr '[:upper:]' '[:lower:]' | tr -s '[:space:]' ' ')

# ── DENY ────────────────────────────────────────────────

printf '%s' "$NORM" | grep -qF ':(){ :|:& };:' && deny "포크밤으로 의심되는 명령입니다."

printf '%s' "$NORM" | grep -qE '(>|of=) *\/dev\/(sd[a-z]|nvme|r?disk)' \
  && deny "디스크 디바이스에 직접 쓰는 명령입니다."

printf '%s' "$NORM" | grep -qE 'mkfs(\.| )' && deny "파일시스템을 새로 생성(mkfs)하는 명령입니다."

# force push. settings.json의 deny는 접두 매칭이라 원격 이름 뒤에 붙인 플래그를 놓친다. 여기서 토큰으로 잡는다
if printf '%s' "$NORM" | grep -qE '(^|[;&|] *)git( +-[a-z] +[^ ]+)* +push( |$)'; then
  printf '%s' "$NORM" | grep -qE ' (--force|-f)( |$)' \
    && deny "force push 명령입니다. 원격 이력을 덮어씁니다."
  printf '%s' "$NORM" | grep -qE ' --force-(with-lease|if-includes)' \
    && ask "force push(--force-with-lease) 명령입니다. 원격 이력을 덮어쓸 수 있습니다."
fi

# ── 대상 판정: 재귀 삭제, 테스트 실행 ───────────────────────

if printf '%s' "$NORM" | grep -qE '(^|[^a-z0-9_-])rm |gradle|mvn'; then
  VERDICT=$(printf '%s' "$INPUT" | python3 "$HARNESS" judge-command 2>/dev/null)
  DECISION=$(printf '%s' "$VERDICT" | jq -r '.decision // empty' 2>/dev/null)
  REASON=$(printf '%s' "$VERDICT" | jq -r '.reasons | join(" / ")' 2>/dev/null)
  case "$DECISION" in
    deny) deny "$REASON" ;;
    ask) ask "$REASON" ;;
    allow) ;;
    # 판정기를 실행할 수 없으면 재귀 삭제만 보수적으로 확인을 받는다
    *) printf '%s' "$NORM" | grep -qE 'rm +-[a-z]*r|rm +--recursive' \
         && ask "재귀 삭제 명령입니다. 판정기(harness judge-command)를 실행하지 못해 확인을 요청합니다." ;;
  esac
fi

# ── ASK ─────────────────────────────────────────────────

if printf '%s' "$NORM" | grep -qE '(psql|mysql|mariadb|docker exec)'; then
  printf '%s' "$NORM" | grep -qE 'drop +(database|table|schema)|truncate table' \
    && ask "DB 클라이언트 컨텍스트에서 DDL 파괴 명령(DROP/TRUNCATE)이 감지되었습니다."
fi

if printf '%s' "$NORM" | grep -q 'chmod' && printf '%s' "$NORM" | grep -q '777'; then
  ask "권한을 777로 변경하는 명령입니다."
fi

printf '%s' "$NORM" | grep -qE 'find .*-delete|xargs .*rm' \
  && ask "find, xargs를 통한 일괄 삭제 명령입니다."

printf '%s' "$NORM" | grep -qE 'git push .*[[:space:]]\+[a-z0-9]' \
  && ask "refspec force-push(+) 패턴이 감지되었습니다."

printf '%s' "$NORM" | grep -qE 'git clean' \
  && ask "git clean은 추적되지 않는 파일과 디렉토리를 삭제합니다."

printf '%s' "$NORM" | grep -qE 'flyway ?(clean|repair)' \
  && ask "Flyway 파괴적 태스크(clean, repair)가 감지되었습니다."

# 파일을 바꾸는 명령일 때만 설정을 읽는다 (매 Bash 호출마다 python을 띄우지 않기 위해)
if printf '%s' "$NORM" | grep -qE 'sed -i|mv |rm |cp |tee |>>|>'; then
  MIGRATION_DIR=$(python3 "$HARNESS" get-config migration.dir 2>/dev/null | tr '[:upper:]' '[:lower:]')
  [ -n "$MIGRATION_DIR" ] && printf '%s' "$NORM" | grep -qF "$MIGRATION_DIR" \
    && ask "적용된 마이그레이션은 불변입니다. 새 버전 파일로 추가하세요."
fi

printf '%s' "$COMMAND" | grep -qE '\.claude/(hooks|scripts|settings|harness\.json)' \
  && printf '%s' "$COMMAND" | grep -qE 'sed -i|tee|>>|>|mv |rm |chmod ' \
  && ask ".claude 훅, 설정 파일을 변경하는 명령입니다. 사용자 승인이 필요합니다."

exit 0
