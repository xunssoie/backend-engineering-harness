"""fix-concurrency 산출물 변환. 측정 원본(k6 요약, 락 카운터 스냅샷, 불변식 검증 출력)을 결정적으로 가공한다.

LLM이 원본을 읽고 손으로 빼고 나누던 일을 여기서 한다. 원본 파일은 고치지 않는다.
수치는 반올림하지 않는다. 표의 `변화` 칸의 배율만 소수 둘째 자리로 표기한다.

명령:
  conc-shell          측정 블록에 넣을 셸 변수와 DB 접속 함수(dbc)
  conc-burst-summary  k6 원본 요약 → k6-burst-summary-{n}.json (+ delta_vs_base)
  conc-lock-delta     락 카운터 BEFORE/AFTER 스냅샷 → 측정 구간 증가분
  conc-compare        대상 디렉토리의 후보 비교표 (항상 -0 대비)
  conc-progress       record.md 진행 상태 표 갱신과 재개 지점 판정
"""
import csv
import glob
import io
import json
import os
import re
import shlex
import shutil

# harness_config.DEFAULTS에 병합한다. DB 접속(db, dbContainer, dbName, dbUser 등)은 perf 절을 같이 쓴다.
CONCURRENCY_DEFAULTS = {
    "concurrency": {
        # 산출물 루트. 이슈별 디렉토리와 시드 모듈(seeds/)이 이 아래에 생긴다
        "dir": ".claude/resources/concurrency",
        # 측정용 애플리케이션 프로파일 이름. appStartCommand의 {profile} 자리에 들어간다
        "profile": "conc",
        # 측정용 프로파일 파일. perf 프로파일과 커넥션 풀 규칙이 반대라 따로 둔다
        "profileFile": "src/main/resources/application-conc.yml",
        # 애플리케이션 기동 명령. restart-app.sh가 레포 루트에서 백그라운드로 실행한다
        "appStartCommand": "./gradlew bootRun --args='--spring.profiles.active={profile}'",
        # 애플리케이션 포트. 재기동 전에 이 포트를 점유한 프로세스를 내린다
        "appPort": 8080,
        # 기동 확인 URL과 응답에 있어야 하는 문자열
        "healthUrl": "http://localhost:8080/actuator/health",
        "healthUpPattern": "\"status\":\"UP\"",
        # 기동 로그 경로(레포 루트 기준)와 기동 대기 상한(초)
        "bootLog": "build/conc-boot.log",
        "bootTimeoutSec": 120,
        # 커넥션 풀 충전 대기 기준(DB 접속 수). 풀의 minimum-idle 값. 0이면 대기하지 않는다
        "poolMinIdle": 0,
        # MySQL 접속 시 SET NAMES에 붙일 collation. 비우면 서버 기본. 컬럼 collation과 다르면 여기에 적는다
        "mysqlCollation": "",
        # 부하 중 대기 중인 락 수를 표본 수집할지와 간격(초). PostgreSQL은 락 대기 누적 카운터가 없어 이것이 주 지표다
        "sampleLockWaits": True,
        "sampleIntervalSec": 0.5,
        # PostgreSQL 통계(pg_stat_database)가 반영될 때까지 AFTER 스냅샷 전에 기다리는 시간(초).
        # 15 이상은 유휴 백엔드가 최대 10초 뒤에 통계를 내보낸다. MySQL은 쓰지 않는다
        "pgStatsFlushWaitSec": 11,
    }
}

MARKS = ("✅", "⏳", "⏭️")
DONE = ("✅", "⏭️")
PREP_PHASES = (1, 2, 3, 4)
CAND_PHASES = (5, 6, 7)
SKILL_DIR = ".claude/skills/fix-concurrency"


def _emit(obj, report=None, code=0):
    print(json.dumps(obj, ensure_ascii=False, separators=(",", ":")))
    if report:
        print("\n" + report)
    return code


def _dump(obj):
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def _section(cfg, name):
    v = cfg.get(name) if isinstance(cfg, dict) else None
    base = CONCURRENCY_DEFAULTS["concurrency"] if name == "concurrency" else {}
    return {**base, **(v or {})}


def _num(s):
    """문자열 숫자를 int 또는 float로. 숫자가 아니면 None. 자릿수는 그대로 둔다."""
    if isinstance(s, bool):
        return None
    if isinstance(s, (int, float)):
        return s
    if not isinstance(s, str):
        return None
    t = s.strip()
    if re.fullmatch(r"[-+]?\d+", t):
        return int(t)
    if re.fullmatch(r"[-+]?(\d+\.\d*|\.\d+|\d+)([eE][-+]?\d+)?", t):
        return float(t)
    return None


def _ratio(base, cand):
    if not isinstance(base, (int, float)) or not isinstance(cand, (int, float)) or base == 0:
        return "-"
    return f"{cand / base:.2f}배"


def _cell(v):
    if v is None:
        return "-"
    return str(v)


# ───────────────────────── conc-shell ─────────────────────────

