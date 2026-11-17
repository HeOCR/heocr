"""The HTML route (D4f): our own reading of a saved web page, with the standard library's parser.

- **Structure:** headings, paragraphs, lists with their numbering (``start``, ``value``, ``type``,
  ``reversed``, and the CSS ``list-style-type`` that Hebrew sites use), tables with their spans,
  ``<br>``, and ``dir`` for direction (``lang`` when no ``dir`` is set; otherwise the first strong
  character, as ``dir="auto"`` does).
- **Encoding:** from the BOM, then the ``<meta charset>`` or the HTTP-style ``<meta http-equiv>``
  declaration, then UTF-8 when the bytes are valid UTF-8, else ``windows-1255`` (the common
  encoding of Israeli sites), flagged ``encoding_assumed``. The evidence is kept in
  ``format_evidence``. ``iso-8859-8`` without ``-i`` is visual order and is flagged.
- **Navigation is a role, not a deletion:** ``nav``; ``header``, ``footer`` and ``aside`` outside
  an article, section or ``main``; ARIA ``navigation``, ``banner`` and ``contentinfo``; and "skip
  to content" links get the role ``navigation``. They stay in the record, out of the body flow.
- **Not body text:** scripts, styles, templates and the head. Hidden elements (``hidden``,
  ``aria-hidden="true"``, inline ``display:none`` or ``visibility:hidden``) are dropped and the
  document is flagged ``hidden_text_dropped``, as in DOCX. ``noscript`` is what a browser without
  scripts shows; it is not read, and is flagged.
- **A page filled in by script** (the gov.il case): a body with fewer than ``SCRIPT_MIN_LETTERS``
  letters outside navigation, and with scripts, goes to ``unread`` as ``html_rendered_by_script``
  and is flagged; it is never read as an empty document.
- **Legislation (the Nevo law, from the volunteer's description):** a section number on its own
  line joins its section's text as the block's label. A margin heading (the section's name,
  set beside the text) becomes the section's heading, never an empty one; an empty heading
  element is skipped and counted. Layout tables that hold a section per row (margin heading,
  number, text) are read as sections, and other layout tables (one column, or cells holding
  headings or tables) are read as the flow they lay out.
- **Never fetched:** images and frames that point elsewhere are counted in ``unread``; heocr
  never fetches from the network. Images inside the page (``data:`` URIs) are read by OCR, as
  DOCX images are.
"""

from __future__ import annotations

import base64
import binascii
import codecs
import re
from collections import Counter
from dataclasses import dataclass, field, replace
from html.parser import HTMLParser
from urllib.parse import unquote_to_bytes

from heocr import repairs
from heocr.contract import Block, Cell, Document, Unread, cap_span
from heocr.docx.numformat import hebrew_numeral, latin_letter, roman

TOOL = "heocr.html"
MAX_HTML_CHARS = 64 * 1024**2  # decoded characters; real legislation pages are far smaller
MAX_ELEMENTS = 2_000_000
# Nesting kept as structure. Deeper elements are flattened into the deepest one kept, with all
# their text: the walk is recursive, and must stay well inside Python's recursion limit.
MAX_DEPTH = 100
MAX_DATA_URI_BYTES = 32 * 1024**2
SCRIPT_MIN_LETTERS = 100  # fewer letters than this in the body, with scripts: the page is drawn by script
MARGIN_MAX_CHARS = 80
MARGIN_MAX_WORDS = 10
SECTION_MIN_LETTERS = 20  # a section's text, beside its number, in a layout table

VOID = frozenset("area base br col embed hr img input keygen link meta param source track wbr".split())
HEADINGS = frozenset(f"h{n}" for n in range(1, 7))
LISTS = frozenset(("ol", "ul", "menu", "dir"))
BLOCK = frozenset(
    "address article aside blockquote body center dd details dialog div dl dt fieldset figcaption figure "
    "footer form header hgroup hr html legend li main nav p pre section summary caption tr td th tbody thead tfoot".split()
)
CLOSES_P = (BLOCK | HEADINGS | LISTS | {"table"}) - {"body", "html", "caption", "tr", "td", "th", "tbody", "thead", "tfoot"}
TABLE_TAGS = frozenset(("table", "caption", "colgroup", "col", "thead", "tbody", "tfoot", "tr", "td", "th"))
NOT_TEXT = frozenset(("script", "style", "template", "head", "title", "meta", "link", "base", "svg", "math", "canvas", "select", "datalist", "rp", "rt"))
EMBEDDED = frozenset(("iframe", "frame", "object", "embed", "applet", "audio", "video"))
SECTIONING = frozenset(("article", "section", "main", "aside", "nav"))
NAV_ARIA = frozenset(("navigation", "banner", "contentinfo"))

