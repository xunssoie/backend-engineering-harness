"""성능 측정 산출물 변환기. optimize-performance, fix-concurrency가 LLM에게 손으로 쓰게 하던 가공본을 결정적으로 만든다.

명령 (모두 압축 JSON 한 줄을 출력하고 성공하면 exit 0. 표가 있으면 JSON 뒤에 빈 줄을 두고 마크다운을 붙인다):
  perf-summarize-k6  k6 handleSummary 원본 → k6-test-summary-{n}.json. 직전 상태 파일이 있으면 delta_vs_prev
  perf-query-stats   digest(MySQL -B) / pg_stat_statements(psql --csv) 원본 → query-stats-summary-{n}.md
  perf-delta         두 상태의 산출물 → 전후 비교 표. 변화 칸 규칙을 고정한다
  perf-progress      record.md 진행 상태 표를 읽고 갱신한다

원칙: 수치는 원본 자릿수를 유지하거나 여기서 정한 고정 자릿수로만 쓴다. 해석(출처, 하는 일, 판정)은 쓰지 않는다.
"""
import base64
import copy
import csv
import io
import json
import os
import re
import sys
import time
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

# `perf` 설정 절. harness_config.DEFAULTS["perf"]로 병합된다. 값은 .claude/harness.json의 "perf"가 덮어쓴다
PERF_DEFAULTS = {
    # 측정 대상 DB. "mysql" 또는 "postgresql". stacks/{db}.md와 perf-env.sh의 접속 함수가 이 값을 따른다
    "db": "mysql",
    # DB 컨테이너 이름. 있으면 docker exec로 컨테이너 안의 클라이언트를 쓴다(호스트에 클라이언트가 없어도 된다)
    # 비우면 호스트 클라이언트로 dbHost:dbPort에 붙는다
    "dbContainer": "",
    "dbHost": "127.0.0.1",
    # 호스트 포트. null이면 DB 기본 포트(3306, 5432)
    "dbPort": None,
    "dbName": "",
    "dbUser": "root",
    # 비밀번호를 담은 환경 변수 이름. 그 변수에 값이 있으면 dbPassword보다 우선한다
    "dbPasswordEnv": "PERF_DB_PASSWORD",
    # 로컬 측정 DB 전용 비밀번호. 운영 비밀번호를 여기 두지 마라
    "dbPassword": "",
    # MySQL 접속 charset과 collation. 컬럼 collation과 다르면 사용자 변수 비교가 ERROR 1267로 죽는다
    "mysqlCharset": "utf8mb4",
    "mysqlCollation": "utf8mb4_unicode_ci",
    # 부하를 받을 애플리케이션 주소 (k6의 BASE_URL)
    "appBaseUrl": "http://localhost:8080",
    # Actuator 기준 URL. 관리 포트를 분리했으면 그 포트를 쓴다. 뒤에 /prometheus, /health를 붙인다
    "managementUrl": "http://localhost:8080/actuator",
    # 측정 프로파일 이름과 그 정의 파일. Phase 2가 풀 크기, SQL 로깅, 서명키를 이 파일에서 읽는다
    "appProfile": "perf",
    "profileConfigFile": "src/main/resources/application-perf.yml",
    # 측정 프로파일로 애플리케이션을 띄우는 명령. 호출자가 별도 터미널에서 실행한다
    "appStartCommand": "./gradlew bootRun --args='--spring.profiles.active=perf'",
    # DB와 캐시 컨테이너를 띄우는 compose 파일. 비우면 Phase 2가 프로젝트 문서에서 찾는다
    "composeFile": "",
    # 부하 도구. 지금은 k6만 지원한다
    "loadTool": "k6",
    # 애플리케이션 캐시 점검, 비우기 명령. 비우면 캐시 제어 없음(warm 고정)
    # 예: "docker exec my-redis redis-cli ping", "docker exec my-redis redis-cli -n 0 FLUSHDB"
    "cachePingCommand": "",
    "cacheFlushCommand": "",
    # 측정 기록 루트와 프로젝트 시드 모듈 디렉토리
    "resultsDir": ".claude/resources/perf",
    "seedsDir": ".claude/resources/perf/seeds",
    # query-stats 가공본에 표로 싣는 상위 쿼리 수. 나머지는 합계 한 줄로 접는다
    "queryStatsTop": 20,
    # 측정용 토큰. mint-tokens.sh와 k6 스크립트가 읽는다
    "tokens": {
        # mint: 서명키로 직접 발급, none: 인증 불필요, custom: 프로젝트 문서의 발급 절차를 따른다
        "mode": "mint",
        # 서명키를 담은 환경 변수 이름. 비어 있으면 secretFile에서 secretKey 줄을 읽는다
        "secretEnv": "PERF_JWT_SECRET",
        "secretFile": "src/main/resources/application-perf.yml",
        "secretKey": "secret-key",
        # utf8: 키 문자열의 바이트 그대로, base64: 앱이 키를 base64 디코드해 쓰는 경우
        "keyEncoding": "utf8",
        # id를 담을 클레임 이름과 타입(string | number)
        "subjectClaim": "sub",
        "subjectType": "string",
        # 모든 토큰에 넣을 고정 클레임. 예: {"role": "USER"}
        "extraClaims": {},
        # tokens.json 항목의 id 필드 이름. 형태: [{"<idField>": 1001, "accessToken": "..."}]
        "idField": "userId",
        "ttlSeconds": 86400,
        # k6가 토큰을 실을 헤더와 접두어
        "header": "Authorization",
        "headerPrefix": "Bearer ",
    },
    # jvm-sampler.sh가 긁는 Prometheus 지표 이름. Micrometer 기본값과 다를 때만 바꾼다
    "metrics": {
        "pool": "hikaricp_connections",
        "httpActive": "http_server_requests_active_seconds",
        "oldGenPool": "long-lived",
        "cache": "cache",
        "redis": "lettuce_seconds",
        "repository": "spring_data_repository_invocations_seconds",
    },
}

SKILL_DIR = ".claude/skills/optimize-performance"
SCRIPTS_DIR = ".claude/scripts/perf"
PHASE_FILES = {
    1: "phase-1-target.md", 2: "phase-2-environment.md", 3: "phase-3-dataset.md", 4: "phase-4-baseline.md",
    5: "phase-5-design.md", 6: "phase-6-snapshot.md", 7: "phase-7-apply.md", 8: "phase-8-verify.md",
    9: "phase-9-report.md",
}
MARKS = ("✅", "⏳", "⏭️")
TX_CONTROL = re.compile(
    r"^\s*(COMMIT|ROLLBACK|BEGIN|START\s+TRANSACTION|SAVEPOINT|RELEASE\s+SAVEPOINT|"
    r"SET\s+(SESSION\s+)?`?autocommit`?|SET\s+(SESSION\s+)?TRANSACTION)\b", re.I)
SRC_PLACEHOLDER = "{출처}"
WHAT_PLACEHOLDER = "{하는 일}"


# ── 공통 ────────────────────────────────────────────────────────────────────────

