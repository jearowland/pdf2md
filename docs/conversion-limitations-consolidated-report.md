# PDF to Markdown Conversion - Limitations Summary

**Source document:** a real consolidated charity financial report (44-page PDF plus 3 auditor pages)
**Evaluation basis:** arithmetic reconciliation of every financial table against the printed PDF, plus a nil-cell audit and a structural check.

---

## Headline

The numeric conversion is clean. Every financial cell reconciles, every primary statement cross-foots, and no nil cell was filled with a fabricated figure. The defect the harness hunts for - a source nil silently replaced by a small plausible number - is absent from this export. The limitations below are non-numeric. They affect text fidelity, structure, and downstream parsing, not the integrity of the figures.

---

## What the converter got right

- All four primary statements cross-foot within tolerance (Profit or Loss, Financial Position, Changes in Equity, Cash Flows), both years.
- Every note table foots, including the movement schedules where a fabricated value hides most easily (PPE and right-of-use asset movements), across every column roll-forward and every row.
- Every cell that is nil in the PDF renders as nil (`-`) in the export. Cross-footing confirms nil is the correct value in each case.
- No stray converter metadata was left in the file.

---

## Limitation 1: entity name is corrupted inconsistently

The PDF prints an unusual organisation name throughout, in both the body text and the auditor pages. The converter renders it two different ways in the same file.

- Prose mentions and a directors'-meeting table header are OCR-corrected to a common real-word twin of the actual name.
- The ABN line and subsidiary names in a later note keep the correct original spelling.

The source is internally consistent, so this inconsistency was introduced by the converter, not preserved from the PDF. The pattern matters: the converter silently "corrects" an unusual proper noun to a common word in running text, but leaves it intact in proper-noun contexts. Any real-word OCR correction of an unusual name should be treated as suspect.

**Impact:** entity-name matching, search, and any automated deduplication keyed on the organisation name will fragment across the two spellings.

## Limitation 2: signature blocks handled two different ways

The document has two auditor signature blocks. One is kept as an image reference. The other is transcribed as an unreadable junk string (a rendering of the handwritten squiggle). Both sit in signature blocks, not in numeric cells, so parsing of the statements is unaffected. The inconsistency is cosmetic.

**Impact:** none on the financials. Flag only if signature-block text is being consumed downstream.

## Limitation 3: tables are HTML, not Markdown pipes

Every table is emitted as an HTML `<table>` block rather than a Markdown pipe table. Downstream tooling must parse HTML, not pipe syntax. Cell content is clean, colspans and rowspans are used correctly (for example a note header spanning two comparative-year columns), and no table collapsed into run-on text.

**Impact:** any pipeline step that assumes pipe tables will read zero tables from this file. Parse the HTML.

## Limitation 4: heading structure is uneven

Two related issues, neither of which loses data.

- Note numbering and sequence are fully intact, but heading levels are inconsistent. Some notes are `##` headings, others are plain text lines.
- Several note headings are concatenated with the following subheading (e.g. a "Commitments and contingencies" heading directly followed by its "Contingencies" subheading with no break).

**Impact:** a table-of-contents generator or a section splitter keyed on heading level will mis-segment. Every value is present, so no reconciliation risk.

---

## Error taxonomy scorecard

| Category | Definition | Count in this export |
|---|---|---|
| A - fabricated value where source is nil | Critical. Silent numeric corruption. | 0 |
| B - OCR junk in a cell | Garbage string in place of a value. | 1 (signature block only, not a data cell) |
| C - structural or reference slip | Heading, name, or reference fidelity. | Entity-name substitution plus heading concatenation |

## Acceptance test

1. Every PDF nil cell renders as nil in the export - **MET**
2. Every source quirk reproduced unchanged - **MET**
3. Every primary table cross-foots within tolerance - **MET**

**Overall result: PASS.** The three failures above are all non-numeric and do not affect the acceptance test.

---

## Recommended pipeline actions

1. Add a post-process step that forces the correct spelling of unusual proper nouns everywhere, and more generally treat any real-word OCR correction of an unusual name as a candidate error to review.
2. Parse tables as HTML, not Markdown pipes.
3. If segmenting by note or heading, normalise heading levels first and split concatenated note titles from their trailing subheadings.
4. Treat signature-block text as unreliable and ignore it for data purposes.