_LETTER = re.compile(r"[^\W\d_]")
_STRONG = re.compile(r"[A-Za-zÀ-ɏ֐-ࣿיִ-﷿ﹰ-ﻼ]")
_RTL_CHAR = re.compile(r"[֐-ࣿיִ-﷿ﹰ-ﻼ]")
_RTL_LANGS = ("he", "iw", "yi", "ji", "ar", "fa", "ur", "ps", "dv", "syr")
_SPACES = re.compile(r"[ \t\n\r\f]+")
_META_CHARSET = re.compile(rb"<meta\b[^>]*?charset\s*=\s*[\"']?\s*([A-Za-z0-9_.:-]+)", re.I | re.S)
_HIDDEN_STYLE = re.compile(r"(?:^|;)\s*(?:display\s*:\s*none|visibility\s*:\s*hidden)\s*(?:!important\s*)?(?:;|$)", re.I)
_FLOAT_STYLE = re.compile(r"(?:^|;)\s*float\s*:\s*(?:right|left|inline-start|inline-end)", re.I)
_DIRECTION_STYLE = re.compile(r"(?:^|;)\s*direction\s*:\s*(rtl|ltr)", re.I)
_LIST_STYLE = re.compile(r"(?:^|;)\s*list-style(?:-type)?\s*:\s*([a-z-]+)", re.I)
_MARGIN_CLASS = re.compile(r"margin|shulay?im|side-?(?:head|title|note)|sidenote", re.I)
_SKIP_LINK = re.compile(
    r"\b(?:skip|jump)\b.*\b(?:content|main|navigation|nav|menu)\b|דלג|דילוג|לדלג|מעבר\s+ל(?:תוכן|תוכן\s+העמוד)|עבור\s+לתוכן", re.I
)
# A section number on its own: 12. / 12א. / 2.1 / (3) / (א) / א.
SECTION_NUMBER = re.compile(r"\d{1,4}[א-ת]{0,3}\.?|\d{1,4}(?:\.\d{1,3}){1,3}\.?|\(\d{1,4}[א-ת]{0,3}\)|\([א-ת]{1,2}\d{0,2}\)|[א-ת]{1,2}\.")

# WHATWG labels Python names differently or decodes differently.
_ENCODINGS = {
    "iso-8859-8-i": "iso8859_8", "logical": "iso8859_8", "csiso88598i": "iso8859_8",
    "iso-8859-1": "cp1252", "iso8859-1": "cp1252", "latin1": "cp1252", "l1": "cp1252", "us-ascii": "cp1252", "ascii": "cp1252",
    "x-user-defined": "cp1252",
}
_VISUAL = ("iso-8859-8", "iso8859-8", "iso_8859-8", "visual", "csisolatinhebrew", "hebrew", "iso-ir-138")


class _Truncated(Exception):
    pass


# -- decoding --------------------------------------------------------------------


def decode(body: bytes) -> tuple[str, str, list[str]]:
    """(text, evidence, flags): the page's characters, and how its encoding was decided."""
    for bom, name in ((codecs.BOM_UTF8, "utf-8"), (codecs.BOM_UTF16_LE, "utf-16-le"), (codecs.BOM_UTF16_BE, "utf-16-be")):
        if body.startswith(bom):
            return _decoded(body[len(bom) :], name, f"encoding {name} from the BOM", [])
    flags: list[str] = []
    match = _META_CHARSET.search(body[:4096])
    if match:
        label = match.group(1).decode("ascii", "replace").strip().lower()
        flags = ["html_visual_order_encoding"] if label in _VISUAL else []
        name = _ENCODINGS.get(label, label)
        if name.startswith("utf-16"):
            name = "utf-8"  # a UTF-16 declaration in a page read as bytes means UTF-8 (WHATWG)
        try:
            name = codecs.lookup(name).name
        except LookupError:
            flags.append("encoding_declaration_unknown")
        else:
            return _decoded(body, name, f"encoding {name} from <meta> ({label})", flags)
    try:
        return body.decode("utf-8"), "encoding utf-8 (no usable declaration; valid UTF-8)", flags
    except UnicodeDecodeError:
        return _decoded(body, "cp1255", "encoding windows-1255 assumed (no usable declaration; not UTF-8)", [*flags, "encoding_assumed"])


def _decoded(data: bytes, name: str, evidence: str, flags: list[str]) -> tuple[str, str, list[str]]:
    text = data.decode(name, "replace")
    if "�" in text and b"\xef\xbf\xbd" not in data:
        flags = [*flags, "undecodable_bytes_replaced"]
    return text, evidence, flags


# -- the tree --------------------------------------------------------------------


class _El:
    __slots__ = ("tag", "attrs", "children", "parent")

    def __init__(self, tag: str, attrs: dict[str, str], parent: "_El | None") -> None:
        self.tag = tag
        self.attrs = attrs
        self.children: list = []
        self.parent = parent

    def iter(self):
        yield self
        for child in self.children:
            if isinstance(child, _El):
                yield from child.iter()


