"""The XLSX route (D4d): our own reading of SpreadsheetML, each sheet a table block.

- **Sheets** in workbook order. Each one is a heading block with the sheet's name, then a
  table block over its used range. A chart sheet goes to ``unread``.
- **Values** as Excel shows them, never recalculated:
  - shared and inline strings, rich text included (phonetic runs are not text);
  - a formula's **cached** value; a formula with no cached value is an empty cell flagged
    ``formula_not_cached``;
  - numbers and dates **as their number format displays them** (``General``, ``0.00``,
    ``#,##0``, percentages, currency symbols and literals, dates and times). When a format is
    beyond this renderer (fractions, scientific, conditions, elapsed time), the cell shows the
    raw value and is flagged ``number_format_unknown``. Excel's locale-dependent short date (id
    14 and 22) is shown day first, as Israeli Excel shows it, and the document is flagged
    ``date_format_locale_assumed``;
  - booleans as TRUE/FALSE, error values as written (flagged ``formula_error``).
- **Merged cells** are spans: the top-left cell gets ``colspan``/``rowspan``, and positions it
  covers in later rows hold empty ``merged_above`` cells, as in the other routes.
- **Direction:** a sheet shown right to left (``sheetView rightToLeft``) is an RTL table.
- **Hidden sheets, rows and columns are read and flagged** (``hidden_sheet`` on the table,
  ``hidden_row`` / ``hidden_column`` on cells), never dropped silently.
- **Also read:** cell comments (role ``comment``, with the cell reference as ``note_id``) and the
  sheet's print header and footer (roles ``header`` and ``footer``, field codes removed).
- **Not read, and recorded:** drawings (pictures, charts, text boxes) on a sheet.
- **Limits:** a sheet whose used range is over ``MAX_SHEET_CELLS`` is cut to the rows that fit,
  and the rest goes to ``unread``.
"""

from __future__ import annotations

import datetime as dt
import io
import posixpath
import re
import zipfile
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

import defusedxml
from defusedxml import ElementTree

from heocr.contract import Block, Cell, Document, Unread
from heocr.docx.reader import DocxError, _Package

TOOL = "heocr.xlsx"
MAX_SHEET_CELLS = 2_000_000
MAX_COLUMNS = 16_384  # Excel's own limit (XFD)
_STRICT = (
    (b"http://purl.oclc.org/ooxml/spreadsheetml/main", b"http://schemas.openxmlformats.org/spreadsheetml/2006/main"),
    (b"http://purl.oclc.org/ooxml/officeDocument/relationships", b"http://schemas.openxmlformats.org/officeDocument/2006/relationships"),
)
S = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"

_REF = re.compile(r"([A-Z]{1,3})(\d{1,7})")
_HEBREW = re.compile(r"[֐-׿]")

# Built-in number formats (ECMA-376 §18.8.30). 14 and 22 are the locale's short date: day first here.
BUILTIN = {
    0: "General", 1: "0", 2: "0.00", 3: "#,##0", 4: "#,##0.00", 9: "0%", 10: "0.00%", 11: "0.00E+00",
    12: "# ?/?", 13: "# ??/??", 14: "dd/mm/yyyy", 15: "d-mmm-yy", 16: "d-mmm", 17: "mmm-yy",
    18: "h:mm AM/PM", 19: "h:mm:ss AM/PM", 20: "h:mm", 21: "h:mm:ss", 22: "dd/mm/yyyy h:mm",
    37: "#,##0 ;(#,##0)", 38: "#,##0 ;[Red](#,##0)", 39: "#,##0.00;(#,##0.00)", 40: "#,##0.00;[Red](#,##0.00)",
    45: "mm:ss", 46: "[h]:mm:ss", 47: "mmss.0", 48: "##0.0E+0", 49: "@",
}
LOCALE_DATES = (14, 22)
_MONTHS = "January February March April May June July August September October November December".split()
_DAYS = "Monday Tuesday Wednesday Thursday Friday Saturday Sunday".split()
_HE_MONTHS = "ינואר פברואר מרץ אפריל מאי יוני יולי אוגוסט ספטמבר אוקטובר נובמבר דצמבר".split()
_HE_DAYS = "יום שני|יום שלישי|יום רביעי|יום חמישי|יום שישי|שבת|יום ראשון".split("|")


