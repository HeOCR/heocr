# Consuming heocr output: a guide for people and agents

This guide describes `heocr_document.v1` as emitted by the current repository. Install it with `pip install git+https://github.com/HeOCR/heocr`. Check `producer.version` when interpreting a record, because older versions may emit fewer flags. The [record reference](record.md), [schema](../src/heocr/schemas/heocr_document.v1.schema.json), and [v1 compatibility rules](contract-v1.md) are the detailed contract.

## The record and its views

`heocr.extract(path_or_bytes)` returns a `Document`; `doc.to_dict()` and `doc.to_json()` serialize it. `heocr input.pdf -f json` emits the same record. It contains:

- `schema`, `producer` (version and components), `document` (content hash, size, detected format and optional page count), and `options`.
- `pages`: page/frame entries with zero-based `index`, dimensions, PDF `class`, `route`, `signals`, `coverage` and `flags`. Not every format has page entries. `coverage.status` describes reading, not a guarantee of correctness.
- `blocks`: ordered objects with `id`, `kind`, `role`, `source`, `tool`, `repairs`, and `flags`. Text blocks have `text`; `kind: table` has `cells` (rows of objects with `text`, optional spans, flags and alternate `readings`). Position, page, slide, direction, notes, OCR metadata and checks are present where applicable. Page indices are zero-based; slide numbers are one-based. A `bbox` is in points, `[x0, y0, x1, y1]`, from the displayed page's top left.
- `unread`: omissions with `scope`, `reason`, and sometimes `page`, `bbox`, and `detail`; top-level `flags`; optional `font_maps`.

`doc.text` is a plain-text view; `doc.markdown` preserves headings and tables and includes unread comments. Both are projections, so keep the JSON record for provenance and review. Roles distinguish body and text boxes from headers, footers, notes and navigation, which the text view places in separate sections. Records do not contain the input file name or local path: keep your own mapping if needed. Content can itself contain sensitive information; handle the record as you would the input.

### Routes and review

For PDF pages, `route` can show `text_layer`, `ocr`, `text_layer+ocr_regions`, or `none`; inspect the actual value and `coverage` rather than assuming all blocks took the same route. `class` T1–T6 describes the page's detected layer, not a quality score. For each block, `source` is `text_layer`, `ocr`, `office_xml`, `converted_docx`, `converted_xlsx`, `converted_pptx`, `html`, or `none`; `tool` identifies the reader. Converted sources went through LibreOffice. OCR blocks carry `ocr.status: "unverified"` and normally the `ocr_unverified` flag: **OCR may contain errors**, even when `unread` is empty. Inspect `ocr` and `checks` for settings, confidence and second readings; verify consequential text against the original. Repairs are logged per block.

Check **all** of top-level `unread` and `flags`, every page's `coverage` and `flags`, every block's `flags`, and table cells' `flags` and `readings`. A page with `coverage.status` `unread` or `partial` needs attention. Also inspect unread entries at document and block scope: page or time limits can leave pages with no `pages` entry at all, and document-level omissions cannot always be assigned to a page. Compare `document.page_count` with entries when present, but do not treat a missing page count or an empty `unread` as proof of completeness. The CLI exits successfully for partial or unread documents; stderr reports unread entries unless `-q` is used. A successful exit is not a completeness check.

## Flag reference