def db_function(cfg, mask_password=False):
    """perf 절의 접속 설정으로 dbc 셸 함수 정의 한 줄을 만든다. 문자열 변수가 아니라 함수여야 zsh에서도 돈다.
    mask_password=True면 비밀번호 자리를 ***로 가린다. 스킬 맥락에 주입할 때 쓴다."""
    perf = _section(cfg, "perf")
    conc = _section(cfg, "concurrency")
    db = (perf.get("db") or "").lower()
    container = perf.get("dbContainer") or ""
    name = perf.get("dbName") or ""
    user = perf.get("dbUser") or ""
    # 비밀번호는 환경 변수(perf.dbPasswordEnv)가 있으면 그것, 없으면 perf.dbPassword. optimize-performance(perf-env.sh)와 같은 순서
    env_name = perf.get("dbPasswordEnv") or ""
    pw = (os.environ.get(env_name) if env_name else None) or perf.get("dbPassword") or None
    if pw and mask_password:
        pw = "***"
    host = perf.get("dbHost") or "127.0.0.1"
    port = perf.get("dbPort")
    q = shlex.quote
    env = []  # 호스트 클라이언트일 때 명령 앞에 붙는 VAR=값. 이름은 따옴표 밖에 둬야 셸이 대입으로 읽는다
    if db == "mysql":
        init = "SET NAMES utf8mb4" + (f" COLLATE {conc['mysqlCollation']}" if conc.get("mysqlCollation") else "")
        client = ["mysql"] + ([f"-u{user}"] if user else []) + ["--default-character-set=utf8mb4", f"--init-command={init}"]
        if container:
            head = ["docker", "exec", "-i"] + (["-e", f"MYSQL_PWD={pw}"] if pw else []) + [container]
        else:
            head = []
            env = [("MYSQL_PWD", pw)] if pw else []
            client[1:1] = ["-h", host] + (["-P", str(port)] if port else [])
        args = head + client + ([name] if name else [])
    elif db == "postgresql":
        client = ["psql", "-X", "-q", "-v", "ON_ERROR_STOP=1"] + (["-U", user] if user else []) + (["-d", name] if name else [])
        if container:
            head = ["docker", "exec", "-i"] + (["-e", f"PGPASSWORD={pw}"] if pw else []) + ["-e", "PGCLIENTENCODING=UTF8", container]
        else:
            head = []
            env = ([("PGPASSWORD", pw)] if pw else []) + [("PGCLIENTENCODING", "UTF8")]
            client[1:1] = ["-h", host] + (["-p", str(port)] if port else [])
        args = head + client
    else:
        return None
    words = [f"{k}={q(str(v))}" for k, v in env] + [q(x) for x in args]
    return "dbc() { " + " ".join(words) + ' "$@"; }'


def shell_prelude(cfg, issue=None, slug=None):
    conc = _section(cfg, "concurrency")
    lines = []
    if issue:
        lines.append(f"export CONC_DIR={shlex.quote(os.path.join(conc['dir'], str(issue)))}")
        if slug:
            lines.append(f'export TARGET_DIR="$CONC_DIR"/{shlex.quote(slug)}')
    fn = db_function(cfg)
    if fn:
        lines.append(fn)
    return "\n".join(lines)


def cmd_shell(cfg, a):
    fn = db_function(cfg)
    if not fn:
        print("echo '!! perf.db가 mysql 또는 postgresql이 아니다. .claude/harness.json의 perf 절을 확인하라' >&2; false")
        return 1
    print(shell_prelude(cfg, a.issue, a.slug))
    return 0


# ───────────────────────── conc-burst-summary ─────────────────────────

def _metric_values(metrics, name):
    m = metrics.get(name)
    if not isinstance(m, dict):
        return {}
    v = m.get("values")
    return v if isinstance(v, dict) else m  # handleSummary 형식은 values 아래, --summary-export 형식은 바로 아래


def _trend(metrics, name):
    v = _metric_values(metrics, name)
    if not v:
        return None
    return {"med": v.get("med"), "p95": v.get("p(95)"), "p99": v.get("p(99)"), "max": v.get("max")}


def shape_burst(raw):
    """k6 원본 요약을 k6-burst-summary 형태로. 이미 가공된 형태면 그대로 돌려준다."""
    if "outcome" in raw and "metrics" not in raw:
        return {k: v for k, v in raw.items() if k != "delta_vs_base"}
    meta = raw.get("burst_meta") or {}
    metrics = raw.get("metrics") or {}

    def count(name):
        return _metric_values(metrics, name).get("count") or 0

    reqs = _metric_values(metrics, "http_reqs")
    succeeded = count("req_succeeded")
    cap = meta.get("capacity")
    out = {
        "target": meta.get("target"),
        "endpoint": meta.get("endpoint"),
        "condition": {k: v for k, v in meta.items() if k not in ("target", "endpoint")},
        "requests": reqs.get("count"),
        "rps": reqs.get("rate"),
        "outcome": {
            "succeeded": succeeded,
            "rejected_expected": count("req_rejected_expected"),
            "rejected_other_4xx": count("req_rejected_other_4xx"),
            "failed_5xx": count("req_failed_5xx"),
            "unparsable": count("req_unparsable"),
        },
        "succeeded_over_capacity": succeeded - cap if isinstance(cap, (int, float)) else None,
        "duration_ms": _trend(metrics, "http_req_duration"),
        "waiting_ms": _trend(metrics, "http_req_waiting"),
    }
    return out


