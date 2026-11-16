"""File-type identification. Placeholder until D1b: every input is ``unknown``."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FileKind:
    format: str
    evidence: str


def identify(body: bytes) -> FileKind:
    return FileKind("unknown", "no identifier before D1b")
