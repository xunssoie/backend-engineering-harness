"""테스트 실행. Docker 확인을 강제하고, 로그는 파일로 두고, JUnit XML 집계만 반환한다."""
import glob
import os
import platform
import re
import shlex
import subprocess
import time
import xml.etree.ElementTree as ET

from . import process_runner

MAX_FAILED = 30
BUILD_ERROR_PATTERNS = (
    r"^e: ",                  # Kotlin 컴파일 오류
    r"error:",                # javac 오류
    r"^\* What went wrong:",  # Gradle 실패 요약
    r"^FAILURE: ",
    r"Could not ",
)


def docker_ready(timeout_sec):
    ok, _, _ = process_runner.run(["docker", "info", "--format", "{{.ServerVersion}}"], os.getcwd(), timeout=15)
    if ok:
        return True, "실행 중"
    if platform.system() != "Darwin":
        return False, "Docker 데몬이 실행 중이 아니다. 데몬을 켠 뒤 다시 실행한다"
    process_runner.run(["open", "-a", "Docker"], os.getcwd(), timeout=15)
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        time.sleep(3)
        ok, _, _ = process_runner.run(["docker", "info", "--format", "{{.ServerVersion}}"], os.getcwd(), timeout=15)
        if ok:
            return True, "꺼져 있어 Docker Desktop을 켜고 기다렸다"
    return False, f"Docker Desktop을 켰지만 {timeout_sec}초 안에 준비되지 않았다"


def _first_line(text, limit=300):
    line = (text or "").strip().splitlines()[0] if (text or "").strip() else ""
    return line[:limit]


def parse_results(results_dir, since):
    totals = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0}
    failed = []
    files = [f for f in glob.glob(os.path.join(results_dir, "**", "*.xml"), recursive=True)
             if os.path.getmtime(f) >= since - 1]
    for f in files:
        try:
            root = ET.parse(f).getroot()
        except ET.ParseError:
            continue
        suites = [root] if root.tag == "testsuite" else root.findall("testsuite")
        for s in suites:
            for k in totals:
                totals[k] += int(s.get(k, 0) or 0)
            for tc in s.findall("testcase"):
                for kind in ("failure", "error"):
                    el = tc.find(kind)
                    if el is not None:
                        cls = (tc.get("classname") or "").split(".")[-1]
                        failed.append({
                            "test": f"{cls}#{tc.get('name')}",
                            "kind": kind,
                            "type": (el.get("type") or "").split(".")[-1],
                            "message": _first_line(el.get("message") or el.text),
                        })
    return len(files), totals, failed


def build_errors(log_path, limit=20):
    out = []
    try:
        with open(log_path, encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    except OSError:
        return out
    for i, line in enumerate(lines):
        if any(re.search(p, line) for p in BUILD_ERROR_PATTERNS):
            out.append(f"{i + 1}: {line.strip()[:300]}")
            if len(out) >= limit:
                break
    return out


def run(cfg, filters=None):
    t = cfg["test"]
    root = cfg["_root"]
    result = {"command": None, "status": None}
    if t.get("requiresDocker"):
        ready, note = docker_ready(t.get("dockerStartTimeoutSec", 90))
        result["docker"] = note
        if not ready:
            result["status"] = "docker-unavailable"
            return result
    args = shlex.split(t["command"])
    for flt in filters or []:
        # Gradle은 `--tests X`, Maven은 `-Dtest=X`처럼 붙여 쓴다
        args += [t["filterArg"] + flt] if t["filterArg"].endswith("=") else [t["filterArg"], flt]
    result["command"] = " ".join(shlex.quote(a) for a in args)
    log_dir = os.path.join(root, t["logDir"])
    os.makedirs(log_dir, exist_ok=True)
    log = os.path.join(log_dir, time.strftime("test-%Y%m%d-%H%M%S.log"))
    result["log"] = os.path.relpath(log, root)
    start = time.time()
    with open(log, "w", encoding="utf-8") as lf:
        try:
            code = subprocess.run(args, cwd=root, stdout=lf, stderr=subprocess.STDOUT).returncode
        except FileNotFoundError:
            result["status"] = "command-not-found"
            return result
    result["exitCode"] = code
    result["durationSec"] = round(time.time() - start, 1)
    n_files, totals, failed = parse_results(os.path.join(root, t["resultsDir"]), start)
    result["totals"] = totals
    result["failed"] = failed[:MAX_FAILED]
    result["failedCount"] = len(failed)
    if n_files == 0:
        result["status"] = "passed" if code == 0 else "build-failed"
        if code != 0:
            result["buildErrors"] = build_errors(log)
    elif failed or code != 0:
        result["status"] = "failed"
        if not failed:
            result["buildErrors"] = build_errors(log)
    else:
        result["status"] = "passed"
    if totals["skipped"]:
        result["note"] = f"skip {totals['skipped']}건. 이번에 작성한 테스트가 skip됐는지 확인한다"
    _record_last_run(cfg, result)
    return result


def _record_last_run(cfg, result):
    """파이프라인 상태 판정용. 마지막 실행 결과와 그때의 HEAD, 작업 트리 해시를 남긴다."""
    import json
    root = cfg["_root"]
    state_dir = os.path.join(root, cfg["stateDir"])
    os.makedirs(state_dir, exist_ok=True)
    record = {
        **result, "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "head": process_runner.git_out(root, "rev-parse", "HEAD"),
        "tree": worktree_fingerprint(root, cfg["stateDir"]),
    }
    with open(os.path.join(state_dir, "last-test.json"), "w", encoding="utf-8") as f:
        json.dump(record, f, ensure_ascii=False)


def worktree_fingerprint(root, state_dir):
    """HEAD와 작업 트리 변경을 합친 지문. 테스트 이후 코드가 바뀌었는지 판정한다.
    하네스 상태 디렉토리는 테스트 실행이 직접 쓰므로 제외한다."""
    import hashlib
    exclude = f":(exclude){state_dir}"
    head = process_runner.git_out(root, "rev-parse", "HEAD") or ""
    diff = process_runner.git_out(root, "diff", "HEAD", "--", ".", exclude) or ""
    untracked = process_runner.git_out(root, "ls-files", "--others", "--exclude-standard", "--", ".", exclude) or ""
    h = hashlib.sha1()
    for part in (head, diff, untracked):
        h.update(part.encode())
    for p in untracked.splitlines():
        try:
            with open(os.path.join(root, p), "rb") as f:
                h.update(f.read())
        except OSError:
            pass
    return h.hexdigest()
