"""보고서 렌더러. 모양이 정해진 출력은 여기서 만들고, LLM은 판단 칸의 값만 넘긴다.

템플릿 문법 (mustache 부분집합):
  {{key}} {{a.b}}         값. 없으면 빈 문자열
  {{#key}}...{{/key}}     리스트면 항목마다 반복(안에서 항목의 키를 먼저 찾는다), 참이면 한 번
  {{^key}}...{{/key}}     거짓이거나 빈 리스트일 때
"""
import os
import re

# .claude/scripts/harness_cli → .claude/skills/<스킬>/template/report.md
SKILLS_DIR = os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "..", "skills")
SECTION = re.compile(r"{{([#^])\s*([\w.]+)\s*}}(.*?){{/\s*\2\s*}}", re.S)
VAR = re.compile(r"{{\s*([\w.]+)\s*}}")


def _lookup(name, stack):
    if name == ".":
        for ctx in reversed(stack):
            if isinstance(ctx, dict) and "." in ctx:
                return ctx["."]
        return None
    for ctx in reversed(stack):
        cur, ok = ctx, True
        for part in name.split("."):
            if isinstance(cur, dict) and part in cur:
                cur = cur[part]
            elif isinstance(cur, list) and part.isdigit() and int(part) < len(cur):
                cur = cur[int(part)]
            else:
                ok = False
                break
        if ok:
            return cur
    return None


def render_text(tmpl, data, stack=None):
    stack = stack or [data]

    def sec(m):
        kind, name, body = m.group(1), m.group(2), m.group(3)
        val = _lookup(name, stack)
        if kind == "^":
            return render_text(body, data, stack) if not val else ""
        if isinstance(val, list):
            return "".join(render_text(body, data, stack + [x if isinstance(x, dict) else {".": x}]) for x in val)
        return render_text(body, data, stack + ([val] if isinstance(val, dict) else [])) if val else ""

    out = SECTION.sub(sec, tmpl)

    def var(m):
        v = _lookup(m.group(1), stack)
        return "" if v is None else str(v)

    return VAR.sub(var, out)


def render(skill, data):
    path = os.path.join(SKILLS_DIR, skill, "template", "report.md")
    if not os.path.exists(path):
        raise FileNotFoundError(f"스킬 '{skill}'의 보고서 템플릿이 없다: .claude/skills/{skill}/template/report.md")
    with open(path, encoding="utf-8") as f:
        tmpl = f.read()
    text = render_text(tmpl, data)
    return re.sub(r"\n{3,}", "\n\n", text).strip() + "\n"
