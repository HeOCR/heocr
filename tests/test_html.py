"""D4f: HTML pages, read with the standard library's parser. Synthetic pages only."""

import base64
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image, ImageDraw

import heocr
from heocr import htmlread
from tests.synth import html as H

FAKE_TESSERACT = [sys.executable, str(Path(__file__).parent / "synth" / "fake_tesseract.py")]
PROSE = "המציע יגיש את הצעתו במועד ובמקום שנקבעו במסמכי המכרז, בצירוף כל המסמכים הנדרשים. " * 3


def read(body: str, **kwargs) -> heocr.Document:
    doc = heocr.extract(H.page(body, **{k: v for k, v in kwargs.items() if k in ("charset", "head", "html_attrs")}), **{k: v for k, v in kwargs.items() if k not in ("charset", "head", "html_attrs")})
    assert heocr.validate(doc.to_dict()) == [], heocr.validate(doc.to_dict())
    return doc


def texts(doc, role="body"):
    return [b.text for b in doc.blocks if b.role == role]


class StructureTest(unittest.TestCase):
    def test_headings_paragraphs_and_breaks(self) -> None:
        doc = read("<h1>כותרת ראשית</h1><p>שורה אחת<br>שורה שנייה</p><div>טקסט   עם\n רווחים</div><h3>משנה</h3>")
        self.assertEqual([(b.kind, b.level, b.text) for b in doc.blocks], [
            ("heading", 1, "כותרת ראשית"), ("text", None, "שורה אחת\nשורה שנייה"), ("text", None, "טקסט עם רווחים"), ("heading", 3, "משנה"),
        ])
        self.assertEqual({b.source for b in doc.blocks}, {"html"})
        self.assertEqual({b.tool for b in doc.blocks}, {"heocr.html"})
        self.assertEqual(doc.format, "html")

    def test_implicit_closes_and_stray_end_tags(self) -> None:
        doc = read("<p>ראשונה<p>שנייה</div><ul><li>א<li>ב</ul><table><tr><td>תא</div>עוד<td>שני</table><p>אחרי")
        self.assertEqual(texts(doc)[:2], ["ראשונה", "שנייה"])
        self.assertEqual([b.label for b in doc.blocks if b.kind == "list_item"], ["•", "•"])
        table = next(b for b in doc.blocks if b.kind == "table")
        self.assertEqual([[c.text for c in row] for row in table.cells], [["תאעוד", "שני"]])
        self.assertEqual(doc.blocks[-1].text, "אחרי")

    def test_preformatted_text_keeps_its_spaces(self) -> None:
        doc = read("<pre>  סעיף   1\n  סעיף   2</pre>")
        self.assertEqual(doc.blocks[0].text, "  סעיף   1\n  סעיף   2")

    def test_direction_from_dir_then_lang_then_first_strong_letter(self) -> None:
        doc = read('<p>עברית</p><p dir="ltr">שמאל</p><div lang="en"><p>English</p></div><p dir="auto">Hello שלום</p>', html_attrs='lang="he"')
        self.assertEqual([b.direction for b in doc.blocks], ["rtl", "ltr", "ltr", "ltr"])
        plain = read("<p>שלום</p><p>Hello</p><p>123</p>", html_attrs="")
        self.assertEqual([b.direction for b in plain.blocks], ["rtl", "ltr", None])
        styled = read('<p style="direction: rtl">Hello</p>', html_attrs="")
        self.assertEqual(styled.blocks[0].direction, "rtl")

    def test_empty_headings_are_skipped_and_counted(self) -> None:
        doc = read('<h6><a name="x"></a></h6><h2>12.</h2><p>טקסט הסעיף</p>')
        self.assertNotIn("heading", [b.kind for b in doc.blocks])
        self.assertIn("empty_headings_skipped", doc.flags)
        # A heading with no letter is not a heading: here it is a section number, joined to its text.
        self.assertEqual([(b.label, b.text) for b in doc.blocks], [("12.", "טקסט הסעיף")])

    def test_direction_marks_are_repaired_and_logged(self) -> None:
        doc = read("<p>בסעיף &rlm;2.2 לחוק</p>")
        self.assertEqual(doc.blocks[0].text, "בסעיף 2.2 לחוק")
        self.assertEqual(doc.blocks[0].repairs, [{"rule": "direction_marks", "from": "U+200F", "to": "", "count": 1}])
        raw = read("<p>בסעיף &rlm;2.2 לחוק</p>", repairs=False)
        self.assertEqual(raw.blocks[0].text, "בסעיף ‏2.2 לחוק")


