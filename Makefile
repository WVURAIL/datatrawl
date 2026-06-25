# datatrawl --- reproducible dev / test environment
#
#   make venv        create a local virtualenv in ./.venv
#   make install     editable install with test deps (into the active env)
#   make test        run the full offline test suite (no CANFAR needed)
#   make smoke       quick CLI checks (list / doctor)
#   make clean       remove build artifacts, caches, and run outputs
#
# Typical first time:   make venv && . .venv/bin/activate && make install && make test

PY ?= python3
VENV ?= .venv

.PHONY: venv install test smoke clean

venv:
	$(PY) -m venv $(VENV)
	@echo "created $(VENV) -- activate with:  . $(VENV)/bin/activate"

install:
	pip install -e ".[dev]"

# All tests are offline: the synthetic pipeline, the per-freq_id fan-out, and the
# CADC archive path (real source code, network faked). No cert or CANFAR needed.
test:
	pytest -q

smoke:
	datatrawl list
	datatrawl doctor

clean:
	rm -rf build dist *.egg-info src/*.egg-info .pytest_cache results data logs
	find . -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true
