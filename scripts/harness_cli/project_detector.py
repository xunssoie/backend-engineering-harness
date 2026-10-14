"""/init-harness용 프로젝트 감지, 설정 생성, 설치 검증.

코드에서 알아낼 수 있는 것은 묻지 않는다. 감지 결과에 근거(evidence)를 남겨 사용자가 확인할 수 있게 한다.
"""
import glob
import json
import os
import re

from . import harness_config, process_runner, skill_context

DEPENDENCY_FLAGS = {
    "mysql": r"mysql-connector|com\.mysql",
    "postgresql": r"org\.postgresql|postgresql:",
    "h2": r"com\.h2database|:h2[:'\"]",
    "flyway": r"flyway",
    "liquibase": r"liquibase",
    "testcontainers": r"testcontainers",
    "querydsl": r"querydsl",
    "security": r"spring-boot-starter-security",
    "oauth2": r"oauth2",
    "springdoc": r"springdoc",
    "actuator": r"spring-boot-starter-actuator",
    "prometheus": r"micrometer-registry-prometheus",
    "redis": r"spring-boot-starter-data-redis|redisson|lettuce",
    "lombok": r"lombok",
    "jpa": r"spring-boot-starter-data-jpa",
    "mybatis": r"mybatis",
}
INFRA_PACKAGES = {"global", "common", "config", "infra", "support", "util", "utils", "core", "shared"}
LAYER_DIRS = ("controller", "facade", "service", "repository", "domain", "entity", "dto", "infra")


def _read(path, limit=200000):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read(limit)
    except OSError:
        return ""


def _rel(root, p):
    return os.path.relpath(p, root)


def _build(root):
    files = {name: os.path.join(root, name) for name in ("build.gradle", "build.gradle.kts", "pom.xml")
             if os.path.exists(os.path.join(root, name))}
    if not files:
        return {"tool": None}
    name, path = next(iter(files.items()))
    text = _read(path)
    tool = "maven" if name == "pom.xml" else "gradle"
    info = {"tool": tool, "file": name}
    m = re.search(r"org\.springframework\.boot['\"]?\)?\s+version\s+['\"]([\d.]+)", text) or \
        re.search(r"<artifactId>spring-boot-starter-parent</artifactId>\s*<version>([\d.]+)", text)
    info["springBoot"] = m.group(1) if m else None
    m = re.search(r"JavaLanguageVersion\.of\((\d+)\)|jvmToolchain\((\d+)\)|VERSION_(\d+)|<java\.version>(\d+)", text)
    info["java"] = next((g for g in m.groups() if g), None) if m else None
    m = re.search(r"kotlin[\"']?\)?(?:\(\"jvm\"\)|\.jvm['\"]?)?\s+version\s+['\"]([\d.]+)", text)
    info["kotlin"] = m.group(1) if m else None
    info["deps"] = sorted(k for k, pat in DEPENDENCY_FLAGS.items() if re.search(pat, text))
    if tool == "gradle":
        info["testCommand"] = "./gradlew test"
        info["filterArg"] = "--tests"
        info["resultsDir"] = "build/test-results/test"
    else:
        wrapper = "./mvnw" if os.path.exists(os.path.join(root, "mvnw")) else "mvn"
        info["testCommand"] = f"{wrapper} test"
        info["filterArg"] = "-Dtest="
        info["resultsDir"] = "target/surefire-reports"
    return info


def _sources(root):
    langs = [x for x in ("kotlin", "java") if os.path.isdir(os.path.join(root, "src", "main", x))]
    app = None
    for lang in langs:
        ext = "kt" if lang == "kotlin" else "java"
        for p in glob.glob(os.path.join(root, "src", "main", lang, "**", f"*.{ext}"), recursive=True):
            if "@SpringBootApplication" in _read(p, 20000):
                app = p
                break
        if app:
            break
    info = {"languages": langs, "application": _rel(root, app) if app else None}
    if not app:
        return info
    text = _read(app, 5000)
    m = re.search(r"^\s*package\s+([\w.]+)", text, re.M)
    pkg = m.group(1) if m else None
    pkg_dir = os.path.dirname(app)
    info["rootPackage"] = pkg
    info["sourceRoot"] = _rel(root, pkg_dir)
    domains, layers = [], set()
    for d in sorted(os.listdir(pkg_dir)):
        full = os.path.join(pkg_dir, d)
        if not os.path.isdir(full):
            continue
        sub = {x for x in os.listdir(full) if os.path.isdir(os.path.join(full, x))}
        found = sub & set(LAYER_DIRS)
        if d in INFRA_PACKAGES:
            info.setdefault("infraPackages", []).append(d)
        elif found:
            domains.append(d)
            layers |= found
    info["domains"] = domains
    info["layers"] = sorted(layers)
    info["hasFacade"] = "facade" in layers
    exc = []
    for p in glob.glob(os.path.join(pkg_dir, "**", "*.*"), recursive=True):
        base = os.path.basename(p)
        if re.match(r".*(ErrorCode|ExceptionCode|Exception)\.(kt|java)$", base):
            exc.append(_rel(root, p))
    info["exceptionFiles"] = sorted(exc)[:10]
    return info


