#!/usr/bin/env bash
# 측정용 JWT(HS256)를 서명키로 직접 만든다. bash 3.2와 openssl만 쓴다.
#
# 왜 로그인 API를 쓰지 않는가:
#   로그인 SQL과 응답시간이 측정에 섞이고, 시드 계정의 비밀번호는 보통 실제 해시가 아니라 로그인을 탈 수 없다.
#   수백 계정을 가입 API로 만드는 것보다 시드 id에 맞춰 직접 서명하는 편이 측정 준비에 맞다.
#   토큰 형태(클레임 이름, 키 인코딩, 금지 클레임)는 프로젝트의 토큰 발급 코드와 같아야 한다. 먼저 그 코드를 읽어라.
#
# 사용:
#   bash mint-tokens.sh --start {id 시작값} --count {개수} --out {경로} [옵션]
#
# 옵션 (생략하면 harness 설정 perf.tokens.*, 그것도 없으면 괄호 안 기본값):
#   --secret VALUE          서명키 문자열. 셸 기록에 남으니 --secret-env를 권한다
#   --secret-env NAME       서명키를 담은 환경 변수 이름 (perf.tokens.secretEnv, PERF_JWT_SECRET)
#   --secret-file FILE      서명키를 읽을 파일. --secret-key 줄의 값을 쓴다 (perf.tokens.secretFile)
#   --secret-key KEY        위 파일에서 찾을 키 이름. `KEY: 값` 또는 `KEY=값` 한 줄 (perf.tokens.secretKey, secret-key)
#   --key-encoding ENC      utf8 | base64. 앱이 키를 base64 디코드해 쓰면 base64 (perf.tokens.keyEncoding, utf8)
#   --subject-claim NAME    id를 담을 클레임 (perf.tokens.subjectClaim, sub)
#   --subject-type TYPE     string | number (perf.tokens.subjectType, string)
#   --claims JSON           모든 토큰에 넣을 고정 클레임 객체. 예: '{"role":"USER"}' (perf.tokens.extraClaims, {})
#   --id-field NAME         출력 항목의 id 필드 이름 (perf.tokens.idField, userId)
#   --ttl SECONDS           유효 시간 (perf.tokens.ttlSeconds, 86400)
#
# 서명키는 --secret > --secret-env 변수 > --secret-file 순으로 찾는다.
#
# 출력 형태 (k6 템플릿이 이 형태를 읽는다):
#   [{ "<id-field>": 1001, "accessToken": "eyJ..." }, ...]

set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
HARNESS="$SCRIPT_DIR/../harness"

cfg() {
    local v=""
    if [ -x "$HARNESS" ]; then
        v=$("$HARNESS" get-config "$1" 2>/dev/null || true)
    fi
    if [ -n "$v" ] && [ "$v" != "{}" ]; then printf '%s' "$v"; else printf '%s' "$2"; fi
}

SECRET=""
SECRET_ENV=""
SECRET_FILE=""
SECRET_KEY=""
KEY_ENCODING=""
SUBJECT_CLAIM=""
SUBJECT_TYPE=""
CLAIMS=""
ID_FIELD=""
TTL_SECONDS=""
START=""
COUNT=""
OUT=""

while [ $# -gt 0 ]; do
    case "$1" in
        --secret)        SECRET="$2";        shift 2 ;;
        --secret-env)    SECRET_ENV="$2";    shift 2 ;;
        --secret-file)   SECRET_FILE="$2";   shift 2 ;;
        --secret-key)    SECRET_KEY="$2";    shift 2 ;;
        --key-encoding)  KEY_ENCODING="$2";  shift 2 ;;
        --subject-claim) SUBJECT_CLAIM="$2"; shift 2 ;;
        --subject-type)  SUBJECT_TYPE="$2";  shift 2 ;;
        --claims)        CLAIMS="$2";        shift 2 ;;
        --id-field)      ID_FIELD="$2";      shift 2 ;;
        --ttl)           TTL_SECONDS="$2";   shift 2 ;;
        --start)         START="$2";         shift 2 ;;
        --count)         COUNT="$2";         shift 2 ;;
        --out)           OUT="$2";           shift 2 ;;
        *) echo "알 수 없는 인자: $1" >&2; exit 1 ;;
    esac
done

for required in START COUNT OUT; do
    if [ -z "${!required}" ]; then
        echo "필수 인자 누락: --$(echo "$required" | tr '[:upper:]' '[:lower:]')" >&2
        exit 1
    fi
done
case "$START$COUNT" in
    *[!0-9]*) echo "--start와 --count는 0 이상의 정수여야 한다" >&2; exit 1 ;;
esac
[ "$COUNT" -ge 1 ] || { echo "--count는 1 이상이어야 한다" >&2; exit 1; }

[ -n "$KEY_ENCODING" ]  || KEY_ENCODING=$(cfg perf.tokens.keyEncoding utf8)
[ -n "$SUBJECT_CLAIM" ] || SUBJECT_CLAIM=$(cfg perf.tokens.subjectClaim sub)
[ -n "$SUBJECT_TYPE" ]  || SUBJECT_TYPE=$(cfg perf.tokens.subjectType string)
[ -n "$CLAIMS" ]        || CLAIMS=$(cfg perf.tokens.extraClaims '{}')
[ -n "$ID_FIELD" ]      || ID_FIELD=$(cfg perf.tokens.idField userId)
[ -n "$TTL_SECONDS" ]   || TTL_SECONDS=$(cfg perf.tokens.ttlSeconds 86400)

