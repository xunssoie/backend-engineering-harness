"""스킬 로드 시 주입하는 맥락. 스킬마다 필요한 사실만 골라 한 번에 돌려준다."""
import json
import os

from . import concurrency_tools, perf_tools, pipeline_stage, process_runner, work_context

PLAN_TEXT_LIMIT = 20000
CLI = os.path.realpath(os.path.join(os.path.dirname(__file__), "..", "harness"))


def _exists(cfg, rel):
    return rel if rel and os.path.exists(os.path.join(cfg["_root"], rel)) else None


def _md_names(cfg, rel_dir, exclude=("README.md",)):
    d = os.path.join(cfg["_root"], rel_dir)
    if not os.path.isdir(d):
        return []
    return sorted(f[:-3] for f in os.listdir(d) if f.endswith(".md") and f not in exclude)


def _issue_body(cfg, number):
    data, err = process_runner.gh_json(cfg["_root"], "issue", "view", str(number), "--json", "title,body,labels")
    if err:
        return {"error": err}
    return {"title": data["title"], "body": data["body"], "labels": [x["name"] for x in data.get("labels", [])]}


def _common(cfg, f):
    b = f.get("branch") or {}
    return {
        "cli": CLI, "root": cfg["_root"], "branch": b.get("name"), "issue": b.get("issue"), "type": b.get("type"),
        "protectedBranch": b.get("protected"), "base": cfg["baseBranch"], "gates": cfg["pipeline"]["gates"],
        "errors": f.get("errors", []),
    }


def build(cfg, skill):
    if skill == "dev-pipeline":
        st = pipeline_stage.compute(cfg, cfg["pipeline"]["stages"])
        f = st.pop("facts")
        return {**_common(cfg, f), **st, "stages": cfg["pipeline"]["stages"]}

    need_github = skill in ("write-plan", "open-pr", "review-feedback") or (
        skill == "implement" and "write-plan" not in cfg["pipeline"]["stages"])
    f = work_context.collect(cfg, with_github=need_github, with_base=skill in ("open-pr", "implement"))
    ctx = _common(cfg, f)

    if skill == "open-issue":
        ctx["types"] = {
            k: {"label": v["label"], "template": _exists(cfg, cfg["issueTemplate"].format(template=v.get("template", k)))}
            for k, v in cfg["types"].items()
        }
        ctx["titleMaxLength"] = cfg["title"]["maxLength"]
        ctx["uncommittedChanges"] = not f["status"]["clean"]

    elif skill == "write-plan":
        if ctx["issue"]:
            ctx["issueDetail"] = _issue_body(cfg, ctx["issue"])
        ctx["plan"] = f.get("plan")
        ctx["planTemplate"] = ".claude/skills/write-plan/template/PLAN-template.md"
        ctx["conventionLayers"] = _md_names(cfg, cfg["conventions"]["dir"], ("README.md", cfg["conventions"]["common"]))
        ctx["policyDomains"] = _md_names(cfg, cfg["policyDir"])
        ctx["migrationRules"] = _exists(cfg, ".claude/rules/migration.md")

    elif skill == "implement":
        plan = f.get("plan")
        ctx["plan"] = plan
        ctx["planStageEnabled"] = "write-plan" in cfg["pipeline"]["stages"]
        if not ctx["planStageEnabled"] and ctx["issue"]:
            ctx["issueDetail"] = _issue_body(cfg, ctx["issue"])
        if plan and plan["exists"]:
            with open(os.path.join(cfg["_root"], plan["path"]), encoding="utf-8") as fh:
                text = fh.read()
            ctx["planText"] = text[:PLAN_TEXT_LIMIT]
            ctx["planTruncated"] = len(text) > PLAN_TEXT_LIMIT

    elif skill == "write-test":
        t = cfg["test"]
        ctx["test"] = {"command": t["command"], "requiresDocker": t["requiresDocker"]}
        ctx["testConvention"] = _exists(cfg, ".claude/spec/test-convention.md")
        ctx["testTemplate"] = _exists(cfg, ".claude/skills/write-test/template/test-code-template.md")
        last = os.path.join(cfg["_root"], cfg["stateDir"], "last-test.json")
        if os.path.exists(last):
            with open(last, encoding="utf-8") as fh:
                ctx["lastTest"] = json.load(fh)

    elif skill == "commit-push":
        ctx["status"] = work_context.trim(f)["status"]
        ctx["upstream"] = f.get("upstream")
        ctx["commitPlanPath"] = os.path.join(cfg["stateDir"], "commit-plan.json")

    elif skill == "open-pr":
        ctx["issueLabels"] = (f.get("issue") or {}).get("labels")
        ctx["vsBase"] = work_context.trim(f).get("vsBase")
        ctx["upstream"] = f.get("upstream")
        ctx["existingPr"] = f.get("pr")
        ctx["prTemplate"] = f.get("prTemplate")
        ctx["prWritingGuide"] = _exists(cfg, ".claude/spec/pr-writing.md")
        ctx["plan"] = f.get("plan")
        ctx["prDraftDir"] = os.path.join(cfg["stateDir"], "pr")

    elif skill == "optimize-performance":
        ctx.update(perf_tools.skill_context(cfg, ctx["issue"]))

    elif skill == "fix-concurrency":
        ctx.update(concurrency_tools.skill_context_fields(cfg, ctx["issue"]))

    elif skill == "review-feedback":
        ctx["pr"] = f.get("pr")
        ctx["reviewBot"] = cfg["reviewBot"]
        ctx["reviewDir"] = os.path.join(cfg["stateDir"], "review")

    return ctx