class ListTest(unittest.TestCase):
    def test_numbering_start_value_type_and_reversed(self) -> None:
        doc = read(
            '<ol start="3"><li>ג</li><li value="7">ז</li><li>ח</li></ol>'
            '<ol type="a"><li>a</li><li>b</li></ol>'
            '<ol type="I"><li>I</li><li>II</li></ol>'
            "<ol reversed><li>שלוש</li><li>שתיים</li><li>אחת</li></ol>"
        )
        self.assertEqual([b.label for b in doc.blocks], ["3.", "7.", "8.", "a.", "b.", "I.", "II.", "3.", "2.", "1."])
        self.assertEqual({b.kind for b in doc.blocks}, {"list_item"})

    def test_hebrew_list_style_and_bullets(self) -> None:
        doc = read(
            '<ol style="list-style-type: hebrew"><li>א</li><li>ב</li></ol>'
            '<ul style="list-style: none"><li>בלי סימן</li></ul><ul type="square"><li>ריבוע</li></ul>'
        )
        self.assertEqual([(b.kind, b.label) for b in doc.blocks], [("list_item", "א."), ("list_item", "ב."), ("text", None), ("list_item", "▪")])

    def test_nested_lists_and_paragraphs_in_items(self) -> None:
        doc = read("<ol><li><p>פריט ראשון</p><ul><li>תת פריט</li></ul>המשך הפריט</li><li>פריט שני</li></ol>")
        self.assertEqual([(b.label, b.text) for b in doc.blocks], [
            ("1.", "פריט ראשון"), ("•", "תת פריט"), (None, "המשך הפריט"), ("2.", "פריט שני"),
        ])


class TableTest(unittest.TestCase):
    def test_spans_keep_the_grid(self) -> None:
        doc = read(
            '<table dir="rtl"><thead><tr><th colspan="2">שם</th><th>סכום</th></tr></thead>'
            '<tbody><tr><td rowspan="2">א</td><td>ב</td><td>1,000</td></tr><tr><td>ג</td><td>2,000</td></tr>'
            '<tr><td colspan="3" rowspan="0">סיכום</td></tr></tbody></table>'
        )
        table = doc.blocks[0]
        self.assertEqual(table.kind, "table")
        self.assertEqual(table.direction, "rtl")
        cells = [[(c.text, c.colspan, c.rowspan, c.flags) for c in row] for row in table.cells]
        self.assertEqual(cells, [
            [("שם", 2, 1, []), ("סכום", 1, 1, [])],
            [("א", 1, 2, []), ("ב", 1, 1, []), ("1,000", 1, 1, [])],
            [("", 1, 1, ["merged_above"]), ("ג", 1, 1, []), ("2,000", 1, 1, [])],
            [("סיכום", 3, 1, [])],
        ])

    def test_a_span_beyond_the_row_is_still_placed(self) -> None:
        doc = read('<table><tr><td>א</td><td rowspan="2">ב</td></tr><tr><td>ג</td></tr><tr><td>ד</td><td>ה</td></tr></table>')
        self.assertEqual([[c.text for c in row] for row in doc.blocks[0].cells], [["א", "ב"], ["ג", ""], ["ד", "ה"]])

    def test_cells_with_lines_lists_and_hidden_rows(self) -> None:
        doc = read('<table><tr><td><p>שורה</p><p>שנייה</p></td><td><ol><li>פריט</li></ol></td></tr><tr hidden><td>סמוי</td><td>x</td></tr></table>')
        self.assertEqual([[c.text for c in row] for row in doc.blocks[0].cells], [["שורה\nשנייה", "1. פריט"]])

    def test_layout_tables_are_read_as_flow(self) -> None:
        one_column = read("<table><tr><td><h2>כותרת</h2></td></tr><tr><td><p>פסקה</p></td></tr></table>")
        self.assertEqual([(b.kind, b.text) for b in one_column.blocks], [("heading", "כותרת"), ("text", "פסקה")])
        self.assertIn("layout_table_read_as_flow", one_column.flags)
        nested = read("<table><tr><td>תפריט</td><td><table><tr><td>א</td><td>ב</td></tr></table></td></tr></table>")
        self.assertEqual([b.kind for b in nested.blocks], ["text", "table"])
        presentation = read('<table role="presentation"><tr><td>ימין</td><td>שמאל</td></tr></table>')
        self.assertEqual(texts(presentation), ["ימין", "שמאל"])

    def test_caption_is_read_before_the_table(self) -> None:
        doc = read("<table><caption>טבלה 1</caption><tr><td>א</td><td>ב</td></tr></table>")
        self.assertEqual([b.kind for b in doc.blocks], ["text", "table"])
        self.assertIn("table_caption", doc.blocks[0].flags)


