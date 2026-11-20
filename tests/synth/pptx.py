"""Synthetic PPTX decks, written part by part (no presentation library). Invented text only."""

from __future__ import annotations

import io
import zipfile
from dataclasses import dataclass, field
from xml.sax.saxutils import escape

NS = (
    'xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
    'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
    'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
)
REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/"
EMU = 12700  # per point


def para(text: str, *, rtl: bool | None = True, lvl: int = 0, bullet: str | None = None, autonum: str | None = None, start: int | None = None) -> str:
    attrs = (f' rtl="{1 if rtl else 0}"' if rtl is not None else "") + (f' lvl="{lvl}"' if lvl else "")
    inner = ""
    if bullet is not None:
        inner += f'<a:buChar char="{escape(bullet)}"/>'
    if autonum is not None:
        inner += f'<a:buAutoNum type="{autonum}"' + (f' startAt="{start}"' if start else "") + "/>"
    runs = "".join(f"<a:r><a:t>{escape(piece)}</a:t></a:r>" if i == 0 else f"<a:br/><a:r><a:t>{escape(piece)}</a:t></a:r>" for i, piece in enumerate(text.split("\n")))
    return f"<a:p><a:pPr{attrs}>{inner}</a:pPr>{runs}</a:p>"


def _xfrm(box) -> str:
    if box is None:
        return ""
    x, y, w, h = (round(v * EMU) for v in box)
    return f'<a:xfrm><a:off x="{x}" y="{y}"/><a:ext cx="{w}" cy="{h}"/></a:xfrm>'


_ids = iter(range(2, 10**6))


def shape(paras: list[str], box=None, *, ph: str | None = None, idx: int | None = None, hidden: bool = False) -> str:
    """A text shape; ``box`` is (x, y, width, height) in points. ``ph`` makes it a placeholder."""
    placeholder = ""
    if ph is not None or idx is not None:
        placeholder = "<p:ph" + (f' type="{ph}"' if ph else "") + (f' idx="{idx}"' if idx is not None else "") + "/>"
    n = next(_ids)
    hide = ' hidden="1"' if hidden else ""
    return (
        f'<p:sp><p:nvSpPr><p:cNvPr id="{n}" name="s{n}"{hide}/><p:cNvSpPr/><p:nvPr>{placeholder}</p:nvPr></p:nvSpPr>'
        f"<p:spPr>{_xfrm(box)}</p:spPr><p:txBody><a:bodyPr/>{''.join(paras)}</p:txBody></p:sp>"
    )


def group(children: list[str], box, child_box) -> str:
    x, y, w, h = (round(v * EMU) for v in box)
    cx, cy, cw, ch = (round(v * EMU) for v in child_box)
    n = next(_ids)
    return (
        f'<p:grpSp><p:nvGrpSpPr><p:cNvPr id="{n}" name="g{n}"/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>'
        f'<p:grpSpPr><a:xfrm><a:off x="{x}" y="{y}"/><a:ext cx="{w}" cy="{h}"/><a:chOff x="{cx}" y="{cy}"/><a:chExt cx="{cw}" cy="{ch}"/></a:xfrm></p:grpSpPr>'
        + "".join(children)
        + "</p:grpSp>"
    )


def table(rows: list[list], box=(50, 200, 600, 200), *, rtl: bool = True) -> str:
    """Rows of cells; a cell is text, or (text, {"gridSpan": 2, "rowSpan": 2, "hMerge": 1, "vMerge": 1})."""
    n = next(_ids)
    width = max(len(r) for r in rows)
    grid = "".join('<a:gridCol w="1000000"/>' for _ in range(width))
    body = ""
    for r in rows:
        body += '<a:tr h="370840">'
        for cell in r:
            text, attrs = (cell, {}) if isinstance(cell, str) else cell
            attr = "".join(f' {k}="{v}"' for k, v in attrs.items())
            body += f"<a:tc{attr}><a:txBody><a:bodyPr/>{para(text, rtl=rtl)}</a:txBody></a:tc>"
        body += "</a:tr>"
    x, y, w, h = (round(v * EMU) for v in box)
    return (
        f'<p:graphicFrame><p:nvGraphicFramePr><p:cNvPr id="{n}" name="t{n}"/><p:cNvGraphicFramePr/><p:nvPr/></p:nvGraphicFramePr>'
        f'<p:xfrm><a:off x="{x}" y="{y}"/><a:ext cx="{w}" cy="{h}"/></p:xfrm>'
        f'<a:graphic><a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/table"><a:tbl><a:tblPr rtl="{1 if rtl else 0}"/>'
        f"<a:tblGrid>{grid}</a:tblGrid>{body}</a:tbl></a:graphicData></a:graphic></p:graphicFrame>"
    )


