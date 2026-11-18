"""A stand-in for the tesseract program, for offline tests of the OCR route.

It answers --version and --list-langs, and writes a TSV for a recognition call. The TSV comes
from $FAKE_TSV (a file) or a built-in two-line Hebrew page; a single-word call (--psm 8)
answers with $FAKE_WORD when it is set (or with the next of $FAKE_WORD_SEQ, "a|b", counted in
$FAKE_WORD_STATE), and a single-line call (--psm 7) with $FAKE_LINE. A whole-page call in ``eng``
(``--psm 3``) answers with $FAKE_PAGE_TSV_ENG (a file) when it is set; no crop call ever gets it.

An empty variable counts as unset, on every OS: Windows drops empty variables from a child's
environment, so a test cannot rely on one. "Nothing found" is $FAKE_WORD / $FAKE_LINE (or an entry of
$FAKE_WORD_SEQ) set to ``NOTHING``. Every call's arguments are appended
to $FAKE_LOG when it is set; $FAKE_FAIL=1 makes recognition fail.

A table-cell call (--psm 6 or 7) with $FAKE_CELLS set (a JSON file mapping grey levels to
text) "reads" the image: the most common grey level darker than paper (0 excluded, being the
rulings) is looked up, within 4 levels, and its text is the answer. A level not in the map is
ink that could not be read: the answer is empty. A call in ``eng`` (the number model) uses
$FAKE_CELLS_ENG instead, when it is set, at confidence $FAKE_CELLS_CONF (default 90; heb cells at $FAKE_CELLS_CONF_HEB).
An ``eng`` cell call below 200 dpi (a half-resolution reading) uses $FAKE_CELLS_ENG_HALF at
$FAKE_CELLS_CONF_HALF, and finds nothing when it is not set.
"""

import json
import os
import sys

NOTHING = "<nothing>"  # a single-word or one-line call that finds no text

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
    for key in [k for k, v in os.environ.items() if v == ""]:
        del os.environ[key]  # as on Windows, where an empty variable never reaches the child
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
    lang = argv[argv.index("-l") + 1] if "-l" in argv else "heb"
    if lang == "eng" and os.environ.get("FAKE_CELLS_ENG"):
        cells = os.environ["FAKE_CELLS_ENG"]
    dpi = int(argv[argv.index("--dpi") + 1]) if "--dpi" in argv else 300
    if lang == "eng" and psm in ("6", "7") and cells and dpi < 200:
        # A cell read at half resolution: $FAKE_CELLS_ENG_HALF when set, else nothing found, so that a
        # test of the full-resolution readings is not decided by this one.
        cells = os.environ.get("FAKE_CELLS_ENG_HALF") or ""
        if not cells:
            with open(base + ".tsv", "w", encoding="utf-8") as stream:
                stream.write("level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n")
            return 0
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
            conf = os.environ.get("FAKE_CELLS_CONF", "90.0") if lang == "eng" else os.environ.get("FAKE_CELLS_CONF_HEB", "90.0")
            if lang == "eng" and dpi < 200:
                conf = os.environ.get("FAKE_CELLS_CONF_HALF", conf)
            rows.append(f"5\t1\t1\t1\t1\t{n}\t2\t2\t40\t20\t{conf}\t{token}")
        with open(base + ".tsv", "w", encoding="utf-8") as stream:
            stream.write("\n".join(rows) + "\n")
        return 0
    word = os.environ.get("FAKE_WORD")
    sequence = os.environ.get("FAKE_WORD_SEQ")  # "a|b|c": successive one-word calls answer a, then b, then c (and c after)
    if psm == "8" and sequence:
        state = os.environ["FAKE_WORD_STATE"]
        calls = int(open(state).read()) if os.path.exists(state) else 0
        answers = sequence.split("|")
        word = answers[min(calls, len(answers) - 1)]
        with open(state, "w") as stream:
            stream.write(str(calls + 1))
    if psm == "7" and os.environ.get("FAKE_LINE") is not None:
        word = os.environ["FAKE_LINE"]
    if word == NOTHING:
        word = ""
    if psm in ("7", "8") and word is not None:
        # A single-word or one-line call: answer with $FAKE_WORD / $FAKE_LINE (empty means nothing found).
        # Tokens are laid out left to right across the image: evenly, or at $FAKE_LINE_SPANS
        # ("0-0.5,0.5-1": each token's left and right edge as a fraction of the image's width).
        rows = ["level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext"]
        tokens = word.split()
        try:
            from PIL import Image

            width, height = Image.open(argv[0]).size
        except Exception:
            width, height = 40 * max(1, len(tokens)), 24
        spans = os.environ.get("FAKE_LINE_SPANS") if psm == "7" else None
        edges = [tuple(float(v) for v in part.split("-")) for part in spans.split(",")] if spans else [(k / len(tokens), (k + 1) / len(tokens)) for k in range(len(tokens))]
        confs = (os.environ.get("FAKE_CONFS") or "").split(",") if psm == "7" else []
        for n, token in enumerate(tokens, 1):
            a, b = edges[n - 1] if n - 1 < len(edges) else edges[-1]
            conf = confs[n - 1] if n - 1 < len(confs) and confs[n - 1] else os.environ.get("FAKE_CONF", "95.0")
            rows.append(f"5\t1\t1\t1\t1\t{n}\t{int(a * width) + 1}\t2\t{max(1, int((b - a) * width) - 2)}\t{max(1, height - 4)}\t{conf}\t{token}")
        with open(base + ".tsv", "w", encoding="utf-8") as stream:
            stream.write("\n".join(rows) + "\n")
        return 0
    source = os.environ.get("FAKE_TSV")
    if lang == "eng" and psm == "3" and os.environ.get("FAKE_PAGE_TSV_ENG"):
        source = os.environ["FAKE_PAGE_TSV_ENG"]  # a whole-page call in the number model (the page number pass)
    tsv = open(source, encoding="utf-8").read() if source else DEFAULT_TSV
    with open(base + ".tsv", "w", encoding="utf-8") as stream:
        stream.write(tsv)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