def emit(obj, code=0):
    rep = obj.pop("report", None) if isinstance(obj, dict) else None
    print(json.dumps(obj, ensure_ascii=False, separators=(",", ":")))
    if rep:
        print("\n" + rep)
    return code


def perf_config(cfg):
    """DEFAULTS에 병합되기 전에도 동작하도록 PERF_DEFAULTS 위에 사용자 설정을 얹는다."""
    def merge(base, over):
        out = copy.deepcopy(base)
        for k, v in (over or {}).items():
            out[k] = merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
        return out
    return merge(PERF_DEFAULTS, (cfg or {}).get("perf"))


def _dec(v):
    if v is None:
        return None
    if isinstance(v, Decimal):
        return v
    s = str(v).strip()
    if s in ("", "NULL", "null", "None", "-"):
        return None
    try:
        return Decimal(s)
    except InvalidOperation:
        return None


def _fixed(v, places):
    """고정 소수 자릿수. 0이 아닌 값이 0으로 반올림되면 유효숫자 2개가 나올 때까지 자릿수를 늘린다."""
    if v is None:
        return "-"
    v = Decimal(v)
    q = v.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
    if q == 0 and v != 0:
        for p in range(places + 1, 16):
            if v.quantize(Decimal(1).scaleb(-p), rounding=ROUND_HALF_UP) != 0:
                q = v.quantize(Decimal(1).scaleb(-(p + 1)), rounding=ROUND_HALF_UP)
                break
    return format(q, "f")


def _state_of(path, prefix):
    """파일명 `{prefix}{n}.ext`에서 상태 번호 n. 없으면 None."""
    m = re.match(re.escape(prefix) + r"(\d+)\.", os.path.basename(path or ""))
    return int(m.group(1)) if m else None


def _sibling(path, prefix, n, ext):
    return os.path.join(os.path.dirname(path), f"{prefix}{n}{ext}")


def _rel(cfg, path):
    root = (cfg or {}).get("_root")
    if root and path and os.path.isabs(path):
        try:
            r = os.path.relpath(path, root)
            return path if r.startswith("..") else r
        except ValueError:
            return path
    return path


# ── k6 요약 ─────────────────────────────────────────────────────────────────────

def _k6_metric(data, name):
    m = (data.get("metrics") or {}).get(name)
    if not isinstance(m, dict):
        return {}
    # handleSummary의 data는 values 아래에, --summary-export는 지표 바로 아래에 값이 있다
    return m.get("values") if isinstance(m.get("values"), dict) else m


def _k6_rate(data, name):
    m = _k6_metric(data, name)
    if "rate" in m:
        return m["rate"]
    if "value" in m:  # --summary-export의 rate 지표
        return m["value"]
    return None


def _k6_trend(data, name):
    m = _k6_metric(data, name)
    return {"med": m.get("med"), "p95": m.get("p(95)"), "p99": m.get("p(99)"), "max": m.get("max")}


def _k6_checks(data):
    root = data.get("root_group") or {}
    out = []

    def walk(group):
        checks = group.get("checks") or []
        if isinstance(checks, dict):  # --summary-export는 이름을 키로 한 객체다
            checks = [dict(v, name=v.get("name", k)) for k, v in checks.items()]
        for c in checks:
            out.append({"name": c.get("name"), "passes": c.get("passes"), "fails": c.get("fails")})
        groups = group.get("groups") or []
        for g in (groups.values() if isinstance(groups, dict) else groups):
            walk(g)

    walk(root)
    return out


def k6_summary(raw):
    """k6 원본(handleSummary data 또는 --summary-export)을 가공본 필드로. 이미 가공본이면 그대로 둔다."""
    if "requests" in raw and "duration_ms" in raw:
        out = {k: v for k, v in raw.items() if k != "delta_vs_prev"}
        return out
    meta = raw.get("harness_meta") or {}
    reqs = _k6_metric(raw, "http_reqs")
    return {
        "phase": meta.get("phase"),
        "target": meta.get("target"),
        "endpoint": meta.get("endpoint"),
        "condition": meta.get("condition"),
        "requests": reqs.get("count"),
        "rps": reqs.get("rate"),
        "failed_rate": _k6_rate(raw, "http_req_failed"),
        "checks_rate": _k6_rate(raw, "checks"),
        "checks": _k6_checks(raw),
        "duration_ms": _k6_trend(raw, "http_req_duration"),
        "waiting_ms": _k6_trend(raw, "http_req_waiting"),
        "bytes_received": _k6_metric(raw, "data_received").get("count"),
    }


def k6_delta(prev, cur, prev_name):
    def pair(a, b):
        return {"before": a, "after": b}
    pd, cd = prev.get("duration_ms") or {}, cur.get("duration_ms") or {}
    return {
        "from": prev_name,
        "requests": pair(prev.get("requests"), cur.get("requests")),
        "rps": pair(prev.get("rps"), cur.get("rps")),
        "duration_med_ms": pair(pd.get("med"), cd.get("med")),
        "duration_p95_ms": pair(pd.get("p95"), cd.get("p95")),
        "duration_p99_ms": pair(pd.get("p99"), cd.get("p99")),
        "duration_max_ms": pair(pd.get("max"), cd.get("max")),
        "failed_rate": pair(prev.get("failed_rate"), cur.get("failed_rate")),
        "checks_rate": pair(prev.get("checks_rate"), cur.get("checks_rate")),
    }


def _load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def cmd_summarize_k6(cfg, a):
    raw = _load_json(a.raw)
    summary = k6_summary(raw)
    reqs = summary.get("requests")
    if not isinstance(reqs, (int, float)) or reqs <= 0:
        return emit({"ok": False, "errors": [f"요청 수가 '{reqs}'다. 측정이 실패했으므로 가공하지 않는다. 원인을 확인하고 재측정하라"],
                     "raw": a.raw}, 1)
    prev = a.prev
    if prev is None and not a.no_prev:
        n = _state_of(a.out, "k6-test-summary-")
        cand = _sibling(a.out, "k6-test-summary-", n - 1, ".json") if n else None
        prev = cand if cand and os.path.exists(cand) else None
    if prev:
        summary["delta_vs_prev"] = k6_delta(k6_summary(_load_json(prev)), summary, os.path.basename(prev))
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
        f.write("\n")
    d = summary.get("duration_ms") or {}
    return emit({
        "ok": True, "out": _rel(cfg, a.out), "requests": reqs, "rps": summary.get("rps"),
        "p95": d.get("p95"), "p99": d.get("p99"), "failed_rate": summary.get("failed_rate"),
        "checks_rate": summary.get("checks_rate"),
        "failedChecks": [c["name"] for c in summary.get("checks", []) if c.get("fails")],
        "deltaFrom": os.path.basename(prev) if prev else None,
    })


# ── 쿼리 통계 ───────────────────────────────────────────────────────────────────

_MYSQL_ESC = {"n": "\n", "t": "\t", "\\": "\\", "0": "\0", "r": "\r"}