class _Builder(HTMLParser):
    """A forgiving tree: void elements, and the implicit closes browsers apply (p, li, td, tr...)."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _El("#root", {}, None)
        self.stack = [self.root]
        self.count = 0
        self.deep = False
        self.excess = 0  # start tags beyond MAX_DEPTH still open: their end tags close nothing

    def handle_starttag(self, tag, attrs) -> None:
        self._start(tag, attrs, tag in VOID)

    def handle_startendtag(self, tag, attrs) -> None:
        # In HTML "<div/>" opens a div; only void elements (and an empty <script/>) close themselves.
        self._start(tag, attrs, tag in VOID or tag in ("script", "style"))

    def _start(self, tag: str, attrs, void: bool) -> None:
        self.count += 1
        if self.count > MAX_ELEMENTS:
            raise _Truncated
        if len(self.stack) >= MAX_DEPTH:
            # Too deep: the element is not kept, and its content stays in the deepest element kept
            # (void ones, such as <br> and <img>, are kept there). Nothing is closed implicitly.
            self.deep = True
            if void:
                self.stack[-1].children.append(_El(tag, {k.lower(): (v if v is not None else "") for k, v in attrs}, self.stack[-1]))
            else:
                self.excess += 1
            return
        self._implicit_close(tag)
        parent = self.stack[-1]
        element = _El(tag, {k.lower(): (v if v is not None else "") for k, v in attrs}, parent)
        parent.children.append(element)
        if not void:
            self.stack.append(element)

    def _close_to(self, names, stops) -> None:
        for i in range(len(self.stack) - 1, 0, -1):
            tag = self.stack[i].tag
            if tag in names:
                del self.stack[i:]
                return
            if tag in stops:
                return

    def _implicit_close(self, tag: str) -> None:
        if tag in CLOSES_P:
            self._close_to({"p"}, {"td", "th", "caption", "table", "button"})
        if tag == "li":
            self._close_to({"li"}, LISTS | {"table", "td", "th"})
        elif tag in ("dt", "dd"):
            self._close_to({"dt", "dd"}, {"dl", "table", "td", "th"})
        elif tag == "tr":
            self._close_to({"tr"}, {"table"})
        elif tag in ("td", "th"):
            self._close_to({"td", "th"}, {"tr", "table"})
        elif tag in ("thead", "tbody", "tfoot"):
            self._close_to({"thead", "tbody", "tfoot"}, {"table"})
        elif tag in ("option", "optgroup"):
            self._close_to({"option"}, {"select", "datalist"})
        if tag in HEADINGS and self.stack[-1].tag in HEADINGS:
            self.stack.pop()

    def handle_endtag(self, tag) -> None:
        if tag == "br":
            self._start("br", [], True)
            return
        if self.excess:
            self.excess -= 1  # the end of an element that was flattened
            return
        for i in range(len(self.stack) - 1, 0, -1):
            current = self.stack[i].tag
            if current == tag:
                del self.stack[i:]
                return
            if tag not in TABLE_TAGS and current in ("table", "td", "th"):
                return  # a stray end tag never closes a table from inside it

    def handle_data(self, data) -> None:
        self.stack[-1].children.append(data)


def _hidden(element: _El) -> bool:
    attrs = element.attrs
    return (
        "hidden" in attrs
        or attrs.get("aria-hidden", "").strip().lower() == "true"
        or bool(_HIDDEN_STYLE.search(attrs.get("style", "")))
        or (element.tag == "input" and attrs.get("type", "").lower() == "hidden")
    )


def _raw_text(element: _El) -> str:
    """The element's visible text, whitespace collapsed (no hidden or script content)."""
    parts: list[str] = []

    def walk(node: _El) -> None:
        for child in node.children:
            if isinstance(child, str):
                parts.append(child)
            elif child.tag == "br":
                parts.append(" ")
            elif child.tag not in NOT_TEXT and child.tag != "noscript" and not _hidden(child):
                walk(child)

    walk(element)
    return _SPACES.sub(" ", "".join(parts)).strip()


def _all_text(element: _El) -> str:
    """Every character under the element (hidden ones included), scripts and styles excluded."""
    return "".join(
        child if isinstance(child, str) else ("" if child.tag in ("script", "style") else _all_text(child)) for child in element.children
    )


def _letters(text: str) -> int:
    return len(_LETTER.findall(text))


def _int(value: str, default: int) -> int:
    match = re.match(r"\s*([+-]?\d+)", value or "")
    return int(match.group(1)) if match else default


# -- reading -------------------------------------------------------------------


@dataclass(frozen=True)
class _Ctx:
    role: str = "body"
    dir: str | None = None  # an explicit dir="rtl" / "ltr" (or CSS direction), inherited
    lang_rtl: bool | None = None
    pre: bool = False
    sectioned: bool = False  # inside article, section, main, aside or nav: header and footer are not the site's