# 서명키: 인자 > 환경 변수 > 파일
if [ -z "$SECRET" ]; then
    [ -n "$SECRET_ENV" ] || SECRET_ENV=$(cfg perf.tokens.secretEnv PERF_JWT_SECRET)
    if [ -n "$SECRET_ENV" ]; then
        SECRET="${!SECRET_ENV:-}"
    fi
fi
if [ -z "$SECRET" ]; then
    [ -n "$SECRET_FILE" ] || SECRET_FILE=$(cfg perf.tokens.secretFile "")
    [ -n "$SECRET_KEY" ]  || SECRET_KEY=$(cfg perf.tokens.secretKey secret-key)
    if [ -n "$SECRET_FILE" ] && [ -f "$SECRET_FILE" ]; then
        # 주석 줄을 건너뛰고 `키: 값` 또는 `키=값`의 값만 집는다. 따옴표는 벗긴다
        SECRET=$(grep -E "^[[:space:]]*${SECRET_KEY}[[:space:]]*[:=]" "$SECRET_FILE" | head -n 1 \
            | sed -E "s/^[[:space:]]*${SECRET_KEY}[[:space:]]*[:=][[:space:]]*//; s/[[:space:]]+#.*$//; s/^[\"']//; s/[\"'][[:space:]]*$//" || true)
    fi
fi
if [ -z "$SECRET" ]; then
    echo "서명키를 찾지 못했다. --secret-env 변수, 또는 --secret-file의 '${SECRET_KEY:-secret-key}' 줄을 확인하라." >&2
    exit 1
fi
case "$SECRET" in
    '${'*) echo "서명키가 '${SECRET}'다. 설정 파일이 환경 변수 자리표시자를 쓴다. 그 변수 값을 --secret-env로 넘겨라." >&2; exit 1 ;;
esac

# HMAC 키를 hex로. HS256은 키가 32바이트 이상이어야 한다. 짧으면 애플리케이션이 토큰을 거절한다
case "$KEY_ENCODING" in
    utf8)   KEY_HEX=$(printf '%s' "$SECRET" | od -An -v -tx1 | tr -d ' \n') ;;
    base64) KEY_HEX=$(printf '%s' "$SECRET" | openssl base64 -d -A | od -An -v -tx1 | tr -d ' \n') ;;
    *) echo "--key-encoding은 utf8 또는 base64다: $KEY_ENCODING" >&2; exit 1 ;;
esac
KEY_BYTES=$(( ${#KEY_HEX} / 2 ))
if [ "$KEY_BYTES" -lt 32 ]; then
    echo "서명키가 ${KEY_BYTES}바이트다(${KEY_ENCODING}). HS256은 32바이트 이상이 필요하다." >&2
    exit 1
fi

# 고정 클레임 객체의 바깥 중괄호를 벗겨 페이로드에 이어 붙인다
EXTRA=$(printf '%s' "$CLAIMS" | sed -E 's/^[[:space:]]*\{//; s/\}[[:space:]]*$//; s/^[[:space:]]+//; s/[[:space:]]+$//')
[ -z "$EXTRA" ] || EXTRA=",$EXTRA"

# base64url: 표준 base64에서 +/ 를 -_ 로 바꾸고 패딩(=)을 뗀다
b64url() {
    openssl base64 -A | tr '+/' '-_' | tr -d '='
}

NOW=$(date +%s)
EXP=$((NOW + TTL_SECONDS))
HEADER=$(printf '{"alg":"HS256","typ":"JWT"}' | b64url)
END_ID=$((START + COUNT - 1))

{
    printf '[\n'
    i=0
    while [ "$i" -lt "$COUNT" ]; do
        ID=$((START + i))
        if [ "$SUBJECT_TYPE" = "number" ]; then SUBJ="$ID"; else SUBJ="\"$ID\""; fi
        PAYLOAD=$(printf '{"%s":%s,"iat":%s,"exp":%s%s}' "$SUBJECT_CLAIM" "$SUBJ" "$NOW" "$EXP" "$EXTRA" | b64url)
        SIGNING_INPUT="${HEADER}.${PAYLOAD}"
        SIGNATURE=$(printf '%s' "$SIGNING_INPUT" \
            | openssl dgst -sha256 -mac HMAC -macopt "hexkey:$KEY_HEX" -binary \
            | b64url)
        SEP=","
        [ "$ID" -lt "$END_ID" ] || SEP=""
        printf '  {"%s": %s, "accessToken": "%s.%s"}%s\n' "$ID_FIELD" "$ID" "$SIGNING_INPUT" "$SIGNATURE" "$SEP"
        i=$((i + 1))
    done
    printf ']\n'
} > "$OUT"

EXP_TEXT=$(date -r "$EXP" '+%Y-%m-%d %H:%M:%S' 2>/dev/null || date -d "@$EXP" '+%Y-%m-%d %H:%M:%S')
echo "토큰 ${COUNT}건 생성: $OUT (id ${START} ~ ${END_ID}, 클레임 ${SUBJECT_CLAIM}, 만료 ${EXP_TEXT})"
echo "다음: 토큰 1건으로 인증이 통하는지 확인하라. 401이면 키 인코딩, 클레임 이름, 금지 클레임을 발급 코드와 대조한다"
