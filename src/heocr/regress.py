"""Offline engineering checks for a pinned synthetic release QA bundle."""

from __future__ import annotations

import argparse
from bisect import bisect_right
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from tempfile import NamedTemporaryFile
import unicodedata

from heocr.api import extract
from heocr.ocr.tesseract import OcrUnavailable, Tesseract

SCHEMA = "heocr_regress.v1"
REPORT_SCHEMA = "heocr_regress_report.v1"
CHAIN_VARIANTS = {
    "p10": {"ocr_heb", "ocr_eng"},
    "mixed_direction": {"plain"},
    "p9_grain": {"grain"},
    "p8": {"outlines", "postscript"},
    "scanned_labels": {"img200"},
}
WORD = re.compile(r"[^\W_]+")
NUMBER = re.compile(r"\d+(?:[.,:/-]\d+)*%?")
BRACKET = re.compile(r"[][(){}]")
WORD_OR_BRACKET = re.compile(r"[^\W_]+|[][(){}]")
BULLETS = re.compile(r"[•–\-*◦▪·]$")
SECTION = re.compile(r"\(?\d+(?:\.\d+)*[.)]?")
FORMAT = str.maketrans("", "", "\u00ad\u061c\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069")


@dataclass(frozen=True)
class Case:
    id: str
    chain: str
    variant: str
    page_count: int
    pdf: Path
    pdf_sha: str
    gold: dict


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _asset(root, name):
    relative = Path(name)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("bundle assets must use relative paths")
    path = (root / relative).resolve(strict=True)
    if not path.is_relative_to(root):
        raise ValueError("bundle asset escapes its root")
    return path


def _checked(path, expected):
    data = path.read_bytes()
    if _sha(data) != expected:
        raise ValueError("bundle asset SHA-256 mismatch")
    return data


def load_cases(manifest, manifest_sha):
    root = manifest.resolve(strict=True).parent
    content = json.loads(_checked(manifest, manifest_sha))
    if content["schema"] != SCHEMA or content["purpose"] != "synthetic_engineering_qa":
        raise ValueError("expected a synthetic engineering QA bundle")
    rank = {chain: index for index, chain in enumerate(CHAIN_VARIANTS)}
    cases, seen = [], set()
    for row in content["cases"]:
        chain, variant = row["chain"], row["variant"]
        if (chain not in CHAIN_VARIANTS or variant not in CHAIN_VARIANTS[chain]
                or type(row["pages"]) is not int or row["pages"] < 1
                or row["id"] in seen):
            raise ValueError("invalid regression case")
        gold = json.loads(_checked(_asset(root, row["gold"]), row["gold_sha256"]))
        if gold["schema"] != "hdoc_gold.v1" or gold["producer"]["pdf_sha256"] != row["producer_pdf_sha256"]:
            raise ValueError("gold does not match the producer PDF")
        if variant == "plain" and row["pdf_sha256"] != row["producer_pdf_sha256"]:
            raise ValueError("plain case differs from its producer PDF")
        pdf = _asset(root, row["pdf"])
        _checked(pdf, row["pdf_sha256"])
        cases.append(Case(row["id"], chain, variant, row["pages"], pdf, row["pdf_sha256"], gold))
        seen.add(row["id"])
    if {(case.chain, case.variant) for case in cases} != {
        (chain, variant) for chain, variants in CHAIN_VARIANTS.items() for variant in variants
    }:
        raise ValueError("bundle is missing a required chain variant")
    return sorted(cases, key=lambda case: rank[case.chain])


def _fold(text):
    return " ".join(unicodedata.normalize("NFC", text).translate(FORMAT).split())


def _observed(block):
    if block.kind == "table":
        return " ".join(cell.text for row in block.cells or [] for cell in row)
    if block.label and not block.text.startswith(block.label + " "):
        return f"{block.label} {block.text}"
    return block.text