def _child_ctx(element: _El, ctx: _Ctx) -> _Ctx:
    """The context an element gives its content: direction, language, role, preformatting."""
    attrs = element.attrs
    changes: dict = {}
    direction = attrs.get("dir", "").strip().lower()
    style_dir = _DIRECTION_STYLE.search(attrs.get("style", ""))
    if direction in ("rtl", "ltr"):
        changes["dir"] = direction
    elif style_dir:
        changes["dir"] = style_dir.group(1).lower()
    elif direction == "auto":
        changes["dir"] = None  # the first strong character decides, whatever the language
        changes["lang_rtl"] = None
    lang = (attrs.get("lang") or attrs.get("xml:lang") or "").strip().lower()
    if lang and direction != "auto":
        changes["lang_rtl"] = lang.split("-")[0] in _RTL_LANGS
    tag = element.tag
    aria = attrs.get("role", "").strip().lower()
    if tag == "nav" or aria in NAV_ARIA or (tag in ("header", "footer", "aside") and not ctx.sectioned):
        changes["role"] = "navigation"
    if tag in SECTIONING or aria == "main":
        changes["sectioned"] = True
    if tag in ("pre", "textarea", "listing", "xmp", "plaintext"):
        changes["pre"] = True
    return replace(ctx, **changes) if changes else ctx


@dataclass
class _Pending:
    ctx: _Ctx
    kind: str = "text"
    level: int | None = None
    label: str | None = None
    flags: list[str] = field(default_factory=list)


class _Reader:
    """State shared by every walker of one page."""

    def __init__(self, doc: Document) -> None:
        self.doc = doc
        self.repairs_on = doc.options.get("repairs", True)
        self.labels: dict[int, str] = {}  # id(li) -> its label
        self.last_heading = 0
        self.hidden_letters = 0
        self.noscript_letters = 0
        self.empty_headings = 0
        self.external_images = 0
        self.embedded = 0
        self.form_controls = 0

    def direction(self, ctx: _Ctx, text: str) -> str | None:
        if ctx.dir:
            return ctx.dir
        if ctx.lang_rtl is not None:
            return "rtl" if ctx.lang_rtl else "ltr"
        strong = _STRONG.search(text)
        if strong is None:
            return None
        return "rtl" if _RTL_CHAR.match(strong.group()) else "ltr"

    def block(self, kind: str, text: str, ctx: _Ctx, *, level=None, label=None, flags=()) -> Block:
        block_repairs: list[dict] = []
        if self.repairs_on:
            text, marks = repairs.direction_marks(text)
            block_repairs += repairs.record(repairs.DIRECTION_MARKS, marks)
            if label:
                label, marks = repairs.direction_marks(label)
                block_repairs += repairs.record(repairs.DIRECTION_MARKS, marks)
        return Block(
            kind=kind,
            text=text,
            role=ctx.role,
            source="html",
            tool=TOOL,
            level=level,
            label=label,
            direction=self.direction(ctx, text),
            repairs=block_repairs,
            flags=list(flags),
        )

    def margin_level(self) -> int:
        return min(6, max(self.last_heading, 1) + 1)


