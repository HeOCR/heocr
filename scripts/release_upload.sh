#!/usr/bin/env bash
# Verify the reviewed distribution files and optionally publish them to PyPI.
set -e -o pipefail

if [[ $# != 3 || ( "$1" != check && "$1" != publish ) ]]; then
    printf 'Usage: bash %s {check|publish} ARTIFACT_DIR MANIFEST_SHA256\n' "$0" >&2
    exit 2
fi
mode=$1
artifact_dir=$2
manifest_sha=$3
python=${HEOCR_RELEASE_PYTHON:-python3}
cd -- "$artifact_dir"

shopt -s nullglob
wheels=(heocr-*.whl)
sdists=(heocr-*.tar.gz)
if [[ ${#wheels[@]} != 1 || ${#sdists[@]} != 1 ]]; then
    printf '%s\n' 'Expected one HeOCR wheel and one HeOCR source distribution.' >&2
    exit 2
fi
distributions=("${wheels[0]}" "${sdists[0]}")
"$python" - "$manifest_sha" "${distributions[@]}" <<'PY'
import hashlib
from pathlib import Path
import sys

expected_manifest_sha, *files = sys.argv[1:]
manifest_bytes = Path("SHA256SUMS").read_bytes()
if hashlib.sha256(manifest_bytes).hexdigest() != expected_manifest_sha:
    sys.exit("SHA256SUMS differs from the reviewed manifest")
pins = {}
for line in manifest_bytes.decode("ascii").splitlines():
    digest, name = line.split("  ", 1)
    if name in pins:
        sys.exit(f"Duplicate artifact in SHA256SUMS: {name}")
    pins[name] = digest
if set(pins) != set(files):
    sys.exit("SHA256SUMS and the distribution directory differ")
for name in files:
    with Path(name).open("rb") as artifact:
        if hashlib.file_digest(artifact, "sha256").hexdigest() != pins[name]:
            sys.exit(f"Artifact differs from the reviewed SHA-256: {name}")
    print(f"SHA-256 verified for {name}")
PY

"$python" -m twine check --strict "${distributions[@]}"
if [[ "$mode" == publish ]]; then
    "$python" -m twine upload --repository-url https://upload.pypi.org/legacy/ \
        --non-interactive "${distributions[@]}"
else
    printf '%s\n' 'Release dry run passed.'
fi