def _unescape_batch(s):
    return re.sub(r"\\(.)", lambda m: _MYSQL_ESC.get(m.group(1), m.group(1)), s)


def read_stats_raw(path):
    """mysql -B(TSV, 백슬래시 이스케이프) 또는 psql --csv 출력을 dict 행 목록으로."""
    with open(path, encoding="utf-8", newline="") as f:
        text = f.read()
    first = text.split("\n", 1)[0]
    rows = []
    if "\t" in first:
        lines = [ln for ln in text.split("\n") if ln != ""]
        header = [h.strip().lower() for h in lines[0].split("\t")]
        for ln in lines[1:]:
            parts = ln.rstrip("\r").split("\t")
            if len(parts) < len(header):
                continue
            if len(parts) > len(header):  # 이스케이프되지 않은 탭이면 마지막 칼럼(쿼리)에 합친다
                parts = parts[:len(header) - 1] + ["\t".join(parts[len(header) - 1:])]
            rows.append({h: (None if p == "NULL" else _unescape_batch(p)) for h, p in zip(header, parts)})
    else:
        reader = csv.reader(io.StringIO(text))
        header = None
        for rec in reader:
            if not rec:
                continue
            if header is None:
                header = [h.strip().lower() for h in rec]
                continue
            if len(rec) == len(header):
                rows.append(dict(zip(header, rec)))
    return rows


def compute_stats(rows, requests):
    """원본 행 → 계산된 행. total_ms 내림차순(동률은 원본 순서)."""
    reqs = Decimal(requests)
    out = []
    for r in rows:
        calls = _dec(r.get("calls")) or Decimal(0)
        if r.get("total_ps") not in (None, ""):
            total = _dec(r["total_ps"]) / Decimal(10) ** 9  # TIMER_WAIT는 피코초
        else:
            total = _dec(r.get("total_ms")) or Decimal(0)
        rows_out = _dec(r.get("rows_sent") if r.get("rows_sent") is not None else r.get("rows"))
        examined = _dec(r.get("rows_examined"))
        hit, read = _dec(r.get("shared_blks_hit")), _dec(r.get("shared_blks_read"))
        q = r.get("query") or r.get("digest_text") or ""
        out.append({
            "query": q, "calls": calls, "total_ms": total,
            "mean_ms": total / calls if calls else None,
            "per_req": calls / reqs if reqs else None,
            "rows_per_call": rows_out / calls if calls and rows_out is not None else None,
            "examined_per_sent": examined / rows_out if examined is not None and rows_out else None,
            "has_examined": "rows_examined" in r,
            "blocks_per_call": (hit + (read or 0)) / calls if hit is not None and calls else None,
            "has_blocks": "shared_blks_hit" in r,
            "tx": bool(TX_CONTROL.match(q)),
        })
    out.sort(key=lambda x: -x["total_ms"])
    grand = sum((x["total_ms"] for x in out), Decimal(0))
    for x in out:
        x["pct"] = (100 * x["total_ms"] / grand) if grand else None
    return out, grand


def _md_cells(line):
    s = line.strip()
    if not (s.startswith("|") and s.endswith("|")):
        return None
    return [c.strip() for c in re.split(r"(?<!\\)\|", s[1:-1])]


def parse_stats_md(path):
    """가공본 md → {header: [...], rows: [{#, cells...}], queries: {n: text}}. 출처 칸의 `|`는 하는 일로 합친다."""
    with open(path, encoding="utf-8") as f:
        text = f.read()
    header, rows = None, []
    for line in text.split("\n"):
        cells = _md_cells(line)
        if not cells:
            continue
        if cells[0] == "#" and "calls" in cells:
            header = cells
            continue
        if header and cells[0].isdigit() and len(cells) >= len(header):
            if len(cells) > len(header):
                cells = cells[:len(header) - 1] + ["|".join(cells[len(header) - 1:])]
            rows.append(dict(zip(header, cells)))
    queries = {}
    body = text.split("\n## 쿼리 원문", 1)
    if len(body) == 2:
        parts = re.split(r"(?m)^\[(\d+)\] ", body[1])
        for i in range(1, len(parts) - 1, 2):
            chunk = parts[i + 1]
            chunk = re.split(r"\n\n(?=> |## )", chunk, maxsplit=1)[0]
            queries[int(parts[i])] = chunk.rstrip("\n")
    for r in rows:
        r["query"] = queries.get(int(r["#"]))
    meta = {}
    for key in ("요청당", "상태"):
        m = re.search(rf"(?m)^{key}: (.*)$", text)
        if m:
            meta[key] = m.group(1)
    return {"header": header or [], "rows": rows, "meta": meta}


def _is_tx_row(r):
    return bool(TX_CONTROL.match(r.get("query") or "")) or r.get("하는 일") == "트랜잭션 제어"


def _stats_totals(rows_like):
    """요청당 SQL, 제어, DB 시간. rows_like는 compute_stats 행 또는 parse_stats_md 행."""
    sql = ctl = Decimal(0)
    for r in rows_like:
        pr = r["per_req"] if "per_req" in r else _dec(r.get("요청당"))
        tx = r["tx"] if "tx" in r else _is_tx_row(r)
        if pr is None:
            continue
        if tx:
            ctl += pr
        else:
            sql += pr
    return sql, ctl


def _condition_text(summary):
    c = (summary or {}).get("condition") or {}
    if not c:
        return ""
    parts = []
    if "vus" in c:
        parts.append(f"VU {c['vus']}")
    if c.get("steady_state_duration"):
        dur = f"유지 {c['steady_state_duration']}"
        if c.get("ramp_up") or c.get("ramp_down"):
            dur = f"ramp-up {c.get('ramp_up', '-')} + {dur} + ramp-down {c.get('ramp_down', '-')}"
        if c.get("total_duration"):
            dur += f" (총 {c['total_duration']})"
        parts.append(dur)
    skip = {"vus", "steady_state_duration", "ramp_up", "ramp_down", "total_duration"}
    parts += [f"{k} {v}" for k, v in c.items() if k not in skip]
    return " / ".join(parts)


