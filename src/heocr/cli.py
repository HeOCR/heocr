"""The ``heocr`` command."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from heocr import __version__
from heocr.api import extract
from heocr.contract import MAX_PAGES

_FORMATS = ("text", "markdown", "json")
_BY_SUFFIX = {".md": "markdown", ".markdown": "markdown", ".json": "json", ".txt": "text"}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="heocr",
        description="Extract the Hebrew text of a document through the best open pipeline for it.",
    )
    parser.add_argument("file", type=Path, nargs="?", help="input document: PDF, DOCX, DOC, RTF, ODT, XLSX, ODS, XLS, PPTX, ODP, PPT, HTML or an image")
    parser.add_argument("-o", "--output", type=Path, help="write here instead of standard output")
    parser.add_argument(
        "-f",
        "--format",
        choices=_FORMATS,
        help="output format (default: from the output suffix, else text)",
    )
    parser.add_argument(
        "--no-repairs",
        action="store_true",
        help="turn every repair rule off: output the extracted text byte for byte",
    )
    parser.add_argument("--no-ocr", action="store_true", help="do not run OCR; pages that need it are reported unread")
    parser.add_argument("--force-ocr", action="store_true", help="read every PDF page by OCR, ignoring its text layer (a workaround for a text layer heocr reads badly)")
    parser.add_argument(
        "--check-numbers",
        choices=("suspect", "tables", "all", "off"),
        default="suspect",
        help="read numbers a second time by OCR: suspect digit groups only (default), also every table cell, all amounts and dates, or off",
    )
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
        default="auto",
        help="the OCR model (default: auto, one HeOCR model per document from the route table, when --ocr-lang is heb); "
        "heb or stock for stock Tesseract; or a released HeOCR model's name (e.g. heocr_r8b) or a .traineddata path",
    )
    parser.add_argument(
        "--download-model",
        metavar="NAME",
        help="download a released HeOCR model on purpose, check its pinned sha256, and exit",
    )
    parser.add_argument("-q", "--quiet", action="store_true", help="do not report unread parts on stderr")
    parser.add_argument("--version", action="version", version=f"heocr {__version__}")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.download_model:
        from heocr.models import ModelError, download

        try:
            path = download(args.download_model)
        except (ModelError, OSError) as exc:
            print(f"heocr: {exc}", file=sys.stderr)
            return 1
        print(f"heocr: {args.download_model} is ready ({path.name}, sha256 checked)", file=sys.stderr)
        return 0
    if args.file is None:
        parser.error("a file is required (or --download-model NAME)")
    fmt = args.format or (_BY_SUFFIX.get(args.output.suffix.lower(), "text") if args.output else "text")
    try:
        doc = extract(
            args.file,
            repairs=not args.no_repairs,
            ocr=not args.no_ocr,
            ocr_lang=args.ocr_lang,
            ocr_psm=args.psm,
            tesseract=args.tesseract,
            ocr_model=args.ocr_model,
            force_ocr=args.force_ocr,
            check_numbers=False if args.check_numbers == "off" else args.check_numbers,
            convert=not args.no_convert,
            soffice=args.soffice,
            convert_timeout=args.convert_timeout,
            max_pages=args.max_pages,
            time_limit=args.time_limit,
        )
    except (OSError, ValueError) as exc:
        print(f"heocr: {exc}", file=sys.stderr)
        return 1
    if fmt == "json":
        out = doc.to_json() + "\n"
    elif fmt == "markdown":
        out = doc.markdown
    else:
        out = doc.text
    if args.output:
        args.output.write_text(out, encoding="utf-8")
    else:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8")
        sys.stdout.write(out)
    if not args.quiet:
        for item in doc.unread:
            where = f"page {item.page + 1}: " if item.page is not None else ""
            detail = f" ({item.detail})" if item.detail else ""
            print(f"heocr: unread: {where}{item.reason}{detail}", file=sys.stderr)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