def _tests(root, sources):
    out = {"dirs": [x for x in ("src/test/kotlin", "src/test/java") if os.path.isdir(os.path.join(root, x))]}
    counts = {"springBootTest": 0, "dataJpaTest": 0, "mockito": 0, "testcontainers": 0, "files": 0}
    annotations = []
    for d in out["dirs"]:
        for p in glob.glob(os.path.join(root, d, "**", "*.*"), recursive=True):
            if not p.endswith((".kt", ".java")):
                continue
            t = _read(p, 50000)
            counts["files"] += 1
            counts["springBootTest"] += "@SpringBootTest" in t
            counts["dataJpaTest"] += "@DataJpaTest" in t
            counts["mockito"] += bool(re.search(r"Mockito|@Mock\b|mockk", t))
            counts["testcontainers"] += "Testcontainers" in t or "@Container" in t
            for m in re.finditer(r"annotation class (\w+)|@interface (\w+)", t):
                annotations.append(m.group(1) or m.group(2))
    out["counts"] = counts
    out["customAnnotations"] = sorted(set(annotations))
    out["fixtureDirs"] = sorted({_rel(root, os.path.dirname(p)) for d in out["dirs"]
                                 for p in glob.glob(os.path.join(root, d, "**", "*Fixture*.*"), recursive=True)})[:10]
    return out


def _migration(root):
    hits = glob.glob(os.path.join(root, "src", "main", "resources", "**", "V*__*.sql"), recursive=True)
    if not hits:
        return {"dir": None}
    dirs = {}
    for h in hits:
        dirs.setdefault(_rel(root, os.path.dirname(h)), []).append(os.path.basename(h))
    main = max(dirs, key=lambda k: len(dirs[k]))
    names = dirs[main]
    style = "V{major}_{minor}__" if any(re.match(r"V\d+_\d+__", n) for n in names) else "V{n}__"
    return {"dir": main, "otherDirs": sorted(k for k in dirs if k != main), "count": len(names), "style": style,
            "examples": sorted(names)[:3]}