DELTA_KEYS = (
    ("rps", ("rps",)),
    ("duration_p95_ms", ("duration_ms", "p95")),
    ("duration_p99_ms", ("duration_ms", "p99")),
    ("succeeded", ("outcome", "succeeded")),
    ("rejected_expected", ("outcome", "rejected_expected")),
    ("rejected_other_4xx", ("outcome", "rejected_other_4xx")),
    ("failed_5xx", ("outcome", "failed_5xx")),
    ("unparsable", ("outcome", "unparsable")),
)


def _dig(d, path):
    for p in path:
        if not isinstance(d, dict):
            return None
        d = d.get(p)
    return d


def condition_warnings(base, cand):
    """후보마다 부하 조건이 같아야 비교가 성립한다. 다른 칸을 짚는다."""
    w = []
    for key in ("target", "endpoint"):
        if base.get(key) != cand.get(key):
            w.append(f"{key}가 다르다: base={base.get(key)} / candidate={cand.get(key)}")
    bc, cc = base.get("condition") or {}, cand.get("condition") or {}
    for key in sorted(set(bc) | set(cc)):
        if bc.get(key) != cc.get(key):
            w.append(f"condition.{key}가 다르다: base={bc.get(key)} / candidate={cc.get(key)}")
    if base.get("requests") != cand.get("requests"):
        w.append(f"requests가 다르다: base={base.get('requests')} / candidate={cand.get('requests')}")
    return w


def burst_summary(raw, base=None, base_name=None):
    s = shape_burst(raw)
    warnings = []
    if base is not None:
        b = shape_burst(base)
        s["delta_vs_base"] = {"from": base_name}
        for key, path in DELTA_KEYS:
            s["delta_vs_base"][key] = {"base": _dig(b, path), "candidate": _dig(s, path)}
        warnings = condition_warnings(b, s)
    return s, warnings


def _load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def cmd_burst_summary(cfg, a):
    raw = _load_json(a.raw)
    base = _load_json(a.base) if a.base else None
    s, warnings = burst_summary(raw, base, os.path.basename(a.base) if a.base else None)
    if a.out:
        if os.path.abspath(a.out) == os.path.abspath(a.raw) and "metrics" in raw:
            return _emit({"ok": False, "errors": ["--out이 원본과 같다. 원본은 덮어쓰지 않는다"]}, code=1)
        if a.base and os.path.abspath(a.out) == os.path.abspath(a.base):
            return _emit({"ok": False, "errors": ["--out이 비교 기준(-0)과 같다. 앞선 파일을 덮어쓰지 마라"]}, code=1)
        with open(a.out, "w", encoding="utf-8") as f:
            f.write(_dump(s) + "\n")
    res = {"ok": True, "out": a.out, "requests": s.get("requests"), "rps": s.get("rps"), "outcome": s.get("outcome"),
           "succeeded_over_capacity": s.get("succeeded_over_capacity")}
    if "delta_vs_base" in s:
        res["delta_vs_base"] = s["delta_vs_base"]
    if warnings:
        res["warnings"] = warnings
    if not a.out:
        res["summary"] = s
    return _emit(res)


# ───────────────────────── 표 형태 출력 파싱 ─────────────────────────

_SEP = re.compile(r"^[\s+\-|=]*$")


def _cells(line):
    """mysql -t / -B, psql aligned / --csv 한 줄을 칸 목록으로. 구분선이면 None."""
    s = line.rstrip("\n").rstrip("\r")
    if not s.strip() or _SEP.match(s) or re.fullmatch(r"\(\d+ rows?\)", s.strip()):
        return None
    if s.lstrip().startswith("|"):
        parts = s.strip().split("|")[1:-1]
        return [p.strip() for p in parts]
    if "\t" in s:
        return [p.strip() for p in s.split("\t")]
    if "|" in s:
        return [p.strip() for p in s.split("|")]
    if "," in s:
        return [p.strip() for p in next(csv.reader([s]))]
    return [s.strip()]


def _split_sections(text):
    """`=== 이름 ===` 줄로 구간을 나눈다. 첫 표지 전 내용은 이름 없는 구간."""
    sections, cur, buf = {}, "", []
    for line in text.splitlines():
        m = re.match(r"^\s*===\s*(.+?)\s*===\s*$", line)
        if m:
            sections.setdefault(cur, []).extend(buf)
            cur, buf = m.group(1).upper(), []
        else:
            buf.append(line)
    sections.setdefault(cur, []).extend(buf)
    return sections


def _name_values(lines):
    """name/value 두 칸 행을 모은다. 머리 행(값이 숫자가 아닌 행)은 건너뛴다."""
    out = {}
    for line in lines:
        c = _cells(line)
        if not c or len(c) < 2:
            continue
        v = _num(c[1])
        if v is None or not c[0]:
            continue
        out[c[0].lower()] = v
    return out


def _first_row(lines):
    """머리 행 다음 첫 데이터 행을 dict로. RESET 구간처럼 결과 한 행짜리를 읽는다."""
    header = None
    for line in lines:
        c = _cells(line)
        if not c:
            continue
        if header is None:
            if all(_num(x) is None for x in c):
                header = [x.lower() for x in c]
            continue
        if len(c) == len(header):
            return {h: (_num(v) if _num(v) is not None else v) for h, v in zip(header, c)}
    return {}


