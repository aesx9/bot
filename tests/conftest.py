from __future__ import annotations

from collections.abc import Iterator

import pytest

from copybot.redaction import clear_registered_secrets

LEADER = "0x" + "ab" * 20


@pytest.fixture(autouse=True)
def _clean_secrets() -> Iterator[None]:
    clear_registered_secrets()
    yield
    clear_registered_secrets()


# Perfil exhaustivo: HYPOTHESIS_PROFILE=thorough make test
import os  # noqa: E402

from hypothesis import settings  # noqa: E402

settings.register_profile("thorough", max_examples=2000, deadline=None)
settings.register_profile("default", deadline=None)
settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "default"))
