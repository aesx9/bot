PY ?= .venv/bin/python
UV ?= uv

.PHONY: venv lock install check test lint audit secrets

venv:
	python3.12 -m venv .venv

lock:
	$(UV) pip compile --python-version 3.12 --generate-hashes requirements.in -o requirements.lock
	$(UV) pip compile --python-version 3.12 --generate-hashes requirements-dev.in -o requirements-dev.lock

install:
	$(PY) -m pip install --require-hashes --no-deps -r requirements.lock -r requirements-dev.lock
	$(PY) -m pip install --no-deps --no-build-isolation -e .

lint:
	$(PY) -m ruff check src tests scripts
	$(PY) -m mypy src

test:
	$(PY) -m pytest

audit:
	$(PY) -m pip_audit --require-hashes --disable-pip -r requirements.lock -r requirements-dev.lock

secrets:
	.venv/bin/detect-secrets-hook --baseline .secrets.baseline $$(git ls-files)

# Flujo completo de verificación (obligatorio antes de cada commit de fase)
check: lint test audit secrets