def _git(root):
    head = process_runner.git_out(root, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")
    default = head.split("/", 1)[1] if head else (process_runner.git_out(root, "branch", "--show-current") or "main")
    long_lived = [b for b in ("main", "master", "dev", "develop")
                  if process_runner.git_out(root, "rev-parse", "--verify", "-q", f"refs/remotes/origin/{b}")]
    has_dev = "dev" in long_lived
    branches = (process_runner.git_out(root, "branch", "-r", "--format=%(refname:short)") or "").splitlines()
    names = [b.split("/", 1)[1] for b in branches if "/" in b and not b.endswith("/HEAD")]
    pattern_hits = [b for b in names if re.match(harness_config.DEFAULTS["branchPattern"], b)]
    types = sorted({b.split("/", 1)[0] for b in pattern_hits})
    return {"defaultBranch": default, "hasDev": has_dev, "longLivedBranches": long_lived, "baseBranch": "dev" if has_dev else default,
            "remoteBranches": len(names), "branchPatternMatches": len(pattern_hits), "branchTypesSeen": types}


def _github(root):
    out = {}
    tdir = os.path.join(root, ".github", "ISSUE_TEMPLATE")
    out["issueTemplates"] = sorted(os.listdir(tdir)) if os.path.isdir(tdir) else []
    pr = next((p for p in (".github/PULL_REQUEST_TEMPLATE.md", ".github/pull_request_template.md",
                           "PULL_REQUEST_TEMPLATE.md") if os.path.exists(os.path.join(root, p))), None)
    out["prTemplate"] = pr
    out["prSections"] = re.findall(r"^##\s+(.+?)\s*$", _read(os.path.join(root, pr)), re.M) if pr else []
    data, err = process_runner.gh_json(root, "label", "list", "--json", "name", "--limit", "200")
    out["labels"] = [x["name"] for x in data] if data else None
    out["labelsError"] = err
    out["coderabbit"] = any(os.path.exists(os.path.join(root, p)) for p in (".coderabbit.yaml", ".coderabbit.yml"))
    return out


def _existing_harness(root):
    c = os.path.join(root, ".claude")
    out = {"claudeMd": [p for p in ("CLAUDE.md", ".claude/CLAUDE.md") if os.path.exists(os.path.join(root, p))],
           "agentsMd": os.path.exists(os.path.join(root, "AGENTS.md"))}
    for sub in ("rules", "spec", "skills", "agents"):
        d = os.path.join(c, sub)
        out[sub] = sorted(_rel(c, p) for p in glob.glob(os.path.join(d, "**", "*.md"), recursive=True)) if os.path.isdir(d) else []
    return out


def detect(root):
    build = _build(root)
    sources = _sources(root)
    return {
        "root": root,
        "build": build,
        "sources": sources,
        "tests": _tests(root, sources),
        "migration": _migration(root),
        "git": _git(root),
        "github": _github(root),
        # 로컬 개발용 compose를 앞에 둔다. 측정은 로컬에서 한다
        "compose": sorted((_rel(root, p) for p in glob.glob(os.path.join(root, "**", "*compose*.y*ml"), recursive=True)
                           if "/node_modules/" not in p), key=lambda x: ("local" not in x, x))[:10],
        "existing": _existing_harness(root),
    }


def _label_key(name):
    """이모지, 공백, 기호를 빼고 비교한다. 라벨이 `🌼 Feat`처럼 꾸며져 있어도 `Feat`와 맞춘다."""
    return re.sub(r"[^0-9a-z]", "", name.lower())


def _match_label(labels, wanted):
    if not labels:
        return wanted
    for lb in labels:
        if _label_key(lb) == _label_key(wanted):
            return lb
    return None


def build_config(detected, answers):
    """감지 결과와 질문 답으로 .claude/harness.json 내용을 만든다. answers 키는 init-harness 질문지와 같다."""
    d, a = detected, answers or {}
    cfg = {"language": a.get("language", "ko")}
    g = d["git"]
    cfg["baseBranch"] = a.get("baseBranch", g["baseBranch"])
    cfg["protectedBranches"] = sorted({g["defaultBranch"], cfg["baseBranch"]} | set(g.get("longLivedBranches", [])))
    labels = d["github"]["labels"]
    types = {}
    for k, v in harness_config.DEFAULTS["types"].items():
        types[k] = {"label": _match_label(labels, v["label"]) or v["label"], "template": v["template"]}
    cfg["types"] = types
    missing = [v["label"] for v in types.values() if labels is not None and v["label"] not in labels]
    cfg["attribution"] = a.get("attribution", harness_config.DEFAULTS["attribution"]) or ""
    if d["github"]["prSections"]:
        cfg["prSections"] = d["github"]["prSections"]
    if d["github"]["prTemplate"]:
        cfg["prTemplate"] = d["github"]["prTemplate"]
    cfg["plans"] = {**harness_config.DEFAULTS["plans"], "tracked": bool(a.get("plansTracked", False))}
    b = d["build"]
    if b.get("tool"):
        cfg["test"] = {"command": b["testCommand"], "filterArg": b["filterArg"], "resultsDir": b["resultsDir"],
                       "requiresDocker": a.get("requiresDocker", "testcontainers" in b.get("deps", [])
                                               or d["tests"]["counts"]["testcontainers"] > 0)}
    m = d["migration"]
    if m.get("dir"):
        cfg["migration"] = {"dir": m["dir"], "filePattern": harness_config.DEFAULTS["migration"]["filePattern"],
                            "immutableRef": f"origin/{g['defaultBranch']}"}
    cfg["reviewBot"] = a.get("reviewBot", harness_config.DEFAULTS["reviewBot"])
    stages = a.get("stages") or list(harness_config.DEFAULTS["pipeline"]["stages"])
    if not a.get("reviewBot", True) and "review-feedback" in stages:
        stages.remove("review-feedback")
    perf_skills = a.get("perfSkills", "none")
    if perf_skills != "none":
        # 코드에서 알 수 있는 값만 채운다. 컨테이너, DB 이름, 토큰 설정은 스킬을 처음 쓸 때 사용자와 채운다
        deps = d["build"].get("deps", [])
        db = "mysql" if "mysql" in deps else "postgresql" if "postgresql" in deps else ""
        cfg["perf"] = {"db": db, "loadTool": a.get("loadTool", "k6")}
        if d.get("compose"):
            cfg["perf"]["composeFile"] = d["compose"][0]
    cfg["pipeline"] = {"stages": stages, "gates": {**harness_config.DEFAULTS["pipeline"]["gates"], **(a.get("gates") or {})}}
    return {"config": cfg, "missingLabels": missing}


def verify(cfg):
    """설치 결과 점검. 실패 항목은 고칠 방법과 함께 돌려준다."""
    root = cfg["_root"]
    checks = []

    def add(name, ok, detail=""):
        checks.append({"check": name, "ok": bool(ok), "detail": detail})

    add("harness.json", cfg["_configFound"] and not cfg["_configError"], cfg["_configError"] or "")
    for h in ("block-dangerous-commands.sh", "protect-migrations.sh", "protect-claude-config.sh", "check-harness-update.sh"):
        p = os.path.join(root, ".claude", "hooks", h)
        add(f"훅 실행 권한 {h}", os.access(p, os.X_OK), "chmod +x로 고친다")
    settings = _read(os.path.join(root, ".claude", "settings.json"))
    add("settings.json 훅 등록", all(h in settings for h in ("block-dangerous-commands.sh", "protect-migrations.sh",
                                                             "check-harness-update.sh")))
    for ref in sorted(set(re.findall(r"\.claude/hooks/([\w.-]+\.sh)", settings))):
        add(f"등록된 훅 파일 {ref}", os.path.exists(os.path.join(root, ".claude", "hooks", ref)),
            "settings.json에 등록됐지만 파일이 없다. 등록을 지우거나 파일을 되살린다")
    add("settings.json에 rm deny 없음", not re.search(r'"Bash\(rm ', settings), "deny의 rm 규칙은 훅 판정을 가린다")
    if cfg.get("migration", {}).get("dir"):
        add("마이그레이션 경로", os.path.isdir(os.path.join(root, cfg["migration"]["dir"])), cfg["migration"]["dir"])
    gi = _read(os.path.join(root, ".claude", ".gitignore"))
    add(".claude/.gitignore에 .state/", ".state/" in gi.splitlines(), "실행 기록이 커밋되지 않게 한다")
    add("하네스 저장소 정리 (.claude/.git 없음)", not os.path.exists(os.path.join(root, ".claude", ".git")),
        "adopt를 실행하지 않았다. 그대로 두면 프로젝트 git이 .claude를 하위 저장소로 본다")
    man = os.path.join(root, ".claude", "harness-manifest.json")
    add("manifest", os.path.exists(man), "adopt가 만든다. 업데이트의 병합 기준이다")
    for stage in cfg["pipeline"]["stages"]:
        skill_md = os.path.join(root, ".claude", "skills", stage, "SKILL.md")
        add(f"스킬 {stage}", os.path.exists(skill_md))
        try:
            ctx = skill_context.build(cfg, stage)
            errs = [e for e in ctx.get("errors", []) if "브랜치명" not in e]
            add(f"skill-context {stage}", not errs, " / ".join(errs))
        except Exception as e:  # 주입 명령이 실패하면 스킬 호출이 중단되므로 여기서 잡는다
            add(f"skill-context {stage}", False, f"{type(e).__name__}: {e}")
    rules_dir = os.path.join(root, ".claude", "rules")
    for p in glob.glob(os.path.join(rules_dir, "**", "*.md"), recursive=True):
        text = _read(p, 3000)
        m = re.search(r"^paths:\s*\n((?:\s+-\s+.+\n)+)", text, re.M)
        if not m:
            continue
        globs_ = re.findall(r'-\s+"?([^"\n]+)"?', m.group(1))
        hit = any(glob.glob(os.path.join(root, g_), recursive=True) for g_ in globs_)
        add(f"rules paths 매칭 {_rel(root, p)}", hit, "어떤 파일과도 맞지 않으면 자동 로드되지 않는다: " + ", ".join(globs_))
    ok, _, _ = process_runner.gh(root, "auth", "status")
    add("gh 인증", ok, "gh auth login")
    return {"ok": all(c["ok"] for c in checks), "failed": [c for c in checks if not c["ok"]], "passed": len([c for c in checks if c["ok"]])}
