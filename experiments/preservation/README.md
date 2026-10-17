# Preservation feasibility experiment (not deployed, not remediation)

Phase 2 risk commitment: compare **openpyxl round trips** with **targeted OOXML edits** as
candidate strategies for future corrections (REQ-07/08/09 remain future work). Nothing here is
imported by `app/`, and no production remediation exists.

```sh
uv run python -m experiments.preservation.run_experiment            # temp output dir
uv run python -m experiments.preservation.run_experiment --keep /tmp/preservation-out
```

- The source workbook is generated in memory from explicit seeds (a picture, chart and shape;
  a formula on `Expenses` that references `Sheet1`; the defined name `Totals`; an internal
  hyperlink cell). Its SHA256 is checked before and after the run. The source never changes.
- Output workbooks are **temporary copies**. Only the JSON summary is committed:
  [results/2026-10-14.json](results/2026-10-14.json).
- `snapshot.py` compares sheet names and order, cell kinds/values/formulas/style indices,
  defined names, relationship types, part inventory and DrawingML object metadata. It uses
  the engine's safe readers, so openpyxl does not judge its own output.
- A changed output hash is expected after a deliberate edit. Each case passes only when
  its differences equal its approved list exactly and no references go stale.

## Results from the 2026-10-14 local run (macOS, Python 3.12.3, openpyxl 3.1.5, Pillow absent)

| Edit | openpyxl round trip | Targeted OOXML edit |
|---|---|---|
| Baseline, no edit | **Unsafe**: picture and shape dropped, chart re-created with id 3 → 1, cached formula values dropped, 4 parts added, image part removed | Safe (byte-identical output) |
| Alt-text insertion (`cNvPr id=2`) | Not possible: no API for cNvPr title/descr | Safe: only `title`/`descr` changed |
| Rename `Sheet1` → `Revenue`, name only | Unsafe: same losses, plus a stale formula and a stale defined name | **Unsafe**: stale `Expenses!A1` formula and stale `Totals` defined name |
| Rename plus formula/defined-name references | (not attempted) | Safe for the references this fixture contains |
| Hyperlink text A4 | Unsafe (same baseline losses) | Safe |
| Header-row text (add C1) | Unsafe (same baseline losses) | Safe |

Conclusions so far:

1. openpyxl round trips are not preservation-safe for workbooks with drawings. Even with no
   edit they lost objects and cached values. Pillow was absent, which explains the dropped
   picture; shapes and DrawingML metadata are lost regardless.
2. Targeted part-level edits preserved everything this comparator measures.
3. Renaming needs reference rewriting. A name-only XML edit leaves broken references. The
   reference rewrite here is a naive token replace and has not been proven general: quoted
   names, 3-D references, chart series formulas, external links, pivot caches, data validation
   and conditional formatting are not yet covered.

## Pending, and not claimed

- **Opening each output in Excel without a repair warning: PENDING** (Jackson/Dylan,
  manual). No approach may be called safe until this is done.
- Not compared: hyperlink relationships and locations, style table contents, conditional
  formatting, data validation, comments, pivot tables and the chart XML body.
- Inputs: synthetic only. The five de-identified real-world workbooks are still pending.
- Unsafe features to send to manual review in future remediation: VML/legacy objects, macro
  content (already rejected at intake), external links, SmartArt internals, and charts whose
  series formulas reference renamed sheets.
