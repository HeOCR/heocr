"""The ``heocr`` command."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from heocr import __version__
from heocr.api import DEFAULT_MODEL, PHOTO_PREPROCESS_MODES, extract
from heocr.contract import MAX_PAGES
from heocr.handwriting import HandwritingConfig
from heocr.ocr.phone_profile import PHONE_PROFILES
from heocr.ocr.routes import (
    CAPTURE_MODELS, DEFAULT_DOMAIN, DOMAINS, FAST_MIX_PROFILES, FAST_MIX_TABLE_PROFILES, candidates,
)
from heocr.ocr.specialists import CATEGORIES
from heocr.ocr.tesseract import DEFAULT_LANG

_FORMATS = ("text", "markdown", "json", "docx", "xlsx", "csv", "forms")
_BY_SUFFIX = {".md": "markdown", ".markdown": "markdown", ".json": "json", ".txt": "text", ".docx": "docx", ".xlsx": "xlsx", ".csv": "csv"}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="heocr",
        description="Extract Hebrew text from documents.",
        epilog="For local release engineering QA, use heocr regress --help.",
    )
    parser.add_argument("file", type=Path, nargs="?", help="input document: PDF, DOCX, DOC, RTF, ODT, XLSX, ODS, XLS, PPTX, ODP, PPT, HTML or an image")
    parser.add_argument("-o", "--output", type=Path, help="write here instead of standard output")
    parser.add_argument(
        "-f",
        "--format",
        choices=_FORMATS,
        help="output format: text, markdown, json, forms (hdoc_form.v1 JSON), docx (a Word file), xlsx (the tables, a sheet each) or csv (the tables: one file each with -o NAME.csv as NAME-1.csv, NAME-2.csv..., else separated by an empty line) (default: from the output suffix, else text)",
    )
    parser.add_argument(
        "--no-repairs",
        action="store_true",
        help="turn every repair rule off: output the extracted text byte for byte",
    )
    parser.add_argument("--no-ocr", action="store_true", help="do not run OCR; pages that need it are reported unread")
    parser.add_argument("--force-ocr", action="store_true", help="read every PDF page by OCR, ignoring its text layer (a workaround for a text layer heocr reads badly)")
    parser.add_argument("--document-roles", action="store_true",
                        help="tag supported page headers, footnotes and footers with pagewise JSON order")
    parser.add_argument("--ocr-line-v6", metavar="MODEL", help="reread Rivok Full prose lines with the pinned local v6 model")
    parser.add_argument("--ocr-line-batch", action="store_true", help="batch Rivok and v6 prose crops within each page (needs --ocr-line-v6)")
    parser.add_argument("--ocr-number-batch", action="store_true", help="batch full-page OCR number-word rereads")
    parser.add_argument("--crnn-model", metavar="MODEL", help="read OCR page prose with a local CPU ONNX line model")
    parser.add_argument("--crnn-sha256", metavar="HEX", help="expected SHA-256 of the ONNX model")
    parser.add_argument("--handwriting", metavar="MODEL", help="opt into Kraken on scan pages with a local .safetensors recognizer")
    parser.add_argument("--handwriting-sha256", metavar="HEX", help="expected SHA-256 of the handwriting recognizer")
    parser.add_argument("--handwriting-mode", choices=("auto", "force"), help="auto gates scan pages, force declares them handwritten")
    parser.add_argument("--handwriting-detector", choices=("tags", "geometry"), help="automatic handwriting gate (default: tags)")
    parser.add_argument("--handwriting-segmenter", metavar="MODEL", help="local .safetensors segmentation model, required for the tags detector")
    parser.add_argument("--handwriting-segmenter-sha256", metavar="HEX", help="expected SHA-256 of the supplied segmenter")
    parser.add_argument(
        "--check-numbers",
        choices=("suspect", "tables", "all", "off"),
        default="suspect",
        help="read numbers a second time by OCR: suspect digit groups only (default), also every table cell, all amounts and dates, or off",
    )
    parser.add_argument("--form-fields", action="store_true",
                        help="include supported label/value associations in JSON block checks")
    parser.add_argument("--form-scan-marks", action="store_true",
                        help="verify printed X marks on scanned forms for -f forms")
    parser.add_argument("--form-grid-recovery", action="store_true",
                        help="recover corroborated grid fields from OCR for -f forms")
    parser.add_argument("--join-form-pages", action="store_true",
                        help="join explicitly continued sections across adjacent pages for -f forms")
    parser.add_argument("--no-convert", action="store_true", help="do not convert DOC, RTF, ODT, ODS, XLS, ODP or PPT with LibreOffice; they are reported unread")
    parser.add_argument("--soffice", help="the LibreOffice program (soffice), when it is not on the PATH")
    parser.add_argument("--convert-timeout", type=float, metavar="SECONDS", help="time limit for one LibreOffice conversion (default: 180)")
    parser.add_argument("--max-pages", type=int, default=MAX_PAGES, metavar="N", help=f"read at most N pages or image frames (default: {MAX_PAGES}); the rest are reported unread")
    parser.add_argument("--time-limit", type=float, metavar="SECONDS", help="stop reading a document after SECONDS (none by default); what is left is reported unread")
    parser.add_argument("--ocr-lang", default="heb", help="Tesseract language list (default: heb; e.g. heb+eng)")
    parser.add_argument("--psm", type=int, default=3, help="Tesseract page segmentation mode (default: 3)")
    parser.add_argument("--tesseract", help="the tesseract program, when it is not on the PATH")
    parser.add_argument(
        "--ocr-model",
        metavar="MODEL",
        help="OCR model. Use auto for experimental Automatic routing across the four measured models. "
        f"Select one explicitly with {', '.join(DOMAINS)}. "
        "Use stock for Tesseract's installed language data or pass a local .traineddata path. "
        f"The default for Hebrew OCR is {DEFAULT_DOMAIN} (Modern MEASURED), with stock fallback when unavailable.",
    )
    parser.add_argument(
        "--download-model",
        metavar="NAME",
        help="install a SHA-256-verified model in the local cache. Use auto to install all four measured models once.",
    )
    parser.add_argument("--ocr-capture-type", choices=CAPTURE_MODELS,
                        help="opt into experimental OCR routing for a declared capture")
    parser.add_argument("--ocr-router", choices=("v2", "specialist"),
                        help="opt into capture routing or category specialist routing")
    parser.add_argument("--ocr-category", choices=CATEGORIES, help="print category for specialist routing")
    parser.add_argument("--ocr-specialist-config", type=Path, help="local JSON routing table for specialist routing")
    parser.add_argument("--ocr-layout", choices=("v1",),
                        help="enable geometric OCR layout for columns, cells, mixed runs and clipped edges")
    parser.add_argument("--tables", choices=("tableformer",),
                        help="opt into local TableFormer table proposals")
    parser.add_argument("--join-tables", action="store_true",
                        help="join verified table continuations in CSV/XLSX, keeping the first header")
    parser.add_argument("--tables-models", type=Path, metavar="DIR",
                        help="directory holding the pinned TableFormer and layout models")
    parser.add_argument("--ocr-old-print-model", metavar="MODEL",
                        help="enable historical-print detection with router v2 and a pinned local P-hist v2 model")
    parser.add_argument("--ocr-hybrid", choices=("haiku",),
                        help="consent to sending selected OCR images to Anthropic (uses ANTHROPIC_API_KEY)")
    parser.add_argument("--ocr-hybrid-old-print", action="store_true",
                        help="declare old print and also send a local R8b OCR draft for correction")
    parser.add_argument("--ocr-hybrid-confidence", type=float, metavar="SCORE",
                        help="with router v2, send only line crops and local OCR below this score (0 to 100)")
    parser.add_argument("--ocr-hybrid-max-usd", metavar="USD",
                        help="cap the Anthropic list-price cost per document (default: 1.00)")
    parser.add_argument("--ocr-hybrid-dry-run", action="store_true",
                        help="estimate the document's cost upper bound locally without sending anything")
    parser.add_argument("--profile", choices=(*PHONE_PROFILES, *FAST_MIX_PROFILES),
                        help="use an opt-in phone or mixed-capture OCR profile")
    parser.add_argument("--phone-model", type=Path, metavar="FILE",
                        help="local .traineddata for the phone-specialist line slot")
    parser.add_argument("--phone-model-sha256", metavar="HEX",
                        help="required SHA-256 of the local phone specialist model")
    parser.add_argument("--ocr-phone-latin-edges", action="store_true",
                        help="reread isolated mixed-script headers and footers on near phone captures with fast-mix")
    parser.add_argument("--ocr-oblique-rivok", action="store_true",
                        help="use local Rivok Full on supported rectified phone lines with fast-mix")
    parser.add_argument("--ocr-issuer-staggered-lanes", action="store_true",
                        help="split overlapping OCR lines at a supported issuer lane boundary with fast-mix")
    parser.add_argument("--ocr-footer-order", action="store_true",
                        help="finish the body before an isolated footer on fast-mix scan and issuer OCR pages")
    parser.add_argument("--ocr-phone-geometry", action="store_true",
                        help="reread isolated lines on angled phone images with supported paper edges")
    parser.add_argument("--ocr-phone-tables", action="store_true",
                        help="recover ruled table and form grids on angled phone images")
    parser.add_argument("--ocr-phone-max-edge", type=int, metavar="PX",
                        help="limit the OCR raster's long edge for standalone phone images")
    parser.add_argument("--photo-preprocess", choices=PHOTO_PREPROCESS_MODES,
                        help="select Otsu thresholding for phone photos, with optional 300 DPI retry for unread ink")
    parser.add_argument("-q", "--quiet", action="store_true", help="do not report unread parts on stderr (a missing OCR model is still reported)")
    parser.add_argument("--version", action="version", version=f"heocr {__version__}")
    return parser


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["regress"]:
        from heocr.regress import main as regress

        return regress(argv[1:])
    parser = _parser()
    args = parser.parse_args(argv)
    if args.download_model:
        if args.profile or args.phone_model is not None or args.phone_model_sha256 is not None:
            parser.error("phone specialist options cannot download a model")
        if args.ocr_hybrid_dry_run:
            parser.error("--ocr-hybrid-dry-run cannot download a model")
        from heocr.models import ModelError, download

        names = tuple(candidates().values()) if args.download_model == "auto" else (args.download_model,)
        try:
            for name in names:
                path = download(name)
                print(f"heocr: {name} is ready ({path.name}, sha256 checked)", file=sys.stderr)
        except (ModelError, OSError) as exc:
            print(f"heocr: {exc}", file=sys.stderr)
            return 1
        return 0
    if args.file is None:
        parser.error("a file is required")
    if args.ocr_hybrid_dry_run:
        if args.profile or args.phone_model is not None or args.phone_model_sha256 is not None:
            parser.error("phone specialist options are for local OCR")
        if args.ocr_hybrid is None or args.no_ocr:
            parser.error("--ocr-hybrid-dry-run requires --ocr-hybrid haiku and OCR")
        from heocr.ocr.hybrid import estimate_cost, parse_cap, parse_confidence

        try:
            confidence = parse_confidence(args.ocr_hybrid_confidence, router=args.ocr_router,
                                          old_print=args.ocr_hybrid_old_print or args.ocr_old_print_model is not None)
            estimate = estimate_cost(args.file, max_pages=args.max_pages, cap=parse_cap(args.ocr_hybrid_max_usd))
            if confidence is not None:
                estimate["confidence_below"] = confidence
                if confidence == 0:
                    estimate["estimated_max_usd"] = "0"
            report = json.dumps(estimate, indent=2) + "\n"
            if args.output:
                args.output.write_text(report, encoding="utf-8")
            else:
                sys.stdout.write(report)
        except (OSError, ValueError, EOFError) as exc:
            print(f"heocr: {exc}", file=sys.stderr)
            return 1
        return 0
    fmt = args.format or (_BY_SUFFIX.get(args.output.suffix.lower(), "text") if args.output else "text")
    if fmt in ("docx", "xlsx") and not args.output and sys.stdout.isatty():
        parser.error(f"a {'Word' if fmt == 'docx' else 'workbook'} file is not shown on a terminal: give -o FILE.{fmt}, or redirect the output")
    try:
        handwriting = None
        if args.handwriting is not None:
            handwriting = HandwritingConfig(
                args.handwriting, args.handwriting_sha256 or "", args.handwriting_mode or "auto",
                args.handwriting_segmenter, args.handwriting_segmenter_sha256,
                detector=args.handwriting_detector or "tags",
            )
        elif any((args.handwriting_sha256, args.handwriting_mode, args.handwriting_segmenter,
                  args.handwriting_segmenter_sha256, args.handwriting_detector)):
            raise ValueError("handwriting options require --handwriting MODEL")
        if args.ocr_hybrid and not args.no_ocr:
            if args.ocr_hybrid_confidence is not None:
                content = "Selected line images and their local OCR drafts"
            else:
                content = "Page images and local OCR drafts" if args.ocr_hybrid_old_print else "Page images"
            print(f"heocr: {content} may be sent to Anthropic when ANTHROPIC_API_KEY is set.", file=sys.stderr)
        implicit_measured_model = (
            args.ocr_model is None and args.ocr_lang == DEFAULT_LANG
            and not (args.profile or args.ocr_router or args.ocr_capture_type)
        )
        ocr_model = DEFAULT_DOMAIN if implicit_measured_model else (
            DEFAULT_MODEL if args.ocr_model is None else args.ocr_model
        )
        doc = extract(
            args.file,
            repairs=not args.no_repairs,
            ocr=not args.no_ocr,
            ocr_lang=args.ocr_lang,
            ocr_psm=args.psm,
            tesseract=args.tesseract,
            ocr_model=ocr_model,
            ocr_router=args.ocr_router,
            ocr_category=args.ocr_category,
            ocr_specialist_config=args.ocr_specialist_config,
            ocr_layout=args.ocr_layout,
            ocr_old_print_model=args.ocr_old_print_model,
            ocr_hybrid=args.ocr_hybrid,
            ocr_hybrid_old_print=args.ocr_hybrid_old_print,
            ocr_hybrid_confidence=args.ocr_hybrid_confidence,
            ocr_hybrid_max_usd=args.ocr_hybrid_max_usd,
            ocr_capture_type=args.ocr_capture_type,
            ocr_line_v6=args.ocr_line_v6,
            ocr_line_batch=args.ocr_line_batch,
            ocr_number_batch=args.ocr_number_batch,
            photo_preprocess=args.photo_preprocess,
            ocr_phone_geometry=args.ocr_phone_geometry,
            ocr_phone_tables=args.ocr_phone_tables,
            ocr_phone_max_edge=args.ocr_phone_max_edge,
            profile=args.profile,
            ocr_phone_latin_edges=args.ocr_phone_latin_edges,
            ocr_oblique_rivok=args.ocr_oblique_rivok,
            ocr_issuer_staggered_lanes=args.ocr_issuer_staggered_lanes,
            ocr_footer_order=args.ocr_footer_order,
            document_roles=args.document_roles,
            phone_model=args.phone_model,
            phone_model_sha256=args.phone_model_sha256,
            force_ocr=args.force_ocr,
            crnn_model=args.crnn_model,
            crnn_sha256=args.crnn_sha256,
            handwriting=handwriting,
            check_numbers=False if args.check_numbers == "off" else args.check_numbers,
            form_fields=args.form_fields or fmt == "forms",
            form_scan_marks=args.form_scan_marks and fmt == "forms",
            form_grid_recovery=args.form_grid_recovery and fmt == "forms",
            convert=not args.no_convert,
            soffice=args.soffice,
            convert_timeout=args.convert_timeout,
            max_pages=args.max_pages,
            time_limit=args.time_limit,
            tables=args.tables,
            tables_models=args.tables_models,
            join_tables=args.join_tables,
            _ocr_fallback_to_stock=implicit_measured_model,
        )
    except ImportError as exc:
        if args.tables is None and args.profile not in FAST_MIX_TABLE_PROFILES:
            raise
        print(f"heocr: {exc}", file=sys.stderr)
        return 1
    except (OSError, ValueError) as exc:
        print(f"heocr: {exc}", file=sys.stderr)
        return 1
    if fmt == "csv":
        text = _csv(doc, args.output)
        out = None if text is None else text.encode("utf-8")  # bytes: CRLF stays CRLF on every platform
    elif fmt == "docx":
        out = doc.to_docx()
    elif fmt == "xlsx":
        out = doc.to_xlsx()
    elif fmt == "json":
        out = doc.to_json() + "\n"
    elif fmt == "forms":
        out = json.dumps(doc.to_forms(join_pages=args.join_form_pages), ensure_ascii=False, indent=2) + "\n"
    elif fmt == "markdown":
        out = doc.markdown
    else:
        out = doc.text
    if out is None:
        pass  # written already, one file per table
    elif isinstance(out, bytes):
        if args.output:
            args.output.write_bytes(out)
        else:
            sys.stdout.buffer.write(out)
    elif args.output:
        args.output.write_text(out, encoding="utf-8")
    else:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8")
        sys.stdout.write(out)
    # A missing OCR model changes the results, so it is reported even with --quiet.
    for line in _model_warnings(doc):
        print(line, file=sys.stderr)
    if "ocr_old_print_model_unavailable" in doc.flags:
        print("heocr: warning: the local P-hist v2 model is missing or failed its pin. Using router v2.", file=sys.stderr)
    if "ocr_hybrid_local_fallback" in doc.flags:
        reasons = sorted({p.signals["ocr_hybrid"] for p in doc.pages
                          if p.signals.get("ocr_hybrid") not in (None, "hosted")})
        print(f"heocr: used local OCR fallback on hybrid pages ({', '.join(reasons)})", file=sys.stderr)
    if not args.quiet:
        for item in doc.unread:
            where = f"page {item.page + 1}: " if item.page is not None else ""
            detail = f" ({item.detail})" if item.detail else ""
            print(f"heocr: unread: {where}{item.reason}{detail}", file=sys.stderr)
    return 0


def _csv(doc, output: "Path | None") -> "str | None":
    """The tables as CSV: one file each when there are several and an output name is given (NAME-1.csv, ...;
    then None), else all of them in one text, separated by an empty line."""
    from heocr.table_export import tables, to_csv

    found = tables(doc)
    if not found:
        print("heocr: no tables were found", file=sys.stderr)
    if output is not None and len(found) > 1:
        for i, block in enumerate(found, 1):
            output.with_name(f"{output.stem}-{i}{output.suffix or '.csv'}").write_text(
                to_csv(block), encoding="utf-8", newline="")
        return None
    return "\r\n".join(to_csv(block) for block in found)


def _model_warnings(doc) -> list[str]:
    """Warnings for an OCR model that was asked for but is not installed, so stock Tesseract read instead."""
    if "bundled_model_unavailable" not in doc.flags and "routed_model_unavailable" not in doc.flags:
        return []
    model = (doc.options.get("ocr") or {}).get("model", "the default model")
    return [
        f"heocr: warning: the OCR model {model} is not installed. "
        "OCR falls back to stock Tesseract language data when available."
    ]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
