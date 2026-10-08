"""B1: secretos y configuración local fuera del repo, y comprobaciones obligatorias en CI."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_local_config_and_secrets_are_ignored_by_git() -> None:
    for name in ("config.toml", "service.env", ".env", "data/live/state.json",
                 "data/live/trades.csv", "STOP"):
        r = subprocess.run(["git", "check-ignore", "-q", name], cwd=ROOT, check=False)
        assert r.returncode == 0, f"{name} debería estar en .gitignore"
    r = subprocess.run(["git", "check-ignore", "-q", "config.example.toml"], cwd=ROOT,
                       check=False)
    assert r.returncode == 1  # el ejemplo sí se versiona


def test_no_secret_or_runtime_file_is_tracked() -> None:
    tracked = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True,
                             check=True).stdout.splitlines()
    forbidden = re.compile(r"(^|/)(\.env(\..*)?|service\.env|config\.toml|state\.json)$")
    allowed = {".env.example"}
    assert [f for f in tracked if forbidden.search(f) and f not in allowed] == []


def test_precommit_blocks_config_and_runtime_files() -> None:
    text = (ROOT / ".pre-commit-config.yaml").read_text()
    rx = re.compile(re.findall(r"files: '(.+)'", text)[0])
    for name in (".env", "config.toml", "data/live/state.json", "service.env", "x/trades.csv"):
        assert rx.search(name), name
    assert not rx.search("config.example.toml") and not rx.search("src/copybot/config.py")


def test_ci_runs_the_full_check_with_pinned_dependencies() -> None:
    wf = (ROOT / ".github" / "workflows" / "check.yml").read_text()
    assert "make venv install" in wf and "make check" in wf
    assert 'python-version: "3.12"' in wf and "contents: read" in wf
