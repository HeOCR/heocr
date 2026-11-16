# Planning

`planning/` is the source of truth for `heocr` milestones and task notation. Merged `main` and merged PR history decide what is done; update these files when they disagree.

Project-wide decisions live in heocr-engine's [`planning/decisions/`](https://github.com/HeOCR/heocr-engine/tree/main/planning/decisions). The ones that set this package's course:

- **0019:** HeOCR's top goal is a document pipeline, not a model.
- **0020:** the package plan: name, licences, external tools, Adanim code, team and the VLM route.
- **The design** is the proposal [`document-pipeline-package.md`](https://github.com/HeOCR/heocr-engine/blob/main/planning/proposals/document-pipeline-package.md); decision 0020 wins where they differ.

## Notation

Planned work uses `D<milestone><slice>`, for example `D1a`. `D` is the document-pipeline notation defined in the proposal's §7; the letter keeps it distinct from heocr-engine's `R` and hocrgen's `A`–`F`. A slice can be split further with a digit, for example `D1d1`.

- **Milestone files:** `milestones/d<N>-<topic>.md`, one per milestone, listing its slices and their status.
- **Decisions** specific to this package are recorded in heocr-engine's `planning/decisions/`, which keeps one numbering for the whole project.
- **GitHub:** each milestone has a GitHub milestone named `D<N> - <title>` and a `roadmap:D<N>` label.

## Milestones

| Milestone | Content | Plan |
|---|---|---|
| **D1** | Repository, contract and CLI; file-type identification; DOCX with numbering and headings; T1 PDF pages; page classes T1–T6 measured and flagged | [d1-foundations.md](milestones/d1-foundations.md) (complete) |
| **D2** | All PDF page classes; repairs; the OCR route (stock Tesseract and HeOCR recognizers); coverage accounting; the VLM route, off by default | [d2-routes.md](milestones/d2-routes.md) |
| **D3** | Tables (PDF and office); number cross-checks | not yet planned |
| **D4** | DOC, RTF and ODT through LibreOffice; images; spreads and columns; PPTX and XLSX | not yet planned |
| **D5** | First PyPI release, after the D-bench gate | not yet planned |