class Body:
    """One body stream with page and block locations, shared by its gold anchors."""

    def __init__(self, blocks):
        parts = []
        self.starts = []
        self.locations = []
        size = 0
        for index, block in enumerate(blocks):
            if block.role != "body" or block.page is None:
                continue
            self.starts.append(size)
            self.locations.append((index, block.page + 1))
            part = _fold(_observed(block))
            parts.append(part)
            size += len(part) + 1
        self.text = "\n".join(parts)
        self.words = list(WORD.finditer(self.text))
        self.positions = defaultdict(list)
        for i, word in enumerate(self.words):
            self.positions[word[0]].append(i)

    def locate(self, offset):
        return bisect_right(self.starts, offset) - 1

    def stems(self, token_sequence):
        return [
            i for i in self.positions[token_sequence[0]]
            if [word[0] for word in self.words[i:i + len(token_sequence)]] == token_sequence
        ]

    def paragraph(self, text, pages):
        expected_tokens = list(WORD.finditer(text))
        if not expected_tokens:
            return None
        words = [token[0] for token in expected_tokens]
        length = min(3, len(words))
        pairs = []
        for start in self.stems(words[:length]):
            first = self.locate(self.words[start].start())
            if self.locations[first][1] not in pages:
                continue
            if len(words) <= 2 * length:
                tail = start + len(words) - length
                tails = [tail] if [word[0] for word in self.words[start:start + len(words)]] == words else []
            else:
                tails = [i for i in self.stems(words[-length:]) if start + length <= i <= start + 2 * len(words)]
            for tail in tails:
                end = tail + length - 1
                last = self.locate(self.words[end].end() - 1)
                if self.locations[last][1] in pages:
                    pairs.append((start, end))
        if len(pairs) != 1:
            return None
        a, b = pairs[0]
        start, end = self.words[a].start(), self.words[b].end()
        first, last = self.locate(start), self.locate(end - 1)
        stop = self.starts[last + 1] if last + 1 < len(self.starts) else len(self.text)
        if b + 1 < len(self.words):
            stop = min(stop, self.words[b + 1].start())
        lead = self.text[self.starts[first]:start].rstrip()
        # Match boundary punctuation to the producer's paragraph boundaries.
        # A neighbouring outline label or paragraph may be in the same block.
        lead_len = len(text[:expected_tokens[0].start()].strip())
        tail_len = len(text[expected_tokens[-1].end():].strip())
        opener = lead[-lead_len:] if lead_len else ""
        if any(ch.isalnum() for ch in opener):
            opener = ""
        before = lead[:len(lead) - len(opener)].rstrip()
        if not before and first and self.locations[first - 1][1] == self.locations[first][1]:
            prior = self.text[self.starts[first - 1]:self.starts[first]].strip()
            if SECTION.fullmatch(prior) or BULLETS.fullmatch(prior):
                before = prior
        tail = self.text[end:stop]
        ending = tail.lstrip()[:tail_len]
        if ending and tail[0].isspace():
            ending = " " + ending
        span = opener + self.text[start:end] + ending
        return (self.locations[first][0], start), _fold(span), before


def _eligible(block):
    return bool(block.get("pages")) and not (block.get("not_rendered") or block.get("partially_rendered"))


def _fragment(tokens, phrase, cursor):
    for i in range(cursor, len(tokens) - len(phrase) + 1):
        if tokens[i:i + len(phrase)] == phrase:
            return i + len(phrase)
    return None


def _brackets(text):
    position = 0
    found = []
    for token in WORD_OR_BRACKET.findall(text):
        if BRACKET.fullmatch(token):
            found.append((position, token))
        else:
            position += 1
    return found


def paragraph_findings(block, actual, prefix, producer_labels):
    expected = _fold(block["text"])
    checks = []
    # Delimiters are compared literally, so a mirrored pair cannot pass
    # just because both readings have the same character inventory.
    if _brackets(expected) != _brackets(actual):
        checks.append("bracket_mirroring")
    if NUMBER.findall(expected) != NUMBER.findall(actual):
        checks.append("numbers_or_amount_order")
    if any(_fold(value["text"]) not in actual for value in block.get("values", [])):
        checks.append("amount_text")

    cursor = 0
    actual_words = WORD.findall(actual)
    for run in block.get("ltr_runs", []):
        words = WORD.findall(_fold(block["text"][run["start"]:run["end"]]))
        if not words:
            continue
        # Include the surrounding logical words so the same English phrase
        # or number moved across its Hebrew neighbours does not pass.
        left = WORD.findall(block["text"][:run["start"]])[-1:]
        right = WORD.findall(block["text"][run["end"]:])[:1]
        position = _fragment(actual_words, left + words + right, max(0, cursor - 2))
        if position is None:
            checks.append("mixed_direction")
            break
        cursor = position
    if WORD.findall(expected) != actual_words:
        checks.append("logical_text_order")

    label = _fold(block.get("label_producer") or "") if producer_labels else ""
    if label:
        rendered = bool(BULLETS.search(prefix)) if BULLETS.fullmatch(label) else bool(
            re.search(r"(?<!\w)" + re.escape(label) + r"$", prefix)
        )
        if not rendered:
            checks.append("producer_section_label")
    return checks


