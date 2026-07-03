# PDF to Markdown Conversion - Known Limitations

Derived from evaluating a real scanned charity financial report (44-page scanned PDF, HTML-table Markdown export). The findings below describe converter behaviour observed on that fixture and the pipeline risks they imply.

## Summary

Numeric fidelity is high. Every financial table that survived conversion reproduces its source figures and cross-foots within tolerance. The material risk is not cell-level corruption but silent structural omission: an entire table can be dropped while everything that remains still reconciles. Arithmetic checks alone will not catch this, so a separate completeness pass is required.

## What the converter did well

All present primary tables cross-foot at the component level, not just on totals. Nil cells (dashes and blanks) are preserved rather than filled. Source quirks are reproduced unchanged rather than "corrected", including a line item disagreeing with its own stated total in the prior year, a figure reading differently by a rounding increment across two summary tables, and mixed United States and Australian spelling in the notes. Reproducing these unchanged is the correct behaviour for a fidelity converter.

## Limitations observed

### 1. Whole-table omission at footnote-interrupted seams

On an accumulated-depreciation note page, two stacked tables (current year and prior year) are separated by a single-line footnote. The converter emitted the current-year table, then failed to re-acquire the region after the footnote. The footnote and the entire prior-year comparative table were both dropped. The equivalent pages with two adjacent tables and no intervening text (property cost, intangibles) retained both tables, merged into one HTML table. The trigger is the interrupting footnote at the table seam.

### 2. Rotated pages without a rotation flag

The landscape financial matrices are printed with glyphs drawn at 90 degrees while the page `/Rotate` flag reads 0. Layout-based table detection operates on the mis-oriented coordinates, which stresses block segmentation on exactly the pages that carry the densest tables.

### 3. Grey-shaded comparative blocks

Prior-year comparative tables sit on a solid grey fill. Shading can be misread as an image region rather than a text table. It did not cause loss on its own (a shaded table survived on the cost page), but it is an aggravating factor when combined with rotation and an interrupting footnote.

### 4. Silent failure mode

The dropped table is a prior-year comparative whose closing balance equals the opening balance of the current-year table that was kept. The surviving table therefore still ties out on its own. No total moves, no cell is corrupted, no column fails to foot. A component re-foot cannot detect a missing table, because the loss is structural, not arithmetic.

### 5. Table merging behaviour

Where two matrices are physically adjacent, the converter merges them into a single HTML `<table>` joined by an empty spacer row (`<tr><td colspan="n"></td></tr>`). This is not an error, but downstream tooling must expect that one HTML table may contain two logical tables and split on the spacer row.

### 6. Output format

Tables are emitted as embedded HTML `<table>` blocks, not Markdown pipe tables. Any downstream step must parse HTML rather than assuming pipe-delimited rows.

## What arithmetic checks will and will not catch

Cross-footing and a nil-cell audit reliably catch fabricated values in nil cells, out-of-tolerance components, garbled cells and shifted references. They do not catch a whole-table omission, a dropped footnote, or any content removed cleanly at a block boundary. The numeric harness is necessary but not sufficient.

## Recommended mitigations

1. Add a structural completeness gate. For every note that carries paired current-year and prior-year matrices, confirm both matrices are present.
2. Flag any landscape or rotated page where a footnote falls between two stacked tables, and review those pages by hand.
3. Run an independent tie-out that reconciles each prior-year closing balance to the corresponding current-year opening balance, from the source rather than the export, so a missing comparative is exposed.
4. Parse HTML tables downstream and split merged tables on the spacer row.
5. Keep the arithmetic reconciliation as the numeric gate and pair it with the completeness gate above. Neither substitutes for the other.

## Scope note

These observations come from a single fixture (a scanned charity financial report with rotated landscape schedules). They should be generalised with caution and re-tested against reports from other converters and other layouts before being treated as pipeline-wide behaviour.
