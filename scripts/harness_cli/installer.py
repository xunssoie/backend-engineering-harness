"""하네스 설치 기록과 업데이트.

설치는 하네스 저장소를 프로젝트의 .claude/로 클론하는 것으로 끝난다.
  git clone <하네스 저장소> .claude

- adopt (/init-harness가 처음에 부른다)
    클론된 버전, 커밋, 원격 주소, 파일별 해시를 manifest에 남기고 .claude/.git과 저장소용 파일을 지운다.
    이후 .claude는 프로젝트의 일반 파일이 된다.
- update (/update-harness가 부른다)
    프로젝트가 고치지 않은 파일   → 새 버전으로 교체
    프로젝트가 고친 파일          → 설치 당시 원본(하네스 저장소의 기록된 커밋)과 3-way 병합
    병합 충돌                    → 파일은 그대로 두고 새 버전을 .claude/.state/update/에 남겨 보고
    프로젝트가 지운 파일          → 다시 만들지 않는다
    settings.json                → 덮어쓰지 않고 권한 목록과 훅을 합친다
"""
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time

MANIFEST = "harness-manifest.json"          # .claude/ 기준
SETTINGS = "settings.json"
# 저장소에만 필요한 파일. 프로젝트에는 남기지 않는다
META_FILES = {"README.md", "LICENSE", "VERSION", "CHANGELOG.md"}
# 하네스 파일이 아닌 디렉토리. 실행 기록, 작업 산출물, 캐시
SKIP_DIRS = {".git", ".state", "resources", "__pycache__"}
UPDATE_DIR = ".state/update"


def _load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _read_text(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _read_bytes(path):
    with open(path, "rb") as f:
        return f.read()


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)
    if path.endswith(".sh") or os.path.basename(path) == "harness":
        os.chmod(path, 0o755)


def _write_json(path, obj):
    _write(path, (json.dumps(obj, ensure_ascii=False, indent=2) + "\n").encode())


def _git(cwd, *args):
    p = subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True)
    return p.stdout.strip() if p.returncode == 0 else None


def harness_files(claude_dir, manifest_name=MANIFEST):
    """하네스가 관리하는 파일 {상대 경로: 전체 경로}."""
    out = {}
    for d, dirs, files in os.walk(claude_dir):
        rel_dir = os.path.relpath(d, claude_dir)
        dirs[:] = [x for x in dirs if x not in SKIP_DIRS]
        for name in files:
            rel = name if rel_dir == "." else f"{rel_dir}/{name}"
            if rel_dir == "." and (name in META_FILES or name == manifest_name or name == "harness.json"):
                continue
            if name.endswith(".pyc") or name == ".DS_Store":
                continue
            out[rel] = os.path.join(d, name)
    return out


def _version(src):
    vf = os.path.join(src, "VERSION")
    return _read_text(vf).strip() if os.path.exists(vf) else "0.0.0"


def adopt(claude_dir):
    """클론한 .claude를 프로젝트 파일로 전환한다. 다시 실행해도 안전하다."""
    claude_dir = os.path.realpath(claude_dir)
    man_path = os.path.join(claude_dir, MANIFEST)
    git_dir = os.path.join(claude_dir, ".git")
    if not os.path.isdir(git_dir):
        if os.path.exists(man_path):
            return {"ok": True, "alreadyAdopted": True, **{k: _load_json(man_path).get(k) for k in ("version", "repoUrl")}}
        return {"ok": False, "errors": [".claude가 하네스 저장소를 클론한 것이 아니다 (.git과 manifest가 없다)"]}
    status = _git(claude_dir, "status", "--porcelain")
    manifest = {
        "version": _version(claude_dir),
        "sourceCommit": _git(claude_dir, "rev-parse", "HEAD"),
        "repoUrl": _git(claude_dir, "remote", "get-url", "origin"),
        "installedAt": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "files": {rel: _sha(_read_bytes(full)) for rel, full in sorted(harness_files(claude_dir).items())},
        "removed": [],
    }
    # 클론 뒤 커밋 전에 고친 파일이 있으면 그 상태를 기준으로 삼는다. 병합 기준(커밋)과 어긋날 수 있어 알린다
    warnings = ["클론 뒤 하네스 파일이 이미 바뀌어 있다. 다음 업데이트에서 그 파일은 수정본으로 다뤄진다"] if status else []
    _write_json(man_path, manifest)
    shutil.rmtree(git_dir)
    removed_meta = []
    for name in sorted(META_FILES):
        p = os.path.join(claude_dir, name)
        if os.path.exists(p):
            os.remove(p)
            removed_meta.append(name)
    return {"ok": True, "version": manifest["version"], "sourceCommit": manifest["sourceCommit"],
            "repoUrl": manifest["repoUrl"], "files": len(manifest["files"]), "removedMeta": removed_meta,
            "warnings": warnings}