class XlsxError(ValueError):
    pass


def _col(letters: str) -> int:
    n = 0
    for ch in letters:
        n = n * 26 + ord(ch) - 64
    return n - 1


def _ref(ref: str) -> tuple[int, int] | None:
    match = _REF.fullmatch(ref or "")
    if not match:
        return None
    return int(match.group(2)) - 1, _col(match.group(1))


# -- number formats ------------------------------------------------------------------


class _Unknown(Exception):
    pass


def _sections(code: str) -> list[str]:
    out, current, quoted, bracket, escape = [], [], False, False, False
    for ch in code:
        if escape:
            current.append(ch)
            escape = False
            continue
        if ch == "\\" and not quoted:
            escape = True
        elif ch == '"':
            quoted = not quoted
        elif ch == "[" and not quoted:
            bracket = True
        elif ch == "]" and not quoted:
            bracket = False
        elif ch == ";" and not quoted and not bracket:
            out.append("".join(current))
            current = []
            continue
        current.append(ch)
    out.append("".join(current))
    return out


def _tokens(section: str) -> tuple[list[tuple[str, str]], bool]:
    """(tokens, hebrew): ("lit", text) or ("code", text), with brackets and spacing resolved."""
    tokens: list[tuple[str, str]] = []
    hebrew = False
    i = 0
    while i < len(section):
        ch = section[i]
        if ch == '"':
            end = section.find('"', i + 1)
            end = len(section) if end < 0 else end
            tokens.append(("lit", section[i + 1 : end]))
            i = end + 1
        elif ch == "\\" and i + 1 < len(section):
            tokens.append(("lit", section[i + 1]))
            i += 2
        elif ch == "_" and i + 1 < len(section):
            tokens.append(("lit", " "))  # space as wide as the next character
            i += 2
        elif ch == "*" and i + 1 < len(section):
            i += 2  # repeat-to-fill: no text
        elif ch == "[":
            end = section.find("]", i)
            end = len(section) if end < 0 else end
            inner = section[i + 1 : end]
            if inner.startswith("$"):
                symbol, _, locale = inner[1:].partition("-")
                if symbol:
                    tokens.append(("lit", symbol))
                if locale.upper().lstrip("0").endswith("40D") or locale.lower().startswith("he"):
                    hebrew = True
            elif inner.lower() in ("red", "black", "blue", "green", "white", "yellow", "magenta", "cyan") or inner.lower().startswith("color"):
                pass
            else:
                raise _Unknown(inner)  # conditions, elapsed time [h], DBNum...
            i = end + 1
        else:
            tokens.append(("code", ch))
            i += 1
    return tokens, hebrew


def _is_date(tokens) -> bool:
    text = "".join(t for kind, t in tokens if kind == "code").lower()
    text = text.replace("general", "")
    return bool(re.search(r"[ymdhs]", text)) or "am/pm" in text or "a/p" in text


def _round(value: float, places: int) -> Decimal:
    try:
        return Decimal(repr(value)).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
    except InvalidOperation:
        raise _Unknown("value") from None


def general(value: float) -> str:
    if value == int(value) and abs(value) < 1e15:
        return str(int(value))
    text = f"{value:.10g}"
    if "e" in text:  # Excel's General: 1.5E-07
        mantissa, _, exponent = f"{value:.5E}".partition("E")
        mantissa = mantissa.rstrip("0").rstrip(".")
        return f"{mantissa}E{exponent[0]}{int(exponent[1:]):02d}"
    return text


