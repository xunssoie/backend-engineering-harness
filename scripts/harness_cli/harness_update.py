"""하네스 버전 확인과 업데이트.

릴리스는 하네스 저장소의 VERSION, CHANGELOG.md를 갱신하고 `v{버전}` 태그를 단다.

- check-update  (세션 시작 훅) 하루 한 번 원격 태그로 최신 버전을 확인하고, 새 버전이 있으면 세션 맥락에 알린다
- skip-update   사용자가 미룬 버전을 기억해 다시 묻지 않는다
- prepare-update  새 버전을 .claude/.state/update-src에 받아 두고 변경 기록을 돌려준다
- apply-update  받아 둔 새 버전의 CLI로 실행한다. 파일 업데이트와 설정 변환을 한다
"""
import json
import os
import re
import subprocess
import time

from . import installer

CHECK_INTERVAL_SEC = 24 * 3600
NETWORK_TIMEOUT_SEC = 8
SRC_DIR = ".state/update-src"
CHECK_STATE = ".state/update-check.json"

# 설정 형식이 바뀐 버전의 변환. {버전: 함수(설정 dict) -> 바꾼 내용 설명 목록}
# 그 버전으로 올라갈 때 한 번 적용된다. 새 버전의 CLI가 실행하므로 여기에 추가하면 된다
MIGRATIONS = {}


def semver(v):
    m = re.match(r"^v?(\d+)\.(\d+)\.(\d+)$", (v or "").strip())
    return tuple(int(x) for x in m.groups()) if m else None


def _claude_dir(cfg):
    return os.path.join(cfg["_root"], ".claude")


def _manifest(cfg):
    path = os.path.join(_claude_dir(cfg), installer.MANIFEST)
    return installer._load_json(path) if os.path.exists(path) else None


def _state(cfg):
    path = os.path.join(_claude_dir(cfg), CHECK_STATE)
    try:
        return installer._load_json(path)
    except (OSError, ValueError):
        return {}


def _save_state(cfg, state):
    installer._write_json(os.path.join(_claude_dir(cfg), CHECK_STATE), state)


def latest_remote_version(repo_url):
    try:
        p = subprocess.run(["git", "ls-remote", "--tags", "--refs", repo_url], capture_output=True, text=True,
                           timeout=NETWORK_TIMEOUT_SEC)
    except subprocess.TimeoutExpired:
        return None, "원격 확인 시간 초과"
    if p.returncode != 0:
        return None, p.stderr.strip()[:200]
    versions = [t.rsplit("/", 1)[-1] for t in p.stdout.split() if t.startswith("refs/tags/")]
    versions = [v for v in versions if semver(v)]
    return (max(versions, key=semver).lstrip("v") if versions else None), None


def check_update(cfg, force=False):
    """세션 시작 훅용. 실패해도 세션을 막지 않도록 예외 없이 결과만 돌려준다."""
    m = _manifest(cfg)
    if not m or not m.get("repoUrl"):
        return {"notify": False, "reason": "manifest나 원격 주소가 없다"}
    state = _state(cfg)
    now = time.time()
    if not force and now - state.get("checkedAt", 0) < CHECK_INTERVAL_SEC and state.get("latest"):
        latest, err = state["latest"], None
    else:
        latest, err = latest_remote_version(m["repoUrl"])
        if latest:
            state.update({"checkedAt": now, "latest": latest})
            _save_state(cfg, state)
    installed = m.get("version")
    newer = bool(latest and semver(latest) and semver(installed) and semver(latest) > semver(installed))
    skipped = state.get("skipped") == latest
    out = {"installed": installed, "latest": latest, "newer": newer, "skipped": skipped, "error": err,
           "notify": newer and not skipped}
    if out["notify"]:
        out["message"] = (f"하네스 새 버전 {latest}이 있다 (설치된 버전 {installed}). 사용자의 첫 요청에 답하기 전에 "
                          f"한 줄로 알리고 `/update-harness`로 업데이트할지 묻는다. 미루겠다고 하면 "
                          f"`.claude/scripts/harness skip-update --version {latest}`를 실행한다.")
    return out


