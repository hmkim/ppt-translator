"""Unit tests for ppt_translator.ooxml_repair.repair_relationships.

Run with:  uv run python -m unittest tests.test_ooxml_repair
"""
import os
import tempfile
import unittest
import zipfile

from ppt_translator.ooxml_repair import repair_relationships, _find_dangling

P = "http://schemas.openxmlformats.org/presentationml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG = "http://schemas.openxmlformats.org/package/2006/relationships"

# presentation.xml references rId1 via r:id (mimics <p:embeddedFont>).
PRESENTATION_XML = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<p:presentation xmlns:p="%s" xmlns:r="%s">'
    '<p:embeddedFontLst><p:embeddedFont><p:regular r:id="rId1"/>'
    '</p:embeddedFont></p:embeddedFontLst></p:presentation>' % (P, R)
).encode("utf-8")

FONT_REL = (
    '<Relationship Id="rId1" '
    'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/font" '
    'Target="NULL"/>'
)

def _rels(body: str) -> bytes:
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="%s">%s</Relationships>' % (PKG, body)
    ).encode("utf-8")


def _write_pptx(path: str, rels_body: str) -> None:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                   '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                   '<Default Extension="xml" ContentType="application/xml"/></Types>')
        z.writestr("ppt/presentation.xml", PRESENTATION_XML)
        z.writestr("ppt/_rels/presentation.xml.rels", _rels(rels_body))


class RepairRelationshipsTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.original = os.path.join(self.dir, "original.pptx")
        self.output = os.path.join(self.dir, "output.pptx")

    def _rel_ids(self, path):
        with zipfile.ZipFile(path) as z:
            return set(_find_dangling(z, set(z.namelist())).keys())

    def test_restores_dropped_relationship(self):
        # Original defines rId1; output has it dropped (simulating python-pptx).
        _write_pptx(self.original, FONT_REL)
        _write_pptx(self.output, "")  # rId1 missing -> dangling reference

        # Precondition: output is corrupt (dangling ref present).
        self.assertEqual(self._rel_ids(self.output), {"ppt/presentation.xml"})

        restored = repair_relationships(self.original, self.output)

        self.assertEqual(restored, {"ppt/presentation.xml": ["rId1"]})
        # No dangling references remain.
        self.assertEqual(self._rel_ids(self.output), set())
        # The restored relationship is present with the original Target.
        with zipfile.ZipFile(self.output) as z:
            rels = z.read("ppt/_rels/presentation.xml.rels").decode("utf-8")
        self.assertIn('Id="rId1"', rels)
        self.assertIn('Target="NULL"', rels)

    def test_noop_on_clean_file(self):
        # Output already defines rId1 -> nothing to repair.
        _write_pptx(self.original, FONT_REL)
        _write_pptx(self.output, FONT_REL)
        with open(self.output, "rb") as fh:
            before = fh.read()

        restored = repair_relationships(self.original, self.output)

        self.assertEqual(restored, {})
        # File left byte-for-byte unchanged.
        with open(self.output, "rb") as fh:
            self.assertEqual(fh.read(), before)


if __name__ == "__main__":
    unittest.main()
