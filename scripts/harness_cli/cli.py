"""harness CLI 진입점. 모든 명령은 JSON 한 덩어리를 출력한다.

스킬 로드 시 주입(`!`harness ...``)에 쓰이는 명령은 실패해도 exit 0으로 끝난다.
주입 명령이 실패하면 스킬 호출 자체가 중단되기 때문이다. 오류는 JSON의 errors에 담는다.
"""
import argparse
import json
import os
import sys

from . import harness_update, concurrency_tools, perf_tools, project_detector, installer, skill_context, command_guard, commit_executor, convention_lint, github_ops, harness_config, pipeline_stage, report_renderer, test_runner, work_context


def emit(obj, code=0):
    # 주입되는 출력이므로 공백 없이 압축한다. report는 사람이 읽는 마크다운이라 따로 뒤에 붙인다
    rep = obj.pop("report", None) if isinstance(obj, dict) else None
    print(json.dumps(obj, ensure_ascii=False, separators=(",", ":")))
    if rep:
        print("\n" + rep)
    return code


def _read_json(path):
    """파일 경로, `-`(stdin), 또는 JSON 문자열 그대로. 문자열을 받으면 파이프 없이 한 명령으로 끝난다."""
    if path.lstrip().startswith(("{", "[")):
        return json.loads(path)
    if path == "-":
        return json.load(sys.stdin)
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _with_report(cfg, skill, result, data):
    if result.get("ok"):
        data.setdefault("next", harness_config.next_stage(cfg, skill) or "없음")
        result["report"] = report_renderer.render(skill, data)
    return result


def cmd_facts(cfg, a):
    return emit(work_context.trim(work_context.collect(cfg, with_github=not a.no_github, with_base=not a.no_base)))


def cmd_status(cfg, a):
    stages = a.stages.split(",") if a.stages else cfg["pipeline"]["stages"]
    st = pipeline_stage.compute(cfg, stages)
    if not a.verbose:
        st.pop("facts", None)
    elif st.get("facts"):
        st["facts"] = work_context.trim(st["facts"])
    return emit(st)


def cmd_lint(cfg, a):
    issue = work_context.branch_info(cfg, _branch(cfg))["issue"]
    if a.kind == "title":
        r = convention_lint.title(a.text, cfg)
    elif a.kind == "body":
        r = convention_lint.body(open(a.file, encoding="utf-8").read(), cfg)
    elif a.kind == "commit-message":
        r = convention_lint.commit(a.text, cfg, issue)
    elif a.kind == "pr":
        r = convention_lint.pr(a.text, open(a.file, encoding="utf-8").read(), cfg, issue)
    else:
        changed = None
        if a.against_diff:
            f = work_context.collect(cfg, with_github=False)
            changed = [line.split("\t")[-1] for line in f["vsBase"]["files"]]
            changed += [x["path"] for x in f["status"]["staged"] + f["status"]["unstaged"]] + f["status"]["untracked"]
        r = convention_lint.plan(a.file, changed)
    return emit(r, 0 if r["ok"] else 1)


def _branch(cfg):
    from . import process_runner
    return process_runner.git_out(cfg["_root"], "branch", "--show-current") or ""


def cmd_commit(cfg, a):
    plan = _read_json(a.plan)
    r = commit_executor.apply(cfg, plan, dry_run=a.dry_run)
    if not a.dry_run and r.get("ok"):
        r = _with_report(cfg, "commit-push", r, {
            "done": r.get("done", []), "branch": r["branch"], "pushed": r.get("pushed"),
            "pushMode": "신규 upstream 설정" if r.get("pushCommand") != "git push" else "기존 추적",
            "leftover": r.get("leftover", []),
        })
    return emit(r, 0 if r.get("ok") else 1)


def cmd_issue(cfg, a):
    r = github_ops.issue_create(cfg, a.type, a.title, a.body_file, dry_run=a.dry_run)
    if not r["ok"] or a.dry_run or not a.slug:
        return emit(r, 0 if r["ok"] else 1)
    br = github_ops.branch_create(cfg, a.type, r["number"], a.slug, a.base)
    r["branch"] = br
    if not br["ok"]:
        r["ok"] = False
        r["errors"] = br["errors"]
        return emit(r, 1)
    r = _with_report(cfg, "open-issue", r, {
        "number": r["number"], "url": r["url"], "type": a.type, "label": r["label"],
        "branch": br["branch"], "base": br["base"],
    })
    return emit(r)


def cmd_branch(cfg, a):
    r = github_ops.branch_create(cfg, a.type, a.issue, a.slug, a.base)
    return emit(r, 0 if r["ok"] else 1)


