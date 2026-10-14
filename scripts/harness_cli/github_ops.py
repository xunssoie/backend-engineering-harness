"""GitHub 부작용(이슈, 브랜치, PR, 리뷰 스레드). 검증을 먼저 하고, 다시 실행해도 같은 결과가 나오게 한다."""
import json
import os
import re

from . import work_context
from . import convention_lint, process_runner


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def issue_template(cfg, type_):
    t = cfg["types"].get(type_)
    if not t:
        return None
    path = cfg["issueTemplate"].format(template=t.get("template", type_))
    return path if os.path.exists(os.path.join(cfg["_root"], path)) else None


def issue_create(cfg, type_, title, body_file, dry_run=False):
    root = cfg["_root"]
    errs = []
    t = cfg["types"].get(type_)
    if not t:
        errs.append(f"타입 '{type_}'이 설정의 types에 없다")
    full_title = f"{type_}: {title.strip()}"
    lr = convention_lint.title(full_title, cfg)
    body = _read(body_file)
    lb = convention_lint.body(body, cfg)
    errs += lr["errors"] + lb["errors"]
    res = {"ok": not errs, "errors": errs, "warnings": lr["warnings"], "title": full_title,
           "label": t["label"] if t else None}
    if errs or dry_run:
        return res
    ok, out, err = process_runner.gh(root, "issue", "create", "--title", full_title, "--body-file", body_file,
                           "--label", t["label"], "--assignee", "@me")
    if not ok:
        return {**res, "ok": False, "errors": [f"gh issue create 실패: {err.strip()}. 라벨이면 gh label list로 확인한다"]}
    url = out.strip().splitlines()[-1]
    m = re.search(r"/issues/(\d+)", url)
    return {**res, "url": url, "number": int(m.group(1)) if m else None}


def branch_create(cfg, type_, issue, slug, base=None):
    root = cfg["_root"]
    base = base or cfg["baseBranch"]
    if not re.match(r"^[a-z0-9]+(-[a-z0-9]+)*$", slug):
        return {"ok": False, "errors": [f"slug '{slug}'는 영문 소문자 kebab-case가 아니다"]}
    name = f"{type_}/{issue}-{slug}"
    if not re.match(cfg["branchPattern"], name):
        return {"ok": False, "errors": [f"'{name}'이 branchPattern과 맞지 않는다"]}
    ok, _, err = process_runner.git(root, "fetch", "-q", "origin", base, timeout=120)
    if not ok:
        return {"ok": False, "errors": [f"origin/{base} fetch 실패: {err.strip()}"]}
    if process_runner.git_out(root, "rev-parse", "--verify", "-q", f"refs/heads/{name}"):
        ok, _, err = process_runner.git(root, "checkout", "-q", name)
        created = False
    else:
        ok, _, err = process_runner.git(root, "checkout", "-q", "-b", name, f"origin/{base}")
        created = True
    if not ok:
        return {"ok": False, "errors": [f"브랜치 체크아웃 실패: {err.strip()}"]}
    # checkout -b ... origin/base는 upstream을 base로 잡는다. 동일명 원격 브랜치로 바로잡는다
    ok, _, err = process_runner.git(root, "push", "-q", "-u", "origin", "HEAD", timeout=120)
    return {"ok": ok, "errors": [] if ok else [f"push 실패: {err.strip()}"], "branch": name, "base": base,
            "created": created, "upstream": f"origin/{name}" if ok else None}


