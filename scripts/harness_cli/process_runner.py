"""git, gh 실행 헬퍼. 실패는 예외 대신 (ok, stdout, stderr)로 돌려준다."""
import json
import os
import shutil
import subprocess


def run(args, cwd, timeout=60, input_text=None):
    try:
        p = subprocess.run(
            args, cwd=cwd, capture_output=True, text=True, timeout=timeout, input=input_text,
        )
        return p.returncode == 0, p.stdout, p.stderr
    except FileNotFoundError:
        return False, "", f"{args[0]}: 명령을 찾을 수 없다"
    except subprocess.TimeoutExpired:
        return False, "", f"{' '.join(args[:3])}: {timeout}초 시간 초과"


def git(cwd, *args, timeout=60):
    return run(["git", *args], cwd, timeout=timeout)


def git_out(cwd, *args):
    ok, out, _ = git(cwd, *args)
    return out.strip() if ok else None


def gh_bin():
    return os.environ.get("HARNESS_GH") or shutil.which("gh")


def gh(cwd, *args, timeout=60):
    b = gh_bin()
    if not b:
        return False, "", "gh CLI가 없다"
    return run([b, *args], cwd, timeout=timeout)


def gh_json(cwd, *args):
    ok, out, err = gh(cwd, *args)
    if not ok:
        return None, err.strip()
    try:
        return json.loads(out), None
    except ValueError:
        return None, "gh 출력이 JSON이 아니다"