# ───────────────────────── conc-lock-delta ─────────────────────────

def detect_db(names):
    if any(n.startswith("innodb_") or n.startswith("lock_") for n in names):
        return "mysql"
    if any(n in ("deadlocks", "xact_commit", "xact_rollback") for n in names):
        return "postgresql"
    return None


def lock_delta(before, after, db=None, window=None, samples=None, deadlock_text=None, reset=None):
    window = window or {}
    db = db or detect_db(list(before) + list(after))
    counters = {}
    for k in sorted(set(before) | set(after)):
        b, a_ = before.get(k), after.get(k)
        counters[k] = {"before": b, "after": a_, "delta": a_ - b if b is not None and a_ is not None else None}

    def d(name):
        c = counters.get(name)
        return c["delta"] if c else None

    notes = []
    if db == "mysql":
        waits, time_ = d("innodb_row_lock_waits"), d("innodb_row_lock_time")
        m = {
            "lock_waits": waits,
            "row_lock_time_ms": time_,
            # 전역 누적 평균은 뺄 수 없다. 구간 평균은 시간 증가분 / 대기 증가분으로 낸다
            "row_lock_time_avg_ms": (time_ / waits) if waits and time_ is not None else None,
            "row_lock_time_max_ms_global": (counters.get("innodb_row_lock_time_max") or {}).get("after"),
            "row_lock_current_waits_after": (counters.get("innodb_row_lock_current_waits") or {}).get("after"),
            "deadlocks": d("lock_deadlocks"),
            "lock_timeouts": d("lock_timeouts"),
        }
        notes.append("row_lock_time_max_ms_global는 인스턴스 기동 이후 최댓값이라 구간 값이 아니다")
        if m["deadlocks"] is None:
            notes.append("lock_deadlocks가 없다. INNODB_METRICS가 비활성인지 확인하라")
    elif db == "postgresql":
        m = {
            # PostgreSQL에는 락 대기 누적 카운터가 없다. log_lock_waits 로그 건수(deadlock_timeout 초과 대기)로 대신한다
            "lock_waits": window.get("log_lock_waits"),
            "deadlocks": d("deadlocks"),
            "deadlocks_logged": window.get("log_deadlocks"),
            "lock_timeouts": window.get("log_lock_timeouts"),
            "xact_commit": d("xact_commit"),
            "xact_rollback": d("xact_rollback"),
        }
        if "log_lock_waits" not in window:
            notes.append("log_lock_waits 로그 집계가 없다. lock_waits는 표본(waiting)으로만 본다")
        if "log_lock_timeouts" not in window:
            notes.append("lock_timeouts 카운터가 없다. 서버 로그 또는 응답 코드(SQLSTATE 55P03)로 센다")
    else:
        m = {}
        notes.append("DB 종류를 판별하지 못했다. --db를 지정하라")
    if samples:
        m["waiting_samples"] = {"count": len(samples), "max": max(samples), "mean": sum(samples) / len(samples)}
    res = {"db": db, "metrics": m, "counters": counters}
    if window:
        res["window"] = window
    if deadlock_text is not None:
        res["deadlockTextPresent"] = deadlock_text
    if reset:
        res["reset"] = reset
    if notes:
        res["notes"] = notes
    return res


def parse_lock_file(text):
    """lock-stats-{n}.txt 한 파일. BEFORE, AFTER, WINDOW, WAITING SAMPLES, LATEST DEADLOCK, RESET 구간."""
    sec = _split_sections(text)
    samples = []
    for line in sec.get("WAITING SAMPLES", []):
        c = _cells(line)
        if c:
            v = _num(c[-1])
            if v is not None:
                samples.append(v)
    dl = None
    if "LATEST DEADLOCK" in sec:
        body = "\n".join(sec["LATEST DEADLOCK"])
        dl = bool(re.search(r"\*\*\* \(1\) TRANSACTION|deadlock detected|Process \d+ waits for", body))
    return {
        "before": _name_values(sec.get("BEFORE", [])),
        "after": _name_values(sec.get("AFTER", [])),
        "window": _name_values(sec.get("WINDOW", [])),
        "samples": samples,
        "deadlock_text": dl,
        "reset": _first_row(sec.get("RESET", [])),
    }


def lock_delta_from_file(path, db=None):
    with open(path, encoding="utf-8", errors="replace") as f:
        p = parse_lock_file(f.read())
    return lock_delta(p["before"], p["after"], db, p["window"], p["samples"], p["deadlock_text"], p["reset"])


def cmd_lock_delta(cfg, a):
    if len(a.files) == 1:
        r = lock_delta_from_file(a.files[0], a.db)
    elif len(a.files) == 2:
        with open(a.files[0], encoding="utf-8", errors="replace") as f:
            before = _name_values(f.read().splitlines())
        with open(a.files[1], encoding="utf-8", errors="replace") as f:
            after = _name_values(f.read().splitlines())
        r = lock_delta(before, after, a.db)
    else:
        return _emit({"ok": False, "errors": ["파일은 1개(구간 표지가 있는 lock-stats) 또는 2개(before, after)다"]}, code=1)
    if not r["counters"]:
        return _emit({"ok": False, "errors": ["카운터를 하나도 읽지 못했다. === BEFORE === / === AFTER === 구간과 name, value 두 칸 행을 확인하라"]}, code=1)
    if a.out:
        with open(a.out, "w", encoding="utf-8") as f:
            f.write(_dump(r) + "\n")
    return _emit({"ok": True, **r})


