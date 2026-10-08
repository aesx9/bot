"""CLI sin red: comandos de estado, parada y perfil de arranque."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from pathlib import Path

import pytest

from copybot.main import EXIT_ERROR, EXIT_OK, EXIT_USAGE, RELEASE_PHRASE, RESET_PHRASE, main
from copybot.state import BotState, InstanceLock, StateStore
from tests.conftest import LEADER


@pytest.fixture(autouse=True)
def _restore_logging() -> Iterator[None]:
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    yield
    for h in list(root.handlers):
        root.removeHandler(h)
        h.close()
    for h in handlers:
        root.addHandler(h)
    root.setLevel(level)


def write_config(tmp: Path, extra: str = "") -> Path:
    cfg = tmp / "config.toml"
    cfg.write_text(f'leader_address = "{LEADER}"\n{extra}\n[paths]\ndata_dir = "{tmp / "data"}"\n')
    return cfg


def store(tmp: Path) -> StateStore:
    return StateStore(tmp / "data" / "state.json")


def answer(text: str):  # type: ignore[no-untyped-def]
    return lambda _prompt: text


@pytest.mark.parametrize("args", [["--live"], ["--check"]])
def test_live_and_check_are_not_available_yet(tmp_path: Path, args: list[str]) -> None:
    assert main(["--config", str(write_config(tmp_path)), *args]) == EXIT_USAGE


def test_live_mode_in_config_is_refused(tmp_path: Path) -> None:
    cfg = write_config(tmp_path, 'mode = "live"')
    assert main(["--config", str(cfg), "--once"]) == EXIT_USAGE


def test_bad_config_does_not_start(tmp_path: Path) -> None:
    cfg = tmp_path / "c.toml"
    cfg.write_text('leader_address = "0x123"\n')
    assert main(["--config", str(cfg), "--status"]) == EXIT_USAGE


def test_status_works_without_env_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert not (tmp_path / ".env").exists()
    assert main(["--config", str(write_config(tmp_path)), "--status"]) == EXIT_OK
    out = json.loads(capsys.readouterr().out)
    assert out["modo"] == "paper" and out["detenido"] is False


def test_reset_halt_requires_written_confirmation(tmp_path: Path) -> None:
    cfg = write_config(tmp_path)
    store(tmp_path).save(BotState(halted=True, halt_reason="drawdown"))
    assert main(["--config", str(cfg), "--reset-halt"], prompt=answer("si")) == EXIT_USAGE
    assert store(tmp_path).load().halted
    assert main(["--config", str(cfg), "--reset-halt"], prompt=answer(RESET_PHRASE)) == EXIT_OK
    assert not store(tmp_path).load().halted


def test_release_startup_profile_requires_confirmation(tmp_path: Path) -> None:
    cfg = write_config(tmp_path)
    store(tmp_path).save(BotState(live_startup_profile=True))
    assert main(["--config", str(cfg), "--release-startup-profile"],
                prompt=answer("vale")) == EXIT_USAGE
    assert store(tmp_path).load().live_startup_profile is True
    assert main(["--config", str(cfg), "--release-startup-profile"],
                prompt=answer(RELEASE_PHRASE)) == EXIT_OK
    assert store(tmp_path).load().live_startup_profile is False


def test_second_instance_is_refused(tmp_path: Path) -> None:
    cfg = write_config(tmp_path)
    with InstanceLock(tmp_path / "data" / "copybot.lock"):
        assert main(["--config", str(cfg), "--status"]) == EXIT_ERROR


def test_corrupt_state_does_not_start(tmp_path: Path) -> None:
    cfg = write_config(tmp_path)
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "state.json").write_text("{roto")
    assert main(["--config", str(cfg), "--status"]) == EXIT_ERROR
