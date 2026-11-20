import unittest

import heocr
from tests.synth.docx import Docx, para

FOOT = '<w:r><w:rPr><w:rStyle w:val="FootnoteReference"/></w:rPr><w:footnoteReference w:id="1"/></w:r>'
END = '<w:r><w:rPr><w:rStyle w:val="EndnoteReference"/></w:rPr><w:endnoteReference w:id="2"/></w:r>'


class NoteRefTest(unittest.TestCase):
    def doc(self, *runs: str):
        d = Docx(body=[para(*runs)], footnotes={1: "הערת שוליים"}, endnotes={2: "הערת סיום"})
        return heocr.extract(d.build())

    def test_reference_offsets_and_markdown(self) -> None:
        doc = self.doc("ראו את ההערה", FOOT, " ואת זו", END, " בסוף")
        body = doc.blocks[0]
        self.assertEqual(body.text, "ראו את ההערה ואת זו בסוף")
        self.assertEqual(body.note_refs, [{"kind": "footnote", "id": "1", "offset": 12}, {"kind": "endnote", "id": "2", "offset": 19}])
        md = doc.markdown
        self.assertIn("ראו את ההערה[^1] ואת זו[^e2] בסוף", md)
        self.assertIn("[^1]: הערת שוליים", md)
        self.assertIn("[^e2]: הערת סיום", md)
        self.assertEqual(doc.text.splitlines()[0], "ראו את ההערה ואת זו בסוף")  # the plain text stays clean
        self.assertEqual(heocr.validate(doc.to_dict()), [])

    def test_offsets_follow_direction_mark_removal(self) -> None:
        doc = self.doc("בסעיף ‏2.2", FOOT, " להלן")
        self.assertEqual(doc.blocks[0].text, "בסעיף 2.2 להלן")
        self.assertEqual(doc.blocks[0].note_refs[0]["offset"], 9)
        off = heocr.extract(Docx(body=[para("בסעיף ‏2.2", FOOT, " להלן")], footnotes={1: "x"}).build(), repairs=False)
        self.assertEqual(off.blocks[0].note_refs[0]["offset"], 10)

    def test_invalid_offset_is_rejected(self) -> None:
        payload = self.doc("טקסט", FOOT).to_dict()
        payload["blocks"][0]["note_refs"][0]["offset"] = 99
        self.assertTrue(any("note_refs" in e for e in heocr.validate(payload)))


if __name__ == "__main__":
    unittest.main()
