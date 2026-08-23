# datatrawl --- reproducible dev / test environment
#
#   make venv        create a local virtualenv in ./.venv
#   make install     editable install with test deps (into the active env)
#   make test        run the full offline test suite (no CANFAR needed)
#   make lint        check for Python static errors
#   make coverage    run the test suite and enforce the coverage baseline
#   make smoke       quick CLI checks (list / doctor)
#   make docs        build the LaTeX data sheet + user guide into docs/out/
#   make diagram     regenerate the assets/*.svg graphics from TikZ sources
#   make slides      build the tutorial slide deck into docs/presentation/out/
#   make clean       remove only reproducible build artifacts and caches
#   make clean-runs  explicitly remove local run outputs (results/data/logs)
#
# Typical first time:   make venv && . .venv/bin/activate && make install && make test

PY ?= python3
VENV ?= .venv

.PHONY: venv install test lint coverage smoke clean clean-runs docs diagram slides

venv:
	$(PY) -m venv $(VENV)
	@echo "created $(VENV) -- activate with:  . $(VENV)/bin/activate"

install:
	pip install -e ".[dev]"

# All tests are offline: the synthetic pipeline, the per-freq_id fan-out, and the
# CADC archive path (real source code, network faked). No cert or CANFAR needed.
test:
	pytest -q

lint:
	$(PY) -m ruff check src tests examples

coverage:
	$(PY) -m pytest -q --cov=datatrawl --cov-report=term-missing --cov-fail-under=80

smoke:
	datatrawl list
	datatrawl doctor

docs:
	@command -v latexmk >/dev/null || { \
	    echo "latexmk not found -- see 'Build documentation' in README.md"; exit 1; }
	rm -rf docs/out
	mkdir -p docs/out
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

# The tutorial deck (28 slides + backups, presenter notes on a second screen).
# Needs LuaLaTeX: the Amurmaple theme's delaunay title decoration runs MetaPost
# through luamesh. See docs/presentation/README.md for the package list.
slides:
	@command -v lualatex >/dev/null || { \
	    echo "lualatex not found -- see docs/presentation/README.md"; exit 1; }
	cd docs/presentation && \
	    latexmk -lualatex -interaction=nonstopmode -halt-on-error \
	        -outdir=out datatrawl_tutorial.tex && \
	    latexmk -lualatex -interaction=nonstopmode -halt-on-error \
	        -outdir=out -jobname=datatrawl_tutorial_slides \
	        -usepretex='\def\slidesonly{1}' datatrawl_tutorial.tex
	@echo "PDFs in docs/presentation/out/ (with notes + slides-only)"

clean:
	rm -rf build dist *.egg-info src/*.egg-info .pytest_cache .ruff_cache .coverage .coverage.* htmlcov docs/out assets/out docs/presentation/out
	find . -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true

# Run products, downloaded inventories, and logs can represent days of work.
# Keep their removal separate from the conventional, safe `make clean` target.
clean-runs:
	@echo "removing local run outputs: results/ data/ logs/"
	rm -rf results data logs
