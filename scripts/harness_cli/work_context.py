"""작업 맥락의 사실을 한 번에 모은다. 스킬이 로드될 때 주입되므로 실패해도 예외를 내지 않는다."""
import os
import re

from . import process_runner

# 주입, 표시용 상한. 판정과 검증(apply-commits, pipeline-stage, lint --against-diff)은 전체 목록을 쓴다
MAX_FILES = 60


def branch_info(cfg, branch):
    m = re.match(cfg["branchPattern"], branch or "")
    info = {"name": branch, "protected": branch in cfg["protectedBranches"], "type": None, "issue": None, "slug": None}
    if m:
        g = m.groupdict()
        info["type"] = g.get("type")
        info["issue"] = int(g["issue"]) if g.get("issue") else None
        info["slug"] = g.get("slug")
    return info


def _status(root):
    ok, out, _ = process_runner.git(root, "status", "--porcelain=v1", "-uall")
    staged, unstaged, untracked = [], [], []
    if ok:
        for line in out.splitlines():
            if len(line) < 4:
                continue
            x, y, path = line[0], line[1], line[3:]
            if " -> " in path:
                path = path.split(" -> ", 1)[1]
            if x == "?":
                untracked.append(path)
                continue
            if x != " ":
                staged.append({"path": path, "status": x})
            if y != " ":
                unstaged.append({"path": path, "status": y})
    return {
        "clean": not (staged or unstaged or untracked),
        "staged": staged,
        "unstaged": unstaged,
        "untracked": untracked,
        "counts": {"staged": len(staged), "unstaged": len(unstaged), "untracked": len(untracked)},
    }


def _upstream(root, branch):
    up = process_runner.git_out(root, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
    info = {"name": up, "matchesBranch": up == f"origin/{branch}", "ahead": None, "behind": None,
            # 네트워크 없이 마지막 fetch 기준의 원격 추적 ref로 판정한다
            "remoteExists": bool(process_runner.git_out(root, "rev-parse", "--verify", "-q", f"refs/remotes/origin/{branch}"))}
    if up:
        counts = process_runner.git_out(root, "rev-list", "--left-right", "--count", f"{up}...HEAD")
        if counts:
            behind, ahead = counts.split()
            info["ahead"], info["behind"] = int(ahead), int(behind)
    # push 방식: upstream이 동일명 원격 브랜치가 아니면 -u로 바로잡는다
    info["pushCommand"] = "git push" if info["matchesBranch"] else "git push -u origin HEAD"
    return info


def _vs_base(root, base):
    ref = f"origin/{base}"
    if not process_runner.git_out(root, "rev-parse", "--verify", "-q", ref):
        ref = base
    log = process_runner.git_out(root, "log", "--oneline", f"{ref}..HEAD") or ""
    stat = process_runner.git_out(root, "diff", "--shortstat", f"{ref}...HEAD") or ""
    files = (process_runner.git_out(root, "diff", "--name-status", f"{ref}...HEAD") or "").splitlines()
    return {"ref": ref, "commits": log.splitlines(), "shortstat": stat, "files": files, "fileCount": len(files)}


def trim(facts, limit=MAX_FILES):
    """스킬에 주입하거나 화면에 보일 때만 목록을 자른다. 전체 수는 counts, fileCount에 있다."""
    out = dict(facts)
    s = facts.get("status")
    if isinstance(s, dict):
        out["status"] = {**s, **{k: s[k][:limit] for k in ("staged", "unstaged", "untracked")},
                         "truncated": any(len(s[k]) > limit for k in ("staged", "unstaged", "untracked"))}
    v = facts.get("vsBase")
    if isinstance(v, dict):
        out["vsBase"] = {**v, "commits": v["commits"][:limit], "files": v["files"][:limit]}
    return out


def _plan(cfg, issue):
    if not issue:
        return None
    path = os.path.join(cfg["plans"]["dir"], cfg["plans"]["file"].format(issue=issue))
    full = os.path.join(cfg["_root"], path)
    info = {"path": path, "exists": os.path.exists(full), "uncheckedDecisions": None}
    if info["exists"]:
        from .convention_lint import plan_unchecked
        info["uncheckedDecisions"] = len(plan_unchecked(full))
    return info


def _issue(root, number):
    if not number:
        return None
    data, err = process_runner.gh_json(root, "issue", "view", str(number), "--json", "number,title,state,labels,url")
    if err:
        return {"number": number, "error": err}
    return {"number": data["number"], "title": data["title"], "state": data["state"], "url": data["url"],
            "labels": [x["name"] for x in data.get("labels", [])]}


def _pr(root, branch):
    data, err = process_runner.gh_json(root, "pr", "list", "--head", branch, "--state", "all",
                             "--json", "number,url,state,title,baseRefName", "--limit", "5")
    if err:
        return {"error": err}
    if not data:
        return None
    open_prs = [p for p in data if p["state"] == "OPEN"]
    return (open_prs or data)[0]


def collect(cfg, with_github=True, with_base=True):
    root = cfg["_root"]
    facts = {"root": root, "errors": []}
    if cfg.get("_configError"):
        facts["errors"].append(cfg["_configError"])
    if not cfg.get("_configFound"):
        facts["errors"].append(".claude/harness.json이 없어 기본값을 쓴다. /init-harness로 만든다")
    branch = process_runner.git_out(root, "branch", "--show-current")
    if branch is None:
        facts["errors"].append("git 저장소가 아니다")
        return facts
    b = branch_info(cfg, branch)
    facts["branch"] = b
    facts["base"] = cfg["baseBranch"]
    if b["type"]:
        t = cfg["types"].get(b["type"])
        facts["typeLabel"] = t["label"] if t else None
        if not t:
            facts["errors"].append(f"브랜치 타입 '{b['type']}'이 설정의 types에 없다")
    facts["status"] = _status(root)
    if not b["protected"] and branch:
        facts["upstream"] = _upstream(root, branch)
    if with_base:
        facts["vsBase"] = _vs_base(root, cfg["baseBranch"])
    facts["plan"] = _plan(cfg, b["issue"])
    tmpl = cfg["prTemplate"]
    facts["prTemplate"] = tmpl if os.path.exists(os.path.join(root, tmpl)) else None
    if with_github:
        facts["issue"] = _issue(root, b["issue"])
        if branch and not b["protected"]:
            facts["pr"] = _pr(root, branch)
    if b["issue"] is None and not b["protected"]:
        facts["errors"].append(f"브랜치명 '{branch}'에서 이슈 번호를 뽑지 못했다 (branchPattern)")
    return facts