class NavigationTest(unittest.TestCase):
    def test_site_frame_is_navigation_and_out_of_the_body(self) -> None:
        doc = read(
            '<a href="#content" class="skip">דלג לתוכן</a>'
            '<header><img src="logo.png"><ul><li>דף הבית</li></ul></header><nav><a href="/a">קישור</a></nav>'
            '<div role="navigation">פירורי לחם</div><aside>קישורים נוספים</aside>'
            '<main id="content"><article><header><h1>כותרת המאמר</h1></header><p>' + PROSE + "</p>"
            "<aside>הערת אגב בתוך המאמר</aside><footer>נכתב בשנת 2026</footer></article></main>"
            '<footer role="contentinfo">כל הזכויות שמורות</footer>'
        )
        self.assertEqual(texts(doc), ["כותרת המאמר", PROSE.strip(), "הערת אגב בתוך המאמר", "נכתב בשנת 2026"])
        self.assertEqual(texts(doc, "navigation"), ["דלג לתוכן", "דף הבית", "קישור", "פירורי לחם", "קישורים נוספים", "כל הזכויות שמורות"])
        body, _, rest = doc.text.partition("--- Navigation ---")
        self.assertNotIn("דף הבית", body)
        self.assertIn("דף הבית", rest)
        self.assertIn("**Navigation**", doc.markdown)
        self.assertEqual([u.reason for u in doc.unread], ["image_not_read"])
        self.assertIn("html_images_not_fetched", doc.flags)

    def test_hebrew_skip_link_in_the_flow(self) -> None:
        doc = read('<p><a href="#main">דילוג לתוכן הראשי</a> טקסט רגיל <a href="#n2">הערה</a></p><p>' + PROSE + "</p>")
        self.assertEqual(texts(doc, "navigation"), ["דילוג לתוכן הראשי"])
        self.assertEqual(texts(doc)[0], "טקסט רגיל הערה")


class HiddenTest(unittest.TestCase):
    def test_hidden_scripts_and_styles_are_not_body_text(self) -> None:
        doc = read(
            "<p>גלוי</p><p hidden>סמוי א</p><div aria-hidden=\"true\">סמוי ב</div><span style=\"display: none\">סמוי ג</span>"
            "<p style=\"visibility:hidden\">סמוי ד</p><style>p{color:red}</style><template><p>תבנית</p></template>"
            "<noscript>אפשרו סקריפטים</noscript><select><option>בחירה</option></select><input type=hidden value=x>",
        )
        self.assertEqual(texts(doc), ["גלוי"])
        for flag in ("hidden_text_dropped", "noscript_not_read", "form_controls_not_read"):
            self.assertIn(flag, doc.flags)

    def test_frames_and_objects_are_counted_unread(self) -> None:
        doc = read('<p>' + PROSE + '</p><iframe src="https://example.org/x"></iframe><object data="x.swf"></object>')
        self.assertEqual([(u.reason, u.detail) for u in doc.unread], [("embedded_content_not_read", "2 frames or embedded objects (not fetched)")])