def _number(tokens, value: float) -> str:
    codes = "".join(t for kind, t in tokens if kind == "code")
    if re.search(r"[eE?/]", codes):
        raise _Unknown("scientific or fraction")
    if re.search(r"[0#],+(?![0#,])", codes):
        raise _Unknown("thousands scaling")
    if "@" in codes:
        return "".join(general(value) if t == "@" and kind == "code" else t for kind, t in tokens)
    placeholder = re.compile(r"[0#.,%]")
    first = next((n for n, (kind, t) in enumerate(tokens) if kind == "code" and t in "0#"), None)
    if first is None:
        out = "".join(t for _, t in tokens)
        if "general" in out.lower():
            return re.sub("general", general(value), out, flags=re.I)
        return out
    last = max(n for n, (kind, t) in enumerate(tokens) if kind == "code" and t in "0#.")
    while first > 0 and tokens[first - 1][0] == "code" and tokens[first - 1][1] in ".,":
        first -= 1
    pattern = "".join(t for _, t in tokens[first : last + 1])
    if not all(placeholder.fullmatch(ch) for ch in pattern):
        raise _Unknown("mixed pattern")
    percent = codes.count("%")
    value = value * (100**percent)
    integer, _, fraction = pattern.partition(".")
    if integer.endswith(","):
        raise _Unknown("thousands scaling")
    places = sum(1 for ch in fraction if ch in "0#")
    rounded = _round(value, places)
    text = f"{rounded:,.{places}f}" if "," in integer else f"{rounded:.{places}f}"
    if places and "0" not in fraction:  # "#.##": trailing zeros are not shown
        text = text.rstrip("0").rstrip(".") if "." in text else text
    whole, dot, frac = text.partition(".")
    if "0" not in integer and whole in ("0", "-0"):
        whole = whole[:-1]  # "#.00" shows .50, not 0.50
    minimum = integer.replace(",", "").count("0")
    digits = whole.lstrip("-").replace(",", "")
    if len(digits) < minimum:
        padded = digits.zfill(minimum)
        whole = ("-" if whole.startswith("-") else "") + (f"{int(padded):,}".zfill(len(padded)) if "," in integer else padded)
    return "".join(t for _, t in tokens[:first]) + whole + dot + frac + "".join(t for _, t in tokens[last + 1 :])


def serial_to_datetime(value: float, date1904: bool) -> dt.datetime:
    if date1904:
        base = dt.datetime(1904, 1, 1)
    else:
        if value < 0 or 60 <= value < 61:
            raise _Unknown("serial 60")  # Excel's 29 February 1900, which never was
        base = dt.datetime(1899, 12, 31) if value < 60 else dt.datetime(1899, 12, 30)
    try:
        return base + dt.timedelta(days=int(value), seconds=round((value - int(value)) * 86400))
    except OverflowError:
        raise _Unknown("date out of range") from None


_DATE_TOKEN = re.compile(r"yyyy|yy|mmmmm|mmmm|mmm|mm|m|dddd|ddd|dd|d|hh|h|ss|s|am/pm|a/p|\.0+", re.I)


def _date(tokens, value: float, date1904: bool, hebrew: bool) -> str:
    moment = serial_to_datetime(value, date1904)
    parts: list[tuple[str, str]] = []  # ("lit", text) or ("tok", token)
    code = ""
    for kind, text in tokens + [("lit", "")]:
        if kind == "code":
            code += text
            continue
        for match_start, piece in _split_date(code):
            parts.append((match_start, piece))
        code = ""
        if text:
            parts.append(("lit", text))
    twelve = any(kind == "tok" and t.lower() in ("am/pm", "a/p") for kind, t in parts)
    if value < 1 and any(kind == "tok" and t.lower()[0] in "yd" for kind, t in parts):
        raise _Unknown("a date before 1 January 1900")  # time-only values have no date to show
    out = []
    for n, (kind, token) in enumerate(parts):
        if kind == "lit":
            out.append(token)
            continue
        low = token.lower()
        if low in ("m", "mm"):
            before = next((t.lower() for k, t in reversed(parts[:n]) if k == "tok"), "")
            after = next((t.lower() for k, t in parts[n + 1 :] if k == "tok"), "")
            minute = before.startswith("h") or after.startswith("s")
            number = moment.minute if minute else moment.month
            out.append(f"{number:02d}" if low == "mm" else str(number))
        elif low == "yyyy":
            out.append(f"{moment.year:04d}")
        elif low == "yy":
            out.append(f"{moment.year % 100:02d}")
        elif low in ("mmm", "mmmm", "mmmmm"):
            name = (_HE_MONTHS if hebrew else _MONTHS)[moment.month - 1]
            out.append(name if low == "mmmm" or hebrew else name[:3] if low == "mmm" else name[0])
        elif low in ("d", "dd"):
            out.append(f"{moment.day:02d}" if low == "dd" else str(moment.day))
        elif low in ("ddd", "dddd"):
            name = (_HE_DAYS if hebrew else _DAYS)[moment.weekday()]
            out.append(name if low == "dddd" or hebrew else name[:3])
        elif low in ("h", "hh"):
            hour = moment.hour % 12 or 12 if twelve else moment.hour
            out.append(f"{hour:02d}" if low == "hh" else str(hour))
        elif low in ("s", "ss"):
            out.append(f"{moment.second:02d}" if low == "ss" else str(moment.second))
        elif low == "am/pm":
            out.append("AM" if moment.hour < 12 else "PM")
        elif low == "a/p":
            out.append("A" if moment.hour < 12 else "P")
        else:  # fractions of a second
            out.append("." + "0" * (len(token) - 1))
    return "".join(out)