class _Walker:
    """Turns one subtree into blocks, in document order."""

    def __init__(self, reader: _Reader, ctx: _Ctx) -> None:
        self.r = reader
        self.blocks: list[Block] = []
        self.pieces: list[str] = []
        self.stack = [_Pending(ctx)]

    # -- flow ----------------------------------------------------------------
    def run(self, element: _El, ctx: _Ctx) -> list[Block]:
        self.children(element, ctx)
        self.flush()
        return _join_section_numbers(self.blocks)

    def children(self, element: _El, ctx: _Ctx) -> None:
        for child in element.children:
            if isinstance(child, str):
                self.pieces.append(child if ctx.pre else _SPACES.sub(" ", child))
            else:
                self.element(child, ctx)

    def flush(self) -> None:
        text = "".join(self.pieces)
        self.pieces = []
        pending = self.stack[-1]
        if pending.ctx.pre:
            text = text.strip("\n")
        else:
            text = re.sub(r" ?\n ?", "\n", re.sub(r" {2,}", " ", text)).strip(" \n")
        if not text.strip():
            return
        kind = pending.kind
        if kind == "heading" and not _LETTER.search(text):
            kind = "text"
        if kind == "list_item" and not pending.label:
            kind = "text"
        block = self.r.block(kind, text, pending.ctx, level=pending.level if kind == "heading" else None, label=pending.label, flags=pending.flags)
        if kind == "heading" and pending.ctx.role == "body":
            self.r.last_heading = pending.level or self.r.last_heading
        self.blocks.append(block)
        # Text after a nested block continues the item, without its label.
        pending.label = None
        if pending.kind == "list_item":
            pending.kind = "text"

    def element(self, element: _El, ctx: _Ctx) -> None:
        tag = element.tag
        if tag in NOT_TEXT:
            if tag in ("select", "datalist") and _raw_text(element):
                self.r.form_controls += 1
            return
        if tag == "noscript":
            self.r.noscript_letters += _letters(_all_text(element))
            return
        if tag in EMBEDDED or tag == "frameset":
            self.r.embedded += 1 + sum(1 for e in element.iter() if e is not element and e.tag in EMBEDDED)
            return
        if _hidden(element):
            self.r.hidden_letters += _letters(_all_text(element))
            return
        if tag == "br":
            self.pieces.append("\n")
            return
        ctx = _child_ctx(element, ctx)
        if tag == "img":
            self.image(element, ctx)
            return
        if tag == "a" and _skip_link(element):
            self.flush()
            self.stack.append(_Pending(replace(ctx, role="navigation")))
            self.children(element, self.stack[-1].ctx)
            self.flush()
            self.stack.pop()
            return
        if self.is_margin(element, ctx):
            text = _raw_text(element)
            self.flush()
            self.blocks.append(self.r.block("heading", text, ctx, level=self.r.margin_level(), flags=["margin_heading"]))
            return
        if tag == "table":
            self.flush()
            self.blocks.extend(_Table(self.r, element, ctx).blocks())
            return
        if tag in LISTS:
            self.flush()
            self.number_items(element)
            self.children(element, ctx)
            self.flush()
            return
        heading_level = int(tag[1]) if tag in HEADINGS else None
        if heading_level is None and element.attrs.get("role", "").lower() == "heading":
            heading_level = min(max(_int(element.attrs.get("aria-level", ""), 2), 1), 6)
        if tag in BLOCK or heading_level is not None:
            self.flush()
            parent = self.stack[-1]
            pending = _Pending(ctx)
            if heading_level is not None:
                pending.kind, pending.level = "heading", heading_level
            if tag == "li":
                pending.label = self.r.labels.get(id(element))
                pending.kind = "list_item"
            elif parent.label and tag not in ("hr",):
                # <li><p>text</p></li>: the item's label goes with its first paragraph.
                pending.label, parent.label = parent.label, None
                if pending.kind == "text":
                    pending.kind = "list_item"
            before = len(self.blocks)
            self.stack.append(pending)
            self.children(element, ctx)
            self.flush()
            self.stack.pop()
            if heading_level is not None and len(self.blocks) == before and not _letters(_raw_text(element)):
                self.r.empty_headings += 1
            return
        self.children(element, ctx)  # inline

    # -- pieces ----------------------------------------------------------------
    def is_margin(self, element: _El, ctx: _Ctx) -> bool:
        if ctx.role != "body" or element.tag in HEADINGS or element.tag in LISTS or element.tag in ("table", "li", "td", "th", "tr"):
            return False
        attrs = element.attrs
        if not (_MARGIN_CLASS.search(attrs.get("class", "")) or _FLOAT_STYLE.search(attrs.get("style", ""))):
            return False
        if any(e.tag in BLOCK or e.tag in HEADINGS or e.tag in LISTS or e.tag == "table" for e in element.iter() if e is not element):
            return False
        text = _raw_text(element)
        return bool(_LETTER.search(text)) and len(text) <= MARGIN_MAX_CHARS and len(text.split()) <= MARGIN_MAX_WORDS

    def number_items(self, element: _El) -> None:
        items = [c for c in element.children if isinstance(c, _El) and c.tag == "li"]
        ordered = element.tag == "ol"
        style = _list_style(element)
        if style is None:
            type_attr = element.attrs.get("type", "")
            style = {"1": "decimal", "a": "lower-alpha", "A": "upper-alpha", "i": "lower-roman", "I": "upper-roman"}.get(type_attr)
            if style is None:
                style = type_attr.lower() if type_attr.lower() in ("disc", "circle", "square") else ("decimal" if ordered else "disc")
        reverse = "reversed" in element.attrs
        value = _int(element.attrs.get("start", ""), len(items) if reverse else 1)
        for item in items:
            if "value" in item.attrs:
                value = _int(item.attrs["value"], value)
            item_style = _list_style(item) or style
            label = _list_label(value, item_style)
            if label:
                self.r.labels[id(item)] = label
            value += -1 if reverse else 1

    def image(self, element: _El, ctx: _Ctx) -> None:
        src = element.attrs.get("src", "").strip()
        if not src.lower().startswith("data:"):
            self.r.external_images += 1
            return
        self.flush()
        size = _data_uri_size(src)
        if size > MAX_DATA_URI_BYTES:
            self.r.doc.unread.append(Unread(scope="block", reason="image_too_large", detail=f"a data: URI image of about {size} bytes; the limit is {MAX_DATA_URI_BYTES}"))
            self.blocks.append(Block(kind="figure", role=ctx.role, source="html", tool=TOOL, flags=["image_too_large"]))
            return
        data = _data_uri(src)
        if data is None:
            self.r.doc.unread.append(Unread(scope="block", reason="image_not_read", detail="a data: URI that does not decode"))
            self.blocks.append(Block(kind="figure", role=ctx.role, source="html", tool=TOOL, flags=["image_not_read"]))
            return
        from heocr.docx.reader import image_blocks

        self.blocks.extend(image_blocks(self.r.doc, data, ctx.role, None, source="html", tool=TOOL))


