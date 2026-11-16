# D4 — Every input format: Word's older formats, images, spreads, PPTX, XLSX and HTML

- Status: planned
- Owner: `pipeline[claude/claude-code#shay-macbook-m4]@heocr/opensource`; the lead reviews and merges (heocr-engine decision 0001)
- Serves: heocr-engine decisions 0019 and 0020 (one package for "Hebrew text from any document"); proposal `document-pipeline-package.md` §3.1 (formats), §3.7 (external tools)
- Builds on: D1 (identification, the DOCX reader), D2 (the OCR route), D3 (tables, furniture, numbers)
- GitHub: milestone `D4 - Formats`, label `roadmap:D4`

## Why

`identify()` already recognises DOC, RTF, ODT/ODS/ODP, PPTX, XLSX, HTML and every common image format, but heocr reads only PDF and DOCX. Everything else ends as `format_not_supported`.

| Input | In the private corpus | What we know |
|---|---|---|
| DOC | 3 files (1 dev, 2 test) | Eval found two things with LibreOffice's **text** export: it **hangs** on a document whose body ends with a table, and it **drops footnotes**. |
| HTML | 1 file: the tender law from Nevo, in the volunteer's private copy, outside eval's split | The volunteer found that both tools put each **section number on its own line**, and turn the **margin headings** (in legislation, the section's name) into empty `######` headings. A gov.il page saved as HTML is an **empty skeleton**, because the content is filled in by JavaScript; that is why the volunteer prints gov.il pages to PDF. |
| PDF spreads | 79 of 81 pages of one dev report are 1191×842 pt, two A4 pages side by side; dev flags 79 pages `possible_spread` | Every tool the volunteer compared reads the **left** half first, so an RTL document's pages come out in the wrong order. |
| gov.il pages printed to PDF | 2 decisions (1 dev, 1 test) | Since D3a, the browser's running header and footer are roles. The **site menu** on the first page (a row of short links) stays in the body, because it does not repeat. |
| Images, TIFF, PPTX, XLSX, RTF, ODT | none | They come from synthetic fixtures and D-bench. |

## Rules that bind every slice

The D2 and D3 rules hold:
- licences;
- tuning on D-bench synthetic data and the dev split only;
- synthetic fixtures only (the Nevo law is a commercial text, and stays out of the repository);
- aggregate figures only in PRs;
- every character is either read or recorded in `unread`.

Four more apply here.

- **External programs are optional and never bundled.** LibreOffice (MPL-2.0) runs as a separate program found on the machine, like Tesseract. When it is missing, the file goes to `unread` with the reason (`converter_unavailable`), and the CLI says how to install it. No Python dependency is added for a format that the standard library, `defusedxml` and Pillow can read.
- **Every subprocess call is bounded.**
  - **A timeout:** every call to LibreOffice or Tesseract has one. On expiry, the whole process group is killed, and the file or page goes to `unread` (`conversion_timeout`, `ocr_timeout`).
  - **An isolated profile:** LibreOffice runs with its own throwaway profile (`-env:UserInstallation=file:///<temp>`) in a temporary folder that is removed afterwards. It never touches the user's profile, and one stuck run cannot block the next.
  - **Recorded:** the version and the executable's sha256 go into `components`, as Tesseract's do.
- **Convert to a format we read ourselves, never to text.** DOC, RTF and ODT are converted to **DOCX** and read by our own DOCX reader, with its numbering, notes, hidden text and repairs. The text export is not used: it hangs and drops footnotes (eval's findings), and it would bypass everything D1–D3 built. The block's `source` says `converted_docx`, and the document records the conversion.
- **Navigation is a role, not a deletion,** like furniture. Site menus, breadcrumbs and "skip to content" links get the role `navigation`. They stay in the record, out of the body flow.

## Slices

Each slice is its own PR from main. None is stacked.

| Slice | Content | Depends on |
|---|---|---|
| **D4a** | **The converter: DOC, RTF and ODT through LibreOffice.**<br>• `heocr.convert`: find `soffice` (or `HEOCR_SOFFICE`), convert to DOCX in a temporary folder with an isolated profile and a timeout, kill the process group on expiry.<br>• The DOCX reader then reads the result. Footnotes and endnotes must survive: an opt-in real test converts a DOC that has notes.<br>• Formats are still identified by content, never by name, and the evidence is kept.<br>• A password-protected or broken file goes to `unread` with LibreOffice's reason, never as an empty document. | D1 |
| **D4b** | **Images as input files:** PNG, JPEG, TIFF (multi-page, including 1-bit fax compression), GIF, BMP, JPEG 2000 where Pillow reads it.<br>• Each frame is a page of class T5, read by the OCR route with its D3e tables, its ink checks and its pixel cap.<br>• The DPI comes from the file, or is assumed to be 300 and flagged `dpi_assumed`.<br>• EXIF orientation is applied.<br>• An image with no ink is `empty`. | D2e, D3e |
| **D4c** | **Spreads.** A PDF page that is two pages side by side is split into two logical pages, but only with evidence:<br>• the page is about twice as wide as a common page size (A4, Letter), or as the document's other pages;<br>• a vertical gutter in the middle has no text crossing it;<br>• both halves have text.<br>In an RTL document the **right** half comes first. Each logical page keeps its source page and its half (`page`, `half`), so boxes still point into the file. Without the evidence, the page keeps `possible_spread` and is not split. | D3c |
| **D4d** | **XLSX**, read with our own XML reader:<br>• each sheet is a table block;<br>• shared and inline strings, with each formula's cached value (never recalculated);<br>• numbers and dates as their format displays them, with the raw value kept when the format is unknown;<br>• merged cells as spans;<br>• the sheet's right-to-left view as the table's direction;<br>• hidden sheets, rows and columns read and flagged, like DOCX hidden text. | D1 |
| **D4e** | **PPTX**, read with our own XML reader:<br>• slides in order, each slide's text shapes in reading order by position, right to left in an RTL deck;<br>• slide tables as table blocks;<br>• speaker notes with the role `notes`;<br>• pictures read by OCR as regions, as DOCX images are (D2e2a);<br>• the slide number on each block. | D1, D2e2a |
| **D4f** | **HTML**, with the standard library's parser:<br>• **Structure:** headings, paragraphs, lists with their numbering, tables with spans, `<br>`, and `dir` / `lang` for direction.<br>• **Encoding:** from the BOM, the `<meta charset>` or the HTTP-style declaration. `windows-1255` is common on Israeli sites.<br>• **Navigation:** `nav`, `header`, `footer`, `aside`, ARIA `navigation` / `banner` / `contentinfo` and "skip" links get the `navigation` role. Scripts, styles and hidden elements (`hidden`, `aria-hidden`, inline `display:none`) are not body text; hidden ones are flagged as in DOCX.<br>• **A page filled in by script:** a body with almost no text and with scripts is flagged `html_rendered_by_script` and goes to `unread`, never read as empty (the gov.il case).<br>• **Legislation (Nevo):** a section number on its own line joins its section's text. A margin heading becomes the section's heading, not an empty `######`. The rules come from the structure of the private copy, inspected locally; the fixtures imitate its markup without its text. | D1 |
| **D4g** | **Web pages printed to PDF.** A browser print is recognised by its running header and footer (date, title, URL, page "n/m"), which D3a already makes roles. On such a document, the site menu at the top of the first page (a row of short pieces above the content's first heading) gets the `navigation` role. Without that evidence, nothing changes. | D3a |
| **D4h** | **ODS and ODP** through the converter (to XLSX and PPTX), once D4a, D4d and D4e are in. | D4a, D4d, D4e |

**Order:** D4a first, since it brings the converter and its safety rules. Then D4b and D4c, which serve the corpus (scans and the spreads). Then D4f and D4g (HTML and web prints), then D4d and D4e, then D4h.

## Measures

Evaluation measures on D-bench at pinned commits, per format: CER/WER in reading order (`line_fold_v1`), table cells, exact numbers, silent loss. The D3 measures apply to every format.

On the dev split, measured by me (counts only):
- **DOC:** its notes and numbering against the same document as DOCX (LibreOffice's conversion of the dev DOC, compared with our reading);
- **spreads:** pages split, pages left `possible_spread`, and the order of the halves;
- **gov.il prints:** menu pieces moved to `navigation` on the dev decision;
- **time:** every LibreOffice call and its timeout rate.

## Exit criteria

- **Formats:** every format `identify()` names is either read or sent to `unread` with a reason. Nothing ends as `format_not_supported` without its own route.
- **DOC:** a DOC with footnotes, converted and read, keeps its notes and their references. No LibreOffice call can hang the extraction: a test with a converter that never returns ends in `conversion_timeout`.
- **Spreads:** a two-A4 spread in an RTL document reads its right page first. The dev report's pages are split.
- **HTML:** a page filled in by script is never silently empty. Navigation is out of the body flow, and legislation keeps its section numbers and margin headings with their sections.
- **CI** is green on Linux, macOS and Windows. Tests that need LibreOffice or Tesseract stay opt-in.

## Not in D4

- **The VLM route** (D2g) waits for eval's registration of the bake-off.
- **Tables in image regions on text-layer pages,** and the 376 pages still flagged `reading_order_unresolved_columns`, are D3 follow-ups. They are planned after D4, each with its own guard.
- **EPUB, email (EML, MSG) and archives:** identified, not read. Each would come with its own plan.
- **The first PyPI release** is D5, after the D-bench gate.