def _base_content(src, commit, rel):
    """설치 당시 원본. 하네스 저장소의 기록된 커밋에서 꺼낸다."""
    if not commit:
        return None
    p = subprocess.run(["git", "-C", src, "show", f"{commit}:{rel}"], capture_output=True)
    return p.stdout if p.returncode == 0 else None


def _merge3(local, base, new):
    with tempfile.TemporaryDirectory() as d:
        paths = []
        for name, data in (("local", local), ("base", base), ("new", new)):
            p = os.path.join(d, name)
            with open(p, "wb") as f:
                f.write(data)
            paths.append(p)
        r = subprocess.run(["git", "merge-file", "-p", *paths], capture_output=True)
        return r.stdout, r.returncode != 0


def _union(a, b):
    out = list(a or [])
    for x in b or []:
        if x not in out:
            out.append(x)
    return out


def merge_settings(existing, ours):
    """프로젝트 설정에 하네스 권한과 훅을 더한다. 프로젝트 값은 지우지 않는다."""
    merged = dict(existing)
    perms = dict(existing.get("permissions") or {})
    for k in ("allow", "deny", "ask"):
        perms[k] = _union(perms.get(k), (ours.get("permissions") or {}).get(k))
    # rm 계열 deny는 훅의 경로 판정보다 먼저 적용돼 판정을 가린다
    perms["deny"] = [r for r in perms["deny"] if not r.startswith("Bash(rm ")]
    merged["permissions"] = perms
    hooks = dict(existing.get("hooks") or {})
    for event, groups in (ours.get("hooks") or {}).items():
        cur = list(hooks.get(event) or [])
        have = {h.get("command") for g in cur for h in g.get("hooks", [])}
        for g in groups:
            new_hooks = [h for h in g["hooks"] if h.get("command") not in have]
            if new_hooks:
                cur.append({**g, "hooks": new_hooks})
        hooks[event] = cur
    merged["hooks"] = hooks
    return merged


def _is_removed(rel, removed):
    """removed에는 파일 경로와, 스킬을 통째로 뺀 경우 `skills/{이름}/` 접두사가 들어 있다."""
    return rel in removed or any(r.endswith("/") and rel.startswith(r) for r in removed)


def update(claude_dir, src, dry_run=False):
    """src(하네스 저장소 체크아웃)의 내용으로 프로젝트 .claude를 업데이트한다."""
    claude_dir = os.path.realpath(claude_dir)
    man_path = os.path.join(claude_dir, MANIFEST)
    if not os.path.exists(man_path):
        return {"ok": False, "errors": ["manifest가 없다. 먼저 /init-harness를 실행한다"]}
    manifest = _load_json(man_path)
    old_files = manifest.get("files", {})
    removed = set(manifest.get("removed", []))
    report = {"ok": True, "from": manifest.get("version"), "to": _version(src), "added": [], "updated": [],
              "merged": [], "kept": [], "conflicts": [], "deletedUpstream": [], "skippedRemoved": [],
              "settings": None, "dryRun": dry_run}
    files = harness_files(src)
    new_hashes = {}
    for rel, full in sorted(files.items()):
        if rel == SETTINGS:
            continue
        dest = os.path.join(claude_dir, rel)
        new = _read_bytes(full)
        new_hashes[rel] = _sha(new)
        exists = os.path.exists(dest)
        if _is_removed(rel, removed) or (rel in old_files and not exists):
            report["skippedRemoved"].append(rel)
            removed.add(rel)
            new_hashes.pop(rel)
            continue
        if not exists:
            report["added"].append(rel)
            if not dry_run:
                _write(dest, new)
            continue
        local = _read_bytes(dest)
        if local == new:
            continue
        if old_files.get(rel) == _sha(local):
            report["updated"].append(rel)
            if not dry_run:
                _write(dest, new)
            continue
        base = _base_content(src, manifest.get("sourceCommit"), rel)
        if base is not None and base == new:
            report["kept"].append(rel)  # 하네스는 그대로고 프로젝트만 고쳤다
            new_hashes[rel] = old_files.get(rel)
            continue
        if base is None:
            report["conflicts"].append({"path": rel, "reason": "설치 당시 원본을 찾지 못해 병합할 수 없다"})
        else:
            merged, conflict = _merge3(local, base, new)
            if not conflict:
                report["merged"].append(rel)
                if not dry_run:
                    _write(dest, merged)
                continue
            report["conflicts"].append({"path": rel, "reason": "프로젝트 수정과 새 버전이 같은 곳을 고쳤다"})
        # 충돌: 프로젝트 파일은 그대로, 새 버전은 옆에. 해시는 예전 것을 유지해 다음에도 수정본으로 본다
        new_hashes[rel] = old_files.get(rel)
        if not dry_run:
            _write(os.path.join(claude_dir, UPDATE_DIR, rel), new)

    for rel, h in old_files.items():
        if rel not in files and not _is_removed(rel, removed):
            dest = os.path.join(claude_dir, rel)
            if os.path.exists(dest) and _sha(_read_bytes(dest)) == h:
                report["deletedUpstream"].append(rel)
                if not dry_run:
                    os.remove(dest)
            elif os.path.exists(dest):
                report["kept"].append(rel)

    spath = os.path.join(claude_dir, SETTINGS)
    existing = _load_json(spath) if os.path.exists(spath) else {}
    merged = merge_settings(existing, _load_json(os.path.join(src, SETTINGS)))
    report["settings"] = "unchanged" if merged == existing else "merged"
    if not dry_run:
        if merged != existing:
            _write_json(spath, merged)
        manifest.update({
            "version": report["to"], "sourceCommit": _git(src, "rev-parse", "HEAD"),
            "updatedAt": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "files": {k: v for k, v in new_hashes.items() if v}, "removed": sorted(removed),
        })
        _write_json(man_path, manifest)
    # 통째로 뺀 스킬은 파일 대신 `skills/{이름}/`으로 묶어 보고한다
    prefixes = sorted(r for r in removed if r.endswith("/"))
    report["skippedRemoved"] = prefixes + [p for p in report["skippedRemoved"] if not any(p.startswith(x) for x in prefixes)]
    # 충돌이 있어도 나머지 파일과 manifest는 적용됐다. 충돌은 conflicts로 따로 보고하고 ok는 실행 자체의 성패다
    report["ok"] = True
    return report