def skip_update(cfg, version):
    state = _state(cfg)
    state["skipped"] = version
    _save_state(cfg, state)
    return {"ok": True, "skipped": version}


def parse_changelog(text):
    """`## 1.2.0 - 날짜` 절로 나눈다. [{version, date, body}]"""
    out = []
    for m in re.finditer(r"^##\s+v?(\d+\.\d+\.\d+)\s*(?:-\s*(\S+))?\s*$(.*?)(?=^##\s+v?\d+\.\d+\.\d+|\Z)", text, re.M | re.S):
        out.append({"version": m.group(1), "date": m.group(2), "body": m.group(3).strip()})
    return out


def prepare_update(cfg, version=None):
    m = _manifest(cfg)
    if not m:
        return {"ok": False, "errors": ["manifest가 없다. 먼저 /init-harness를 실행한다"]}
    url = m.get("repoUrl")
    src = os.path.join(_claude_dir(cfg), SRC_DIR)
    if os.path.isdir(os.path.join(src, ".git")):
        cmd = ["git", "-C", src, "fetch", "--quiet", "--tags", "origin"]
    else:
        cmd = ["git", "clone", "--quiet", url, src]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if p.returncode != 0:
        return {"ok": False, "errors": [f"하네스 저장소를 받지 못했다: {p.stderr.strip()[:200]}"]}
    if not version:
        latest, err = latest_remote_version(url)
        if not latest:
            return {"ok": False, "errors": [f"최신 버전을 찾지 못했다: {err or '태그 없음'}"]}
        version = latest
    p = subprocess.run(["git", "-C", src, "checkout", "--quiet", "--detach", f"v{version}"], capture_output=True, text=True)
    if p.returncode != 0:
        return {"ok": False, "errors": [f"태그 v{version}이 없다: {p.stderr.strip()[:200]}"]}
    installed = m.get("version")
    changelog_path = os.path.join(src, "CHANGELOG.md")
    entries = parse_changelog(installer._read_text(changelog_path)) if os.path.exists(changelog_path) else []
    base = semver(installed) or (0, 0, 0)
    between = [e for e in entries if base < semver(e["version"]) <= semver(version)]
    between.sort(key=lambda e: semver(e["version"]))
    return {"ok": True, "installed": installed, "target": version, "srcDir": src,
            "newCli": os.path.join(src, "scripts", "harness"), "changelog": between,
            "actionRequired": [e["version"] for e in between if re.search(r"###\s*필요한 조치\s*\n(?!\s*-\s*없음)", e["body"] + "\n")]}


def apply_update(cfg, src, dry_run=False):
    """새 버전의 CLI로 실행한다. 파일 업데이트 뒤 설정 변환을 적용한다."""
    claude_dir = _claude_dir(cfg)
    before = (_manifest(cfg) or {}).get("version")
    report = installer.update(claude_dir, src, dry_run=dry_run)
    target = report.get("to")
    migrated = []
    cfg_path = os.path.join(claude_dir, "harness.json")
    if not dry_run and os.path.exists(cfg_path) and semver(before) and semver(target):
        data = installer._load_json(cfg_path)
        for v in sorted(MIGRATIONS, key=semver):
            if semver(before) < semver(v) <= semver(target):
                migrated += [f"{v}: {x}" for x in MIGRATIONS[v](data)]
        if migrated:
            installer._write_json(cfg_path, data)
    report["migrations"] = migrated
    # 충돌이 남아도 버전은 올라갔으므로 미룬 기록은 지운다
    if not dry_run and report.get("to"):
        state = _state(cfg)
        state.pop("skipped", None)
        _save_state(cfg, state)
    return report