def cmd_pr(cfg, a):
    title = a.title
    if a.title_file:
        with open(a.title_file, encoding="utf-8") as f:
            title = f.read().strip().splitlines()[0]
    if not title:
        return emit({"ok": False, "errors": ["--title 또는 --title-file이 필요하다"]}, 1)
    r = github_ops.pr_create(cfg, title, a.body_file, a.base, dry_run=a.dry_run)
    if r.get("ok") and not a.dry_run:
        r = _with_report(cfg, "open-pr", r, {
            "number": r.get("number"), "url": r.get("url"), "existing": r.get("existing"),
            "base": r["base"], "branch": _branch(cfg), "labelText": ", ".join(r.get("labels") or []) or "없음",
            "issue": r.get("issue"), "closesIssue": r.get("closesIssue"),
        })
    return emit(r, 0 if r.get("ok") else 1)


def cmd_test(cfg, a):
    r = test_runner.run(cfg, a.tests)
    return emit(r, 0 if r["status"] == "passed" else 1)


def cmd_collect_review_comments(cfg, a):
    out_dir = a.out or os.path.join(cfg["_root"], cfg["stateDir"], "review")
    return emit(github_ops.review_collect(cfg, a.pr, out_dir))


def cmd_render_verdict_table(cfg, a):
    return emit(github_ops.review_table(a.rows))


def cmd_resolve_review_threads(cfg, a):
    return emit(github_ops.review_resolve(cfg, a.pr, a.ids))


def _last_test_summary(cfg):
    """write-test 보고서의 수치는 run-tests 기록에서 가져온다. LLM이 옮겨 적지 않게 한다."""
    path = os.path.join(cfg["_root"], cfg["stateDir"], "last-test.json")
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        r = json.load(f)
    t = r.get("totals") or {"tests": 0, "failures": 0, "errors": 0, "skipped": 0}
    return {
        "command": r.get("command"), "durationSec": r.get("durationSec"), "totals": t, "log": r.get("log"),
        "passed": t["tests"] - t["failures"] - t["errors"] - t["skipped"],
        "failedCount": r.get("failedCount", 0), "buildErrors": r.get("buildErrors", []),
        "failed": [{"test": x["test"], "cause": x["message"]} for x in r.get("failed", [])],
    }


def cmd_report(cfg, a):
    data = _read_json(a.data)
    if a.skill == "write-test":
        data = {**_last_test_summary(cfg), **{k: v for k, v in data.items() if v not in (None, [], "")}}
    data.setdefault("next", harness_config.next_stage(cfg, a.skill) or "없음")
    sys.stdout.write(report_renderer.render(a.skill, data))
    return 0


def cmd_context(cfg, a):
    """계획에 필요한 컨벤션 절과 정책 파일만 이어 붙여 돌려준다."""
    root = cfg["_root"]
    conv = cfg["conventions"]
    files = [os.path.join(conv["dir"], conv["common"])]
    files += [os.path.join(conv["dir"], f"{x.strip()}.md") for x in (a.layers or "").split(",") if x.strip()]
    if a.migration:
        files.append(".claude/rules/migration.md")
    files += [os.path.join(cfg["policyDir"], f"{x.strip()}.md") for x in (a.domains or "").split(",") if x.strip()]
    out, missing = [], []
    for rel in files:
        p = os.path.join(root, rel)
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                out.append(f"===== {rel}\n{f.read().strip()}\n")
        else:
            missing.append(rel)
    if missing:
        out.append("===== 없는 파일: " + ", ".join(missing))
    sys.stdout.write("\n".join(out) + "\n")
    return 0


def cmd_detect_project(cfg, a):
    return emit(project_detector.detect(cfg["_root"]))


def cmd_build_config(cfg, a):
    detected = _read_json(a.detected)
    answers = _read_json(a.answers) if a.answers else {}
    r = project_detector.build_config(detected, answers)
    if a.write:
        path = os.path.join(cfg["_root"], harness_config.CONFIG_PATH)
        with open(path, "w", encoding="utf-8") as f:
            f.write(json.dumps(r["config"], ensure_ascii=False, indent=2) + "\n")
        r["written"] = harness_config.CONFIG_PATH
        r["gitignoreChanged"] = installer.set_plans_tracked(_claude_dir(cfg), r["config"]["plans"]["tracked"])
    return emit(r)


def cmd_verify_install(cfg, a):
    r = project_detector.verify(cfg)
    return emit(r, 0 if r["ok"] else 1)


def _claude_dir(cfg):
    return os.path.join(cfg["_root"], ".claude")