class ScriptRenderedTest(unittest.TestCase):
    def test_a_page_filled_in_by_script_is_unread_not_empty(self) -> None:
        doc = heocr.extract(H.govil_skeleton())
        self.assertEqual(heocr.validate(doc.to_dict()), [])
        self.assertEqual([u.reason for u in doc.unread], ["html_rendered_by_script"])
        self.assertIn("3 script elements", doc.unread[0].detail)
        for flag in ("html_rendered_by_script", "not_read", "noscript_not_read"):
            self.assertIn(flag, doc.flags)
        self.assertEqual(texts(doc), [])
        self.assertIn("דף הבית", texts(doc, "navigation"))  # the frame is kept, as navigation

    def test_a_page_with_text_and_scripts_is_read(self) -> None:
        doc = read("<p>" + PROSE + "</p><script>track()</script>")
        self.assertEqual(doc.unread, [])
        self.assertNotIn("html_rendered_by_script", doc.flags)
        data = read('<p>קצר</p><script type="application/ld+json">{"a": 1}</script>')
        self.assertNotIn("html_rendered_by_script", data.flags)  # data, not a script that draws the page

    def test_an_empty_page_without_scripts_is_no_text(self) -> None:
        doc = read("")
        self.assertIn("no_text", doc.flags)
        self.assertEqual(doc.unread, [])


class EncodingTest(unittest.TestCase):
    def test_windows_1255_from_meta_charset_and_http_equiv(self) -> None:
        body = "<html><head><meta charset=\"windows-1255\"></head><body><p>שלום עולם</p></body></html>".encode("cp1255")
        doc = heocr.extract(body)
        self.assertEqual(texts(doc), ["שלום עולם"])
        self.assertIn("windows-1255", doc.format_evidence)
        doc = heocr.extract(H.page("<p>שלום עולם</p>", charset="windows-1255"))
        self.assertEqual(texts(doc), ["שלום עולם"])
        self.assertNotIn("encoding_assumed", doc.flags)

    def test_bom_wins_and_undeclared_bytes_are_utf8_or_windows_1255(self) -> None:
        bom = b"\xef\xbb\xbf" + '<html><head><meta charset="windows-1255"></head><body><p>שלום</p></body></html>'.encode()
        self.assertEqual(texts(heocr.extract(bom)), ["שלום"])
        utf8 = heocr.extract("<html><body><p>שלום</p></body></html>".encode())
        self.assertEqual(texts(utf8), ["שלום"])
        self.assertNotIn("encoding_assumed", utf8.flags)
        legacy = heocr.extract("<html><body><p>שלום</p></body></html>".encode("cp1255"))
        self.assertEqual(texts(legacy), ["שלום"])
        self.assertIn("encoding_assumed", legacy.flags)
        self.assertIn("windows-1255 assumed", legacy.format_evidence)

    def test_visual_hebrew_and_unknown_declarations_are_flagged(self) -> None:
        visual = heocr.extract('<html><head><meta charset="iso-8859-8"></head><body><p>םולש</p></body></html>'.encode("iso8859_8"))
        self.assertIn("html_visual_order_encoding", visual.flags)
        logical = heocr.extract('<html><head><meta charset="iso-8859-8-i"></head><body><p>שלום</p></body></html>'.encode("iso8859_8"))
        self.assertEqual(texts(logical), ["שלום"])
        self.assertNotIn("html_visual_order_encoding", logical.flags)
        unknown = heocr.extract('<html><head><meta charset="x-no-such"></head><body><p>שלום</p></body></html>'.encode())
        self.assertIn("encoding_declaration_unknown", unknown.flags)
        self.assertEqual(texts(unknown), ["שלום"])

    def test_xhtml_with_an_xml_prolog_is_html(self) -> None:
        body = '<?xml version="1.0" encoding="utf-8"?>\n<!DOCTYPE html>\n<html xmlns="http://www.w3.org/1999/xhtml"><body><p>שלום</p></body></html>'
        doc = heocr.extract(body.encode())
        self.assertEqual(doc.format, "html")
        self.assertEqual(texts(doc), ["שלום"])