Flags are open strings, not a closed enum; preserve and surface unknown flags for review. A flag is emitted only when its condition is detected; absence is not proof that the condition did not occur. The tables cover flags emitted by the current implementation, including later additions. Some also propagate to other levels; interpret them wherever they appear. `unread` reasons are also open and are documented in the [record reference](record.md#unread-what-was-not-read-and-why).

### Document flags

| Flag | Meaning and consumer action |
|---|---|
| `not_read` | No usable document reading; inspect `unread` and fix the route or input. |
| `no_text` | Reader found no text; inspect the input before assuming it is blank. |
| `page_limit_reached`, `time_limit_reached` | Processing stopped at a limit; inspect `unread` and rerun with an appropriate limit. |
| `ocr_call_failed` | One or more OCR calls failed or timed out; inspect `unread` and retry. |
| `bundled_model_unavailable`, `routed_model_unavailable` | The requested or auto-selected model was unavailable. OCR falls back to stock Tesseract when installed. Inspect model provenance before comparing results. |
| `numbers_not_checked` | Number checking could not be completed; verify numbers independently. |
| `numbers_read_with_main_model` | The separate number model was unavailable; check numbers independently. |
| `converted_by_libreoffice` | Input was converted before reading; inspect conversion if fidelity matters. |
| `font_cp1255_shifted` | Shifted font encoding detected and repaired; inspect repairs and affected text. |
| `hidden_paragraph_dropped`, `white_paragraph_dropped`, `hidden_text_dropped` | Hidden or white text was excluded; inspect the source if it matters. |
| `hidden_sheets_read`, `hidden_slides_read` | Hidden sheets or slides were included; decide whether to include them downstream. |
| `numbering_reference_unknown` | Word numbering references an unknown definition; check affected labels in the source. |
| `date_format_locale_assumed` | Spreadsheet date formatting used an assumed locale; verify dates. |
| `threaded_comments_read_as_notes` | Spreadsheet threaded comments were read as notes; retain them if comments matter. |
| `deck_rtl`, `deck_ltr` | Presentation direction inferred or specified; use it when interpreting slide order. |
| `image_frames_skipped` | Some image frames were skipped; inspect `unread`. |
| `browser_print` | PDF identified as a browser print; inspect navigation roles. |
| `encoding_assumed`, `encoding_declaration_unknown`, `undecodable_bytes_replaced` | HTML encoding was assumed, unrecognized, or had replacement characters; verify the source encoding. |
| `html_visual_order_encoding` | HTML declared a visual-order encoding; check reading order. |
| `html_truncated` | HTML parsing hit a limit; inspect `unread`. |
| `html_nesting_flattened`, `group_depth_flattened` | Deep HTML or slide groups were flattened; check structure if it matters. |
| `noscript_not_read`, `form_controls_not_read`, `html_images_not_fetched`, `html_rendered_by_script` | HTML content was not read or fetched; inspect `unread` and use a rendered source where needed. |
| `empty_headings_skipped` | Empty headings were omitted; inspect structure if needed. |
| `legislation_layout_table`, `layout_table_read_as_flow` | HTML layout table treated as document flow; check structure against the source. |

### Page flags

| Flag | Meaning and consumer action |
|---|---|
| `possible_spread`, `spread_split` | A side-by-side spread was suspected or split; check the halves and order. |
| `text_may_be_vector_paths`, `text_layer_too_small`, `images_may_hold_text` | Text may be drawn as paths, the layer is too small, or images may contain text; inspect OCR and `unread`. |
| `text_layer_is_ocr`, `foreign_ocr_layer` | The layer appears to be existing OCR, or was rejected as foreign OCR; review route and text. |
| `ocr_layer_numbers_and_codes`, `ocr_layer_kept_by_digits` | Existing OCR layer contains numbers/codes; it may have been kept after a digit check. Verify consequential values. |
| `ocr_layer_unchecked`, `ocr_layer_disagrees`, `ocr_layer_disagrees_kept` | Existing OCR could not be checked, disagreed and was rejected, or disagreed but was kept; review against the image. |
| `broken_font_mapping`, `font_cp1255_shifted` | Font mapping is broken or shifted; inspect repairs, `font_maps` and text. |
| `text_layer_visual_order`, `reversed_after_line_building` | Layer is in visual order or remained reversed after line building; check route and reading order. |
| `ocr_forced`, `text_layer_ignored`, `text_layer_ignored_broken_mapping` | OCR was forced or a layer was ignored, including for broken mapping; review OCR output. |
| `route_unavailable`, `ocr_failed`, `ocr_call_failed` | A required route or OCR call failed; inspect `unread` and retry. |
| `ocr_short_for_ink`, `table_cells_ink_unread`, `coverage_mismatch` | Detected ink or layer characters may be missing from output; review the page and `unread`. |
| `render_dpi_reduced`, `deskewed`, `ocr_rest_blank` | OCR render resolution was reduced, image deskewed, or the non-table remainder found blank; check image and resulting text. |
| `dpi_assumed`, `image_downscaled` | Image DPI was assumed or the image downscaled; check OCR and coordinates. |
| `page_rotated`, `text_sideways_on_screen`, `rotated_text_read`, `rotated_text_not_read`, `text_outside_page` | Rotation or text outside the displayed page affected reading; inspect the original and `unread`. |
| `lone_surrogates_replaced` | Invalid Unicode surrogates were replaced; inspect the source text. |
| `reading_order_invalid_geometry`, `reading_order_unresolved_columns`, `reading_order_row_major_unverified`, `reading_order_column_major_unverified` | Geometry or column order is uncertain; compare the order with the page. |
| `columns_were_page_furniture`, `columns_were_site_menu` | Apparent columns were classified as page furniture or site navigation; check roles if order matters. |
| `ruled_tables`, `unruled_tables`, `ocr_tables` | Tables were detected using rules, layout, or OCR; inspect table blocks and cells. |
| `image_region_tables` | A PDF page's image regions held tables, which were read as tables; inspect those table blocks and cells. |
| `table_grid_unresolved`, `table_rejected_crosses_text` | Table geometry was unresolved or a candidate crossed text; check the page for lost structure. |
| `site_menu`, `site_footer_menu` | Browser-print navigation was identified; inspect navigation blocks before filtering. |

### Block flags

| Flag | Meaning and consumer action |
|---|---|
| `ocr_unverified`, `ocr_short_for_ink` | OCR text may omit ink and has not been verified; compare important content with the original. |
| `from_image_region`, `from_embedded_image`, `layer_text_masked`, `repeated_image_text` | Text came from an image region or embedded image, overlapping layer text was masked, or a previous image reading was reused; inspect provenance and `checks.repeated_image`. |
| `reading_order_region_appended`, `reading_order_region_placed`, `reading_order_rotated_appended` | Image or rotated text was appended or placed in flow; verify its position. |
| `bidi_ambiguous_ltr_run`, `rtl_brackets_unverified` | Bidirectional ordering or brackets are uncertain; check against the source. |
| `rotated_text`, `rotated_90`, `rotated_270` | Vertical text was read separately; check position and orientation. |
| `brackets_mirrored`, `brackets_kept_valid_as_read`, `bullet_symbol_mapped` | Brackets were repaired or kept, or a symbol bullet mapped; inspect `repairs` and verify where significant. |
| `unmapped_characters`, `suspicious_glyph_mapping`, `implausible_date`, `implausible_time`, `digit_groups_suspect` | Text contains unmapped/suspicious glyphs, an implausible date/time, or suspect digit groups; verify against the source. |
| `digit_groups_confirmed`, `numbers_from_second_reading`, `numbers_read_by_number_model` | A number was checked or supplied by a second reading; inspect `checks`, and verify important values. |
| `number_disagreement`, `numbers_unconfirmed` | Number readings disagree or are not confirmed; inspect `checks` and verify. |
| `ruled_table`, `unruled_table`, `table_rows_unruled`, `table_grid_unresolved`, `table_cells_ink_unread` | Table structure was inferred, rows unruled, grid unresolved, or ink left unread; check cells and original. |
| `table_ltr_layout` | A Hebrew table has left-to-right layout; preserve or check its layout. |
| `page_furniture`, `site_menu`, `site_footer_menu` | Block classified as recurring furniture or site navigation; use `role` when selecting content. |
| `from_text_box`, `from_smartart`, `sheet_name`, `hidden_sheet`, `hidden_slide`, `hidden_shape` | Block came from the named Office structure or hidden content; decide whether it belongs in the consumer's output. |
| `hidden_text_dropped`, `white_text_dropped`, `math_not_read`, `symbol_unreadable` | Text, math or a symbol was omitted or unreadable; inspect the source and `unread`. |
| `symbol_font_char`, `has_note_reference` | Symbol-font character or note reference present; inspect text and `note_refs`. |
| `numbering_on_section_break_paragraph`, `numbering_parent_unused`, `numbering_hebrew2_beyond_22`, `numbering_format_unsupported:<format>` | Numbering had an unusual section break, unused parent, out-of-range Hebrew format, or unsupported format; check the displayed label. |
| `label_suffix_nothing` | No space follows the label; preserve that when rendering. |
| `empty_rows_left_out`, `empty_columns_left_out`, `hidden_rows_read`, `hidden_columns_read` | Spreadsheet table omits empty rows/columns or includes hidden ones; check whether its shape fits the use case. |
| `nested_table`, `image_not_read`, `image_too_large`, `image_too_small`, `image_blank`, `image_format_not_supported`, `image_no_text_found`, `image_ocr_low_confidence`, `ocr_failed`, `time_limit` | Nested content or an image was flattened/not read, blank, unsupported, too small, or stopped by OCR or a time limit; inspect the source and `unread`. |
| `group_depth_flattened`, `from_layout_table`, `margin_heading`, `section_number_joined`, `table_caption`, `outside_table_cells` | Structure was flattened, inferred, joined or extracted from a layout table; check it before reconstructing the document. |

### Table-cell flags

| Flag | Meaning and consumer action |
|---|---|
| `merged_above`, `merged_cell_has_text`, `span_capped` | Cell continues a vertical merge, has text under a merge, or a span was capped; inspect source and spans before rebuilding the table. |
| `hidden_row`, `hidden_column` | Cell was in a hidden spreadsheet row/column; decide whether to include it. |
| `formula_not_cached`, `formula_error`, `shared_string_missing` | Cached formula result is missing or an error, or shared text is missing; inspect the workbook. |
| `number_not_numeric`, `number_format_unknown` | Numeric value or format could not be interpreted; verify the original. |
| `nested_table`, `image_not_read`, `hidden_text_dropped`, `white_text_dropped` | Nested or hidden content was flattened or omitted; inspect the source. |
| `ocr_failed`, `ocr_empty_with_ink`, `faint_ink_read` | OCR failed, found no text despite ink, or read faint ink; verify the cell. |
| `table_cell_unconfirmed`, `table_cell_garbled`, `table_cell_not_a_number` | OCR cell readings are uncertain, garbled, or nonnumeric in a numeric column; inspect `readings`, `unread`, and source. |
| `number_disagreement`, `number_read_by_number_model`, `number_read_at_half_resolution` | Number readings disagreed, or a number model or lower-resolution retry supplied the reading; inspect `readings` and verify. |

## OCR models and export formats

The GitHub install has no bundled OCR models. With Tesseract and Hebrew language data installed, the default OCR falls back to stock `heb`. The CLI warns and the record includes `bundled_model_unavailable`. Use `ocr_model="heb"` or `--ocr-model heb` to select stock explicitly. Verify any niqqud against the source. These settings affect OCR only.

TXT (`doc.text`, `-f text`, `.txt`), Markdown (`doc.markdown`, `-f markdown`, `.md`), DOCX (`heocr.write_docx`, `-f docx`, `.docx`), and the tables alone as XLSX (`heocr.write_xlsx`, `-f xlsx`, `.xlsx`) or CSV (`doc.tables_csv()`, `-f csv`, `.csv`) work with the GitHub install. DOCX creates an editable Word view. Keep JSON for the full record.

## Copy-paste recipes

Replace `input.html` with your own input. These examples use a small HTML file so no OCR or external program is required:

```sh
printf '%s\n' '<!doctype html><html><body><h1>Example</h1><table><tr><th>Item</th><th>Value</th></tr><tr><td>Apple</td><td>2</td></tr></table></body></html>' > input.html
```

Find all tables, including non-body tables:

```python
import heocr

doc = heocr.extract("input.html")
for block in doc.to_dict()["blocks"]:
    if block["kind"] == "table":
        print(block["id"], block.get("page"), block["cells"])
```

List pages needing review and surface omissions that cannot be assigned to one page. This is deliberately conservative: any page, block, or cell flag triggers review, including informational flags. Documents without page entries still need document/block review.

```python
import heocr

record = heocr.extract("input.html").to_dict()
review = set()
for page in record["pages"]:
    if page["flags"] or page["coverage"].get("status") in {"partial", "unread"}:
        review.add(page["index"])
for block in record["blocks"]:
    cell_flags = any(cell.get("flags") for row in block.get("cells", []) for cell in row)
    if (block["flags"] or cell_flags or block.get("source") == "ocr") and "page" in block:
        review.add(block["page"])
for item in record["unread"]:
    if "page" in item:
        review.add(item["page"])
    print("unread:", item)
for page in sorted(review):
    print("review page", page + 1)
print("document flags:", record["flags"])
for block in record["blocks"]:
    if "page" not in block and (block["flags"] or block.get("source") == "ocr" or any(cell.get("flags") for row in block.get("cells", []) for cell in row)):
        print("review unpaged block:", block["id"])
```

Export Markdown, text and JSON via CLI:

```sh
heocr input.html -o output.md
heocr input.html -o output.txt
heocr input.html -o output.json
```

Or export Markdown in Python:

```python
from pathlib import Path
import heocr

Path("output.md").write_text(heocr.extract("input.html").markdown, encoding="utf-8")
```

With 0.3.0a1 installed, export Word:

```sh
heocr input.html -o output.docx
```

```python
import heocr

heocr.write_docx(heocr.extract("input.html"), "output.docx")
```