def remove_skill(claude_dir, name):
    """프로젝트에서 쓰지 않는 스킬을 지우고, 업데이트 때 다시 생기지 않게 manifest에 남긴다."""
    claude_dir = os.path.realpath(claude_dir)
    man_path = os.path.join(claude_dir, MANIFEST)
    if not os.path.exists(man_path):
        return {"ok": False, "errors": ["manifest가 없다. 먼저 /init-harness를 실행한다"]}
    manifest = _load_json(man_path)
    prefix = f"skills/{name}/"
    paths = [p for p in manifest["files"] if p.startswith(prefix)]
    if not paths:
        return {"ok": False, "errors": [f"설치된 스킬 '{name}'이 없다"]}
    # 하네스가 설치한 파일만 지운다. 같은 폴더의 사용자 파일은 남긴다
    for p in paths:
        full = os.path.join(claude_dir, p)
        if os.path.exists(full):
            os.remove(full)
        manifest["files"].pop(p)
    skill_dir = os.path.join(claude_dir, "skills", name)
    for d, _, _ in sorted(os.walk(skill_dir), key=lambda x: -len(x[0])):
        if os.path.isdir(d) and not os.listdir(d):
            os.rmdir(d)
    left = sorted(os.path.relpath(os.path.join(b, f), skill_dir)
                  for b, _, fs in os.walk(skill_dir) for f in fs) if os.path.isdir(skill_dir) else []
    # 접두사도 남겨, 나중에 그 스킬에 새 파일이 추가돼도 설치하지 않는다
    manifest["removed"] = sorted(set(manifest.get("removed", [])) | set(paths) | {prefix})
    _write_json(man_path, manifest)
    cfg_path = os.path.join(claude_dir, "harness.json")
    stage_removed = False
    if os.path.exists(cfg_path):
        cfg = _load_json(cfg_path)
        stages = (cfg.get("pipeline") or {}).get("stages")
        if stages and name in stages:
            cfg["pipeline"]["stages"] = [x for x in stages if x != name]
            _write_json(cfg_path, cfg)
            stage_removed = True
    return {"ok": True, "removed": paths, "userFilesLeft": left, "removedFromStages": stage_removed}


def set_plans_tracked(claude_dir, tracked):
    """계획서 추적 여부를 .claude/.gitignore에 반영한다."""
    path = os.path.join(claude_dir, ".gitignore")
    lines = _read_text(path).splitlines() if os.path.exists(path) else []
    has = "resources/plans/" in lines
    if tracked and has:
        lines = [x for x in lines if x != "resources/plans/"]
    elif not tracked and not has:
        lines.append("resources/plans/")
    else:
        return False
    _write(path, ("\n".join(lines) + "\n").encode())
    return True
