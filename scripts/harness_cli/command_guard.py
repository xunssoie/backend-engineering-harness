"""재귀 삭제(rm -r 계열) 판정. 명령 문자열이 아니라 삭제 대상 경로로 판정한다.

deny  : 루트, 홈, 프로젝트 루트와 그 상위, .git
allow : 존재하지 않는 경로, 임시 디렉토리, 프로젝트 안의 gitignore 대상(build/ 등)
ask   : 프로젝트 안의 추적, 미추적 소스, 프로젝트 밖, 해석할 수 없는 경로(변수, 치환)
"""
import glob
import os
import shlex
import tempfile

from . import process_runner

OPERATORS = {"&&", "||", ";", "|", "&", "\n", ";;", "|&"}
WRAPPERS = {"sudo", "command", "nohup", "time", "exec"}
RANK = {"allow": 0, "ask": 1, "deny": 2}


def _tmp_roots():
    roots = {"/tmp", "/private/tmp", "/var/folders", "/private/var/folders", tempfile.gettempdir()}
    if os.environ.get("TMPDIR"):
        roots.add(os.environ["TMPDIR"])
    return {os.path.realpath(r) for r in roots if r}


def _under(path, base):
    return path == base or path.startswith(base.rstrip("/") + "/")


def _segments(command):
    lex = shlex.shlex(command, posix=True, punctuation_chars=";&|")
    lex.whitespace_split = True
    lex.commenters = ""
    seg = []
    for tok in lex:
        if tok in OPERATORS or set(tok) <= set(";&|"):
            if seg:
                yield seg
            seg = []
        else:
            seg.append(tok)
    if seg:
        yield seg


def _strip_prefix(seg):
    i = 0
    while i < len(seg) and ("=" in seg[i] and not seg[i].startswith("-") and seg[i].split("=", 1)[0].isidentifier()):
        i += 1
    while i < len(seg) and seg[i] in WRAPPERS:
        i += 1
        while i < len(seg) and seg[i].startswith("-"):
            i += 1
    return seg[i:]


def _rm_targets(seg):
    """rm 세그먼트면 (재귀 여부, 대상 목록). 아니면 None."""
    if not seg or os.path.basename(seg[0]) != "rm":
        return None
    recursive, targets, opts_done = False, [], False
    for tok in seg[1:]:
        if not opts_done and tok == "--":
            opts_done = True
        elif not opts_done and tok.startswith("--"):
            recursive |= tok == "--recursive"
        elif not opts_done and tok.startswith("-") and len(tok) > 1:
            recursive |= "r" in tok or "R" in tok
        else:
            targets.append(tok)
    return recursive, targets


def _judge_path(raw, cwd, root, home):
    if any(c in raw for c in "$`") or raw.startswith("~") and not (raw == "~" or raw.startswith("~/")):
        return "ask", f"'{raw}': 변수나 치환이 들어 있어 대상을 확정할 수 없다"
    p = os.path.expanduser(raw)
    if not os.path.isabs(p):
        p = os.path.join(cwd, p)
    if any(c in p for c in "*?["):
        prefix = p.split("*")[0].split("?")[0].split("[")[0]
        prefix_dir = os.path.realpath(prefix if prefix.endswith("/") else os.path.dirname(prefix))
        if prefix_dir in ("/", home) or _under(root, prefix_dir):
            return "deny", f"'{raw}': 루트, 홈, 프로젝트 루트 바로 아래를 와일드카드로 지운다"
        matches = glob.glob(p)
        verdicts = [_judge_resolved(os.path.realpath(m), raw, root, home) for m in matches]
        return max(verdicts, key=lambda v: RANK[v[0]]) if verdicts else ("allow", "")
    real = os.path.realpath(p)
    return _judge_resolved(real, raw, root, home)


def _judge_resolved(real, raw, root, home):
    if real == "/" or real == home:
        return "deny", f"'{raw}': 루트나 홈 디렉토리를 지운다"
    if _under(root, real):
        return "deny", f"'{raw}': 프로젝트 루트이거나 그 상위 디렉토리다"
    if _under(real, os.path.join(root, ".git")):
        return "deny", f"'{raw}': .git 디렉토리다"
    if not os.path.lexists(real):
        return "allow", ""
    # 프로젝트가 임시 디렉토리 안에 있을 수 있으므로 프로젝트 판정을 먼저 한다
    if _under(real, root):
        rel = os.path.relpath(real, root)
        ok, _, _ = process_runner.git(root, "check-ignore", "-q", "--", rel)
        if ok:
            return "allow", ""
        return "ask", f"'{raw}': 프로젝트 소스 경로다 (gitignore 대상 아님)"
    if any(_under(real, t) for t in _tmp_roots()):
        return "allow", ""
    return "ask", f"'{raw}': 프로젝트 밖 경로다"


BUILD_TOOLS = {"gradlew", "gradle", "mvnw", "mvn"}


def _bypasses_test_runner(seg, tasks):
    """테스트를 돌리는 빌드 명령인지. `-x test`처럼 테스트를 뺀 실행은 통과시킨다."""
    if not seg or os.path.basename(seg[0]) not in BUILD_TOOLS:
        return False
    args = seg[1:]
    excluded = {args[i + 1] for i, x in enumerate(args[:-1]) if x in ("-x", "--exclude-task")}
    if "test" in excluded or "-DskipTests" in args or "-Dmaven.test.skip=true" in args:
        return False
    return any(a in tasks or a.split(":")[-1] in tasks for a in args if not a.startswith("-"))


def judge(command, cwd, root, cfg=None):
    """명령 전체의 판정. {"decision": allow|ask|deny, "reasons": [...]}"""
    home = os.path.realpath(os.path.expanduser("~"))
    root = os.path.realpath(root)
    cwd = os.path.realpath(cwd or root)
    decision, reasons = "allow", []
    try:
        segments = list(_segments(command))
    except ValueError:
        if "rm " in command:
            return {"decision": "ask", "reasons": ["따옴표가 맞지 않아 rm 대상을 해석할 수 없다"]}
        return {"decision": "allow", "reasons": []}
    for seg in segments:
        seg = _strip_prefix(seg)
        if seg and seg[0] == "cd" and len(seg) > 1:
            nxt = os.path.expanduser(seg[1])
            nxt = nxt if os.path.isabs(nxt) else os.path.join(cwd, nxt)
            if os.path.isdir(nxt):
                cwd = os.path.realpath(nxt)
            continue
        t = (cfg or {}).get("test") or {}
        if t.get("enforceRunner", True) and _bypasses_test_runner(seg, set(t.get("guardedTasks", ["test", "check", "build"]))):
            decision = "deny"
            reasons.append("테스트는 `harness run-tests`로 실행한다 (Docker 확인, 로그 파일 보관, 결과 집계). "
                           "컴파일만 확인하려면 `-x test`를 붙인다")
            continue
        r = _rm_targets(seg)
        if not r or not r[0]:
            continue
        if not r[1]:
            continue
        for raw in r[1]:
            d, why = _judge_path(raw, cwd, root, home)
            if RANK[d] > RANK[decision]:
                decision = d
            if why:
                reasons.append(why)
    return {"decision": decision, "reasons": reasons}
