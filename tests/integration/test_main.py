"""CLI sin red: comandos de estado, parada y perfil de arranque."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

import copybot.checks as checks_mod
import copybot.main as main_mod
from copybot.checks import CheckReport, config_hash, key_fingerprint
from copybot.main import (
    EXIT_ERROR,
    EXIT_OK,
    EXIT_USAGE,
    LIVE_PHRASE,
    RELEASE_PHRASE,
    RESET_PHRASE,
    main,
)
from copybot.state import BotState, InstanceLock, StateStore
from tests.conftest import LEADER
from tests.fake_kraken import SECRET


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


@pytest.mark.parametrize("args", [["--live"], ["--check"], ["--live", "--once"]])
def test_live_and_check_need_live_config(tmp_path: Path, args: list[str]) -> None:
    assert main(["--config", str(write_config(tmp_path)), *args]) == EXIT_USAGE


def test_live_config_without_live_flag_does_not_run(tmp_path: Path) -> None:
    cfg = write_config(tmp_path, 'mode = "live"')
    assert main(["--config", str(cfg), "--once"]) == EXIT_USAGE
    assert main(["--config", str(cfg)]) == EXIT_USAGE


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


# --- live ---


def write_env(tmp: Path, mode: int = 0o600) -> Path:
    env = tmp / ".env"
    env.write_text(f"KRAKEN_FUTURES_API_KEY=clave-publica-prueba\n"  # pragma: allowlist secret
                   f"KRAKEN_FUTURES_API_SECRET={SECRET}\n")
    env.chmod(mode)
    return env


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Sustituye la ejecución real (red) por stubs que registran la llamada."""
    rec: dict[str, Any] = {}

    async def fake_run_bot(cfg, state, store, once, env, creds=None):  # type: ignore[no-untyped-def]
        rec["run_bot"] = {"live": creds is not None, "profile": state.live_startup_profile}
        return EXIT_OK

    async def fake_check(cfg, state, creds, prompt):  # type: ignore[no-untyped-def]
        report = CheckReport()
        report.add("simulado", rec.get("check_ok", True), "")
        if report.passed:
            state.live_check = {"config_hash": config_hash(cfg),
                                "key_fingerprint": key_fingerprint(creds)}
        return report

    monkeypatch.setattr(main_mod, "run_bot", fake_run_bot)
    monkeypatch.setattr(main_mod, "run_check_command", fake_check)
    return rec


def live_args(tmp: Path, *extra: str) -> list[str]:
    return ["--config", str(write_config(tmp, 'mode = "live"')),
            "--env", str(tmp / ".env"), *extra]


def test_live_requires_a_passed_check(tmp_path: Path, calls: dict[str, Any]) -> None:
    write_env(tmp_path)
    assert main(live_args(tmp_path, "--live"), prompt=answer(LIVE_PHRASE)) == EXIT_USAGE
    assert "run_bot" not in calls


def test_failed_check_does_not_enable_live(tmp_path: Path, calls: dict[str, Any]) -> None:
    write_env(tmp_path)
    calls["check_ok"] = False
    assert main(live_args(tmp_path, "--check")) == EXIT_ERROR
    assert main(live_args(tmp_path, "--live"), prompt=answer(LIVE_PHRASE)) == EXIT_USAGE