def _split_date(code: str):
    i = 0
    while i < len(code):
        match = _DATE_TOKEN.match(code, i)
        if match:
            yield "tok", match.group()
            i = match.end()
        else:
            if code[i].isalnum() and code[i].lower() not in "e":
                raise _Unknown(code[i])
            yield "lit", code[i]
            i += 1


def format_number(value: float, code: str, date1904: bool = False) -> str:
    """``value`` as the number format ``code`` displays it; raises _Unknown when beyond this renderer."""
    sections = _sections(code)
    negative_section = len(sections) >= 2 and value < 0
    if len(sections) >= 3 and value == 0:
        section = sections[2]
    elif negative_section:
        section, value = sections[1], -value
    else:
        section = sections[0]
    tokens, hebrew = _tokens(section)
    if not [t for k, t in tokens if k == "code"] and not any(t for k, t in tokens):
        return ""
    if "".join(t for k, t in tokens if k == "code").strip().lower() == "general":
        text = general(value)
        prefix = "".join(t for k, t in tokens[: next(n for n, (k, _) in enumerate(tokens) if k == "code")])
        return prefix + text if not negative_section else prefix + text
    if _is_date(tokens):
        if value < 0:
            raise _Unknown("negative date")
        return _date(tokens, value, date1904, hebrew)
    sign = "-" if value < 0 and not negative_section else ""
    text = _number(tokens, abs(value) if sign else value)
    return sign + text if re.search(r"[1-9]", text) or not sign else text


# -- the workbook ------------------------------------------------------------------