def cmd_adopt(cfg, a):
    r = installer.adopt(_claude_dir(cfg))
    return emit(r, 0 if r["ok"] else 1)


def cmd_remove_skill(cfg, a):
    r = installer.remove_skill(_claude_dir(cfg), a.name)
    return emit(r, 0 if r["ok"] else 1)


def cmd_check_update(cfg, a):
    """세션 시작 훅. 알릴 것이 있을 때만 세션 맥락을 출력한다. 어떤 경우에도 exit 0."""
    try:
        r = harness_update.check_update(cfg, force=a.force)
    except Exception as e:  # 세션 시작을 막지 않는다
        r = {"notify": False, "error": f"{type(e).__name__}: {e}"}
    if a.hook:
        if r.get("notify"):
            print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart",
                                                     "additionalContext": r["message"]}}, ensure_ascii=False))
        return 0
    return emit(r)


def cmd_skip_update(cfg, a):
    return emit(harness_update.skip_update(cfg, a.version))


def cmd_prepare_update(cfg, a):
    r = harness_update.prepare_update(cfg, a.version)
    return emit(r, 0 if r["ok"] else 1)


def cmd_apply_update(cfg, a):
    r = harness_update.apply_update(cfg, a.src, dry_run=a.dry_run)
    return emit(r, 0 if r.get("ok") else 1)


def cmd_skill_context(cfg, a):
    return emit(skill_context.build(cfg, a.skill))


def cmd_config(cfg, a):
    """병합된 설정. 훅(bash)이 같은 기본값을 중복해 들고 있지 않게 여기서 읽는다."""
    if a.key:
        cur = cfg
        for part in a.key.split("."):
            cur = cur.get(part) if isinstance(cur, dict) else None
        if isinstance(cur, (dict, list)):
            print(json.dumps(cur, ensure_ascii=False))
        elif isinstance(cur, bool):
            print("true" if cur else "false")
        else:
            print("" if cur is None else cur)
        return 0
    return emit({k: v for k, v in cfg.items() if not k.startswith("_")})


def cmd_guard(cfg, a):
    data = json.load(sys.stdin)
    cmd = (data.get("tool_input") or {}).get("command") or ""
    return emit(command_guard.judge(cmd, data.get("cwd") or cfg["_root"], cfg["_root"], cfg))