def chart(box=(50, 50, 200, 200)) -> str:
    n = next(_ids)
    x, y, w, h = (round(v * EMU) for v in box)
    return (
        f'<p:graphicFrame><p:nvGraphicFramePr><p:cNvPr id="{n}" name="c{n}"/><p:cNvGraphicFramePr/><p:nvPr/></p:nvGraphicFramePr>'
        f'<p:xfrm><a:off x="{x}" y="{y}"/><a:ext cx="{w}" cy="{h}"/></p:xfrm>'
        '<a:graphic><a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/chart"><c:chart xmlns:c="http://schemas.openxmlformats.org/drawingml/2006/chart" r:id="rIdC"/></a:graphicData></a:graphic></p:graphicFrame>'
    )


def picture(rid: str, box=(50, 300, 300, 100)) -> str:
    n = next(_ids)
    return (
        f'<p:pic><p:nvPicPr><p:cNvPr id="{n}" name="p{n}"/><p:cNvPicPr/><p:nvPr/></p:nvPicPr>'
        f'<p:blipFill><a:blip r:embed="{rid}"/></p:blipFill><p:spPr>{_xfrm(box)}</p:spPr></p:pic>'
    )


def smartart(rid: str = "rIdDm", box=(50, 50, 400, 200)) -> str:
    n = next(_ids)
    x, y, w, h = (round(v * EMU) for v in box)
    return (
        f'<p:graphicFrame><p:nvGraphicFramePr><p:cNvPr id="{n}" name="d{n}"/><p:cNvGraphicFramePr/><p:nvPr/></p:nvGraphicFramePr>'
        f'<p:xfrm><a:off x="{x}" y="{y}"/><a:ext cx="{w}" cy="{h}"/></p:xfrm>'
        '<a:graphic><a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/diagram">'
        f'<dgm:relIds xmlns:dgm="http://schemas.openxmlformats.org/drawingml/2006/diagram" r:dm="{rid}" r:lo="rIdLo" r:qs="rIdQs" r:cs="rIdCs"/>'
        "</a:graphicData></a:graphic></p:graphicFrame>"
    )


@dataclass
class Slide:
    shapes: list[str] = field(default_factory=list)
    notes: list[str] | None = None  # paragraphs of the speaker notes
    hidden: bool = False
    images: dict[str, bytes] = field(default_factory=dict)  # rId -> image bytes
    smartart: list[str] | None = None  # node texts of a SmartArt data part (rIdDm)


