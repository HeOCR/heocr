# heocr

**The best open pipeline for getting Hebrew text out of any document.**

`heocr` looks at a document, works out what it is (its format, and for a PDF the state of each page's text layer), and sends each part to the right tool: the text layer where it can be trusted, the office XML for Word files, and OCR where the text exists only as an image. Every decision is recorded in the output, and text that could not be read is reported, never dropped silently.

> Status: early development (milestone D2). Nothing is on PyPI yet.

```python
import heocr

doc = heocr.extract("tender.pdf")   # or .docx
doc.text        # plain text in reading order
doc.markdown    # headings, numbered sections and tables kept
doc.to_json()   # heocr_document.v1: every block with its route, tool, flags
```

```sh
heocr tender.docx -o tender.md
```

## Licence

Apache-2.0. The core depends only on permissive libraries (pypdfium2, pdfminer.six, Pillow, defusedxml). Copyleft tools are never in a default route; where they are offered, it is as an optional extra that says so.

## Project

`heocr` is part of [HeOCR](https://github.com/HeOCR), an open Hebrew OCR project. Its plan is in [`planning/`](planning/); the project-wide decisions behind it (0019, 0020) are in [heocr-engine](https://github.com/HeOCR/heocr-engine/tree/main/planning/decisions).