class _Book:
    def __init__(self, body: bytes, doc: Document) -> None:
        self.pkg = _Package(body)
        self.doc = doc
        main = next((t for _, typ, t in self.pkg.rels("") if typ.endswith("/officeDocument")), "xl/workbook.xml")
        self.main = main
        self.rels = {rid: (typ, target) for rid, typ, target in self.pkg.rels(main)}
        root = self.xml(main)
        if root is None:
            raise XlsxError("no workbook part")
        pr = root.find(S + "workbookPr")
        self.date1904 = pr is not None and pr.get("date1904", "0").lower() in ("1", "true")
        self.sheets = [(s.get("name", ""), s.get(R + "id", ""), s.get("state", "visible")) for s in root.iter(S + "sheet")]
        self.strings = self._strings()
        self.formats, self.xf_formats = self._styles()
        self.locale_dates = 0

    def read(self, name: str) -> bytes | None:
        data = self.pkg.read(name)
        if data is None:
            return None
        for strict, transitional in _STRICT:
            data = data.replace(strict, transitional)
        return data

    def xml(self, name: str):
        data = self.read(name)
        return ElementTree.fromstring(data) if data else None

    def part(self, suffix: str) -> str | None:
        return next((target for typ, target in self.rels.values() if typ.endswith("/" + suffix)), None)

    def _strings(self) -> list[str]:
        name = self.part("sharedStrings")
        root = self.xml(name) if name else None
        return [_rich_text(si) for si in root.findall(S + "si")] if root is not None else []

    def _styles(self) -> tuple[dict[int, str], list[int]]:
        name = self.part("styles")
        root = self.xml(name) if name else None
        formats: dict[int, str] = dict(BUILTIN)
        xfs: list[int] = []
        if root is not None:
            for fmt in root.iter(S + "numFmt"):
                if (fmt.get("numFmtId") or "").isdigit():
                    formats[int(fmt.get("numFmtId"))] = fmt.get("formatCode", "General")
            cell_xfs = root.find(S + "cellXfs")
            if cell_xfs is not None:
                xfs = [int(x.get("numFmtId", "0")) if (x.get("numFmtId") or "0").isdigit() else 0 for x in cell_xfs.findall(S + "xf")]
        return formats, xfs

    # -- one cell ------------------------------------------------------------------
    def value(self, c) -> tuple[str, list[str]]:
        kind = c.get("t", "n")
        v = c.find(S + "v")
        f = c.find(S + "f")
        raw = v.text if v is not None and v.text is not None else None
        if kind == "inlineStr":
            inline = c.find(S + "is")
            return (_rich_text(inline) if inline is not None else ""), []
        if raw is None:
            return "", (["formula_not_cached"] if f is not None else [])
        if kind == "s":
            try:
                return self.strings[int(raw)], []
            except (ValueError, IndexError):
                return "", ["shared_string_missing"]
        if kind in ("str", "d"):
            return raw, []
        if kind == "b":
            return ("TRUE" if raw.strip() in ("1", "true") else "FALSE"), []
        if kind == "e":
            return raw, ["formula_error"]
        try:
            number = float(raw)
        except ValueError:
            return raw, ["number_not_numeric"]
        style = c.get("s", "0")
        fmt_id = self.xf_formats[int(style)] if style.isdigit() and int(style) < len(self.xf_formats) else 0
        code = self.formats.get(fmt_id, "General")
        try:
            text = format_number(number, code, self.date1904)
        except (_Unknown, ValueError, OverflowError):
            return _raw(raw), ["number_format_unknown"]
        if fmt_id in LOCALE_DATES and fmt_id not in self._custom_ids():
            self.locale_dates += 1
        return text, []

    def _custom_ids(self) -> set[int]:
        return {i for i in self.formats if i not in BUILTIN or self.formats[i] != BUILTIN[i]}

    # -- one sheet -----------------------------------------------------------------
    def sheet(self, name: str, part: str, state: str) -> list[Block]:
        data = self.read(part)
        if data is None:
            self.doc.unread.append(Unread(scope="block", reason="sheet_missing", detail=f"sheet {name!r}: its part is missing"))
            return []
        cells: dict[tuple[int, int], Cell] = {}
        hidden_rows: set[int] = set()
        hidden_cols: set[int] = set()
        merges: list[tuple[int, int, int, int]] = []
        rtl = False
        drawings = 0
        header_footer: list[tuple[str, str]] = []
        row_index = -1
        for _, element in ElementTree.iterparse(io.BytesIO(data), events=("end",)):
            tag = element.tag
            if tag == S + "c":
                continue  # read with its row
            if tag == S + "row":
                r = element.get("r")
                row_index = int(r) - 1 if r and r.isdigit() else row_index + 1
                if element.get("hidden", "0").lower() in ("1", "true"):
                    hidden_rows.add(row_index)
                col_index = -1
                for c in element.findall(S + "c"):
                    position = _ref(c.get("r", ""))
                    col_index = position[1] if position else col_index + 1
                    text, flags = self.value(c)
                    if text or flags:
                        cells[(row_index, col_index)] = Cell(text, flags=flags)
                element.clear()
            elif tag == S + "col":
                if element.get("hidden", "0").lower() in ("1", "true"):
                    lo, hi = int(element.get("min", "1")), int(element.get("max", "1"))
                    hidden_cols.update(range(lo - 1, min(hi, MAX_COLUMNS)))
            elif tag == S + "mergeCell":
                a, _, b = (element.get("ref") or "").partition(":")
                start, end = _ref(a), _ref(b or a)
                if start and end:
                    merges.append((min(start[0], end[0]), min(start[1], end[1]), max(start[0], end[0]), max(start[1], end[1])))
            elif tag == S + "sheetView":
                rtl = element.get("rightToLeft", "0").lower() in ("1", "true")
            elif tag == S + "drawing" or tag == S + "legacyDrawing":
                drawings += 1
            elif tag in (S + "oddHeader", S + "oddFooter", S + "evenHeader", S + "evenFooter", S + "firstHeader", S + "firstFooter"):
                if element.text:
                    header_footer.append(("header" if "Header" in tag else "footer", element.text))
        blocks = [Block(kind="heading", text=name, level=2, source="office_xml", tool=TOOL, direction="rtl" if rtl or _HEBREW.search(name) else "ltr", flags=["sheet_name"] + (["hidden_sheet"] if state != "visible" else []))]
        if drawings:
            self.doc.unread.append(Unread(scope="block", reason="xlsx_drawing_not_read", detail=f"sheet {name!r}: {drawings} drawings (pictures, charts or text boxes)"))
        table = self._grid(name, cells, merges, hidden_rows, hidden_cols, rtl)
        if table is not None:
            if state != "visible":
                table.flags.append("hidden_sheet")
            blocks.append(table)
        blocks += self._comments(part, name)
        seen = set()
        for role, text in header_footer:
            clean = _header_footer_text(text)
            if clean and (role, clean) not in seen:
                seen.add((role, clean))
                blocks.append(Block(kind="text", text=clean, role=role, source="office_xml", tool=TOOL, direction="rtl" if _HEBREW.search(clean) else "ltr"))
        return blocks

    def _grid(self, name, cells, merges, hidden_rows, hidden_cols, rtl) -> Block | None:
        used = set(cells)
        for r0, c0, r1, c1 in merges:
            if (r0, c0) in cells:
                used.add((r1, c1))
        if not cells:
            return None
        top = min(r for r, _ in used)
        bottom = max(r for r, _ in used)
        left = min(c for _, c in used)
        right = max(c for _, c in used)
        width = right - left + 1
        if width * (bottom - top + 1) > MAX_SHEET_CELLS:
            keep = max(1, MAX_SHEET_CELLS // width)
            self.doc.unread.append(Unread(scope="block", reason="sheet_truncated", detail=f"sheet {name!r}: rows {top + keep + 1}-{bottom + 1} not read ({width} columns; the limit is {MAX_SHEET_CELLS} cells)"))
            bottom = top + keep - 1
        covered: dict[tuple[int, int], tuple[int, int, int]] = {}  # position -> (the merge's top row, left column, colspan)
        spans: dict[tuple[int, int], tuple[int, int]] = {}
        for r0, c0, r1, c1 in merges:
            if r0 < top or c0 < left:
                continue
            r1, c1 = min(r1, bottom), min(c1, right)
            spans[(r0, c0)] = (r1 - r0 + 1, c1 - c0 + 1)
            for r in range(r0, r1 + 1):
                for c in range(c0, c1 + 1):
                    if (r, c) != (r0, c0):
                        covered[(r, c)] = (r0, c0, c1 - c0 + 1)
        rows: list[list[Cell]] = []
        for r in range(top, bottom + 1):
            row: list[Cell] = []
            c = left
            while c <= right:
                cover = covered.get((r, c))
                if cover is not None:
                    anchor_row, anchor_col, colspan = cover
                    if anchor_row == r or c != anchor_col:
                        c += 1  # inside a colspan: the anchor, or this row's placeholder, covers it
                        continue
                    hidden_text = cells.get((r, c))
                    flags = ["merged_above"] + (["merged_cell_has_text"] if hidden_text is not None and hidden_text.text else [])
                    row.append(Cell("", colspan=colspan, flags=flags))
                    c += colspan
                    continue
                cell = cells.get((r, c)) or Cell("")
                rowspan, colspan = spans.get((r, c), (1, 1))
                cell.rowspan, cell.colspan = rowspan, colspan
                if r in hidden_rows:
                    cell.flags.append("hidden_row")
                if c in hidden_cols:
                    cell.flags.append("hidden_column")
                row.append(cell)
                c += colspan
            rows.append(row)
        flags = []
        if hidden_rows & set(range(top, bottom + 1)):
            flags.append("hidden_rows_read")
        if hidden_cols & set(range(left, right + 1)):
            flags.append("hidden_columns_read")
        return Block(kind="table", cells=rows, source="office_xml", tool=TOOL, direction="rtl" if rtl else "ltr", flags=flags)

    def _comments(self, sheet_part: str, name: str) -> list[Block]:
        out = []
        for _, typ, target in self.pkg.rels(sheet_part):
            if not typ.endswith("/comments"):
                continue
            root = self.xml(target)
            if root is None:
                continue
            for comment in root.iter(S + "comment"):
                text_el = comment.find(S + "text")
                text = _rich_text(text_el) if text_el is not None else ""
                if text.strip():
                    out.append(Block(kind="text", text=text, role="comment", note_id=f"{name}!{comment.get('ref', '')}", source="office_xml", tool=TOOL, direction="rtl" if _HEBREW.search(text) else "ltr"))
            if any(typ.endswith("/threadedComment") for _, typ, _ in self.pkg.rels(sheet_part)):
                self.doc.flags.append("threaded_comments_read_as_notes")
        return out


def _rich_text(element) -> str:
    """The text of a shared or inline string: ``t`` and rich runs, without phonetic runs."""
    parts = []
    for child in element:
        if child.tag == S + "t":
            parts.append(child.text or "")
        elif child.tag == S + "r":
            t = child.find(S + "t")
            parts.append(t.text or "" if t is not None else "")
    return re.sub(r"_x([0-9A-Fa-f]{4})_", lambda m: chr(int(m.group(1), 16)), "".join(parts))


def _raw(text: str) -> str:
    try:
        return general(float(text))
    except (_Unknown, ValueError, OverflowError):
        return text


_HF_CODE = re.compile(r'&(?:"[^"]*"|\d+|[LCRPNDTFAZGSUXYBIEKOH&+\-])')


def _header_footer_text(text: str) -> str:
    """A print header or footer without its codes: sections (&L &C &R) joined by spaces."""
    text = text.replace("&&", "\x00")
    pieces = [p.strip() for p in re.split(r"&[LCR]", text)]
    clean = [_HF_CODE.sub("", p).replace("\x00", "&").strip() for p in pieces]
    return "  ".join(p for p in clean if p)


def read_xlsx(body: bytes, doc: Document) -> None:
    """Fill ``doc`` with the sheets of an XLSX workbook."""
    doc.components["defusedxml"] = defusedxml.__version__
    try:
        book = _Book(body, doc)
        blocks: list[Block] = []
        for name, rid, state in book.sheets:
            typ, target = book.rels.get(rid, ("", ""))
            if typ.endswith("/chartsheet"):
                doc.unread.append(Unread(scope="block", reason="chart_not_read", detail=f"chart sheet {name!r}"))
                continue
            if not typ.endswith("/worksheet"):
                doc.unread.append(Unread(scope="block", reason="sheet_not_read", detail=f"sheet {name!r} of type {posixpath.basename(typ) or 'unknown'}"))
                continue
            blocks += book.sheet(name, target, state)
    except (zipfile.BadZipFile, DocxError, XlsxError, ElementTree.ParseError, defusedxml.DefusedXmlException) as exc:
        doc.unread.append(Unread(scope="document", reason="xlsx_unreadable", detail=f"{type(exc).__name__}: {exc}"[:300]))
        doc.flags.append("not_read")
        return
    doc.blocks.extend(blocks)
    if book.locale_dates:
        doc.flags.append("date_format_locale_assumed")
    if any("hidden_sheet" in b.flags for b in blocks):
        doc.flags.append("hidden_sheets_read")
    if not any(b.kind == "table" for b in blocks):
        doc.flags.append("no_text")
