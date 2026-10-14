#!/bin/bash
# SessionStart 훅: 하네스 새 버전 확인
# 하루 한 번만 원격 태그를 본다. 새 버전이 있고 사용자가 미루지 않았으면 세션 맥락에 알린다.
# 네트워크가 없거나 실패해도 세션을 막지 않는다 (항상 exit 0).

HARNESS="$(cd "$(dirname "${BASH_SOURCE[0]}")/../scripts" && pwd)/harness"
cat >/dev/null
python3 "$HARNESS" check-update --hook 2>/dev/null
exit 0
