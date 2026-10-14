"""제목, 본문, 커밋 메시지, PR, 계획서 규칙 검사. 산문 규칙을 기억시키는 대신 실행 직전에 검사한다."""
import re

DECISION_HEADINGS = ("결정 필요", "Decisions needed")


def _result(errors, warnings=None):
    return {"ok": not errors, "errors": errors, "warnings": warnings or []}


def _forbidden(text, cfg, where):
    errs = []
    for ch, repl in cfg["title"]["forbidden"].items():
        if ch in text:
            errs.append(f"{where}: '{ch}' 대신 '{repl}'를 쓴다 ({text.count(ch)}곳)")
    return errs


def _strip_title(text, types):
    """`{type}: 내용(#번호)`에서 내용만 남긴다. 길이 규칙은 내용에 적용한다."""
    t = text.strip()
    m = re.match(r"^([a-z]+):\s*(.*)$", t)
    if m and m.group(1) in types:
        t = m.group(2)
    return re.sub(r"\s*\(#\d+\)\s*$", "", t)


def title(text, cfg):
    errs, warns = [], []
    body = _strip_title(text, cfg["types"])
    limit = cfg["title"]["maxLength"]
    if len(body) > limit:
        errs.append(f"제목 내용이 {len(body)}자다. {limit}자 이내로 줄인다: '{body}'")
    errs += _forbidden(text, cfg, "제목")
    if re.search(r"\([^()]*\([^()]*\)[^()]*\)", body):
        errs.append("제목에 괄호가 중첩됐다")
    camel = re.findall(r"\b[A-Z][a-z]+(?:[A-Z][a-z0-9]+)+\b", body)
    if len(camel) >= 2:
        warns.append(f"제목에 클래스명이 나열됐다({', '.join(camel)}). 무엇을 해결했는지를 남긴다")
    return _result(errs, warns)


def body(text, cfg):
    errs = _forbidden(text, cfg, "본문")
    if "<!--" in text:
        errs.append("본문에 템플릿 작성 가이드 주석(<!-- -->)이 남아 있다")
    return _result(errs)


def commit(msg, cfg, issue=None):
    errs = []
    first = msg.strip().splitlines()[0] if msg.strip() else ""
    m = re.match(r"^([a-z]+): (.+?)(\(#(\d+)\))?$", first)
    if not m:
        errs.append(f"커밋 메시지 형식이 '{cfg['commitFormat']}'가 아니다: '{first}'")
        return _result(errs)
    if m.group(1) not in cfg["types"]:
        errs.append(f"커밋 타입 '{m.group(1)}'이 설정의 types에 없다")
    if issue and m.group(4) and int(m.group(4)) != int(issue):
        errs.append(f"커밋 메시지의 이슈 번호 #{m.group(4)}가 브랜치 이슈 #{issue}와 다르다")
    if issue and not m.group(4):
        errs.append(f"커밋 메시지 끝에 (#{issue})가 없다")
    t = title(first, cfg)
    return _result(errs + t["errors"], t["warnings"])


def pr(title_text, body_text, cfg, issue=None):
    errs, warns = [], []
    t = title(title_text, cfg)
    errs += t["errors"]
    warns += t["warnings"]
    if issue and not re.search(rf"\(#{issue}\)\s*$", title_text):
        errs.append(f"PR 제목 끝에 (#{issue})가 없다")
    b = body(body_text, cfg)
    errs += b["errors"]
    headings = re.findall(r"^##\s+(.+?)\s*$", body_text, re.M)
    pos = -1
    for s in cfg["prSections"]:
        if s not in headings:
            errs.append(f"PR 본문에 '## {s}' 섹션이 없다")
        else:
            i = headings.index(s)
            if i < pos:
                errs.append(f"'## {s}' 섹션 순서가 템플릿과 다르다")
            pos = i
    if issue and not re.search(rf"(?i)\b(close|closes|fix|fixes|resolve|resolves)\s+#{issue}\b", body_text):
        errs.append(f"본문에 '- close #{issue}'가 없어 머지 후 이슈가 닫히지 않는다")
    return _result(errs, warns)


def _section(lines, names):
    """`## {name}`부터 다음 `## `까지의 줄."""
    out, inside = [], False
    for line in lines:
        if line.startswith("## "):
            inside = any(n in line for n in names)
            continue
        if inside:
            out.append(line)
    return out


def plan_unchecked(path):
    with open(path, encoding="utf-8") as f:
        lines = f.read().splitlines()
    sec = _section(lines, DECISION_HEADINGS)
    # 템플릿 자리표시자(`{미결정 사항}`)는 미결정으로 세지 않는다
    return [line.strip() for line in sec if re.match(r"^\s*- \[ \]", line) and "{" not in line]


def plan_paths(path):
    """계획서 '영향 범위'에 백틱으로 적힌 경로."""
    with open(path, encoding="utf-8") as f:
        lines = f.read().splitlines()
    sec = _section(lines, ("영향 범위",))
    paths = []
    for line in sec:
        m = re.match(r"^\s*-\s*`([^`]+)`", line)
        if m and "/" in m.group(1) and "{" not in m.group(1):
            paths.append(m.group(1))
    return paths


def plan(path, changed_files=None):
    errs, warns = [], []
    unchecked = plan_unchecked(path)
    if unchecked:
        errs.append(f"미결정 항목 {len(unchecked)}개: " + " / ".join(unchecked))
    if changed_files is not None:
        missing = [p for p in plan_paths(path) if not any(c.endswith(p) or p.endswith(c) for c in changed_files)]
        if missing:
            errs.append("계획서 영향 범위에 있지만 변경되지 않은 파일: " + ", ".join(missing))
    return _result(errs, warns)