def _skip_link(element: _El) -> bool:
    href = element.attrs.get("href", "").strip()
    if not href.startswith("#"):
        return False
    return "skip" in element.attrs.get("class", "").lower() or bool(_SKIP_LINK.search(_raw_text(element)))


def _list_style(element: _El) -> str | None:
    match = _LIST_STYLE.search(element.attrs.get("style", ""))
    return match.group(1).lower() if match else None


def _list_label(value: int, style: str) -> str | None:
    bullets = {"disc": "•", "circle": "◦", "square": "▪"}
    if style in bullets:
        return bullets[style]
    if style == "none":
        return None
    numbers = {
        "decimal": str,
        "decimal-leading-zero": lambda v: f"{v:02d}" if 0 <= v < 10 else str(v),
        "lower-alpha": latin_letter,
        "lower-latin": latin_letter,
        "upper-alpha": lambda v: latin_letter(v).upper(),
        "upper-latin": lambda v: latin_letter(v).upper(),
        "lower-roman": lambda v: roman(v).lower(),
        "upper-roman": roman,
        "hebrew": hebrew_numeral,
    }
    render = numbers.get(style, str)
    text = render(value) if value > 0 else str(value)
    return f"{text}." if text else None


def _data_uri_size(src: str) -> int:
    """The decoded size of a data: URI, estimated from its length without decoding it."""
    head, _, payload = src.partition(",")
    if head.lower().endswith(";base64"):
        return len(payload) * 3 // 4
    return len(payload) - 2 * payload.count("%")


def _data_uri(src: str) -> bytes | None:
    head, _, payload = src.partition(",")
    try:
        if head.lower().endswith(";base64"):
            return base64.b64decode(re.sub(r"\s+", "", payload), validate=True)
        return unquote_to_bytes(payload)
    except (binascii.Error, ValueError):
        return None