@dataclass
class Deck:
    slides: list[Slide] = field(default_factory=list)
    rtl: bool | None = None
    layout_shapes: list[str] = field(default_factory=list)  # placeholders with positions, in the one layout
    order: list[int] | None = None  # the slide list's order of the slide parts (default: as given)

    def build(self) -> bytes:
        out = io.BytesIO()
        ct = "application/vnd.openxmlformats-officedocument.presentationml"
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
            overrides = [("/ppt/presentation.xml", f"{ct}.presentation.main+xml"), ("/ppt/slideLayouts/slideLayout1.xml", f"{ct}.slideLayout+xml"), ("/ppt/slideMasters/slideMaster1.xml", f"{ct}.slideMaster+xml")]
            overrides += [(f"/ppt/slides/slide{i}.xml", f"{ct}.slide+xml") for i in range(1, len(self.slides) + 1)]
            overrides += [(f"/ppt/notesSlides/notesSlide{i}.xml", f"{ct}.notesSlide+xml") for i, s in enumerate(self.slides, 1) if s.notes]
            z.writestr(
                "[Content_Types].xml",
                '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                '<Default Extension="xml" ContentType="application/xml"/><Default Extension="png" ContentType="image/png"/>'
                + "".join(f'<Override PartName="{n}" ContentType="{k}"/>' for n, k in overrides)
                + "</Types>",
            )
            z.writestr("_rels/.rels", f'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="{REL}officeDocument" Target="ppt/presentation.xml"/></Relationships>')
            rels = [f'<Relationship Id="rIdM" Type="{REL}slideMaster" Target="slideMasters/slideMaster1.xml"/>']
            ids = []
            order = self.order or list(range(1, len(self.slides) + 1))
            for n in order:
                rels.append(f'<Relationship Id="rId{n}" Type="{REL}slide" Target="slides/slide{n}.xml"/>')
                ids.append(f'<p:sldId id="{255 + n}" r:id="rId{n}"/>')
            rtl = f' rtl="{1 if self.rtl else 0}"' if self.rtl is not None else ""
            z.writestr("ppt/presentation.xml", f"<p:presentation {NS}{rtl}><p:sldMasterIdLst><p:sldMasterId id=\"2147483648\" r:id=\"rIdM\"/></p:sldMasterIdLst><p:sldIdLst>{''.join(ids)}</p:sldIdLst><p:sldSz cx=\"9144000\" cy=\"6858000\"/></p:presentation>")
            z.writestr("ppt/_rels/presentation.xml.rels", '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">' + "".join(rels) + "</Relationships>")
            tree = '<p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr><p:grpSpPr/>'
            z.writestr("ppt/slideMasters/slideMaster1.xml", f"<p:sldMaster {NS}><p:cSld><p:spTree>{tree}</p:spTree></p:cSld></p:sldMaster>")
            z.writestr("ppt/slideLayouts/slideLayout1.xml", f"<p:sldLayout {NS}><p:cSld><p:spTree>{tree}{''.join(self.layout_shapes)}</p:spTree></p:cSld></p:sldLayout>")
            z.writestr("ppt/slideLayouts/_rels/slideLayout1.xml.rels", f'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="{REL}slideMaster" Target="../slideMasters/slideMaster1.xml"/></Relationships>')
            for i, slide in enumerate(self.slides, 1):
                show = ' show="0"' if slide.hidden else ""
                z.writestr(f"ppt/slides/slide{i}.xml", f"<p:sld {NS}{show}><p:cSld><p:spTree>{tree}{''.join(slide.shapes)}</p:spTree></p:cSld></p:sld>")
                srels = [f'<Relationship Id="rIdL" Type="{REL}slideLayout" Target="../slideLayouts/slideLayout1.xml"/>']
                if slide.notes:
                    srels.append(f'<Relationship Id="rIdN" Type="{REL}notesSlide" Target="../notesSlides/notesSlide{i}.xml"/>')
                    body = shape(slide.notes, ph="body", idx=1)
                    number = shape([para("7", rtl=None)], ph="sldNum", idx=2)
                    image = '<p:sp><p:nvSpPr><p:cNvPr id="90" name="img"/><p:cNvSpPr/><p:nvPr><p:ph type="sldImg"/></p:nvPr></p:nvSpPr><p:spPr/></p:sp>'
                    z.writestr(f"ppt/notesSlides/notesSlide{i}.xml", f"<p:notes {NS}><p:cSld><p:spTree>{tree}{image}{body}{number}</p:spTree></p:cSld></p:notes>")
                for rid, data in slide.images.items():
                    srels.append(f'<Relationship Id="{rid}" Type="{REL}image" Target="../media/s{i}{rid}.png"/>')
                    z.writestr(f"ppt/media/s{i}{rid}.png", data)
                if slide.smartart is not None:
                    srels.append(f'<Relationship Id="rIdDm" Type="{REL}diagramData" Target="../diagrams/data{i}.xml"/>')
                    points = "".join(
                        f'<dgm:pt modelId="{k}"><dgm:t><a:bodyPr/>{para(text)}</dgm:t></dgm:pt>' for k, text in enumerate(slide.smartart, 1)
                    )
                    z.writestr(f"ppt/diagrams/data{i}.xml", f'<dgm:dataModel xmlns:dgm="http://schemas.openxmlformats.org/drawingml/2006/diagram" {NS}><dgm:ptLst><dgm:pt modelId="0" type="doc"/>{points}</dgm:ptLst></dgm:dataModel>')
                z.writestr(f"ppt/slides/_rels/slide{i}.xml.rels", '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">' + "".join(srels) + "</Relationships>")
        return out.getvalue()