def _grid(block):
    """Index cells in the reader's logical row order, skipping occupied span slots."""
    occupied = set()
    grid = []
    for r, row in enumerate(block.cells or []):
        column = 0
        cells = []
        for cell in row:
            # PDF rows can carry explicit padding for slots occupied by a rowspan.
            # The occupied slots below already account for those cells.
            if "merged_above" in cell.flags:
                continue
            while (r, column) in occupied:
                column += 1
            cells.append((column, cell.rowspan, cell.colspan, _fold(cell.text)))
            occupied.update((ri, ci) for ri in range(r, r + cell.rowspan)
                            for ci in range(column, column + cell.colspan))
            column += cell.colspan
        grid.append(cells)
    return grid


def table_alignment(table, gold_blocks, actual, consumed):
    pages = set()
    expected = [[] for _ in range(table["rows"])]
    for cell in table["cells"]:
        paragraphs = [gold_blocks[i] for i in cell["paragraphs"]]
        if any(not _eligible(p) for p in paragraphs):
            return None, "unscored"
        pages.update(page for p in paragraphs for page in p["pages"])
        expected[cell["r"]].append((cell["c"], cell["rowspan"], cell["colspan"], _fold(cell["text"])))
    if not pages:
        return None, "unscored"
    words = Counter(cell[3] for row in expected for cell in row if cell[3])
    picked = []
    for page in sorted(pages):
        ranked = []
        for index, block in actual.get(page, []):
            if index in consumed:
                continue
            grid = _grid(block)
            present = Counter(cell[3] for row in grid for cell in row if cell[3])
            rank = sum((words & present).values())
            if rank:
                ranked.append((rank, index, grid))
        ranked.sort(reverse=True)
        if not ranked or len(ranked) > 1 and ranked[0][0] == ranked[1][0]:
            return None, "table_unmapped"
        picked.append(ranked[0])

    grid = []
    for _, _, rows in picked:
        if table.get("repeat_header") and grid and rows and rows[0] == expected[0]:
            rows = rows[1:]
        grid.extend(rows)
    consumed.update(index for _, index, _ in picked)
    position = (picked[0][1], 0)
    return position, None if grid == expected else "table_cells_or_spans"


def inspect_document(case, doc):
    findings = []
    unscored = Counter()

    def add(check, subject):
        findings.append({"check": check, "subject": str(subject)})

    if doc.page_count != case.page_count or len(doc.pages) != case.page_count:
        add("page_inventory", "document")
    for page in doc.pages:
        if case.variant == "ocr_heb" and (
            page.page_class != "T1" or "text_layer_is_ocr" not in page.flags
        ):
            add("correct_ocr_layer_not_kept", page.index + 1)
        elif case.variant == "ocr_eng" and (page.page_class != "T4" or page.route != "ocr"):
            add("wrong_ocr_layer_not_replaced", page.index + 1)
    for unread in doc.unread:
        if unread.reason in {"ocr_failed", "ocr_call_failed", "ocr_route_unavailable", "ocr_found_no_text"}:
            add(unread.reason, "document" if unread.page is None else unread.page + 1)

    gold = case.gold
    by_id = {block["id"]: block for block in gold["blocks"]}
    tables = {table["id"]: table for table in gold["tables"]}
    body = Body(doc.blocks)
    actual_tables = defaultdict(list)
    for i, block in enumerate(doc.blocks):
        if block.kind == "table" and block.role == "body" and block.page is not None:
            actual_tables[block.page + 1].append((i, block))
    consumed = set()
    previous = (-1, -1)
    producer_labels = gold["label_basis"] == "producer"
    for subject in gold["reading_order"]:
        block = by_id[subject]
        if subject in tables:
            position, outcome = table_alignment(tables[subject], by_id, actual_tables, consumed)
            if outcome == "unscored":
                unscored["tables_without_complete_producer_geometry"] += 1
                continue
            if outcome:
                add(outcome, subject)
        elif not _eligible(block):
            unscored["blocks_without_complete_producer_geometry"] += 1
            continue
        else:
            if block.get("label_model") and (not producer_labels or not block.get("label_producer")):
                unscored["producer_label_unavailable"] += 1
            match = body.paragraph(_fold(block["text"]), set(block["pages"]))
            if match is None:
                add("body_block_unmapped", subject)
                continue
            position, actual, prefix = match
            for outcome in paragraph_findings(block, actual, prefix, producer_labels):
                add(outcome, subject)
        if position is not None:
            if position <= previous:
                add("body_reading_order", subject)
            previous = max(previous, position)
    return {"id": case.id, "chain": case.chain, "variant": case.variant, "pages": case.page_count,
            "findings": findings, "unscored": dict(unscored)}


