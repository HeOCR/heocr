"""Formats read through LibreOffice: converted to an Office Open XML format we read ourselves.

- D4a: DOC, RTF and ODT to **DOCX**, read by our DOCX reader;
- D4h: ODS and XLS to **XLSX**, read by our XLSX reader; ODP and PPT to **PPTX**, read by our PPTX
  reader. The blocks' ``source`` names the conversion (``converted_docx``, ``converted_xlsx``,
  ``converted_pptx``).

LibreOffice (MPL-2.0) is an optional external program, found on the machine and never
bundled, like Tesseract. Its output is **an OOXML file, never text**: the text export hangs on a body
that ends with a table and drops footnotes (eval's findings), and it would bypass the DOCX
reader's numbering, notes, hidden text and repairs.

Every call is bounded:

- **an isolated profile:** a throwaway ``-env:UserInstallation`` in a temporary folder, removed
  afterwards. The user's own profile is never touched, and one stuck run cannot block the next
  (LibreOffice otherwise hands the job to an instance already running);
- **a timeout:** on expiry the whole process group is killed (``soffice`` starts a child,
  ``soffice.bin``, that a plain kill would leave behind), and the file goes to ``unread``;
- **recorded:** the program's version and its executable's sha256 go into ``components``.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import signal
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

CONVERT_TIMEOUT = 180.0  # seconds for one conversion, a cold start of a fresh profile included
VERSION_TIMEOUT = 60.0
# Formats converted, with the input extension LibreOffice is given, the target, and its export filter.
CONVERSIONS = {
    "doc": ("doc", "docx"),
    "rtf": ("rtf", "docx"),
    "odt": ("odt", "docx"),
    "ods": ("ods", "xlsx"),
    "xls": ("xls", "xlsx"),
    "odp": ("odp", "pptx"),
    "ppt": ("ppt", "pptx"),
}
FILTERS = {"docx": "docx:MS Word 2007 XML", "xlsx": "xlsx:Calc MS Excel 2007 XML", "pptx": "pptx:Impress MS PowerPoint 2007 XML"}
TO_DOCX = {fmt: ext for fmt, (ext, target) in CONVERSIONS.items() if target == "docx"}
_MAC_APP = "/Applications/LibreOffice.app/Contents/MacOS/soffice"


class ConversionError(Exception):
    """A conversion that did not produce a document. ``reason`` is the unread reason."""

    def __init__(self, reason: str, detail: str):
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


@dataclass
class LibreOffice:
    cmd: list[str]
    version: str
    executable_sha256: str
    timeout: float = CONVERT_TIMEOUT

    @classmethod
    def find(cls, cmd: str | Sequence[str] | None = None, *, timeout: float = CONVERT_TIMEOUT) -> "LibreOffice":
        if cmd is None:
            cmd = os.environ.get("HEOCR_SOFFICE") or shutil.which("soffice") or shutil.which("libreoffice")
            if not cmd and Path(_MAC_APP).is_file():
                cmd = _MAC_APP
        if not cmd:
            raise ConversionError("converter_unavailable", "LibreOffice (soffice) not found; install it, or pass soffice=")
        argv = [cmd] if isinstance(cmd, str) else list(cmd)
        version, sha = _identity(tuple(argv))
        return cls(argv, version, sha, timeout)

    def provenance(self) -> dict[str, str]:
        return {"libreoffice": self.version, "libreoffice:executable_sha256": self.executable_sha256}

    def to_docx(self, body: bytes, fmt: str) -> bytes:
        """Convert a DOC, RTF or ODT file's bytes to DOCX bytes."""
        return self.convert(body, fmt)

    def convert(self, body: bytes, fmt: str) -> bytes:
        """Convert a file's bytes to its target format (``CONVERSIONS``): DOCX, XLSX or PPTX bytes."""
        extension, target = CONVERSIONS[fmt]
        with tempfile.TemporaryDirectory(prefix="heocr-lo-") as temp:
            source = Path(temp) / f"input.{extension}"
            source.write_bytes(body)
            out = Path(temp) / "out"
            out.mkdir()
            argv = [
                *self.cmd,
                _profile(temp),
                "--headless",
                "--norestore",
                "--nologo",
                "--nodefault",
                "--nolockcheck",
                "--convert-to",
                FILTERS[target],
                "--outdir",
                str(out),
                str(source),
            ]
            code, output = _run(argv, self.timeout, temp)
            result = out / f"input.{target}"
            if not result.is_file() or result.stat().st_size == 0:
                tail = output.decode("utf-8", "replace").strip().splitlines()[-1:] or [f"exit code {code}"]
                raise ConversionError("conversion_failed", f"LibreOffice produced no {target.upper()} ({tail[0][:200]})")
            return result.read_bytes()


