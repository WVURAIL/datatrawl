# Datatrawl tutorial slides

A 28-slide tutorial (plus four backup slides) covering why the infrastructure
is necessary and how a run works: motivation, design, the anatomy of a run,
a real first run, and recovery. Full presenter notes are on every slide.

## Build

```bash
make slides          # from the repository root
```

This produces two PDFs in `docs/presentation/out/`:

- `datatrawl_tutorial.pdf` — double-wide pages with presenter notes on the
  right half, for presenting with a second screen (`pdfpc`, Skim, etc.).
- `datatrawl_tutorial_slides.pdf` — the clean 16:9 export for projectors
  and sharing.

The deck needs **LuaLaTeX**: the Amurmaple theme's `delaunay` title decoration
drives MetaPost through `luamesh`. On Debian/Ubuntu, this package set builds it
(the first three are the same set used for `make docs`):

```bash
sudo apt-get install --no-install-recommends \
    texlive-latex-base texlive-latex-recommended texlive-latex-extra \
    texlive-fonts-recommended texlive-pictures lmodern latexmk \
    texlive-luatex texlive-metapost
```

`texlive-latex-extra` provides the Amurmaple beamer theme; `texlive-pictures`
provides `luamesh`; `texlive-metapost` provides the MetaPost base files that
luamesh runs.

## Structure

- `datatrawl_tutorial.tex` — the deck. Frame titles carry the primary claims;
  `\note{...}` blocks carry the spoken narration.
- `beamercolorthemewvu.sty` — the WVU brand color theme for Amurmaple.
- `imgs/` — WVU and GWAC marks used on the title page and slide corners.
- Shared figures live in `assets/` at the repository root:
  `assets/datatrawl-figures.sty` is the palette and TikZ vocabulary, and
  `assets/fig-*.tikz` are figure bodies that both this deck and the README's
  SVG assets (`make diagram`) render. Edit a figure once; rebuild both.

## Conventions

- The Amurmaple `sidebar` option is deliberately not used: it narrows the text
  area to ~12.9 cm and every figure is laid out for the full ~14.4 cm width.
- Command-line options render as two literal hyphens everywhere (a microtype
  `\DisableLigatures` rule covers typewriter fonts); keep flags inside
  `\texttt{...}` so the rule applies.
- Content was verified against `WVURAIL/datatrawl @ 6d7a239` (v1.0.0); the
  `\source{...}` lines on each slide pin the specific claims. Re-check them
  when behavior changes on master.
