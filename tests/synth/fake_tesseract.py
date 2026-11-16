"""A stand-in for the tesseract program, for offline tests of the OCR route.

It answers --version and --list-langs, and writes a TSV for a recognition call. The TSV comes
from $FAKE_TSV (a file) or a built-in two-line Hebrew page. Every call's arguments are appended
to $FAKE_LOG when it is set; $FAKE_FAIL=1 makes recognition fail.
"""

import json
import os
import sys

DEFAULT_TSV = "\n".join(
    [
        "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext",
        "1\t1\t0\t0\t0\t0\t0\t0\t2480\t3508\t-1\t",
        "4\t1\t1\t1\t1\t0\t300\t200\t1800\t60\t-1\t",
        "5\t1\t1\t1\t1\t1\t1500\t200\t600\t60\t96.5\tשלום",
        "5\t1\t1\t1\t1\t2\t300\t200\t1100\t60\t91.0\tעולם",
        "5\t1\t1\t1\t2\t1\t900\t300\t1200\t60\t88.0\tשורה",
        "5\t1\t1\t1\t2\t2\t300\t300\t500\t60\t-1\t ",
        "5\t1\t2\t1\t1\t1\t300\t600\t1200\t60\t80.0\tפסקה",
        "",
    ]
)


def main(argv: list[str]) -> int:
    log = os.environ.get("FAKE_LOG")
    if log:
        with open(log, "a", encoding="utf-8") as stream:
            stream.write(json.dumps(argv) + "\n")
    if argv[:1] == ["--version"]:
        print("tesseract 5.9.9\n leptonica-0.0.0")
        return 0
    if argv[:1] == ["--list-langs"]:
        folder = os.environ.get("FAKE_TESSDATA", "")
        print(f'List of available languages in "{folder}/" (1):\nheb')
        return 0
    if os.environ.get("FAKE_FAIL") == "1":
        return 1
    base = argv[1]
    source = os.environ.get("FAKE_TSV")
    tsv = open(source, encoding="utf-8").read() if source else DEFAULT_TSV
    with open(base + ".tsv", "w", encoding="utf-8") as stream:
        stream.write(tsv)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
