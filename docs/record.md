# The record, `heocr_document.v1`: a reference

What `heocr.extract(...)` returns, as `doc.to_dict()` / `doc.to_json()` / `heocr -f json`. The
exact shape is the JSON Schema in
[`src/heocr/schemas/heocr_document.v1.schema.json`](../src/heocr/schemas/heocr_document.v1.schema.json),
and how v1 may change is in [contract-v1.md](contract-v1.md).

## Top level

| Field | What it holds |
|---|---|
| `schema` | `"heocr_document.v1"` |
| `producer` | `name` (`heocr`), `version`, and `components`: every external tool and model used, with its version or hash |
| `document` | `sha256`, `bytes`, `format` (decided from the content), `format_evidence`, and `page_count` when known. **Never a file name or path.** |
| `options` | The options the record was made with: `repairs`, `ocr`, `convert`, `max_pages`, `check_numbers` |
| `pages` | One entry per page, slide or image frame, in order (below) |
| `blocks` | The text, in reading order (below) |
| `unread` | What was not read, and why (below) |
| `flags` | Flags for the whole document |
| `font_maps` | Learned code → letter maps for broken PDF fonts (T3), when any |

## Pages

Each page has:
- `index` (its position), `width` and `height` in points;
- `class`: `T1`–`T6`, or `empty` (see the README's class table);
- `route`: how it was read, e.g. `text_layer`, `ocr`, `text_layer+ocr_regions`, `none`;
- `signals`: what the classifier saw;
- `coverage`: what was read (`chars_total`, `chars_read`, `chars_ocr`, `status`, image region counts, number checks);
- `flags`.

## Blocks

Each block has an `id` (`b0`, `b1`, …), a `kind`, a `role`, a `source`, a `tool`, `repairs` and `flags`, and depending on what it is:
- **content:** `text`, or `cells` for a table;
- **position:** `page`, `bbox` (points, top-left origin), `lines` (each with `text`, `bbox`, `direction`), `direction`;
- **structure:** `level`, `label` (a list or section number), `note_id`, `note_refs`, `half` (a split spread), `slide`;
- **provenance:** `ocr` (engine, settings, confidence, always `status: "unverified"`) and `checks` (second readings).

| `kind` | |
|---|---|
| `text`, `heading`, `list_item` | Running text, a heading (with `level`), a list item (with `label`) |
| `table` | `cells`: rows of cells, each with `text`, and optionally `colspan`, `rowspan` (capped at 1,000), `flags`, and `readings` (each model's reading of an OCR'd number) |
| `figure` | A picture's text |

| `role` | |
|---|---|
| `body` | The document's text, in reading order |
| `header`, `footer` | Running headers and footers, page numbers |
| `footnote`, `endnote`, `comment` | Notes and comments, tied to their place by `note_refs` |
| `textbox` | Text boxes and shapes outside the flow |
| `navigation` | Site menus and footers of web pages, and of web pages printed to PDF |
| `notes` | Speaker notes of slides |

Only `body` (and `textbox`) make up `doc.text`'s main flow. The other roles follow in their own sections.

| `source` | |
|---|---|
| `text_layer` | A PDF's own text layer |
| `office_xml` | A DOCX, XLSX or PPTX, read by heocr's own readers |
| `converted_docx`, `converted_xlsx`, `converted_pptx` | A DOC, RTF, ODT, ODS, XLS, ODP or PPT, converted by LibreOffice first |
| `html` | A web page |
| `ocr` | OCR (`tool` names the engine; `ocr` gives its settings and confidence) |
| `none` | Nothing was read (with an unread entry) |

## Flags

Flags are open lists of strings: a later v1 may add more. The main ones, by what they warn of:

- **Reading order:**
  - `reading_order_unresolved_columns`, `reading_order_row_major_unverified`, `reading_order_column_major_unverified`;
  - `reading_order_region_appended`, `reading_order_region_placed` (an image region's text, placed among the layer's blocks by its top edge), `reading_order_rotated_appended`;
  - `bidi_ambiguous_ltr_run`.
- **The page's text layer:**
  - `text_layer_is_ocr`, `foreign_ocr_layer`, `ocr_layer_disagrees`, `ocr_layer_disagrees_kept`, `ocr_layer_unchecked`;
  - `text_layer_ignored`, `text_layer_visual_order`, `text_sideways_on_screen`;
  - `rotated_text_read`, `rotated_text_not_read`, `text_outside_page`, `page_rotated`;
  - `possible_spread`, `spread_split`.
- **OCR:**
  - `ocr_unverified`, `ocr_short_for_ink`, `ocr_empty_with_ink`, `render_dpi_reduced`, `deskewed`;
  - `from_image_region`, `layer_text_masked`, `repeated_image_text`.
- **Tables:**
  - `ruled_table`, `unruled_table`, `ocr_tables`;
  - `table_cells_ink_unread`, `table_cell_unconfirmed`, `merged_above`, `span_capped`.
- **Numbers:**
  - `number_read_by_number_model`, `numbers_from_second_reading`;
  - `number_disagreement`, `numbers_unconfirmed`, `numbers_not_checked`, `numbers_read_with_main_model`;
  - `digit_groups_suspect`, `digit_groups_confirmed`.
- **Repairs:**
  - `brackets_mirrored`, `brackets_kept_valid_as_read`, `rtl_brackets_unverified`;
  - `bullet_symbol_mapped`, `font_cp1255_shifted`, `unmapped_characters`.
- **Office and web:**
  - `hidden_text_dropped`, `white_text_dropped`, `hidden_rows_read`, `hidden_columns_read`, `hidden_sheets_read`, `hidden_slides_read`;
  - `converted_by_libreoffice`;
  - `html_nesting_flattened`, `group_depth_flattened`;
  - `site_menu`, `site_footer_menu`, `browser_print`.
- **Furniture and structure:** `page_furniture`, `columns_were_page_furniture`, `section_number_joined`.

## `unread`: what was not read, and why

Each entry has:
- a `scope`: `document`, `page` or `block`;
- a `reason`;
- where possible a `page` and a `bbox`;
- a human `detail`.

Reasons are an open list. Those in use:

| Reason | When |
|---|---|
| `format_not_supported` | A format with no route (EPUB, email, archives) |
| `pdf_encrypted`, `pdf_unreadable`, `docx_unreadable`, `xlsx_unreadable`, `pptx_unreadable`, `html_unreadable`, `image_unreadable` | A file that does not open or parse |
| `converter_unavailable`, `conversion_failed`, `conversion_timeout` | LibreOffice missing, refusing the file, or stopped at its time limit |
| `ocr_route_unavailable`, `ocr_failed`, `ocr_found_no_text`, `ocr_short_for_ink` | Tesseract missing or failing; a page whose ink OCR did not read |
| `image_region_not_read`, `image_region_too_small`, `image_region_no_text_found`, `image_region_ocr_low_confidence` | Image regions on text-layer pages |
| `image_not_read`, `image_too_small`, `image_too_large`, `image_blank`, `image_no_text_found`, `image_ocr_low_confidence`, `image_format_not_supported` | Embedded images and image files |
| `table_row_ink_unread` | A table row with ink in its cells that OCR read none of |
| `table_cell_unconfirmed` | A scanned table cell whose two readings disagree, neither confident; both readings are in the cell |
| `rotated_text_not_read` | Characters drawn at an angle other than vertical |
| `text_outside_page` | Characters drawn outside the page's visible box |
| `html_rendered_by_script`, `html_too_large`, `html_truncated` | A web page filled in by JavaScript, or over a limit |
| `page_limit`, `sheet_truncated`, `merges_not_applied`, `sheet_missing`, `sheet_not_read`, `slide_missing` | A limit reached, or a part missing |
| `chart_not_read`, `smartart_not_read`, `xlsx_drawing_not_read`, `embedded_content_not_read`, `docx_altchunk_not_read` | Charts, drawings and embedded objects |

What is **no loss** is counted in `coverage`, not listed here: an image region whose text is already in the layer (`image_regions_text_in_layer`), a blank one (`image_regions_blank`), the scan under a kept OCR layer (`image_regions_layer_scan`), and a repeated image (`image_regions_repeated`).