def render_stats_md(stats, grand, requests, db, n, slug, state, measure, top, prev, carry):
    examined = db == "mysql" or any(x["has_examined"] for x in stats)
    blocks = db != "mysql" and any(x["has_blocks"] for x in stats)
    shown = stats[:top]
    rest = stats[top:]
    sql, ctl = _stats_totals(stats)
    lines = [f"# query-stats-summary-{n} - {slug}", "", f"상태: {state}"]
    lines.append(f"측정: {measure + ' / ' if measure else ''}요청 {requests}건")
    db_per_req = grand / Decimal(requests) if requests else None
    lines.append(f"요청당: SQL {_fixed(sql, 4)} + 트랜잭션 제어 {_fixed(ctl, 4)} = 왕복 {_fixed(sql + ctl, 4)}, "
                 f"DB 실행 시간 {_fixed(db_per_req, 6)} ms")
    if prev is not None:
        lines.append(f"직전 상태 대비: {prev['summary']}")
    lines.append("")
    head = ["#", "요청당", "calls", "mean_ms", "total_ms", "비중", "행/호출"]
    if examined:
        head.append("읽은행/반환행")
    if blocks:
        head.append("블록/호출")
    head += ["출처", "하는 일"]
    lines.append("| " + " | ".join(head) + " |")
    lines.append("|" + "---|" * len(head))
    pending = []
    for i, x in enumerate(shown, 1):
        if x["tx"]:
            src, what = "-", "트랜잭션 제어"
        else:
            src, what = carry.get(x["query"], (SRC_PLACEHOLDER, WHAT_PLACEHOLDER))
            if src == SRC_PLACEHOLDER or what == WHAT_PLACEHOLDER:
                pending.append(i)
        cells = [str(i), _fixed(x["per_req"], 4), str(x["calls"]), _fixed(x["mean_ms"], 6), _fixed(x["total_ms"], 6),
                 (_fixed(x["pct"], 4) + "%") if x["pct"] is not None else "-", _fixed(x["rows_per_call"], 4)]
        if examined:
            cells.append(_fixed(x["examined_per_sent"], 4))
        if blocks:
            cells.append(_fixed(x["blocks_per_call"], 4))
        cells += [src, what]
        lines.append("| " + " | ".join(cells) + " |")
    if rest:
        rt = sum((x["total_ms"] for x in rest), Decimal(0))
        lines += ["", f"상위 {top}건 밖: {len(rest)}건, total_ms 합 {_fixed(rt, 6)}, "
                      f"비중 {_fixed(100 * rt / grand if grand else None, 4)}%, 요청당 {_fixed(sum(x['per_req'] for x in rest), 4)}"]
    if prev is not None and prev["lines"]:
        lines += ["", "## 직전 상태 대비", ""] + prev["lines"]
    lines += ["", "## 쿼리 원문", ""]
    for i, x in enumerate(shown, 1):
        lines.append(f"[{i}] {x['query']}")
        if db == "mysql" and x["query"].rstrip().endswith("..."):
            lines.append("")
            lines.append("> 원문이 `...`로 끝난다. `performance_schema_max_digest_length`에 잘린 것이다")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n", pending


def stats_prev_compare(prev_md, stats, requests, prev_name):
    """직전 가공본과 같은 쿼리 원문끼리 대조한다. 원문이 바뀐 쿼리는 사라짐과 새로 생김으로 나온다."""
    p = parse_stats_md(prev_md)
    prows = [r for r in p["rows"] if r.get("query") is not None]
    by_q = {r["query"]: r for r in prows}
    p_sql, p_ctl = _stats_totals(prows)
    c_sql, c_ctl = _stats_totals(stats)
    summary = (f"`{prev_name}` 대비 요청당 SQL {_fixed(p_sql, 4)} → {_fixed(c_sql, 4)}, "
               f"트랜잭션 제어 {_fixed(p_ctl, 4)} → {_fixed(c_ctl, 4)}")
    lines = []
    matched = [(i, x, by_q[x["query"]]) for i, x in enumerate(stats, 1) if x["query"] in by_q and not x["tx"]]
    if matched:
        lines += ["| # | 직전 # | 출처 | 요청당 | mean_ms | total_ms | 읽은행/반환행 |", "|---|---|---|---|---|---|---|"]
        for i, x, r in matched:
            ex_b = r.get("읽은행/반환행", "-")
            lines.append(f"| {i} | {r['#']} | {r.get('출처', '-')} | {r.get('요청당')} → {_fixed(x['per_req'], 4)} | "
                         f"{r.get('mean_ms')} → {_fixed(x['mean_ms'], 6)} | {r.get('total_ms')} → {_fixed(x['total_ms'], 6)} | "
                         f"{ex_b} → {_fixed(x['examined_per_sent'], 4)} |")
    cur_q = {x["query"] for x in stats}
    gone = [r for r in prows if r["query"] not in cur_q and not _is_tx_row(r)]
    new = [i for i, x in enumerate(stats, 1) if x["query"] not in by_q and not x["tx"]]
    if gone:
        lines.append("")
        lines.append("직전에 있었고 이번에 없는 쿼리: " + ", ".join(
            f"직전 [{r['#']}] {r.get('출처', '-')} (요청당 {r.get('요청당')}, total_ms {r.get('total_ms')})" for r in gone))
    if new:
        lines.append("")
        lines.append("이번에 새로 나타난 쿼리: " + ", ".join(f"[{i}]" for i in new))
    if gone or new:
        lines.append("")
        lines.append("> 직전 가공본은 상위 쿼리만 담는다. 원문이 한 글자라도 바뀌면 사라짐과 새로 생김으로 나온다")
    carry = {r["query"]: (r.get("출처", SRC_PLACEHOLDER), r.get("하는 일", WHAT_PLACEHOLDER)) for r in prows}
    return {"summary": summary, "lines": lines}, carry


def cmd_query_stats(cfg, a):
    pc = perf_config(cfg)
    db = a.db or pc["db"]
    requests = a.requests
    k6 = None
    if a.requests_from:
        k6 = k6_summary(_load_json(a.requests_from))
        requests = requests or k6.get("requests")
    try:
        requests = int(requests)
    except (TypeError, ValueError):
        requests = 0
    if requests <= 0:
        return emit({"ok": False, "errors": [f"요청 수가 '{requests}'다. 분모가 0이라 요청당 값을 낼 수 없다"]}, 1)
    rows = read_stats_raw(a.raw)
    if not rows:
        return emit({"ok": False, "errors": [f"원본에 행이 없다: {a.raw}. 통계 리셋 뒤 부하가 DB에 닿았는지 확인하라"]}, 1)
    stats, grand = compute_stats(rows, requests)
    n = _state_of(a.out, "query-stats-summary-")
    n_text = str(n) if n is not None else "{n}"
    slug = os.path.basename(os.path.dirname(os.path.abspath(a.out)))
    if a.state:
        state = f"{n_text} = {a.state}"
    elif n == 0:
        state = "0 = 원본"
    else:
        state = f"{n_text} = 사이클 {n_text} 적용 후: {a.technique or '{적용한 기법}'}"
    prev_path = a.prev
    if prev_path is None and not a.no_prev and n:
        cand = _sibling(a.out, "query-stats-summary-", n - 1, ".md")
        prev_path = cand if os.path.exists(cand) else None
    prev, carry = None, {}
    if prev_path:
        prev, carry = stats_prev_compare(prev_path, stats, requests, os.path.basename(prev_path))
    top = a.top or int(pc.get("queryStatsTop") or 20)
    text, pending = render_stats_md(stats, grand, requests, db, n_text, slug, state, _condition_text(k6), top, prev, carry)
    with open(a.out, "w", encoding="utf-8") as f:
        f.write(text)
    sql, ctl = _stats_totals(stats)
    return emit({
        "ok": True, "out": _rel(cfg, a.out), "requests": requests, "digests": len(stats), "shown": min(top, len(stats)),
        "sqlPerRequest": _fixed(sql, 4), "txControlPerRequest": _fixed(ctl, 4),
        "dbMsPerRequest": _fixed(grand / requests, 6),
        "top": [{"#": i, "per_req": _fixed(x["per_req"], 4), "pct": _fixed(x["pct"], 4), "tx": x["tx"],
                 "query": x["query"][:120]} for i, x in enumerate(stats[:min(top, 5)], 1)],
        "pendingRows": pending, "prev": os.path.basename(prev_path) if prev_path else None,
        "truncated": [i for i, x in enumerate(stats[:top], 1) if db == "mysql" and x["query"].rstrip().endswith("...")],
    })


