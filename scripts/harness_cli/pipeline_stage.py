"""파이프라인 현재 단계 판정. 대화가 아니라 산출물(브랜치, 이슈, 계획서, 테스트 기록, PR)로 정한다."""
import json
import os

from . import work_context
from . import process_runner, test_runner

STAGES = ["open-issue", "write-plan", "implement", "write-test", "commit-push", "open-pr", "review-feedback"]


def _last_test(cfg):
    path = os.path.join(cfg["_root"], cfg["stateDir"], "last-test.json")
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _unhandled_review(cfg, pr_number):
    """답글도 없고 스레드도 resolve되지 않은 리뷰 봇 최상위 코멘트 수. review_collect의 pending과 같은 기준이다."""
    from . import github_ops
    root = cfg["_root"]
    repo, err = process_runner.gh_json(root, "repo", "view", "--json", "nameWithOwner")
    if err:
        return None
    ok, out, _ = process_runner.gh(root, "api", "--paginate", f"repos/{repo['nameWithOwner']}/pulls/{pr_number}/comments",
                         "--jq", ".[] | [.id, .in_reply_to_id, .user.login] | @tsv")
    if not ok:
        return None
    tops, replied = set(), set()
    for line in out.splitlines():
        cid, reply_to, login = (line.split("\t") + ["", "", ""])[:3]
        if reply_to and reply_to != "null":
            replied.add(reply_to)
        elif login == cfg["reviewBot"]:
            tops.add(cid)
    resolved, _ = github_ops.resolved_comment_ids(root, repo["nameWithOwner"], pr_number)
    return len(tops - replied - resolved)


def _is_harness_path(path):
    """계획서, 하네스 설정처럼 .claude/ 아래 파일은 구현 진행 판정에서 뺀다."""
    return path.startswith(".claude/")


def _dirty_paths(status):
    return [x["path"] for x in status["staged"] + status["unstaged"]] + status["untracked"]


def compute(cfg, enabled=None):
    enabled = enabled or STAGES
    f = work_context.collect(cfg)
    st = {"facts": f, "stage": None, "reason": None, "done": []}
    if "branch" not in f:
        st["stage"], st["reason"] = None, "git 저장소가 아니다"
        return st
    b = f["branch"]

    def at(stage, reason):
        if stage in enabled:
            st["stage"], st["reason"] = stage, reason
            return True
        st["done"].append(f"{stage}(비활성)")
        return False

    if (b["protected"] or not b["issue"]) and at("open-issue", "작업 브랜치나 이슈 번호가 없다"):
        return st
    plan = f.get("plan")
    if "write-plan" in enabled:
        if not plan or not plan["exists"]:
            at("write-plan", "계획서가 없다")
            return st
        if plan["uncheckedDecisions"]:
            at("write-plan", f"계획서에 미결정 항목 {plan['uncheckedDecisions']}개가 있다")
            return st
    st["done"].append("write-plan")
    vs = f.get("vsBase", {})
    has_code = any(not _is_harness_path(line.split("\t")[-1]) for line in vs.get("files", [])) or any(
        not _is_harness_path(p) for p in _dirty_paths(f["status"]))
    if not has_code and at("implement", "base 대비 변경이 없다"):
        return st
    st["done"].append("implement")
    if "write-test" in enabled:
        last = _last_test(cfg)
        fp = test_runner.worktree_fingerprint(cfg["_root"], cfg["stateDir"])
        if not last or last.get("tree") != fp:
            at("write-test", "마지막 테스트 이후 코드가 바뀌었거나 실행 기록이 없다")
            return st
        if last.get("status") != "passed":
            at("write-test", f"마지막 테스트 결과가 {last.get('status')}다")
            return st
        st["done"].append("write-test")
    if not f["status"]["clean"] and at("commit-push", "커밋되지 않은 변경이 있다"):
        return st
    up = f.get("upstream") or {}
    if (not up.get("remoteExists") or (up.get("ahead") or 0) > 0) and at("commit-push", "push되지 않은 커밋이 있다"):
        return st
    st["done"].append("commit-push")
    pr = f.get("pr")
    if (not pr or pr.get("error") or pr.get("state") != "OPEN") and at("open-pr", "열린 PR이 없다"):
        return st
    st["done"].append("open-pr")
    if "review-feedback" in enabled and pr and pr.get("number"):
        n = _unhandled_review(cfg, pr["number"])
        if n:
            at("review-feedback", f"처리 이력이 없는 리뷰 봇 코멘트 {n}건")
            return st
    st["stage"], st["reason"] = "done", "남은 단계가 없다. 리뷰와 머지를 기다린다"
    return st
