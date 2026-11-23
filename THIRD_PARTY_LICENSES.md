# Third-party licences of the heocr installation

heocr is Apache-2.0. **Its wheel is pure Python and bundles no third-party code or binary.** It depends on three packages, which bring their own compiled libraries. Decision 0020 keeps the default install free of copyleft. This file records how that was checked, so the claim is checked rather than assumed.

- **Audited:** 2026-11-17, with heocr 0.0.1.
- **Versions:** the versions pip resolved then, Pillow 13.0.0 and pypdfium2 5.15.2. heocr accepts `Pillow>=10` and `pypdfium2>=4.30`.
- **Wheels inspected:** macOS arm64, manylinux x86_64 and Windows amd64.
- **Licence texts:** taken from each wheel's own licence files (`*.dist-info/licenses/`).
- **In CI:** `scripts/package_smoke.py` checks every runtime dependency's declared licence against an allow-list on every CI run.

## Runtime dependencies

| Package | Licence | What it bundles |
|---|---|---|
| **defusedxml** 0.7.1 | PSF-2.0 | Nothing (pure Python) |
| **Pillow** 13.0.0 | MIT-CMU (HPND) | Image libraries, listed below. They are shared libraries on macOS and Linux, and linked into the extension module on Windows |
| **pypdfium2** 5.15.2 | Apache-2.0 or BSD-3-Clause; the binary under its dependencies' licences | **PDFium** (`libpdfium.dylib` / `.so` / `pdfium.dll`), with the libraries listed below built in |

## Libraries inside pypdfium2's PDFium

These are from the wheel's `BUILD_LICENSES`.

| Library | Licence |
|---|---|
| PDFium | BSD-3-Clause |
| Abseil, Highway | Apache-2.0 |
| AGG 2.3 (anti-aliasing) | AGG licence (permissive, BSD-style) |
| Dragonbox | Apache-2.0 with LLVM exception, or Boost |
| fast_float | MIT, Apache-2.0 or Boost |
| FreeType | The FreeType Project License (FTL), chosen over its GPLv2 alternative; its acknowledgement is below |
| HarfBuzz | MIT ("Old MIT") |
| ICU | Unicode License v3 |
| Little CMS | MIT |
| libjpeg-turbo | IJG, BSD-3-Clause and zlib |
| OpenJPEG | BSD-2-Clause |
| libpng | PNG Reference Library License v2 |
| libtiff | libtiff licence (BSD-style) |
| simdutf | Apache-2.0 or MIT |
| zlib | zlib |
| Brotli | MIT |
| libyuv | BSD-3-Clause |
| LLVM libc pieces | Apache-2.0 with LLVM exception |

## Libraries inside Pillow

These are from the wheel's `LICENSE` and its bundled libraries.

| Library | Licence |
|---|---|
| libjpeg-turbo | IJG, BSD-3-Clause and zlib |
| libtiff | libtiff licence (BSD-style) |
| libwebp (with libsharpyuv, webpdemux, webpmux) | BSD-3-Clause |
| OpenJPEG | BSD-2-Clause |
| libpng | PNG Reference Library License v2 |
| zlib-ng / zlib | zlib |
| FreeType | FTL, chosen over GPLv2 |
| HarfBuzz | MIT |
| Little CMS | MIT |
| libavif | BSD-2-Clause |
| Brotli | MIT |
| liblzma (XZ Utils) | Public domain (0BSD). XZ's GPL and LGPL files are tools and build scripts that, as its notice says, do not end up in the binaries |
| Zstandard (Linux wheels) | BSD-3-Clause, chosen over GPLv2 |
| libxcb, libXau (macOS and Linux wheels) | MIT (X11) |

## Copyleft terms seen, and why none apply

- **Dual licences.** FreeType and Zstandard are dual-licensed: their permissive licences (FTL, BSD-3-Clause) apply. Nothing requires the GPLv2 alternative.
- **XZ Utils.** It names GPL and LGPL files that are not in the shipped library.
- **Libraries Pillow can load, but doesn't ship.** Some optional libraries are LGPL, such as FriBiDi for complex text layout. Pillow loads them at run time only when the user installs them; its wheels do not ship them, and heocr does not use them.

## Acknowledgements the licences ask for

The FreeType Project License asks for this line in the documentation of software that uses it. FreeType comes inside PDFium (pypdfium2) and inside Pillow:

> Portions of this software are copyright © 2025 The FreeType Project (www.freetype.org). All rights reserved.

## External programs (not installed by pip)

heocr runs these when they are installed. It never bundles them. See NOTICE.

| Program | Licence | Used for |
|---|---|---|
| Tesseract OCR | Apache-2.0 | OCR (optional) |
| LibreOffice | MPL-2.0 | DOC, RTF, ODT, ODS, XLS, ODP and PPT conversion (optional) |

## Optional OCR models

The opt-in capture router can use Rivok Labs' Rivok Full v2.0 int8, licensed
under Apache-2.0 and downloaded explicitly. Its
[component model card](src/heocr/models_data/MODEL_CARD.heb_rivok.md) records
the weight pin and upstream attribution. The distribution preserves the
upstream [LICENSE](src/heocr/models_data/LICENSE.heb_rivok) and
[NOTICE](src/heocr/models_data/NOTICE.heb_rivok).

## When to redo this audit

Redo it whenever a dependency is added, or a minimum version raised, and before each release (D5f). A new runtime dependency must also pass the allow-list in `scripts/package_smoke.py`.