_IDENTITIES: dict[tuple[str, ...], tuple[str, str]] = {}


def _identity(argv: tuple[str, ...]) -> tuple[str, str]:
    """The program's version and executable sha256, found once per process (one start of LibreOffice)."""
    if argv not in _IDENTITIES:
        with tempfile.TemporaryDirectory(prefix="heocr-lo-") as temp:
            code, out = _run([*argv, _profile(temp), "--version"], VERSION_TIMEOUT, temp)
        line = out.decode("utf-8", "replace").strip().splitlines()[0] if out.strip() else ""
        if code != 0 or "LibreOffice" not in line:
            raise ConversionError("converter_unavailable", "soffice --version gave no LibreOffice version")
        executable = shutil.which(argv[0]) or argv[0]
        sha = _sha256(Path(executable)) if Path(executable).is_file() else "unknown"
        _IDENTITIES[argv] = (" ".join(line.split()[:2]), sha)
    return _IDENTITIES[argv]


def _profile(temp: str) -> str:
    return "-env:UserInstallation=" + Path(temp, "profile").resolve().as_uri()


def _run(argv: list[str], timeout: float, cwd: str) -> tuple[int, bytes]:
    """Run a command in its own process group; on timeout, kill the whole group."""
    kwargs: dict = {"stdout": subprocess.PIPE, "stderr": subprocess.STDOUT, "stdin": subprocess.DEVNULL, "cwd": cwd}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    try:
        proc = subprocess.Popen(argv, **kwargs)
    except OSError as exc:
        raise ConversionError("converter_unavailable", f"LibreOffice could not start: {exc.strerror or exc}") from exc
    try:
        out, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_group(proc)
        proc.communicate()
        raise ConversionError("conversion_timeout", f"LibreOffice did not finish within {timeout:g} s; its processes were stopped") from None
    return proc.returncode, out or b""


def _kill_group(proc: subprocess.Popen) -> None:
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True)
        return
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        proc.kill()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_converted(body: bytes, doc) -> None:
    """The reader for converted formats: convert with LibreOffice, then read with our own reader."""
    from heocr.contract import Unread
    from heocr.docx.reader import read_docx
    from heocr.identify import identify
    from heocr.pptx import read_pptx
    from heocr.xlsx import read_xlsx

    target = CONVERSIONS[doc.format][1]
    settings = doc._convert_settings
    if settings is None:
        doc.unread.append(Unread(scope="document", reason="converter_unavailable", detail=f"format {doc.format}; conversion turned off"))
        doc.flags.append("not_read")
        return
    try:
        office = LibreOffice.find(settings["cmd"], timeout=settings["timeout"])
        doc.components.update(office.provenance())
        converted = office.convert(body, doc.format)
    except ConversionError as exc:
        doc.unread.append(Unread(scope="document", reason=exc.reason, detail=f"format {doc.format}; {exc.detail}"))
        doc.flags.append("not_read")
        return
    if identify(converted).format != target:
        doc.unread.append(Unread(scope="document", reason="conversion_failed", detail=f"format {doc.format}; LibreOffice's output is not the expected {target.upper()} file"))
        doc.flags.append("not_read")
        return
    {"docx": read_docx, "xlsx": read_xlsx, "pptx": read_pptx}[target](converted, doc)
    for block in doc.blocks:
        if block.source == "office_xml":
            block.source = f"converted_{target}"
    doc.flags.append("converted_by_libreoffice")