class LegislationTest(unittest.TestCase):
    """The Nevo case, from the volunteer's description, with invented text."""

    def check(self, doc) -> None:
        self.assertEqual(heocr.validate(doc.to_dict()), [])
        body = [b for b in doc.blocks if b.role == "body"][1:]  # after the title
        expected = []
        for number, margin, text in H.SECTIONS:
            expected += [("heading", None, margin), ("list_item", number, text)]
        self.assertEqual([(b.kind, b.label, b.text) for b in body], expected)
        self.assertTrue(all("margin_heading" in b.flags for b in body if b.kind == "heading"))
        self.assertTrue(all("section_number_joined" in b.flags for b in body if b.kind == "list_item"))
        # Never a lone section number, never an empty heading.
        self.assertFalse(any(htmlread.SECTION_NUMBER.fullmatch(b.text) for b in doc.blocks))
        self.assertTrue(all(b.text.strip() for b in doc.blocks if b.kind == "heading"))
        self.assertIn("## הגדרות\n\n1\\. בחוק זה", doc.markdown)

    def test_flow_with_a_number_alone_and_a_floated_margin_heading(self) -> None:
        doc = heocr.extract(H.nevo_like_flow())
        self.check(doc)
        self.assertIn("windows-1255", doc.format_evidence)
        self.assertIn("empty_headings_skipped", doc.flags)

    def test_layout_table_with_a_section_per_row(self) -> None:
        doc = heocr.extract(H.nevo_like_table())
        self.check(doc)
        self.assertIn("legislation_layout_table", doc.flags)
        self.assertNotIn("table", [b.kind for b in doc.blocks])

    def test_a_data_table_with_a_number_column_stays_a_table(self) -> None:
        doc = read("<table><tr><td>1.</td><td>עט</td><td>5</td></tr><tr><td>2.</td><td>מחברת</td><td>10</td></tr></table>")
        self.assertEqual([b.kind for b in doc.blocks], ["table"])

    def test_margin_heading_is_short_and_has_no_blocks(self) -> None:
        long_float = read('<div style="float:right"><p>' + PROSE + "</p></div>")
        self.assertNotIn("margin_heading", [f for b in long_float.blocks for f in b.flags])
        sidebar = read('<div class="margin-note">' + PROSE + "</div>")
        self.assertEqual(sidebar.blocks[0].kind, "text")