def pr_create(cfg, title, body_file, base=None, dry_run=False):
    root = cfg["_root"]
    f = work_context.collect(cfg)
    b = f.get("branch") or {}
    issue = b.get("issue")
    base = base or cfg["baseBranch"]
    body = _read(body_file)
    lr = convention_lint.pr(title, body, cfg, issue)
    errs = list(lr["errors"])
    up = f.get("upstream") or {}
    if not up.get("remoteExists"):
        errs.append("현재 브랜치가 원격에 없다. commit-push를 먼저 한다")
    elif up.get("ahead"):
        errs.append(f"push되지 않은 커밋 {up['ahead']}개가 있다")
    labels = (f.get("issue") or {}).get("labels") or []
    if not labels and b.get("type") in cfg["types"]:
        labels = [cfg["types"][b["type"]]["label"]]
    res = {"ok": not errs, "errors": errs, "warnings": lr["warnings"], "base": base, "labels": labels,
           "issue": issue}
    existing = f.get("pr")
    if existing and not existing.get("error") and existing.get("state") == "OPEN":
        return {**res, "ok": True, "errors": [], "existing": True, "number": existing["number"], "url": existing["url"]}
    if errs or dry_run:
        return res
    args = ["pr", "create", "--title", title, "--body-file", body_file, "--base", base, "--assignee", "@me"]
    for lb in labels:
        args += ["--label", lb]
    ok, out, err = process_runner.gh(root, *args, timeout=120)
    if not ok:
        # 생성 응답이 실패여도 PR이 만들어졌을 수 있다. 다시 조회해 확인한다
        again = work_context._pr(root, b.get("name"))
        if again and not again.get("error") and again.get("state") == "OPEN":
            ok, out = True, again["url"]
        else:
            return {**res, "ok": False, "errors": [f"gh pr create 실패: {err.strip()}"]}
    url = out.strip().splitlines()[-1]
    m = re.search(r"/pull/(\d+)", url)
    number = int(m.group(1)) if m else None
    res.update({"url": url, "number": number})
    if number:
        data, _ = process_runner.gh_json(root, "pr", "view", str(number), "--json", "closingIssuesReferences")
        refs = [x["number"] for x in (data or {}).get("closingIssuesReferences", [])]
        res["closesIssue"] = bool(issue and issue in refs)
    return res


# ---- 리뷰 봇 피드백 ----

def _concat_pages(out):
    """`gh api --paginate`는 페이지별 JSON 배열을 이어 붙여 출력한다. 배열을 하나씩 떼어 합친다."""
    dec, i, items = json.JSONDecoder(), 0, []
    while i < len(out):
        while i < len(out) and out[i].isspace():
            i += 1
        if i >= len(out):
            break
        page, i = dec.raw_decode(out, i)
        items += page if isinstance(page, list) else [page]
    return items


def _repo(root):
    data, err = process_runner.gh_json(root, "repo", "view", "--json", "nameWithOwner")
    return (data or {}).get("nameWithOwner"), err


def review_threads(root, repo, pr_number):
    """PR의 리뷰 스레드 전체 (페이지네이션). {첫 코멘트 databaseId(str): (threadId, isResolved)}. 실패하면 (None, 오류)."""
    owner, name = repo.split("/")
    q = """query($owner:String!,$repo:String!,$pr:Int!,$after:String){repository(owner:$owner,name:$repo){pullRequest(number:$pr){
      reviewThreads(first:100,after:$after){pageInfo{hasNextPage endCursor}
        nodes{id isResolved comments(first:1){nodes{databaseId}}}}}}}"""
    threads, after = {}, None
    while True:
        args = ["api", "graphql", "-f", f"query={q}", "-F", f"owner={owner}", "-F", f"repo={name}", "-F", f"pr={pr_number}"]
        if after:
            args += ["-f", f"after={after}"]
        data, err = process_runner.gh_json(root, *args)
        if err:
            return None, err
        try:
            conn = data["data"]["repository"]["pullRequest"]["reviewThreads"]
        except (KeyError, TypeError):
            return None, "스레드 조회 응답 형식이 다르다"
        for n in conn["nodes"]:
            c = n["comments"]["nodes"]
            if c:
                threads[str(c[0]["databaseId"])] = (n["id"], n["isResolved"])
        if not conn["pageInfo"]["hasNextPage"]:
            return threads, None
        after = conn["pageInfo"]["endCursor"]


def resolved_comment_ids(root, repo, pr_number):
    """resolve된 스레드의 첫 코멘트 id 집합(str). 조회에 실패하면 (빈 집합, 오류)로 답글 기준만 남긴다."""
    threads, err = review_threads(root, repo, pr_number)
    return {cid for cid, (_, done) in (threads or {}).items() if done}, err


