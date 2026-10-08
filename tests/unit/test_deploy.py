"""Ficheros de despliegue: endurecimiento, coherencia con el código y wrapper."""

from __future__ import annotations

import logging
import logging.handlers
import os
import re
import subprocess
from pathlib import Path

import pytest

from copybot.logging_setup import setup_logging
from copybot.main import EXIT_HALTED, EXIT_USAGE

ROOT = Path(__file__).resolve().parents[2]
DEPLOY = ROOT / "deploy"


def service() -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    text = (DEPLOY / "copybot.service").read_text().replace("\\\n", " ")
    for line in text.splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            out.setdefault(k.strip(), []).append(v.strip())
    return out


def test_service_is_hardened_and_unprivileged() -> None:
    s = service()
    assert s["User"] == ["copybot"] and s["StandardInput"] == ["null"]
    for key, value in [("NoNewPrivileges", "yes"), ("ProtectSystem", "strict"),
                       ("PrivateTmp", "yes"), ("ProtectHome", "yes"),
                       ("ReadWritePaths", "/var/lib/copybot"), ("CapabilityBoundingSet", "")]:
        assert s[key] == [value], key
    assert s["Restart"] == ["on-failure"]


def test_service_does_not_restart_after_a_halt_or_missing_confirmation() -> None:
    codes = set(service()["RestartPreventExitStatus"][0].split())
    assert {str(EXIT_USAGE), str(EXIT_HALTED)} <= codes


def test_service_runs_from_src_with_service_paths() -> None:
    s = service()
    assert s["Environment"] == ["PYTHONPATH=/opt/copybot/app/src"]
    assert "--config /var/lib/copybot/config.toml" in s["ExecStart"][0]
    assert "--env /var/lib/copybot/.env" in s["ExecStart"][0]
    assert "--live" not in s["ExecStart"][0]  # live solo vía service.env, a propósito


def test_logrotate_never_rotates_fiscal_csvs() -> None:
    text = (DEPLOY / "logrotate.conf").read_text()
    paths = [line for line in text.splitlines() if line.startswith("/")]
    assert paths == ["/var/lib/copybot/data/logs/*.log {"]
    assert "create 0600 copybot copybot" in text


def test_setup_script_checks_ssh_key_before_touching_sshd() -> None:
    text = (DEPLOY / "setup_vps.sh").read_text()
    subprocess.run(["bash", "-n", str(DEPLOY / "setup_vps.sh")], check=True)
    assert text.index("authorized_keys") < text.index("PasswordAuthentication no")
    assert "sshd -t" in text and "ufw default deny incoming" in text
    assert "--require-hashes" in text
    assert not re.search(r"\d{8,}:[A-Za-z0-9_-]{30,}", text)  # ningún token pegado


@pytest.fixture
def fake_bin(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "systemctl").write_text('#!/bin/sh\n[ "$SERVICE_ACTIVE" = 1 ]\n')
    (bin_dir / "sudo").write_text('#!/bin/sh\necho "SUDO $*"\n')
    for f in bin_dir.iterdir():
        f.chmod(0o755)
    (tmp_path / "data").mkdir()
    return bin_dir


def run_cli(fake_bin: Path, active: bool, *args: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PATH": f"{fake_bin}:{os.environ['PATH']}",
           "SERVICE_ACTIVE": "1" if active else "0",
           "COPYBOT_APP": "/opt/app", "COPYBOT_DATA": str(fake_bin.parent / "data")}
    return subprocess.run(["bash", str(DEPLOY / "copybot-cli"), *args], env=env,
                          capture_output=True, text=True, check=False)


def test_cli_refuses_to_run_alongside_the_service(fake_bin: Path) -> None:
    r = run_cli(fake_bin, True, "--live")
    assert r.returncode == 2 and "systemctl stop copybot" in r.stderr
    assert run_cli(fake_bin, True, "--once").returncode == 2


def test_cli_allows_status_while_running_and_uses_service_user(fake_bin: Path) -> None:
    r = run_cli(fake_bin, True, "--status")
    assert r.returncode == 0 and "SUDO -u copybot -H -- env PYTHONPATH=/opt/app/src" in r.stdout
    r = run_cli(fake_bin, False, "--check")
    assert r.returncode == 0 and r.stdout.rstrip().endswith("--check")


def test_external_log_rotation_reopens_file(tmp_path: Path) -> None:
    root = logging.getLogger()
    saved = list(root.handlers), root.level
    try:
        setup_logging(tmp_path, external_rotation=True)
        assert any(isinstance(h, logging.handlers.WatchedFileHandler) for h in root.handlers)
        setup_logging(tmp_path)
        assert any(isinstance(h, logging.handlers.RotatingFileHandler) for h in root.handlers)
    finally:
        for h in list(root.handlers):
            root.removeHandler(h)
            h.close()
        for h in saved[0]:
            root.addHandler(h)
        root.setLevel(saved[1])