class _Table:
    """A table: a data table with its spans, a layout table read as its flow, or legislation's sections."""

    def __init__(self, reader: _Reader, element: _El, ctx: _Ctx) -> None:
        self.r = reader
        self.el = element
        self.ctx = ctx
        self.caption: _El | None = None
        self.rows: list[list[_El]] = []
        # Content outside any cell (misnested, or flattened beyond MAX_DEPTH): read before the
        # table, as browsers place it ("foster parenting"), never dropped.
        self.outside = _El("div", {}, element)
        self._rows(element, top=True)

    def _rows(self, group: _El, top: bool) -> None:
        stray: list[_El] = []  # cells outside any row: one implicit row, as browsers make
        for child in group.children:
            if isinstance(child, _El) and child.tag in ("td", "th"):
                if not _hidden(child):
                    stray.append(child)
                continue
            if stray:
                self.rows.append(stray)
                stray = []
            if isinstance(child, str) or child.tag not in TABLE_TAGS:
                self.outside.children.append(child)
            elif _hidden(child):
                continue
            elif child.tag == "tr":
                self.rows.append(self._cells(child))
            elif top and child.tag in ("thead", "tbody", "tfoot"):
                self._rows(child, top=False)
            elif top and child.tag == "caption" and self.caption is None:
                self.caption = child
            elif child.tag in ("table", "caption", "thead", "tbody", "tfoot"):
                self.outside.children.append(child)  # misnested: read as flow, not dropped
        if stray:
            self.rows.append(stray)

    def _cells(self, tr: _El) -> list[_El]:
        cells = []
        for child in tr.children:
            if isinstance(child, str) or child.tag not in TABLE_TAGS:
                self.outside.children.append(child)
            elif child.tag in ("td", "th"):
                if not _hidden(child):
                    cells.append(child)
            elif child.tag not in ("col", "colgroup"):
                self.outside.children.append(child)  # misnested (a table inside a row): read as flow
        return cells

    def flow(self, element: _El, ctx: _Ctx | None = None) -> list[Block]:
        ctx = ctx or self.ctx
        ctx = _child_ctx(element, ctx)
        return _Walker(self.r, ctx).run(element, ctx)

    def blocks(self) -> list[Block]:
        out = self.flow(self.caption) if self.caption is not None else []
        for block in out:
            block.flags.append("table_caption")
        outside = self.flow(self.outside) if self.outside.children else []
        for block in outside:
            block.flags.append("outside_table_cells")
        out += outside
        if not self.rows:
            return out
        texts = [[_raw_text(c) for c in row] for row in self.rows]
        if self._legislation(texts):
            self.r.doc.flags.append("legislation_layout_table")
            return out + self._sections(texts)
        if self._layout(texts):
            self.r.doc.flags.append("layout_table_read_as_flow")
            for row in self.rows:
                for cell in row:
                    for block in self.flow(cell):
                        block.flags.append("from_layout_table")
                        out.append(block)
            return _join_section_numbers(out)
        return out + [self._grid()]

    def _layout(self, texts: list[list[str]]) -> bool:
        if self.el.attrs.get("role", "").lower() in ("presentation", "none"):
            return True
        if all(len(row) <= 1 for row in self.rows):
            return True
        return any(e.tag in HEADINGS or e.tag == "table" for row in self.rows for cell in row for e in cell.iter() if e is not cell)

    @staticmethod
    def _section_row(row: list[str]) -> bool:
        numbers = [i for i, t in enumerate(row) if SECTION_NUMBER.fullmatch(t)]
        return bool(numbers) and any(_letters(t) >= SECTION_MIN_LETTERS for i, t in enumerate(row) if i != numbers[0])

    def _legislation(self, texts: list[list[str]]) -> bool:
        sections = sum(1 for row in texts if self._section_row(row))
        with_text = sum(1 for row in texts if any(_letters(t) for t in row))
        return sections >= 2 and sections * 2 >= with_text

    def _sections(self, texts: list[list[str]]) -> list[Block]:
        out: list[Block] = []
        for row, row_texts in zip(self.rows, texts):
            number = next((i for i, t in enumerate(row_texts) if SECTION_NUMBER.fullmatch(t)), None) if self._section_row(row_texts) else None
            candidates = [i for i in range(len(row)) if i != number]
            main = max(candidates, key=lambda i: _letters(row_texts[i]), default=None)
            has_main = main is not None and _letters(row_texts[main]) >= SECTION_MIN_LETTERS
            for i, cell in enumerate(row):
                text = row_texts[i]
                if i == number or not text:
                    continue
                if has_main and i != main and _letters(text) and len(text) <= MARGIN_MAX_CHARS and len(text.split()) <= MARGIN_MAX_WORDS:
                    ctx = _child_ctx(cell, self.ctx)
                    out.append(self.r.block("heading", text, ctx, level=self.r.margin_level(), flags=["margin_heading", "from_layout_table"]))
            if number is not None and main is not None:
                section = self.flow(row[main])
                first = next((b for b in section if b.kind in ("text", "list_item") and not b.label), None)
                if first is not None:
                    first.label = row_texts[number]
                    first.kind = "list_item"
                    first.flags.append("section_number_joined")
                else:
                    section.insert(0, self.r.block("text", row_texts[number], self.ctx))
                out += self._tagged(section)
            elif number is not None:
                out += self._tagged(self.flow(row[number]))
            for i, cell in enumerate(row):
                if i in (number, main if number is not None else None):
                    continue
                if has_main and i != main and _letters(row_texts[i]) and len(row_texts[i]) <= MARGIN_MAX_CHARS and len(row_texts[i].split()) <= MARGIN_MAX_WORDS:
                    continue  # read above, as the margin heading
                out += self._tagged(self.flow(cell))
        return _join_section_numbers(out)

    @staticmethod
    def _tagged(blocks: list[Block]) -> list[Block]:
        for block in blocks:
            block.flags.append("from_layout_table")
        return blocks

    def _cell(self, cell: _El, table_repairs: Counter) -> tuple[str, list[str]]:
        lines: list[str] = []
        flags: list[str] = []
        for block in self.flow(cell):
            for entry in block.repairs:
                table_repairs[(entry["rule"], entry["from"], entry["to"])] += entry["count"]
            if block.kind == "table":
                flags.append("nested_table")
                lines.extend(" | ".join(c.text.replace("\n", " ") for c in row) for row in block.cells or [])
            elif block.kind == "figure":
                flags.append("image_not_read")
            else:
                lines.append(f"{block.label} {block.text}" if block.label else block.text)
        return "\n".join(lines), flags

    def _grid(self) -> Block:
        grid: list[list[Cell]] = []
        carry: dict[int, list[int]] = {}  # column -> [rows still covered, colspan]
        table_repairs: Counter = Counter()
        total = len(self.rows)
        for r, row in enumerate(self.rows):
            out: list[Cell] = []
            column = 0

            def covered() -> None:
                nonlocal column
                while column in carry:
                    out.append(Cell("", colspan=carry[column][1], flags=["merged_above"]))
                    column += carry[column][1]

            started: list[int] = []
            for cell in row:
                covered()
                text, flags = self._cell(cell, table_repairs)
                colspan = cap_span(_int(cell.attrs.get("colspan", ""), 1), flags)
                rowspan = _int(cell.attrs.get("rowspan", ""), 1)
                rowspan = total - r if rowspan == 0 else min(max(rowspan, 1), total - r)
                out.append(Cell(text, colspan=colspan, rowspan=rowspan, flags=flags))
                if rowspan > 1:
                    started.append(column)
                    carry[column] = [rowspan, colspan]
                column += colspan
            for col in sorted(c for c in carry if c >= column and c not in started):
                out.append(Cell("", colspan=carry[col][1], flags=["merged_above"]))
            for col in list(carry):
                carry[col][0] -= 1
                if carry[col][0] <= 0:
                    del carry[col]
            grid.append(out)
        all_text = " ".join(c.text for row in grid for c in row)
        return Block(
            kind="table",
            cells=grid,
            role=self.ctx.role,
            source="html",
            tool=TOOL,
            direction=self.r.direction(self.ctx, all_text),
            repairs=[{"rule": rule, "from": a, "to": b, "count": n} for (rule, a, b), n in sorted(table_repairs.items())],
        )