def review_collect(cfg, pr_number, out_dir):
    """원문은 out_dir 파일로, 컨텍스트에는 인덱스만 돌려준다.
    처리된 항목 = 답글이 달렸거나 스레드가 resolve된 것. review-feedback은 반영한 항목을 답글 없이 resolve한다."""
    root = cfg["_root"]
    repo, err = _repo(root)
    if not repo:
        return {"ok": False, "errors": [f"저장소 확인 실패: {err}"]}
    bot = cfg["reviewBot"]
    ok, out, err = process_runner.gh(root, "api", "--paginate", f"repos/{repo}/pulls/{pr_number}/comments", timeout=120)
    if not ok:
        return {"ok": False, "errors": [f"코멘트 수집 실패: {err.strip()}"]}
    comments = _concat_pages(out)
    replied = {c["in_reply_to_id"] for c in comments if c.get("in_reply_to_id")}
    resolved, thread_err = resolved_comment_ids(root, repo, pr_number)
    items = [c for c in comments if c["user"]["login"] == bot and not c.get("in_reply_to_id")]
    os.makedirs(out_dir, exist_ok=True)
    items_path = os.path.join(out_dir, f"rf-{pr_number}-items.jsonl")
    pending, handled = [], 0
    with open(items_path, "w", encoding="utf-8") as fo:
        for c in items:
            if c["id"] in replied or str(c["id"]) in resolved:
                handled += 1
                continue
            fo.write(json.dumps({"id": c["id"], "path": c["path"], "line": c.get("line"), "body": c["body"]},
                                ensure_ascii=False) + "\n")
            pending.append({"id": c["id"], "path": c["path"], "line": c.get("line")})
    ok, out, _ = process_runner.gh(root, "api", "--paginate", f"repos/{repo}/pulls/{pr_number}/reviews",
                         "--jq", f'.[] | select(.user.login == "{bot}") | .body', timeout=120)
    reviews_path = os.path.join(out_dir, f"rf-{pr_number}-reviews.txt")
    with open(reviews_path, "w", encoding="utf-8") as fo:
        fo.write(out if ok else "")
    res = {"ok": True, "repo": repo, "pr": pr_number, "itemsFile": items_path, "reviewsFile": reviews_path,
           "reviewsNonEmpty": bool(ok and out.strip()), "pending": pending, "handledExcluded": handled}
    if thread_err:
        res["warnings"] = [f"스레드 resolve 상태를 조회하지 못해 답글 유무로만 제외했다: {thread_err}"]
    return res


VERDICT_ORDER = {"상": 0, "중": 1, "하": 2}


def review_table(rows_path):
    """feedback-judge가 쓴 판정 행(`id | 판정 | 대상 | 요지 | 근거 | 수정 지침`)을 판정 표로 렌더링한다.
    rows_path가 디렉토리면 그 안의 verdict-*.txt를 모두 읽는다 (판정 에이전트가 항목마다 파일 하나를 쓴다)."""
    import glob as _glob
    if os.path.isdir(rows_path):
        text = "\n".join(_read(f) for f in sorted(_glob.glob(os.path.join(rows_path, "verdict-*.txt"))))
    else:
        text = _read(rows_path)
    rows, bad = [], []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("```"):
            continue
        parts = [p.strip() for p in line.strip("|").split("|")]
        if len(parts) != 6 or parts[1] not in VERDICT_ORDER:
            bad.append(line)
            continue
        rows.append(dict(zip(("id", "verdict", "target", "point", "reason", "fix"), parts)))
    rows.sort(key=lambda r: VERDICT_ORDER[r["verdict"]])
    lines = ["| # | 판정 | 대상 | 지적 요지 | 판정 근거 |", "|---|------|------|-----------|-----------|"]
    for i, r in enumerate(rows, 1):
        r["no"] = i
        lines.append(f"| {i} | {r['verdict']} | `{r['target']}` | {r['point']} | {r['reason']} |")
    counts = {k: sum(1 for r in rows if r["verdict"] == k) for k in VERDICT_ORDER}
    return {"table": "\n".join(lines), "counts": counts, "rows": rows, "malformed": bad}


def review_resolve(cfg, pr_number, comment_ids):
    root = cfg["_root"]
    repo, err = _repo(root)
    if not repo:
        return {"ok": False, "errors": [f"저장소 확인 실패: {err}"]}
    threads, err = review_threads(root, repo, pr_number)
    if err:
        return {"ok": False, "errors": [f"스레드 조회 실패: {err}"]}
    resolved, missing, already = [], [], []
    m = "mutation($id:ID!){resolveReviewThread(input:{threadId:$id}){thread{isResolved}}}"
    for cid in map(str, comment_ids):
        if cid not in threads:
            missing.append(cid)
        elif threads[cid][1]:
            already.append(cid)
        else:
            ok, _, _ = process_runner.gh(root, "api", "graphql", "-f", f"query={m}", "-F", f"id={threads[cid][0]}")
            (resolved if ok else missing).append(cid)
    return {"ok": not missing, "resolved": resolved, "alreadyResolved": already, "notFound": missing}
