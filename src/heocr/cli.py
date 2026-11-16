"""The ``heocr`` command."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from heocr import __version__
from heocr.api import extract

_FORMATS = ("text", "markdown", "json")
_BY_SUFFIX = {".md": "markdown", ".markdown": "markdown", ".json": "json", ".txt": "text"}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="heocr",
        description="Extract the Hebrew text of a document through the best open pipeline for it.",
    )
    parser.add_argument("file", type=Path, help="input document (PDF, DOCX, ...)")
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
    parser.add_argument("-q", "--quiet", action="store_true", help="do not report unread parts on stderr")
    parser.add_argument("--version", action="version", version=f"heocr {__version__}")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    fmt = args.format or (_BY_SUFFIX.get(args.output.suffix.lower(), "text") if args.output else "text")
    try:
        doc = extract(args.file, repairs=not args.no_repairs)
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
