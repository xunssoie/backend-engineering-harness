"""커밋 계획(JSON)을 검증하고 실행한다. LLM은 그룹과 메시지만 정하고, 스테이징, 커밋, push는 여기서 한다.

계획 형식:
{"commits": [{"type": "feat", "message": "북마크 기능 구현", "files": ["src/..."]}], "push": true}
"""
import json

from . import work_context
from . import convention_lint, process_runner


def _format(cfg, c, issue):
    fmt = cfg["commitFormat"]
    if not issue:
        fmt = fmt.replace("(#{issue})", "")
    return fmt.format(type=c["type"], message=c["message"].strip(), issue=issue)


def _changed(status):
    paths = {x["path"] for x in status["staged"]} | {x["path"] for x in status["unstaged"]}
    return paths | set(status["untracked"])


def validate(cfg, plan):
    f = work_context.collect(cfg, with_github=False, with_base=False)
    errs = []
    if "branch" not in f:
        return {"ok": False, "errors": f["errors"]}
    b = f["branch"]
    if b["protected"]:
        errs.append(f"보호 브랜치 '{b['name']}'에는 직접 커밋하지 않는다")
    changed = _changed(f["status"])
    if not changed:
        errs.append("커밋할 변경이 없다")
    planned, commits = set(), []
    for i, c in enumerate(plan.get("commits", []), 1):
        files = c.get("files") or []
        if not files:
            errs.append(f"커밋 {i}에 파일이 없다")
        unknown = [p for p in files if p not in changed]
        if unknown:
            errs.append(f"커밋 {i}의 파일이 변경 목록에 없다: {', '.join(unknown)}")
        dup = [p for p in files if p in planned]
        if dup:
            errs.append(f"커밋 {i}의 파일이 앞 커밋과 겹친다: {', '.join(dup)}")
        planned |= set(files)
        msg = _format(cfg, c, b["issue"])
        lr = convention_lint.commit(msg, cfg, b["issue"])
        errs += [f"커밋 {i}: {e}" for e in lr["errors"]]
        commits.append({"message": msg, "files": files, "warnings": lr["warnings"]})
    staged_outside = sorted({x["path"] for x in f["status"]["staged"]} - planned)
    if staged_outside:
        errs.append("계획에 없는 파일이 이미 스테이징돼 있다: " + ", ".join(staged_outside))
    return {
        "ok": not errs, "errors": errs, "branch": b["name"], "issue": b["issue"], "commits": commits,
        # 표시는 상한까지, 전체 수는 leftoverCount. 검증은 위에서 전체 목록으로 끝났다
        "leftover": sorted(changed - planned)[:work_context.MAX_FILES],
        "leftoverCount": len(changed - planned),
        "push": bool(plan.get("push", True)),
        "pushCommand": f.get("upstream", {}).get("pushCommand"),
    }


def apply(cfg, plan, dry_run=False):
    v = validate(cfg, plan)
    if not v["ok"] or dry_run:
        return v
    root = cfg["_root"]
    done = []
    for c in v["commits"]:
        ok, _, err = process_runner.git(root, "add", "-A", "--", *c["files"])
        if not ok:
            return {**v, "ok": False, "errors": [f"git add 실패: {err.strip()}"], "done": done}
        msg = c["message"]
        if cfg.get("attribution"):
            msg += "\n\n" + cfg["attribution"]
        ok, _, err = process_runner.git(root, "commit", "-q", "-m", msg, "--", *c["files"])
        if not ok:
            return {**v, "ok": False, "errors": [f"git commit 실패: {err.strip()}"], "done": done}
        done.append({"hash": process_runner.git_out(root, "rev-parse", "--short", "HEAD"), "message": c["message"]})
    result = {**v, "done": done}
    if v["push"]:
        args = ["push"] if v["pushCommand"] == "git push" else ["push", "-u", "origin", "HEAD"]
        ok, out, err = process_runner.git(root, *args, timeout=120)
        result["pushed"] = ok
        result["pushOutput"] = (err or out).strip().splitlines()[-3:]
        if not ok:
            result["ok"] = False
            result["errors"] = [f"push 실패: {' '.join(result['pushOutput'])}"]
    return result


def load_plan(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)
