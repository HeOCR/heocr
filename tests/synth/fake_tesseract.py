"""A stand-in for the tesseract program, for offline tests of the OCR route.

It answers --version and --list-langs, and writes a TSV for a recognition call. The TSV comes
from $FAKE_TSV (a file) or a built-in two-line Hebrew page; a single-word call (--psm 8)
answers with $FAKE_WORD when it is set, and a single-line call (--psm 7) with $FAKE_LINE. Every call's arguments are appended
to $FAKE_LOG when it is set; $FAKE_FAIL=1 makes recognition fail.

A table-cell call (--psm 6 or 7) with $FAKE_CELLS set (a JSON file mapping grey levels to
text) "reads" the image: the most common grey level darker than paper (0 excluded, being the
rulings) is looked up, within 4 levels, and its text is the answer. A level not in the map is
ink that could not be read: the answer is empty.
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
    psm = argv[argv.index("--psm") + 1] if "--psm" in argv else "3"
    cells = os.environ.get("FAKE_CELLS")
    if psm in ("6", "7") and cells:
        from PIL import Image

        mapping = {int(k): v for k, v in json.load(open(cells, encoding="utf-8")).items()}
        histogram = Image.open(argv[0]).convert("L").histogram()
        levels = [(count, level) for level, count in enumerate(histogram[1:150], 1) if count]
        text = ""
        if levels:
            level = max(levels)[1]
            near = [k for k in mapping if abs(k - level) <= 4]
            text = mapping[near[0]] if near else ""
        rows = ["level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext"]
        for n, token in enumerate(text.split(), 1):
            rows.append(f"5\t1\t1\t1\t1\t{n}\t2\t2\t40\t20\t90.0\t{token}")
        with open(base + ".tsv", "w", encoding="utf-8") as stream:
            stream.write("\n".join(rows) + "\n")
        return 0
    word = os.environ.get("FAKE_WORD")
    if psm == "7" and os.environ.get("FAKE_LINE") is not None:
        word = os.environ["FAKE_LINE"]
    if psm in ("7", "8") and word is not None:
        # A single-word call: answer with $FAKE_WORD (empty means nothing found).
        rows = ["level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext"]
        for n, token in enumerate(word.split(), 1):
            rows.append(f"5\t1\t1\t1\t1\t{n}\t2\t2\t40\t20\t{os.environ.get('FAKE_CONF', '95.0')}\t{token}")
        with open(base + ".tsv", "w", encoding="utf-8") as stream:
            stream.write("\n".join(rows) + "\n")
        return 0
    source = os.environ.get("FAKE_TSV")
    tsv = open(source, encoding="utf-8").read() if source else DEFAULT_TSV
    with open(base + ".tsv", "w", encoding="utf-8") as stream:
        stream.write(tsv)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
