"""Synthetic HTML pages for the HTML route (D4f). No real page's text is used.

``nevo_like_flow`` and ``nevo_like_table`` imitate the structure the volunteer described in the
Nevo copy of the tender law (each section number on its own line; the section's margin heading
set beside its text), in the two ways such pages are commonly laid out: Word's "save as web
page" paragraphs with a floated margin heading, and a layout table with one section per row.
The sentences are invented. ``script_filled_skeleton`` imitates a gov.il page saved without its scripts
having run: a frame of menus, and an empty mount point.
"""

from __future__ import annotations

# Invented legislation: section numbers, margin headings and texts.
SECTIONS = [
    ("1.", "הגדרות", 'בחוק זה, "גוף ציבורי" - משרד ממשלתי, רשות מקומית או תאגיד שהוקם בחוק.'),
    ("2.", "חובת פרסום", "גוף ציבורי יפרסם הודעה על כוונתו להתקשר בחוזה, במקום ובמועד שנקבעו."),
    ("2א.", "פטור", "השר רשאי לקבוע בתקנות סוגי התקשרויות שאינן טעונות פרסום, ובלבד שנימק זאת."),
    ("3.", "ועדת מכרזים", "בכל גוף ציבורי תמונה ועדה שתדון בהצעות ותחליט על ההצעה הזוכה."),
]
TITLE = "חוק ההתקשרויות לדוגמה, התשפ\"ז-2026"


def _page(body: str, *, charset: str = "utf-8", head: str = "", html_attrs: str = 'lang="he" dir="rtl"') -> str:
    return (
        "<!DOCTYPE html>\n"
        f"<html {html_attrs}><head><meta http-equiv=\"Content-Type\" content=\"text/html; charset={charset}\">"
        f"<title>{TITLE}</title>{head}</head>\n<body>{body}</body></html>"
    )


def nevo_like_flow(*, charset: str = "windows-1255") -> bytes:
    """Word-exported paragraphs: the number alone, the margin heading floated beside the text."""
    parts = [f'<p class="MsoTitle" dir="RTL"><span lang="HE">{TITLE}</span></p>']
    for number, margin, text in SECTIONS:
        parts.append(f'<p class="MsoNormal" dir="RTL"><span lang="HE">{number}</span></p>')
        parts.append(
            f'<p class="MsoNormal" dir="RTL"><span class="hearatShulaim" style="float:right;width:90px">{margin}</span>'
            f'<span lang="HE">{text}</span></p>'
        )
        # What both tools turned into an empty "######": a heading element without text.
        parts.append('<h6><a name="seif"></a></h6>')
    return _page("\n".join(parts), charset=charset).encode(charset)


def nevo_like_table(*, charset: str = "windows-1255") -> bytes:
    """A layout table: per row, the margin heading, the section number and the section's text."""
    rows = "\n".join(
        f'<tr><td class="margin" width="15%"><span lang="HE">{margin}</span></td>'
        f'<td width="5%"><span lang="HE">{number}</span></td>'
        f'<td><p class="MsoNormal" dir="RTL">{text}</p></td></tr>'
        for number, margin, text in SECTIONS
    )
    body = f'<p class="MsoTitle" dir="RTL">{TITLE}</p>\n<table dir="rtl" border="0">{rows}</table>'
    return _page(body, charset=charset).encode(charset)


def script_filled_skeleton() -> bytes:
    """A page whose content is filled in by script: menus, an empty mount point and scripts."""
    head = '<script src="/runtime.js"></script><script src="/main.js" type="module"></script>'
    body = (
        '<a class="skip-link" href="#main-content">דלג לתוכן העמוד</a>'
        '<header><nav aria-label="ראשי"><ul><li><a href="/">דף הבית</a></li><li><a href="/he/departments">משרדים</a></li></ul></nav></header>'
        '<div id="main-content"><app-root></app-root></div>'
        "<noscript>כדי להציג את העמוד יש לאפשר JavaScript</noscript>"
        '<footer><p>כל הזכויות שמורות</p></footer><script>window.boot()</script>'
    )
    return _page(body, head=head).encode("utf-8")


def page(body: str, **kwargs) -> bytes:
    charset = kwargs.get("charset", "utf-8")
    return _page(body, **kwargs).encode(charset)