def test_live_requires_written_confirmation_and_activates_startup_profile(
    tmp_path: Path, calls: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    write_env(tmp_path)
    assert main(live_args(tmp_path, "--check")) == EXIT_OK
    assert main(live_args(tmp_path, "--live"), prompt=answer("si")) == EXIT_USAGE
    assert "run_bot" not in calls
    assert store(tmp_path).load().live_startup_profile is None  # no llegó a arrancar
    out = capsys.readouterr().out
    assert "DINERO REAL" in out and "ACTIVO (1x, 100 USD/activo)" in out
    assert main(live_args(tmp_path, "--live"), prompt=answer(LIVE_PHRASE)) == EXIT_OK
    assert calls["run_bot"] == {"live": True, "profile": True}
    assert store(tmp_path).load().live_startup_profile is True


def test_config_change_after_check_blocks_live(tmp_path: Path, calls: dict[str, Any]) -> None:
    write_env(tmp_path)
    assert main(live_args(tmp_path, "--check")) == EXIT_OK
    cfg = write_config(tmp_path, 'mode = "live"\n[sizing]\nmultiplier = 2')
    assert main(["--config", str(cfg), "--env", str(tmp_path / ".env"), "--live"],
                prompt=answer(LIVE_PHRASE)) == EXIT_USAGE
    assert "run_bot" not in calls


def test_env_with_open_permissions_is_rejected(tmp_path: Path, calls: dict[str, Any]) -> None:
    write_env(tmp_path, 0o644)
    assert main(live_args(tmp_path, "--check")) == EXIT_ERROR


def test_paper_never_loads_keys(tmp_path: Path, calls: dict[str, Any]) -> None:
    write_env(tmp_path, 0o644)  # ni siquiera se mira
    assert main(["--config", str(write_config(tmp_path)), "--once"]) == EXIT_OK
    assert calls["run_bot"]["live"] is False


def _confirmed_live(tmp_path: Path, calls: dict[str, Any]) -> None:
    write_env(tmp_path)
    assert main(live_args(tmp_path, "--check")) == EXIT_OK
    assert main(live_args(tmp_path, "--live"), prompt=answer(LIVE_PHRASE)) == EXIT_OK
    calls.pop("run_bot")


def test_confirmation_persists_for_unattended_restarts(tmp_path: Path,
                                                       calls: dict[str, Any]) -> None:
    _confirmed_live(tmp_path, calls)

    def no_stdin(_: str) -> str:  # como systemd: sin terminal
        raise EOFError

    assert main(live_args(tmp_path, "--live"), prompt=no_stdin) == EXIT_OK
    assert calls["run_bot"]["live"] is True


def test_confirmation_is_invalidated_by_reset_halt(tmp_path: Path, calls: dict[str, Any]) -> None:
    _confirmed_live(tmp_path, calls)
    st = store(tmp_path).load()
    st.halted, st.halt_reason = True, "prueba"
    store(tmp_path).save(st)
    assert main(live_args(tmp_path, "--reset-halt"), prompt=answer(RESET_PHRASE)) == EXIT_OK
    assert main(live_args(tmp_path, "--live"), prompt=answer("")) == EXIT_USAGE
    assert "run_bot" not in calls


def test_any_halt_invalidates_confirmation() -> None:
    from copybot.risk import halt

    st = BotState(live_confirmation={"config_hash": "x"})
    halt(st, "drawdown")
    assert st.live_confirmation is None


def test_code_change_invalidates_confirmation(tmp_path: Path, calls: dict[str, Any],
                                              monkeypatch: pytest.MonkeyPatch) -> None:
    _confirmed_live(tmp_path, calls)
    monkeypatch.setattr(checks_mod, "code_fingerprint", lambda: "otro-codigo")
    assert main(live_args(tmp_path, "--live"), prompt=answer("")) == EXIT_USAGE
    assert main(live_args(tmp_path, "--live"), prompt=answer(LIVE_PHRASE)) == EXIT_OK


def test_key_change_invalidates_confirmation(tmp_path: Path, calls: dict[str, Any]) -> None:
    from pydantic import SecretStr

    from copybot.checks import live_confirmation_valid
    from copybot.config import Config
    from copybot.credentials import KrakenCredentials

    cfg = Config.model_validate({"leader_address": LEADER, "mode": "live"})
    creds = KrakenCredentials(api_key=SecretStr("a"), api_secret=SecretStr(SECRET))
    st = BotState()
    st.live_confirmation = checks_mod.confirmation_record(cfg, creds,
                                                          __import__("datetime").datetime.now())
    assert live_confirmation_valid(st, cfg, creds) is None
    other = KrakenCredentials(api_key=SecretStr("b"), api_secret=SecretStr(SECRET))
    assert "clave" in (live_confirmation_valid(st, cfg, other) or "")
    other_cfg = cfg.model_copy(update={"leader_address": "0x" + "cd" * 20})
    assert "configuración" in (live_confirmation_valid(st, other_cfg, creds) or "")