# ───────────────────────── 불변식 출력 파싱 ─────────────────────────

def parse_invariants(text):
    """invariant-check.sql 출력에서 invariant/violations 행을 모은다. 참고(metric) 행은 따로."""
    invariants, refs = [], []
    header = None
    for line in text.splitlines():
        if re.match(r"^\s*\|?\s*===.*===\s*\|?\s*$", line):
            continue
        c = _cells(line)
        if not c:
            continue
        low = [x.lower() for x in c]
        if "invariant" in low and "violations" in low:
            header = ("inv", low)
            continue
        if "metric" in low and all(_num(x) is None for x in c):
            header = ("ref", low)
            continue
        if header is None or len(c) != len(header[1]):
            continue
        row = dict(zip(header[1], c))
        if header[0] == "inv":
            name = row.get("invariant", "")
            m = re.match(r"^\s*([A-Za-z]+\d+)\b", name)
            invariants.append({
                "id": m.group(1) if m else name,
                "invariant": name,
                "expected": row.get("expected"),
                "actual": row.get("actual"),
                "violations": _num(row.get("violations")),
            })
        else:
            refs.append({k: (_num(v) if _num(v) is not None else v) for k, v in row.items()})
    return {"invariants": invariants, "refs": refs}


def parse_invariant_file(path):
    with open(path, encoding="utf-8", errors="replace") as f:
        return parse_invariants(f.read())


# ───────────────────────── record.md 진행 상태 ─────────────────────────

def _progress_bounds(lines):
    start = next((i for i, l in enumerate(lines) if re.match(r"^##\s+진행 상태", l)), None)
    if start is None:
        return None, None
    end = next((i for i in range(start + 1, len(lines)) if re.match(r"^##\s", lines[i])), len(lines))
    return start, end


def _row_cells(line):
    """표 한 줄의 칸. 기법 칸의 `\\|`는 `|`로 되돌린다 (_row_line이 다시 이스케이프한다)."""
    return [c.strip().replace("\\|", "|") for c in re.split(r"(?<!\\)\|", line.strip().strip("|"))]


def _row_line(cells):
    return "| " + " | ".join(c.replace("|", "\\|") for c in cells) + " |"


def _tables(lines, start, end):
    """진행 상태 구간의 표들. [(머리 줄 번호, 머리 칸, [행 줄 번호...])]"""
    out, i = [], start
    while i < end:
        if lines[i].lstrip().startswith("|") and i + 1 < end and re.match(r"^\s*\|[\s\-:|]+\|\s*$", lines[i + 1]):
            head = _row_cells(lines[i])
            rows, j = [], i + 2
            while j < end and lines[j].lstrip().startswith("|"):
                rows.append(j)
                j += 1
            out.append((i, head, rows))
            i = j
        else:
            i += 1
    return out


def _phase_col(head, phase):
    return next((k for k, h in enumerate(head) if re.match(rf"^{phase}\.", h)), None)


def _mark_of(cell):
    return next((m for m in MARKS if m in cell), cell.strip())


def read_progress(text):
    lines = text.splitlines()
    start, end = _progress_bounds(lines)
    if start is None:
        return None
    prep, cands, adopt = {}, [], {}
    for _, head, rows in _tables(lines, start, end):
        cells = [_row_cells(lines[r]) for r in rows]
        if head and head[0] == "#":
            for c in cells:
                if not c or not c[0].strip():
                    continue
                n = _num(c[0])
                entry = {"n": n if n is not None else c[0], "technique": c[1] if len(c) > 1 else ""}
                for p in CAND_PHASES:
                    k = _phase_col(head, p)
                    entry[str(p)] = _mark_of(c[k]) if k is not None and k < len(c) else None
                cands.append(entry)
        elif cells:
            for p in PREP_PHASES + (8,):
                k = _phase_col(head, p)
                if k is not None and k < len(cells[0]):
                    (prep if p != 8 else adopt)[str(p)] = _mark_of(cells[0][k])
    return {"prep": prep, "candidates": cands, "adopt": adopt}


def resume_point(st):
    """⏳로 남은 가장 이른 Phase. 후보가 다 끝났고 8이 남았으면 다음 후보와 채택 중 선택이 필요하다."""
    for p in PREP_PHASES:
        if st["prep"].get(str(p)) not in DONE:
            return {"phase": p}
    for c in st["candidates"]:
        for p in CAND_PHASES:
            if c.get(str(p)) not in DONE:
                return {"phase": p, "candidate": c["n"]}
    if st["adopt"].get("8") in DONE:
        return {"phase": None, "done": True}
    nums = [c["n"] for c in st["candidates"] if isinstance(c["n"], int)]
    return {"phase": "5|8", "candidate": (max(nums) + 1) if nums else 1, "needsDecision": True}