# ── 전후 비교 ───────────────────────────────────────────────────────────────────

def change(before, after, kind="lower"):
    """변화 칸. 감소는 %, 처리량 증가는 배수, 퍼센트 값은 %p, 같으면 `동일`."""
    b, c = _dec(before), _dec(after)
    if b is None and c is None:
        return "-"
    if b is None:
        return "신규"
    if c is None:
        return "소멸"
    if b == c:
        return "동일"
    if kind == "pct":
        d = c - b
        return f"{'+' if d > 0 else '-'}{_fixed(abs(d), 2)}%p"
    if b == 0:
        return f"+{_fixed(c, 4)}" if c > 0 else f"-{_fixed(-c, 4)}"
    if kind == "throughput" and c > b:
        r = c / b
        return f"{_fixed(r, 2 if r < 10 else 1)}배"
    d = (c - b) / abs(b) * 100
    return f"{'+' if d > 0 else '-'}{_fixed(abs(d), 1)}%"


def _metric_k6(path):
    s = k6_summary(_load_json(path))
    d = s.get("duration_ms") or {}

    def pct(v):
        return None if v is None else Decimal(str(v)) * 100

    return [
        ("의존", "p95 (ms)", d.get("p95"), 3, "lower"),
        ("의존", "p99 (ms)", d.get("p99"), 3, "lower"),
        ("의존", "med (ms)", d.get("med"), 3, "lower"),
        ("의존", "max (ms)", d.get("max"), 3, "lower"),
        ("의존", "RPS", s.get("rps"), 2, "throughput"),
        ("의존", "에러율 (%)", pct(s.get("failed_rate")), 2, "pct"),
        ("의존", "check 통과율 (%)", pct(s.get("checks_rate")), 2, "pct"),
    ]


def _md_tables(path):
    """md의 `## 제목` 아래 표를 {제목: [행 셀 목록]}으로."""
    with open(path, encoding="utf-8") as f:
        text = f.read()
    out, cur = {}, None
    for line in text.split("\n"):
        if line.startswith("## "):
            cur = line[3:].strip()
            out.setdefault(cur, [])
            continue
        cells = _md_cells(line)
        if cur and cells and not set("".join(cells)) <= set("-: "):
            out[cur].append(cells)
    return out


def _first_num(s):
    m = re.search(r"-?\d+(?:\.\d+)?", s or "")
    return m.group(0) if m and "미수집" not in (s or "") else None


def _metric_jvm(path):
    t = _md_tables(path)
    gauge = {r[0]: r for r in t.get("게이지 (샘플 중 최대, 평균)", [])[1:]}
    cum = {r[0]: r for r in t.get("누적 (측정 구간 증분)", [])[1:]}

    def g(name, col=1, part=0):
        r = gauge.get(name)
        if not r:
            return None
        v = r[col].split("/")
        return _first_num(v[part]) if part < len(v) else None

    def c(name, col):
        r = cum.get(name)
        return _first_num(r[col]) if r else None

    out = [
        ("의존", "heap 최대 (MB)", g("heap 사용 (MB)"), 1, "lower"),
        ("의존", "GC 일시정지 합 (ms)", c("GC 일시정지 합 (ms)", 3), 1, "lower"),
        ("의존", "GC 최장 정지 (ms)", g("GC 최장 정지 (ms)"), 1, "lower"),
        ("의존", "GC overhead 최대", g("GC overhead"), 3, "lower"),
        ("의존", "HikariCP pending 최대", g("HikariCP pending"), 0, "lower"),
        ("의존", "HikariCP acquire max (ms)", g("HikariCP acquire max (ms)"), 1, "lower"),
        ("의존", "커넥션 보유 평균 (ms)", c("커넥션 보유 평균 (ms)", 3), 1, "lower"),
        ("의존", "blocked 스레드 최대", g("스레드 수 / blocked", 1, 1), 0, "lower"),
        ("의존", "process CPU 최대", g("process CPU"), 3, "lower"),
        ("독립", "요청당 할당량 (MB)", c("할당량 (MB)", 4), 3, "lower"),
        ("독립", "요청당 GC 횟수", c("GC 횟수", 4), 3, "lower"),
    ]
    repo = t.get("리포지토리 호출", [])[1:]
    if repo:
        total = sum((_dec(r[2]) or Decimal(0)) for r in repo)
        out.append(("독립", "요청당 리포지토리 호출 수", total, 3, "lower"))
    else:
        out.append(("독립", "요청당 리포지토리 호출 수", 0, 3, "lower"))
    for r in t.get("캐시", [])[1:]:
        out.append(("독립", f"캐시 적중률 `{r[0]}` (%)", _first_num(r[-1]), 1, "pct"))
    return out


def _metric_stats(path):
    p = parse_stats_md(path)
    rows = p["rows"]
    meta = p["meta"].get("요청당", "")
    # 요청당 합계는 머리글에서 읽는다. 표는 상위 N행만 있어 행 합산은 전체보다 작다
    mh = re.search(r"SQL ([\d.]+) \+ 트랜잭션 제어 ([\d.]+)", meta)
    if mh:
        sql, ctl = Decimal(mh.group(1)), Decimal(mh.group(2))
    else:
        sql, ctl = _stats_totals(rows)
    m = re.search(r"DB 실행 시간 ([\d.]+) ms", meta)
    out = [
        ("의존", "요청당 DB 실행 시간 (ms)", m.group(1) if m else None, 4, "lower"),
        ("독립", "요청당 SQL 수", sql, 4, "lower"),
        ("독립", "요청당 트랜잭션 제어 수", ctl, 4, "lower"),
    ]
    return out, [r for r in rows if r.get("query") and not _is_tx_row(r)]


def _named(src):
    return bool(src) and src not in (SRC_PLACEHOLDER, "-", "미상")


def _pair_queries(bq, cq, min_pct=Decimal(1)):
    """같은 원문끼리 짝짓고, 원문이 바뀐 쿼리는 출처가 같고 양쪽에서 유일하면 짝짓는다. 비중 1% 미만은 뺀다."""
    pairs, used = [], set()
    by_text = {r["query"]: i for i, r in enumerate(cq)}

    def uniq(rows, src):
        return sum(1 for r in rows if r.get("출처") == src) == 1

    for rb in bq:
        j = by_text.get(rb["query"])
        if j is None and _named(rb.get("출처")) and uniq(bq, rb["출처"]) and uniq(cq, rb["출처"]):
            j = next(i for i, r in enumerate(cq) if r.get("출처") == rb["출처"])
        if j is not None and j not in used:
            used.add(j)
            pairs.append((rb, cq[j]))
        else:
            pairs.append((rb, None))
    pairs += [(None, r) for i, r in enumerate(cq) if i not in used]

    def pct(r):
        return _dec((r or {}).get("비중", "").rstrip("%")) or Decimal(0)
    return [(x, y) for x, y in pairs if max(pct(x), pct(y)) >= min_pct]