def run_case(case, tesseract):
    document = extract(_checked(case.pdf, case.pdf_sha), ocr_model="heb", tesseract=tesseract, convert=False)
    return inspect_document(case, document)


def _regressions(results, baseline):
    known = {} if baseline is None else {row["id"]: row for row in baseline}
    new = []
    for row in results:
        prior = Counter((f["check"], f["subject"]) for f in known.get(row["id"], {}).get("findings", []))
        observed = Counter((f["check"], f["subject"]) for f in row["findings"])
        new.extend({"id": row["id"], "check": check, "subject": subject}
                   for check, subject in (observed - prior).elements())
    return new


def main(argv):
    parser = argparse.ArgumentParser(prog="heocr regress", description="Run local release-candidate engineering QA.")
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--manifest-sha256", required=True)
    parser.add_argument("--report", type=Path, required=True, help="new local JSON report")
    parser.add_argument("--baseline", type=Path, help="previous local report of known findings")
    parser.add_argument("--tesseract", help="Tesseract executable, with stock Hebrew and English installed")
    parser.add_argument("--jobs", type=int, choices=range(1, 9), default=1, metavar="1-8")
    args = parser.parse_args(argv)
    try:
        if args.report.exists():
            raise ValueError("report already exists")
        if not args.report.parent.is_dir():
            raise ValueError("report directory does not exist")
        cases = load_cases(args.manifest, args.manifest_sha256)
        tools = {language: Tesseract.find(args.tesseract, lang=language).provenance()
                 for language in ("heb", "eng")}
        baseline = None
        if args.baseline is not None:
            receipt = json.loads(args.baseline.read_bytes())
            baseline = receipt["cases"]
            if (receipt["schema"] != REPORT_SCHEMA or receipt["manifest_sha256"] != args.manifest_sha256
                    or receipt["ocr_tools"] != tools or [row["id"] for row in baseline] != [case.id for case in cases]):
                raise ValueError("baseline uses a different bundle or Tesseract toolchain")
        if args.jobs == 1:
            results = [run_case(case, args.tesseract) for case in cases]
        else:
            with ProcessPoolExecutor(max_workers=args.jobs) as workers:
                results = list(workers.map(run_case, cases, [args.tesseract] * len(cases)))
        new = _regressions(results, baseline)
        report = NamedTemporaryFile(mode="w", encoding="utf-8", dir=args.report.parent, delete=False)
        try:
            with report:
                json.dump({"schema": REPORT_SCHEMA, "manifest_sha256": args.manifest_sha256,
                           "ocr_tools": tools, "cases": results, "new_findings": new}, report, indent=2)
                report.write("\n")
            # Link without clobbering a report another invocation may have completed.
            os.link(report.name, args.report)
        finally:
            Path(report.name).unlink()
    except (OSError, ValueError, KeyError, TypeError, OcrUnavailable) as error:
        print(f"heocr regress: {error}", file=sys.stderr)
        return 2
    print("Release QA report written locally.", file=sys.stderr)
    return 1 if new else 0