def update_progress(text, phase, mark, candidate=None, technique=None):
    if mark not in MARKS:
        raise ValueError(f"mark는 {', '.join(MARKS)} 중 하나다")
    lines = text.splitlines()
    start, end = _progress_bounds(lines)
    if start is None:
        raise ValueError("record.md에 '## 진행 상태' 절이 없다")
    tables = _tables(lines, start, end)
    target = next(((h, head, rows) for h, head, rows in tables if _phase_col(head, phase) is not None), None)
    if target is None:
        raise ValueError(f"Phase {phase} 칸이 있는 표가 없다")
    h, head, rows = target
    col = _phase_col(head, phase)
    if head[0] == "#":
        if candidate is None:
            raise ValueError(f"Phase {phase}는 후보 표에 있다. --candidate가 필요하다")
        row_line = next((r for r in rows if _row_cells(lines[r])[0] == str(candidate)), None)
        if row_line is None:
            # 새 후보 행은 Phase 5에서 다음 번호로만 덧붙인다. 번호를 건너뛰면 비교표의 후보 순서가 어긋난다
            nums = [n for n in (_num(_row_cells(lines[r])[0]) for r in rows) if isinstance(n, int)]
            nxt = (max(nums) + 1) if nums else 1
            if phase != CAND_PHASES[0] or candidate != nxt:
                raise ValueError(f"후보 {candidate} 행이 없다. 새 후보는 Phase {CAND_PHASES[0]}에서 다음 번호({nxt})로만 추가한다")
            cells = [str(candidate)] + [""] * (len(head) - 1)
            for p in CAND_PHASES:
                k = _phase_col(head, p)
                if k is not None:
                    cells[k] = "⏳"
            insert_at = (rows[-1] + 1) if rows else h + 2
            lines.insert(insert_at, _row_line(cells))
            row_line = insert_at
        cells = _row_cells(lines[row_line])
        cells += [""] * (len(head) - len(cells))
        cells[col] = mark
        if technique is not None and len(head) > 1:
            cells[1] = technique
    else:
        if not rows:
            raise ValueError(f"Phase {phase} 표에 행이 없다")
        row_line = rows[0]
        cells = _row_cells(lines[row_line])
        cells += [""] * (len(head) - len(cells))
        cells[col] = mark
    lines[row_line] = _row_line(cells)
    return "\n".join(lines) + ("\n" if text.endswith("\n") else "")


def cmd_progress(cfg, a):
    with open(a.record, encoding="utf-8") as f:
        text = f.read()
    if a.phase is not None:
        if a.mark is None:
            return _emit({"ok": False, "errors": ["--phase에는 --mark가 필요하다"]}, code=1)
        try:
            text = update_progress(text, a.phase, a.mark, a.candidate, a.technique)
        except ValueError as e:
            return _emit({"ok": False, "errors": [str(e)]}, code=1)
        with open(a.record, "w", encoding="utf-8") as f:
            f.write(text)
    st = read_progress(text)
    if st is None:
        return _emit({"ok": False, "errors": ["record.md에 '## 진행 상태' 절이 없다"]}, code=1)
    return _emit({"ok": True, "record": a.record, "progress": st, "resume": resume_point(st)})


# ───────────────────────── conc-compare ─────────────────────────

def _labels(target_dir):
    ns = set()
    for pat in ("invariant-*.txt", "lock-stats-*.txt", "k6-burst-summary-*.json"):
        for p in glob.glob(os.path.join(target_dir, pat)):
            m = re.search(r"-([A-Za-z0-9]+)\.(txt|json)$", os.path.basename(p))
            if m:
                ns.add(m.group(1))
    ints = sorted(int(x) for x in ns if x.isdigit())
    others = sorted(x for x in ns if not x.isdigit())
    return [str(i) for i in ints] + others


def collect(target_dir, db=None):
    """후보 번호별 측정 원본을 읽어 한 덩어리로."""
    out, missing = {}, []
    techniques = {}
    rec = os.path.join(target_dir, "record.md")
    if os.path.exists(rec):
        with open(rec, encoding="utf-8") as f:
            st = read_progress(f.read())
        for c in (st or {}).get("candidates", []):
            techniques[str(c["n"])] = c["technique"]
    for n in _labels(target_dir):
        e = {"n": n, "technique": "(원본)" if n == "0" else techniques.get(n, "")}
        p = os.path.join(target_dir, f"invariant-{n}.txt")
        if os.path.exists(p):
            e["invariants"] = parse_invariant_file(p)["invariants"]
        else:
            missing.append(os.path.basename(p))
        p = os.path.join(target_dir, f"k6-burst-summary-{n}.json")
        if os.path.exists(p):
            e["burst"] = shape_burst(_load_json(p))
        else:
            missing.append(os.path.basename(p))
        p = os.path.join(target_dir, f"lock-stats-{n}.txt")
        if os.path.exists(p):
            e["lock"] = lock_delta_from_file(p, db)
        else:
            missing.append(os.path.basename(p))
        out[n] = e
    return out, missing


def _violations(e):
    inv = e.get("invariants")
    if inv is None:
        return None
    return {i["id"]: i["violations"] for i in inv}


def _consistent(e):
    v = _violations(e)
    if not v:
        return None
    return all(x == 0 for x in v.values())


def _outcome_text(b):
    o = (b or {}).get("outcome")
    if not o:
        return "-"
    return (f"2xx {o.get('succeeded')}, 기대거절 {o.get('rejected_expected')}, 기타4xx {o.get('rejected_other_4xx')}, "
            f"5xx {o.get('failed_5xx')}, 파싱불가 {o.get('unparsable')}")