PLAN_HANDLER_KEYS = ("Handler_read_rnd_next", "Handler_read_key", "Handler_read_next", "Handler_read_rnd",
                     "Sort_rows", "Sort_scan")


def parse_plan(path):
    """query-plan txt를 라벨 블록(`=== [Q1] 이름 ===`)별로. 같은 라벨이 다시 나오면 마지막 것을 쓴다."""
    with open(path, encoding="utf-8", errors="replace") as f:
        text = f.read()
    blocks, order = {}, []
    parts = re.split(r"(?m)^=== \[([^\]]+)\][^\n]*===\s*$", text)
    if len(parts) == 1:
        parts = ["", "Q1", text]
    for i in range(1, len(parts) - 1, 2):
        label, body = parts[i].strip(), parts[i + 1]
        info = {}
        m = re.search(r"Execution Time: ([\d.]+) ms", body)
        if m:  # PostgreSQL
            info["total_ms"] = m.group(1)
            b = re.search(r"Buffers: shared( hit=(\d+))?( read=(\d+))?", body)
            if b:
                info["shared_blocks"] = str(int(b.group(2) or 0) + int(b.group(4) or 0))
            removed, loops = Decimal(0), Decimal(1)
            for line in body.split("\n"):
                lm = re.search(r"\(actual time=[\d.]+\.\.[\d.]+ rows=[\d.]+ loops=(\d+)\)", line)
                if lm:
                    loops = Decimal(lm.group(1))
                rm = re.search(r"Rows Removed by (?:Filter|Join Filter|Index Recheck): (\d+)", line)
                if rm:
                    removed += Decimal(rm.group(1)) * loops
            info["rows_removed"] = str(removed)
        else:  # MySQL EXPLAIN ANALYZE 루트 노드
            m = re.search(r"-> [^\n]*?\(actual time=[\d.]+\.\.([\d.]+) rows=[\d.]+ loops=(\d+)\)", body)
            if m:
                info["total_ms"] = str(Decimal(m.group(1)) * Decimal(m.group(2)))
            for k in PLAN_HANDLER_KEYS:
                hm = re.findall(rf"(?m)^{k}\s+(\d+)\s*$", body)
                if hm:
                    info[k] = hm[-1]
                elif "=== STATUS COUNTERS ===" in body:
                    info[k] = "0"  # 카운터 블록은 0보다 큰 값만 싣는다
        if label not in blocks:
            order.append(label)
        blocks[label] = info
    return [(k, blocks[k]) for k in order]


def _metric_plan(path):
    out = []
    for label, info in parse_plan(path):
        out.append(("의존", f"쿼리 전체 소요 [{label}] (ms)", info.get("total_ms"), 3, "lower"))
        for k in PLAN_HANDLER_KEYS:
            if k in info:
                out.append(("독립", f"`{k}` [{label}]", info[k], 0, "lower"))
        if "shared_blocks" in info:
            out.append(("독립", f"shared hit+read [{label}]", info["shared_blocks"], 0, "lower"))
        if "rows_removed" in info:
            out.append(("독립", f"Rows Removed 합 [{label}]", info["rows_removed"], 0, "lower"))
    return out


KINDS = (
    ("k6", "k6-test-summary-", ".json"),
    ("jvm", "jvm-metrics-", ".md"),
    ("stats", "query-stats-summary-", ".md"),
    ("plan", "query-plan-", ".txt"),
)


def _kind_of(path):
    base = os.path.basename(path)
    for kind, prefix, ext in KINDS:
        if base.startswith(prefix) and base.endswith(ext):
            return kind
    return None


def _fmt(v, places):
    d = _dec(v)
    return "-" if d is None else _fixed(d, places)


def delta_rows(kind, base, cur):
    if kind == "k6":
        b, c = _metric_k6(base), _metric_k6(cur)
    elif kind == "jvm":
        b, c = _metric_jvm(base), _metric_jvm(cur)
    elif kind == "plan":
        b, c = _metric_plan(base), _metric_plan(cur)
    else:
        (b, bq), (c, cq) = _metric_stats(base), _metric_stats(cur)
        seen = {}
        for rb, rc in _pair_queries(bq, cq):
            ref = rc or rb
            name = ref.get("출처") or "-"
            if not _named(name):
                name = f"[{ref['#']}]"
            # 같은 출처가 여러 쿼리로 쪼개지면 번호를 붙여 행이 겹치지 않게 한다
            seen[name] = seen.get(name, 0) + 1
            if seen[name] > 1:
                name = f"{name} ({seen[name]})"
            for col in ("요청당", "읽은행/반환행"):
                if (rb and col in rb) or (rc and col in rc):
                    b.append(("독립", f"{col} {name}", rb.get(col) if rb else None, 4, "lower"))
                    c.append(("독립", f"{col} {name}", rc.get(col) if rc else None, 4, "lower"))
    cur_map = {m[1]: m for m in c}
    names = [m[1] for m in b] + [m[1] for m in c if m[1] not in {x[1] for x in b}]
    base_map = {m[1]: m for m in b}
    rows = []
    for name in names:
        mb, mc = base_map.get(name), cur_map.get(name)
        ref = mb or mc
        vb, vc = (mb[2] if mb else None), (mc[2] if mc else None)
        if vb is None and vc is None:
            continue
        rows.append({"group": "하드웨어 " + ref[0], "metric": name, "before": _fmt(vb, ref[3]),
                     "after": _fmt(vc, ref[3]), "change": change(vb, vc, ref[4]), "source": kind})
    return rows


def render_delta(rows, labels):
    head = f"| 구분 | 지표 | {labels[0]} | {labels[1]} | 변화 |\n|---|---|---|---|---|\n"
    body, last = [], None
    for grp in ("하드웨어 의존", "하드웨어 독립"):
        for r in rows:
            if r["group"] != grp:
                continue
            g = grp if grp != last else ""
            last = grp
            body.append(f"| {g} | {r['metric']} | {r['before']} | {r['after']} | {r['change']} |")
    return head + "\n".join(body) + "\n"


