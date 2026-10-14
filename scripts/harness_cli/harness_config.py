"""프로젝트 설정(.claude/harness.json) 로드. 없는 키는 DEFAULTS로 채운다."""
import copy
import json
import os
import subprocess

from .concurrency_tools import CONCURRENCY_DEFAULTS
from .perf_tools import PERF_DEFAULTS

CONFIG_PATH = ".claude/harness.json"

DEFAULTS = {
    "language": "ko",
    "baseBranch": "main",
    "protectedBranches": ["main", "dev"],
    # 브랜치명에서 type, issue, slug를 뽑는 정규식. 이름 붙은 그룹을 쓴다
    "branchPattern": r"^(?P<type>[a-z]+)/(?P<issue>\d+)-(?P<slug>.+)$",
    "types": {
        "feat": {"label": "Feat", "template": "feat"},
        "fix": {"label": "Fix", "template": "fix"},
        "hotfix": {"label": "HotFix", "template": "hotfix"},
        "refactor": {"label": "Refactor", "template": "refactor"},
        "docs": {"label": "Docs", "template": "docs"},
        "test": {"label": "Test", "template": "feat"},
        "cicd": {"label": "CICD", "template": "feat"},
        "chore": {"label": "Chore", "template": "feat"},
        "analysis": {"label": "Analysis", "template": "feat"},
    },
    "commitFormat": "{type}: {message}(#{issue})",
    "attribution": "Co-Authored-By: Claude <noreply@anthropic.com>",
    "title": {"maxLength": 40, "forbidden": {"·": ",", "—": "-"}},
    "issueTemplate": ".github/ISSUE_TEMPLATE/{template}-issue-template.md",
    "prTemplate": ".github/PULL_REQUEST_TEMPLATE.md",
    "prSections": ["PR Summary", "Problem", "Solution", "Related Issue"],
    "plans": {"dir": ".claude/resources/plans", "file": "PLAN-{issue}.md", "tracked": False},
    "test": {
        "command": "./gradlew test",
        "filterArg": "--tests",
        "resultsDir": "build/test-results/test",
        "requiresDocker": True,
        "dockerStartTimeoutSec": 90,
        "logDir": ".claude/.state/test-logs",
    },
    "migration": {
        "dir": "src/main/resources/db/migration",
        "filePattern": r"V[0-9]+(_[0-9]+)*__.*\.sql",
        "immutableRef": "origin/main",
    },
    "reviewBot": "coderabbitai[bot]",
    "conventions": {"dir": ".claude/rules/code-convention", "common": "common.md"},
    "policyDir": ".claude/spec/service-policy",
    "pipeline": {
        "stages": ["open-issue", "write-plan", "implement", "write-test", "commit-push", "open-pr", "review-feedback"],
        # 사용자 확인 게이트. false면 확인 없이 다음 단계로 진행한다
        "gates": {"issue": True, "plan": True, "commit": True, "pr": True},
    },
    "stateDir": ".claude/.state",
    # 성능 측정, 동시성 측정 설정. 정의와 설명은 perf_tools, concurrency_tools에 있다
    "perf": PERF_DEFAULTS,
    **CONCURRENCY_DEFAULTS,
}


def _merge(base, over):
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def project_root(start=None):
    env = os.environ.get("CLAUDE_PROJECT_DIR")
    if env and os.path.isdir(env):
        return os.path.realpath(env)
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=start or os.getcwd(), capture_output=True, text=True, timeout=10,
        )
        if out.returncode == 0:
            return os.path.realpath(out.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        pass
    return os.path.realpath(start or os.getcwd())


def load(root=None):
    root = root or project_root()
    path = os.path.join(root, CONFIG_PATH)
    user = {}
    error = None
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                user = json.load(f)
        except (OSError, ValueError) as e:
            error = f"{CONFIG_PATH} 파싱 실패: {e}"
    cfg = _merge(DEFAULTS, user)
    cfg["_root"] = root
    cfg["_configFound"] = os.path.exists(path)
    cfg["_configError"] = error
    return cfg


def next_stage(cfg, current):
    stages = cfg["pipeline"]["stages"]
    if current not in stages:
        return None
    i = stages.index(current)
    return stages[i + 1] if i + 1 < len(stages) else None