class ImageTest(unittest.TestCase):
    def png(self) -> bytes:
        image = Image.new("L", (600, 200), 255)
        ImageDraw.Draw(image).rectangle([20, 20, 580, 60], fill=0)
        out = io.BytesIO()
        image.save(out, format="PNG", dpi=(200, 200))
        return out.getvalue()

    def test_a_data_uri_image_is_read_by_ocr(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tessdata = Path(tmp) / "tessdata"
            tessdata.mkdir()
            (tessdata / "heb.traineddata").write_bytes(b"fake heb model")
            with mock.patch.dict(os.environ, {"FAKE_TESSDATA": str(tessdata)}):
                os.environ.pop("TESSDATA_PREFIX", None)
                src = "data:image/png;base64," + base64.b64encode(self.png()).decode()
                doc = read(f'<p>לפני</p><img src="{src}"><p>אחרי</p>', tesseract=FAKE_TESSERACT)
        self.assertEqual(texts(doc), ["לפני", "שלום עולם\nשורה", "פסקה", "אחרי"])
        self.assertEqual(doc.blocks[1].source, "ocr")

    def test_without_ocr_or_with_a_broken_uri_the_image_is_unread(self) -> None:
        src = "data:image/png;base64," + base64.b64encode(self.png()).decode()
        off = read(f'<img src="{src}">', ocr=False)
        self.assertEqual([b.kind for b in off.blocks], ["figure"])
        self.assertEqual([u.reason for u in off.unread], ["image_not_read"])
        broken = read('<img src="data:image/png;base64,@@@">')
        self.assertEqual([u.reason for u in broken.unread], ["image_not_read"])

    def test_a_data_uri_above_the_limit_is_too_large(self) -> None:
        src = "data:image/png;base64," + base64.b64encode(self.png()).decode()
        with mock.patch.object(htmlread, "MAX_DATA_URI_BYTES", 100):
            doc = read(f'<img src="{src}">')
        self.assertEqual([u.reason for u in doc.unread], ["image_too_large"])
        self.assertIn("the limit is 100", doc.unread[0].detail)
        self.assertEqual(doc.blocks[0].flags, ["image_too_large"])


class LimitsTest(unittest.TestCase):
    def test_deep_nesting_is_flattened_not_fatal(self) -> None:
        with mock.patch.object(htmlread, "MAX_DEPTH", 20):
            doc = read("<div>" * 60 + "<p>עמוק</p>" + "</div>" * 60)
        self.assertEqual(texts(doc), ["עמוק"])
        self.assertIn("html_nesting_flattened", doc.flags)

    def test_nesting_far_beyond_the_cap_keeps_every_text_and_never_crashes(self) -> None:
        cases = {
            "divs": "<div>" * 5000 + "<p>טקסט עמוק</p>" + "</div>" * 5000,
            "closed tables": "<table><tr><td>" * 5000 + "טקסט עמוק" + "</td></tr></table>" * 5000,
            "open tables": "<table><tr><td>" * 300 + "טקסט עמוק",
            "mixed": "<div><table><tr><td><ul><li><span>" * 1000 + "טקסט עמוק",
        }
        for name, body in cases.items():
            with self.subTest(name):
                doc = read(body + "<p>אחרי</p>")
                self.assertIn("טקסט עמוק", doc.text)
                self.assertIn("אחרי", doc.text)
                self.assertIn("html_nesting_flattened", doc.flags)
        for depth in (htmlread.MAX_DEPTH - 10, htmlread.MAX_DEPTH + 10, 3 * htmlread.MAX_DEPTH):
            with self.subTest(depth=depth):
                doc = read("<table><tr><td>" * depth + "תא פנימי" + "</td></tr></table>" * depth + "<p>אחרי</p>")
                self.assertEqual(doc.text.count("תא פנימי"), 1)
                self.assertTrue(doc.text.rstrip().endswith("אחרי"))

    def test_text_outside_table_cells_is_read_before_the_table(self) -> None:
        doc = read("<table><td>א</td><td>ב</td>טקסט חופשי<tr><td>ג</td><td>ה</td><table><tr><td>ד</td></tr></table></tr></table>")
        self.assertEqual([b.text for b in doc.blocks if b.kind != "table"], ["טקסט חופשי", "ד"])
        self.assertTrue(all("outside_table_cells" in b.flags for b in doc.blocks if b.kind != "table"))
        table = next(b for b in doc.blocks if b.kind == "table")
        self.assertEqual([[c.text for c in row] for row in table.cells], [["א", "ב"], ["ג", "ה"]])  # stray cells make a row

    def test_too_many_elements_is_truncated_and_says_so(self) -> None:
        with mock.patch.object(htmlread, "MAX_ELEMENTS", 50):
            doc = read("".join(f"<p>פסקה {i}</p>" for i in range(100)))
        self.assertEqual([u.reason for u in doc.unread], ["html_truncated"])
        self.assertIn("html_truncated", doc.flags)
        self.assertLess(len(doc.blocks), 50)


@unittest.skipUnless(os.environ.get("HEOCR_TEST_NEVO_HTML"), "opt-in; a local copy of a Nevo law page")
class NevoCopyTest(unittest.TestCase):
    """Against a local, private copy of a Nevo law page. Checks structure only; prints no text."""

    def test_sections_keep_numbers_and_margin_headings(self) -> None:
        doc = heocr.extract(Path(os.environ["HEOCR_TEST_NEVO_HTML"]))
        self.assertEqual(heocr.validate(doc.to_dict()), [])
        body = [b for b in doc.blocks if b.role == "body"]
        lone = sum(1 for b in body if b.kind == "text" and htmlread.SECTION_NUMBER.fullmatch(b.text.strip()))
        empty = sum(1 for b in doc.blocks if b.kind == "heading" and not b.text.strip())
        joined = sum(1 for b in body if "section_number_joined" in b.flags)
        margins = sum(1 for b in body if "margin_heading" in b.flags)
        summary = f"blocks={len(body)} joined={joined} margins={margins} lone_numbers={lone} empty_headings={empty}"
        self.assertEqual((lone, empty), (0, 0), summary)
        self.assertGreater(joined, 0, summary)
        self.assertGreater(margins, 0, summary)


if __name__ == "__main__":
    unittest.main()
