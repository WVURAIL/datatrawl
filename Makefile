# datatrawl --- reproducible dev / test environment
#
#   make venv        create a local virtualenv in ./.venv
#   make install     editable install with test deps (into the active env)
#   make test        run the full offline test suite (no CANFAR needed)
#   make smoke       quick CLI checks (list / doctor)
#   make docs        build the LaTeX data sheet + user guide into docs/out/
#   make diagram     regenerate the assets/*.svg graphics from TikZ sources
#   make clean       remove build artifacts, caches, and run outputs
#
# Typical first time:   make venv && . .venv/bin/activate && make install && make test

PY ?= python3
VENV ?= .venv

.PHONY: venv install test smoke clean docs diagram

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

docs:
	@command -v latexmk >/dev/null || { \
	    echo "latexmk not found -- see 'Build documentation' in README.md"; exit 1; }
	cd docs && for t in *.tex; do \
	    latexmk -pdf -interaction=nonstopmode -halt-on-error -outdir=out $$t || exit 1; \
	done
	@echo "PDFs in docs/out/"

# The README graphics are TikZ-sourced (assets/*.tex); the committed .svg
# files are generated from them. scour (pip) is optional and shrinks the
# output ~30%.
diagram:
	@command -v latexmk >/dev/null && command -v pdftocairo >/dev/null || { \
	    echo "needs latexmk and pdftocairo (apt: poppler-utils);" \
	         "CANFAR images ship no TeX -- build locally"; exit 1; }
	cd assets && for t in *.tex; do \
	    latexmk -pdf -interaction=nonstopmode -halt-on-error \
	        -outdir=out $$t || exit 1; \
	    base=$$(basename $$t .tex); \
	    pdftocairo -svg out/$$base.pdf $$base.svg; \
	    python3 -m scour.scour -q --enable-comment-stripping --shorten-ids \
	        $$base.svg $$base.svg.opt 2>/dev/null \
	        && mv $$base.svg.opt $$base.svg \
	        || rm -f $$base.svg.opt; \
	    echo "regenerated assets/$$base.svg"; \
	done

clean:
	rm -rf build dist *.egg-info src/*.egg-info .pytest_cache results data logs
	find . -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true