def cmd_delta(cfg, a):
    pairs, missing = [], []
    if a.dir:
        for kind, prefix, ext in KINDS:
            b = os.path.join(a.dir, f"{prefix}{a.base}{ext}")
            c = os.path.join(a.dir, f"{prefix}{a.current}{ext}")
            if os.path.exists(b) and os.path.exists(c):
                pairs.append((kind, b, c))
            else:
                missing.append(f"{prefix}{{{a.base},{a.current}}}{ext}")
    else:
        kind = _kind_of(a.base) or _kind_of(a.current)
        if not kind or _kind_of(a.current) not in (kind, None):
            return emit({"ok": False, "errors": ["파일 종류를 알 수 없거나 서로 다르다. "
                                                 "k6-test-summary-*.json, jvm-metrics-*.md, query-stats-summary-*.md, query-plan-*.txt"]}, 1)
        pairs.append((kind, a.base, a.current))
    if not pairs:
        return emit({"ok": False, "errors": ["비교할 파일 쌍이 없다"], "missing": missing}, 1)
    rows = []
    for kind, b, c in pairs:
        rows += delta_rows(kind, b, c)
    labels = [x.strip() for x in (a.labels or "개선 전,개선 후").split(",")][:2]
    table = render_delta(rows, labels)
    if a.out:
        with open(a.out, "w", encoding="utf-8") as f:
            f.write(table)
    return emit({"ok": True, "compared": [os.path.basename(c) for _, _, c in pairs], "missing": missing,
                 "rows": len(rows), "out": _rel(cfg, a.out) if a.out else None,
                 "llmRows": ["접근 방식과 인덱스", "대상 쿼리 지정(출처)", "캐시 hit / miss (캐싱 사이클)"],
                 "report": table})


# ── 진행 상태 ───────────────────────────────────────────────────────────────────

PREP_HEADER = re.compile(r"^\|\s*1\. ")
CYCLE_HEADER = re.compile(r"^\|\s*#\s*\|\s*기법\s*\|")
REPORT_HEADER = re.compile(r"^\|\s*9\. ")


def _find_table(lines, header_re):
    for i, line in enumerate(lines):
        if header_re.match(line):
            j = i + 2
            rows = []
            while j < len(lines) and lines[j].strip().startswith("|"):
                rows.append(j)
                j += 1
            return i, rows
    return None, []


def _mark_of(cell):
    for m in MARKS:
        if m in cell:
            return m
    return cell.strip() or None


def read_progress(path):
    """record.md의 진행 상태. next는 재개할 Phase(⏳로 표기된 가장 이른 것)."""
    with open(path, encoding="utf-8") as f:
        lines = f.read().split("\n")
    out = {"record": path, "prep": {}, "cycles": [], "phase9": None, "memo": None}
    _, prep_rows = _find_table(lines, PREP_HEADER)
    if prep_rows:
        cells = _md_cells(lines[prep_rows[0]]) or []
        for k, c in enumerate(cells[:4], 1):
            out["prep"][str(k)] = _mark_of(c)
    _, cyc_rows = _find_table(lines, CYCLE_HEADER)
    for j in cyc_rows:
        cells = _md_cells(lines[j]) or []
        if len(cells) < 6 or not cells[0].strip().isdigit():
            continue
        out["cycles"].append({"n": int(cells[0]), "technique": cells[1],
                              **{str(p): _mark_of(cells[p - 3]) for p in range(5, 9)}})
    _, rep_rows = _find_table(lines, REPORT_HEADER)
    if rep_rows:
        out["phase9"] = _mark_of((_md_cells(lines[rep_rows[0]]) or [""])[0])
    for line in lines:
        if line.startswith("**재개 메모**"):
            out["memo"] = line.split(":", 1)[1].strip() if ":" in line else ""
            break
    out["next"] = _next_phase(out)
    return out


def _next_phase(p):
    def nxt(phase, cycle=None):
        return {"phase": phase, "cycle": cycle, "file": f"{SKILL_DIR}/phases/{PHASE_FILES[phase]}"}
    for k in range(1, 5):
        if p["prep"].get(str(k)) not in ("✅", "⏭️"):
            return nxt(k)
    if not p["cycles"]:
        return nxt(5, 1)
    last = p["cycles"][-1]
    for k in range(5, 9):
        if last.get(str(k)) not in ("✅", "⏭️"):
            return nxt(k, last["n"])
    if p["phase9"] == "✅":
        return {"phase": None, "cycle": None, "done": True}
    return {"phase": None, "cycle": last["n"] + 1, "choices": [5, 9],
            "note": "마지막 사이클이 끝났다. 사이클 판정과 호출자 선택에 따라 Phase 5(다음 사이클) 또는 Phase 9"}


def _set_cell(line, idx, value):
    cells = _md_cells(line)
    cells[idx] = value
    return "| " + " | ".join(cells) + " |"


def update_progress(path, phase, mark, cycle=None, technique=None, memo=None):
    with open(path, encoding="utf-8") as f:
        lines = f.read().split("\n")
    if mark not in MARKS:
        raise ValueError(f"표기는 {', '.join(MARKS)} 중 하나다")
    if 1 <= phase <= 4:
        _, rows = _find_table(lines, PREP_HEADER)
        if not rows:
            raise ValueError("준비 표(| 1. 대상 | ...)를 찾지 못했다")
        lines[rows[0]] = _set_cell(lines[rows[0]], phase - 1, mark)
    elif 5 <= phase <= 8:
        hdr, rows = _find_table(lines, CYCLE_HEADER)
        if hdr is None:
            raise ValueError("사이클 표(| # | 기법 | ...)를 찾지 못했다")
        nums = {}
        for j in rows:
            cells = _md_cells(lines[j]) or []
            if cells and cells[0].strip().isdigit():
                nums[int(cells[0])] = j
        if cycle is None:
            # Phase 5는 새 사이클의 시작이라 번호 없이 받으면 끝난 사이클 행을 덮어쓴다
            if phase == 5 and nums:
                raise ValueError(f"Phase 5는 --cycle이 필요하다. 진행 중인 사이클은 {max(nums)}, 새 사이클이면 {max(nums) + 1}이다")
            cycle = max(nums) if nums else 1
        if cycle not in nums:
            if phase != 5 or (nums and cycle != max(nums) + 1) or (not nums and cycle != 1):
                raise ValueError(f"사이클 {cycle} 행이 없다. 새 사이클은 Phase 5에서 다음 번호로만 추가한다")
            new = f"| {cycle} | | ⏳ | ⏳ | ⏳ | ⏳ |"
            at = (rows[-1] + 1) if rows else hdr + 2
            lines.insert(at, new)
            nums[cycle] = at
        j = nums[cycle]
        if technique is not None:
            lines[j] = _set_cell(lines[j], 1, technique.replace("|", "\\|"))
        lines[j] = _set_cell(lines[j], phase - 3, mark)
    elif phase == 9:
        _, rows = _find_table(lines, REPORT_HEADER)
        if not rows:
            raise ValueError("마무리 표(| 9. 보고 |)를 찾지 못했다")
        lines[rows[0]] = _set_cell(lines[rows[0]], 0, mark)
    else:
        raise ValueError("Phase는 1~9다")
    if memo is not None:
        for i, line in enumerate(lines):
            if line.startswith("**재개 메모**"):
                lines[i] = f"**재개 메모**: {memo}"
                break
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return read_progress(path)


