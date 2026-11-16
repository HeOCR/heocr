"""A stand-in for LibreOffice's soffice, for offline tests of the converter (D4a).

It answers --version. A conversion (--convert-to ... --outdir DIR INPUT) copies $FAKE_SOFFICE_DOCX
to DIR/<input stem>.docx. Every call's arguments are appended to $FAKE_SOFFICE_LOG when it is set.
$FAKE_SOFFICE_HANG=1 starts a child that sleeps (its pid written to $FAKE_SOFFICE_PIDFILE) and then
sleeps itself, as a stuck soffice and its soffice.bin would; $FAKE_SOFFICE_FAIL=1 fails without
output; $FAKE_SOFFICE_GARBAGE=1 writes a file that is not a DOCX.
"""

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path


def main(argv: list[str]) -> int:
    log = os.environ.get("FAKE_SOFFICE_LOG")
    if log:
        with open(log, "a", encoding="utf-8") as stream:
            stream.write(json.dumps(argv) + "\n")
    if "--version" in argv:
        print("LibreOffice 7.9.9.9 0000000000000000000000000000000000000000")
        return 0
    if os.environ.get("FAKE_SOFFICE_HANG") == "1":
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
        pidfile = os.environ.get("FAKE_SOFFICE_PIDFILE")
        if pidfile:
            Path(pidfile).write_text(str(child.pid))
        time.sleep(600)
        return 0
    if os.environ.get("FAKE_SOFFICE_FAIL") == "1":
        print("Error: source file could not be loaded")
        return 1
    outdir = Path(argv[argv.index("--outdir") + 1])
    source = Path(argv[-1])
    target = outdir / (source.stem + ".docx")
    if os.environ.get("FAKE_SOFFICE_GARBAGE") == "1":
        target.write_bytes(b"not a docx at all")
        return 0
    shutil.copyfile(os.environ["FAKE_SOFFICE_DOCX"], target)
    print(f"convert {source} as a Writer document -> {target} using filter : MS Word 2007 XML")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