def build_parser():
    p = argparse.ArgumentParser(prog="harness")
    # 대상 프로젝트 루트. 하네스 소스(개인 스킬 위치)에서 실행할 때 프로젝트를 명시한다
    p.add_argument("--project", help="대상 프로젝트 루트 (기본: CLAUDE_PROJECT_DIR, 없으면 git 루트)")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("work-context", help="작업 맥락 사실 수집")
    s.add_argument("--no-github", action="store_true")
    s.add_argument("--no-base", action="store_true")
    s.set_defaults(fn=cmd_facts)

    s = sub.add_parser("pipeline-stage", help="파이프라인 현재 단계")
    s.add_argument("--stages")
    s.add_argument("--verbose", action="store_true")
    s.set_defaults(fn=cmd_status)

    s = sub.add_parser("lint", help="규칙 검사")
    s.add_argument("kind", choices=["title", "body", "commit-message", "pr", "plan"])
    s.add_argument("--text")
    s.add_argument("--file")
    s.add_argument("--against-diff", action="store_true")
    s.set_defaults(fn=cmd_lint)

    s = sub.add_parser("apply-commits", help="커밋 계획 검증과 실행")
    s.add_argument("plan", help="계획 JSON 경로 또는 -")
    s.add_argument("--dry-run", action="store_true")
    s.set_defaults(fn=cmd_commit)

    s = sub.add_parser("create-issue", help="이슈 생성 (+ --slug면 브랜치까지)")
    s.add_argument("--type", required=True)
    s.add_argument("--title", required=True)
    s.add_argument("--body-file", required=True)
    s.add_argument("--slug")
    s.add_argument("--base")
    s.add_argument("--dry-run", action="store_true")
    s.set_defaults(fn=cmd_issue)

    s = sub.add_parser("create-branch", help="작업 브랜치 생성")
    s.add_argument("--type", required=True)
    s.add_argument("--issue", required=True, type=int)
    s.add_argument("--slug", required=True)
    s.add_argument("--base")
    s.set_defaults(fn=cmd_branch)

    s = sub.add_parser("create-pr", help="PR 생성 (멱등)")
    s.add_argument("--title")
    s.add_argument("--title-file")
    s.add_argument("--body-file", required=True)
    s.add_argument("--base")
    s.add_argument("--dry-run", action="store_true")
    s.set_defaults(fn=cmd_pr)

    s = sub.add_parser("run-tests", help="테스트 실행 (Docker 확인 강제)")
    s.add_argument("--tests", action="append", help="필터. 여러 번 줄 수 있다")
    s.set_defaults(fn=cmd_test)

    s = sub.add_parser("collect-review-comments", help="리뷰 봇 코멘트 수집. 원문은 파일로, 인덱스만 출력")
    s.add_argument("--pr", type=int, required=True)
    s.add_argument("--out")
    s.set_defaults(fn=cmd_collect_review_comments)

    s = sub.add_parser("render-verdict-table", help="feedback-judge 판정 행을 판정 표로 렌더링")
    s.add_argument("--rows", required=True)
    s.set_defaults(fn=cmd_render_verdict_table)

    s = sub.add_parser("resolve-review-threads", help="반영한 코멘트의 리뷰 스레드 resolve")
    s.add_argument("--pr", type=int, required=True)
    s.add_argument("ids", nargs="+")
    s.set_defaults(fn=cmd_resolve_review_threads)

    s = sub.add_parser("render-report", help="보고서 렌더링")
    s.add_argument("skill")
    s.add_argument("--data", required=True)
    s.set_defaults(fn=cmd_report)

    s = sub.add_parser("plan-references", help="계획용 컨벤션, 정책 묶음")
    s.add_argument("--layers")
    s.add_argument("--domains")
    s.add_argument("--migration", action="store_true")
    s.set_defaults(fn=cmd_context)

    s = sub.add_parser("detect-project", help="/init-harness용 프로젝트 감지")
    s.set_defaults(fn=cmd_detect_project)

    s = sub.add_parser("build-config", help="감지 결과와 질문 답으로 harness.json 생성")
    s.add_argument("--detected", required=True, help="detect-project 출력 파일 또는 JSON")
    s.add_argument("--answers", help="질문 답 JSON")
    s.add_argument("--write", action="store_true")
    s.set_defaults(fn=cmd_build_config)

    s = sub.add_parser("verify-install", help="설치 결과 점검")
    s.set_defaults(fn=cmd_verify_install)

    s = sub.add_parser("adopt", help="클론한 .claude를 프로젝트 파일로 전환 (manifest 기록, .git 정리)")
    s.set_defaults(fn=cmd_adopt)

    s = sub.add_parser("remove-skill", help="쓰지 않는 스킬 제거 (업데이트 때 다시 설치하지 않음)")
    s.add_argument("--name", required=True)
    s.set_defaults(fn=cmd_remove_skill)

    s = sub.add_parser("check-update", help="새 하네스 버전 확인 (하루 한 번 원격 확인)")
    s.add_argument("--hook", action="store_true", help="세션 시작 훅 출력 형식")
    s.add_argument("--force", action="store_true", help="하루 한 번 제한을 무시")
    s.set_defaults(fn=cmd_check_update)

    s = sub.add_parser("skip-update", help="이 버전은 다시 묻지 않음")
    s.add_argument("--version", required=True)
    s.set_defaults(fn=cmd_skip_update)

    s = sub.add_parser("prepare-update", help="새 버전을 받아 두고 변경 기록 반환")
    s.add_argument("--version")
    s.set_defaults(fn=cmd_prepare_update)

    s = sub.add_parser("apply-update", help="받아 둔 새 버전으로 업데이트 (새 버전의 CLI로 실행)")
    s.add_argument("--src", required=True)
    s.add_argument("--dry-run", action="store_true")
    s.set_defaults(fn=cmd_apply_update)

    s = sub.add_parser("skill-context", help="스킬 로드 시 주입할 맥락")
    s.add_argument("skill")
    s.set_defaults(fn=cmd_skill_context)

    s = sub.add_parser("get-config", help="병합된 설정 조회")
    s.add_argument("key", nargs="?")
    s.set_defaults(fn=cmd_config)

    perf_tools.register(sub)
    concurrency_tools.register(sub)

    s = sub.add_parser("judge-command", help="훅용: stdin의 Bash 명령에서 rm 대상 판정")
    s.set_defaults(fn=cmd_guard)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.project:
        os.environ["CLAUDE_PROJECT_DIR"] = os.path.realpath(args.project)
    cfg = harness_config.load()
    try:
        return args.fn(cfg, args)
    except Exception as e:  # 주입 명령이 스킬을 중단시키지 않도록 오류도 JSON으로 낸다
        emit({"ok": False, "errors": [f"{type(e).__name__}: {e}"]})
        return 0 if args.cmd in ("work-context", "pipeline-stage", "plan-references", "skill-context", "detect-project", "check-update") else 1