def cmd_progress(cfg, a):
    if a.phase is None:
        p = read_progress(a.record)
    else:
        if not a.mark:
            return emit({"ok": False, "errors": ["--phase에는 --mark가 필요하다"]}, 1)
        p = update_progress(a.record, a.phase, a.mark, a.cycle, a.technique, a.memo)
    p["record"] = _rel(cfg, p["record"])
    return emit({"ok": True, **p})


# ── 스킬 맥락 ───────────────────────────────────────────────────────────────────

def _token_info(path):
    """tokens.json의 건수와 첫 토큰 만료 시각. 서명은 검증하지 않는다."""
    try:
        data = _load_json(path)
        tok = data[0]["accessToken"] if data and isinstance(data[0], dict) else data[0]
        payload = tok.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        exp = claims.get("exp")
        return {"count": len(data), "exp": exp,
                "expiresAt": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(exp)) if exp else None,
                "expired": bool(exp and exp <= time.time())}
    except (OSError, ValueError, KeyError, IndexError, TypeError, AttributeError):
        return {"count": None, "exp": None, "expiresAt": None, "expired": None}


def skill_context(cfg, issue):
    """optimize-performance 로드 시 주입할 perf 맥락. skill_context.build가 공통 필드 위에 얹는다."""
    root = cfg["_root"]
    pc = perf_config(cfg)
    if pc.get("dbPassword"):
        pc["dbPassword"] = "(설정됨)"  # 비밀번호 값은 맥락에 싣지 않는다
    db = (pc.get("db") or "").lower()
    db_ok = db in ("mysql", "postgresql")
    ctx = {"perf": pc, "perfConfigured": bool((cfg.get("perf") or {}).get("dbName")) and db_ok,
           "stackGuide": f"{SKILL_DIR}/stacks/{db}.md" if db_ok else None,
           "perfErrors": [] if db_ok else [f"perf.db가 '{db}'다. mysql 또는 postgresql로 .claude/harness.json의 perf 절을 채워라"],
           "perfEnv": f"{SKILL_DIR}/template/perf-env.sh",
           "scripts": {"jvmSampler": f"{SCRIPTS_DIR}/jvm-sampler.sh", "mintTokens": f"{SCRIPTS_DIR}/mint-tokens.sh"}}
    seeds_dir = os.path.join(root, pc["seedsDir"])
    ctx["seedModules"] = sorted(f for f in os.listdir(seeds_dir) if f.endswith(".sql")) if os.path.isdir(seeds_dir) else []
    ctx["seedsReadme"] = os.path.join(pc["seedsDir"], "README.md") if os.path.exists(os.path.join(seeds_dir, "README.md")) else None
    ctx["perfDir"] = None
    ctx["targets"] = []
    ctx["shared"] = {"seeds": None, "tokens": None}
    if issue:
        rel = os.path.join(pc["resultsDir"], str(issue))
        ctx["perfDir"] = rel
        d = os.path.join(root, rel)
        if os.path.isdir(d):
            for name in sorted(os.listdir(d)):
                rec = os.path.join(d, name, "record.md")
                if os.path.isfile(rec):
                    try:
                        prog = read_progress(rec)
                    except (OSError, ValueError, IndexError) as e:
                        prog = {"error": str(e)}
                    prog.pop("record", None)
                    ctx["targets"].append({"slug": name, "record": os.path.join(rel, name, "record.md"), "progress": prog})
            if os.path.exists(os.path.join(d, "seeds.sql")):
                ctx["shared"]["seeds"] = os.path.join(rel, "seeds.sql")
            tp = os.path.join(d, "tokens.json")
            if os.path.exists(tp):
                ctx["shared"]["tokens"] = {"path": os.path.join(rel, "tokens.json"), **_token_info(tp)}
    return ctx


# ── 등록 ────────────────────────────────────────────────────────────────────────

def register(subparsers):
    s = subparsers.add_parser("perf-summarize-k6", help="k6 원본 요약 → k6-test-summary-{n}.json (직전 상태 delta 포함)")
    s.add_argument("raw", help="handleSummary 원본(harness_meta 포함) 또는 --summary-export 출력. 가공본도 받는다")
    s.add_argument("--out", required=True, help="k6-test-summary-{n}.json. 원본과 같은 경로여도 된다")
    s.add_argument("--prev", help="비교할 직전 가공본. 생략하면 같은 디렉토리의 -{n-1}")
    s.add_argument("--no-prev", action="store_true")
    s.set_defaults(fn=cmd_summarize_k6)

    s = subparsers.add_parser("perf-query-stats", help="digest / pg_stat_statements 원본 → query-stats-summary-{n}.md")
    s.add_argument("raw", help="mysql -B(TSV) 또는 psql --csv 출력")
    s.add_argument("--db", choices=["mysql", "postgresql"], help="생략하면 perf.db")
    s.add_argument("--requests", type=int, help="요청 수 (per_req 분모)")
    s.add_argument("--requests-from", help="k6-test-summary-{n}.json. 요청 수와 측정 조건을 여기서 읽는다")
    s.add_argument("--out", required=True)
    s.add_argument("--state", help="상태 설명. 생략하면 0은 원본, n은 '사이클 n 적용 후: --technique'")
    s.add_argument("--technique", help="이번 상태에 적용한 기법")
    s.add_argument("--prev", help="직전 가공본. 생략하면 같은 디렉토리의 -{n-1}")
    s.add_argument("--no-prev", action="store_true")
    s.add_argument("--top", type=int, help="표에 싣는 상위 쿼리 수. 생략하면 perf.queryStatsTop")
    s.set_defaults(fn=cmd_query_stats)

    s = subparsers.add_parser("perf-delta", help="두 상태의 산출물 전후 비교 표")
    s.add_argument("base", help="개선 전 파일, 또는 --dir와 함께 상태 번호")
    s.add_argument("current", help="개선 후 파일, 또는 --dir와 함께 상태 번호")
    s.add_argument("--dir", help="대상 디렉토리. 주면 base, current를 상태 번호로 보고 산출물 네 종류를 모두 비교한다")
    s.add_argument("--labels", help="칼럼 이름 두 개. 기본 '개선 전,개선 후'")
    s.add_argument("--out", help="표를 파일로도 쓴다")
    s.set_defaults(fn=cmd_delta)

    s = subparsers.add_parser("perf-progress", help="record.md 진행 상태 조회, 갱신")
    s.add_argument("record")
    s.add_argument("--phase", type=int, help="1~9. 생략하면 조회만")
    s.add_argument("--mark", choices=list(MARKS))
    s.add_argument("--cycle", type=int, help="Phase 5~8의 사이클 번호. Phase 5는 필수(다음 번호면 행 추가), 6~8은 생략하면 마지막 행")
    s.add_argument("--technique", help="사이클 행의 기법 칸")
    s.add_argument("--memo", help="재개 메모 줄을 이 내용으로 바꾼다")
    s.set_defaults(fn=cmd_progress)


if __name__ == "__main__":  # 단독 실행(테스트, 디버깅)
    import argparse
    p = argparse.ArgumentParser(prog="perf_tools")
    register(p.add_subparsers(dest="cmd", required=True))
    args = p.parse_args()
    sys.exit(args.fn({}, args))