def _join_section_numbers(blocks: list[Block]) -> list[Block]:
    """A section number on its own line joins the text after it, as its label (legislation).

    Margin headings between the two move before the section: [12.][heading][text] reads as
    [heading][12. text].
    """
    out: list[Block] = []
    i = 0
    while i < len(blocks):
        block = blocks[i]
        if block.kind == "text" and not block.label and SECTION_NUMBER.fullmatch(block.text.strip()):
            j = i + 1
            while j < len(blocks) and blocks[j].kind == "heading" and "margin_heading" in blocks[j].flags and blocks[j].role == block.role:
                j += 1
            if j < len(blocks):
                section = blocks[j]
                if section.kind == "text" and not section.label and section.role == block.role and not SECTION_NUMBER.fullmatch(section.text.strip()):
                    section.label = block.text.strip()
                    section.kind = "list_item"
                    section.repairs = block.repairs + section.repairs
                    section.flags.append("section_number_joined")
                    out.extend(blocks[i + 1 : j])
                    out.append(section)
                    i = j + 1
                    continue
        out.append(block)
        i += 1
    return out


def _scripts(root: _El) -> int:
    count = 0
    for element in root.iter():
        if element.tag != "script":
            continue
        kind = element.attrs.get("type", "").strip().lower()
        if kind in ("", "module", "text/javascript", "application/javascript", "text/ecmascript", "application/ecmascript"):
            count += 1
    return count


def _body_letters(blocks: list[Block]) -> int:
    total = 0
    for block in blocks:
        if block.role == "navigation":
            continue
        if block.kind == "table":
            total += sum(_letters(c.text) for row in block.cells or [] for c in row)
        else:
            total += _letters(block.text)
    return total


def read_html(body: bytes, doc: Document) -> None:
    """Fill ``doc`` with the blocks of an HTML page."""
    text, evidence, flags = decode(body)
    doc.format_evidence = f"{doc.format_evidence}; {evidence}"
    doc.flags.extend(flags)
    if len(text) > MAX_HTML_CHARS:
        doc.unread.append(Unread(scope="document", reason="html_too_large", detail=f"{len(text)} characters; the limit is {MAX_HTML_CHARS}"))
        doc.flags.append("not_read")
        return
    builder = _Builder()
    try:
        builder.feed(text)
        builder.close()
    except _Truncated:
        doc.unread.append(Unread(scope="document", reason="html_truncated", detail=f"more than {MAX_ELEMENTS} elements; the rest of the page was not read"))
        doc.flags.append("html_truncated")
    except (AssertionError, ValueError) as exc:  # older parsers raise on some malformed declarations
        doc.unread.append(Unread(scope="document", reason="html_unreadable", detail=f"{type(exc).__name__}: {exc}"[:300]))
        doc.flags.append("not_read")
        return
    if builder.deep:
        doc.flags.append("html_nesting_flattened")
    reader = _Reader(doc)
    blocks = _Walker(reader, _Ctx()).run(builder.root, _Ctx())
    doc.blocks.extend(blocks)

    if reader.hidden_letters:
        doc.flags.append("hidden_text_dropped")
    if reader.noscript_letters:
        doc.flags.append("noscript_not_read")
    if reader.empty_headings:
        doc.flags.append("empty_headings_skipped")
    if reader.form_controls:
        doc.flags.append("form_controls_not_read")
    if reader.external_images:
        doc.unread.append(Unread(scope="document", reason="image_not_read", detail=f"{reader.external_images} images outside the page (not fetched)"))
        doc.flags.append("html_images_not_fetched")
    if reader.embedded:
        doc.unread.append(Unread(scope="document", reason="embedded_content_not_read", detail=f"{reader.embedded} frames or embedded objects (not fetched)"))
    scripts = _scripts(builder.root)
    letters = _body_letters(blocks)
    if scripts and letters < SCRIPT_MIN_LETTERS:
        doc.unread.append(
            Unread(
                scope="document",
                reason="html_rendered_by_script",
                detail=f"{letters} letters outside navigation and {scripts} script elements: the content is filled in by script; print the page to PDF from a browser",
            )
        )
        doc.flags.append("html_rendered_by_script")
        if not letters:
            doc.flags.append("not_read")
    elif not blocks:
        doc.flags.append("no_text")