def compare_table(data):
    rows, md = [], [
        "| # | 기법 | 불변식 위반 | 정합성 | 성공 요청 수 | RPS | p99 | 락 대기 | 데드락 | 응답 코드 |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for n, e in data.items():
        v = _violations(e)
        b = e.get("burst") or {}
        lm = (e.get("lock") or {}).get("metrics") or {}
        ok = _consistent(e)
        row = {
            "n": n, "technique": e["technique"], "violations": v, "consistent": ok,
            "succeeded": _dig(b, ("outcome", "succeeded")), "rps": b.get("rps"),
            "p99": _dig(b, ("duration_ms", "p99")), "lockWaits": lm.get("lock_waits"),
            "deadlocks": lm.get("deadlocks"), "outcome": b.get("outcome"),
        }
        rows.append(row)
        vtxt = " / ".join(f"{k} {x}" for k, x in v.items()) if v else "-"
        md.append("| " + " | ".join([
            n, e["technique"] or "-", vtxt, "-" if ok is None else ("확보" if ok else "실패"),
            _cell(row["succeeded"]), _cell(row["rps"]), _cell(row["p99"]), _cell(row["lockWaits"]),
            _cell(row["deadlocks"]), _outcome_text(b),
        ]) + " |")
    return rows, "\n".join(md)


CONTENTION_ROWS = (
    ("성공 요청 수", lambda e: _dig(e.get("burst"), ("outcome", "succeeded"))),
    ("5xx", lambda e: _dig(e.get("burst"), ("outcome", "failed_5xx"))),
    ("RPS", lambda e: (e.get("burst") or {}).get("rps")),
    ("p95 (ms)", lambda e: _dig(e.get("burst"), ("duration_ms", "p95"))),
    ("p99 (ms)", lambda e: _dig(e.get("burst"), ("duration_ms", "p99"))),
)
LOCK_ROWS = (
    ("락 대기 증가분", "lock_waits"),
    ("행 락 대기 시간 증가분 (ms)", "row_lock_time_ms"),
    ("행 락 구간 평균 대기 (ms)", "row_lock_time_avg_ms"),
    ("데드락 증가분", "deadlocks"),
    ("락 타임아웃 증가분", "lock_timeouts"),
    ("롤백 증가분", "xact_rollback"),
)


def candidate_tables(data, n):
    base, cand = data.get("0"), data.get(str(n))
    errors = []
    if base is None:
        errors.append("-0 측정이 없다. 비교 기준은 항상 원본(-0)이다")
    if cand is None:
        errors.append(f"-{n} 측정이 없다")
    if errors:
        return None, None, errors
    bv, cv = _violations(base) or {}, _violations(cand) or {}
    inv_rows, md = [], ["**정합성 (1급)**", "", "| 불변식 | 원본(`-0`) 위반 | 후보(`-%s`) 위반 | 판정 |" % n, "|---|---|---|---|"]
    for k in list(dict.fromkeys(list(bv) + list(cv))):
        b, c = bv.get(k), cv.get(k)
        if c is None:
            verdict = "측정 없음"
        elif c != 0:
            verdict = "실패"
        elif b:
            verdict = "확보"
        else:
            verdict = "유지"
        inv_rows.append({"id": k, "base": b, "candidate": c, "verdict": verdict})
        md.append(f"| {k} | {_cell(b)} | {_cell(c)} | {verdict} |")
    ok = bool(cv) and all(x == 0 for x in cv.values())
    md += ["", f"판정: **{'정합성 확보' if ok else '실패'}**", "", "**경합 (2급)**", "",
           "| 지표 | 원본(`-0`) | 후보(`-%s`) | 변화 |" % n, "|---|---|---|---|"]
    cont = []
    for label, fn in CONTENTION_ROWS:
        b, c = fn(base), fn(cand)
        cont.append({"metric": label, "base": b, "candidate": c})
        md.append(f"| {label} | {_cell(b)} | {_cell(c)} | {_ratio(b, c)} |")
    bl = (base.get("lock") or {}).get("metrics") or {}
    cl = (cand.get("lock") or {}).get("metrics") or {}
    for label, key in LOCK_ROWS:
        if key not in bl and key not in cl:
            continue
        b, c = bl.get(key), cl.get(key)
        cont.append({"metric": label, "base": b, "candidate": c})
        md.append(f"| {label} | {_cell(b)} | {_cell(c)} | {_ratio(b, c)} |")
    md += ["", f"응답 코드 (`-0`): {_outcome_text(base.get('burst'))}", f"응답 코드 (`-{n}`): {_outcome_text(cand.get('burst'))}"]
    return {"consistent": ok, "invariants": inv_rows, "contention": cont}, "\n".join(md), []


def cmd_compare(cfg, a):
    if not os.path.isdir(a.target_dir):
        return _emit({"ok": False, "errors": [f"디렉토리가 없다: {a.target_dir}"]}, code=1)
    data, missing = collect(a.target_dir, a.db)
    if not data:
        return _emit({"ok": False, "errors": ["측정 파일(invariant-*.txt, k6-burst-summary-*.json, lock-stats-*.txt)이 없다"]}, code=1)
    warnings = []
    base_b = (data.get("0") or {}).get("burst")
    for n, e in data.items():
        if n != "0" and base_b and e.get("burst"):
            warnings += [f"-{n}: {w}" for w in condition_warnings(base_b, e["burst"])]
    res = {"ok": True, "labels": list(data), "missing": missing}
    if warnings:
        res["warnings"] = warnings
    if a.candidate is not None:
        r, md, errors = candidate_tables(data, a.candidate)
        if errors:
            return _emit({"ok": False, "errors": errors, "missing": missing}, code=1)
        res["candidate"] = str(a.candidate)
        res.update(r)
        return _emit(res, md)
    rows, md = compare_table(data)
    res["rows"] = rows
    return _emit(res, md)


# ───────────────────────── skill-context ─────────────────────────

def skill_context_fields(cfg, issue):
    """skill_context.build의 fix-concurrency 분기에서 _common 위에 덧붙일 필드."""
    root = cfg.get("_root") or os.getcwd()
    conc = _section(cfg, "concurrency")
    perf = _section(cfg, "perf")
    db = (perf.get("db") or "").lower() or None
    stack = f"{SKILL_DIR}/stacks/{db}.md" if db in ("mysql", "postgresql") else None
    ctx = {
        "concurrency": {k: v for k, v in conc.items()},
        "db": {
            "kind": db,
            "container": perf.get("dbContainer"),
            "name": perf.get("dbName"),
            "user": perf.get("dbUser"),
            "passwordSet": bool(perf.get("dbPassword") or os.environ.get(perf.get("dbPasswordEnv") or "")),
            "appBaseUrl": perf.get("appBaseUrl"),
        },
        "stack": stack,
        # 비밀번호는 가린다. 실제 블록은 conc-shell을 eval해서 만든다
        "dbFunction": db_function(cfg, mask_password=True),
        "profileFileExists": os.path.exists(os.path.join(root, conc["profileFile"])),
        "tools": {t: bool(shutil.which(t)) for t in ("k6", "docker", "jq", "lsof")},
    }
    errors = []
    if stack is None:
        errors.append("perf.db가 mysql 또는 postgresql이 아니다. .claude/harness.json의 perf 절을 채워라")
    seeds = os.path.join(root, conc["dir"], "seeds")
    ctx["seedModules"] = sorted(f for f in os.listdir(seeds) if f.endswith(".sql")) if os.path.isdir(seeds) else []
    targets = []
    if issue:
        issue_dir = os.path.join(conc["dir"], str(issue))
        ctx["issueDir"] = issue_dir
        ctx["issueShared"] = {
            f: os.path.exists(os.path.join(root, issue_dir, f)) for f in ("seeds.sql", "tokens.json")
        }
        for rec in sorted(glob.glob(os.path.join(root, issue_dir, "*", "record.md"))):
            rel = os.path.relpath(rec, root)
            with open(rec, encoding="utf-8") as f:
                st = read_progress(f.read())
            targets.append({
                "slug": os.path.basename(os.path.dirname(rec)),
                "record": rel,
                "resume": resume_point(st) if st else None,
            })
    ctx["targets"] = targets
    if errors:
        ctx["concurrencyErrors"] = errors
    return ctx


# ───────────────────────── 등록 ─────────────────────────

def register(sub):
    s = sub.add_parser("conc-shell", help="fix-concurrency: 측정 블록용 셸 변수와 dbc 함수")
    s.add_argument("--issue")
    s.add_argument("--slug")
    s.set_defaults(fn=cmd_shell)

    s = sub.add_parser("conc-burst-summary", help="fix-concurrency: k6 원본 요약을 k6-burst-summary로 (+ delta_vs_base)")
    s.add_argument("raw", help="k6 원본 요약 JSON (k6-burst-raw-{n}.json)")
    s.add_argument("--base", help="비교 기준. 항상 k6-burst-summary-0.json")
    s.add_argument("--out", help="쓸 파일 (k6-burst-summary-{n}.json)")
    s.set_defaults(fn=cmd_burst_summary)

    s = sub.add_parser("conc-lock-delta", help="fix-concurrency: 락 카운터 스냅샷의 측정 구간 증가분")
    s.add_argument("--db", choices=["mysql", "postgresql"])
    s.add_argument("files", nargs="+", help="lock-stats-{n}.txt 하나, 또는 before, after 두 파일")
    s.add_argument("--out")
    s.set_defaults(fn=cmd_lock_delta)

    s = sub.add_parser("conc-compare", help="fix-concurrency: 후보 비교표 (항상 -0 대비)")
    s.add_argument("target_dir")
    s.add_argument("--candidate", type=int, help="이 후보만 -0과 비교 (Phase 7 표)")
    s.add_argument("--db", choices=["mysql", "postgresql"])
    s.set_defaults(fn=cmd_compare)

    s = sub.add_parser("conc-progress", help="fix-concurrency: record.md 진행 상태 갱신과 재개 지점")
    s.add_argument("record")
    s.add_argument("--phase", type=int, choices=range(1, 9))
    s.add_argument("--mark", choices=list(MARKS))
    s.add_argument("--candidate", type=int)
    s.add_argument("--technique")
    s.set_defaults(fn=cmd_progress)
