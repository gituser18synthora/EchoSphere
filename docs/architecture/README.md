# System Architecture document — sources & build

This folder holds the editable sources of `SYSTEM_ARCHITECTURE.pdf` / `SYSTEM_ARCHITECTURE.docx`
(both generated to the repo root).

- `SYSTEM_ARCHITECTURE.html` — the master document (single source of truth for both outputs).
- `diagrams/*.svg` — the eleven diagram sources (hand-authored, shared visual language).
  `diagrams/*.png` are 2× rasters generated from the SVGs for DOCX embedding.
- `build/doc.css` — print stylesheet (A4, running headers, page counters, TOC leaders).
- `build/render_pdf.mjs` — HTML → paginated PDF via paged.js + Playwright Chromium.
  Auto-builds the Table of Contents (with real page numbers) from `main h1/h2[id]`.
- `build/build_docx.py` — HTML → DOCX via python-docx (cover, native TOC field,
  headers/footers with page fields, styles, tables, embedded PNGs).

## Rebuilding

```bash
# one-time, in any scratch dir (NOT the repo): needs Chromium for Playwright
npm i playwright@1.49.1 pagedjs && npx playwright install chromium

# rasterize diagrams changed since last build (writes .png next to each .svg)
node <scratch>/svg2png.mjs docs/architecture/diagrams/*.svg   # or any SVG→PNG tool @2x

# PDF (run from the dir where playwright+pagedjs are installed)
node docs/architecture/build/render_pdf.mjs \
     docs/architecture/SYSTEM_ARCHITECTURE.html SYSTEM_ARCHITECTURE.pdf \
     <scratch>/node_modules/pagedjs

# DOCX (python-docx is already in the project venv)
env/bin/python docs/architecture/build/build_docx.py \
     docs/architecture/SYSTEM_ARCHITECTURE.html SYSTEM_ARCHITECTURE.docx
```

Conventions the converters rely on: section headings are `h1/h2/h3` with `id`s and literal
numbering; figures are `<figure><img src="diagrams/x.svg"><figcaption>…` (DOCX uses the `.png`
sibling); verification labels are `<span class="tag v|i|u">`; callouts are `<aside>`.

Document produced 2026-08-25 from full-repository analysis at commit `ea4750c24`
(branch `feature/voice-runtime-kmrag-integration`).
