import itertools
import json
from pathlib import Path
import re
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

import pypdfium2 as pdfium
from pypdf import PdfReader, PdfWriter
from pypdf.generic import RectangleObject
from reportlab.pdfgen.canvas import Canvas

from fold_proof import impose


ROOT = Path(__file__).resolve().parent


def panels(path):
    pairs = []
    with pdfium.PdfDocument(path) as document:
        for page in document:
            width, height = page.get_size()
            text = page.get_textpage()
            pair = []
            for half in range(2):
                words = text.get_text_bounded(half * width / 2, 0, (half + 1) * width / 2, height)
                if words.strip():
                    numbers = re.findall(r"PAGE (\d+)", words)
                    if len(numbers) != 1:
                        raise AssertionError(f"expected one folio in panel: {words!r}")
                    pair.append(int(numbers[0]))
                else:
                    pair.append(None)
            pairs.append(pair)
            page.close()
    return pairs


def fold_and_read(sides, sheets, binding):
    result = []
    for start in range(0, len(sides), 2 * sheets):
        gathering = sides[start:start + 2 * sheets]
        inside = []
        opening = 1 if binding == "left" else 0
        for front, back in reversed(list(zip(gathering[::2], gathering[1::2], strict=True))):
            inside = [front[opening], back[1 - opening], *inside, back[opening], front[1 - opening]]
        result.extend(inside)
    return result


class FoldTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.source = self.root / "source.pdf"
        self.output = self.root / "result.pdf"

    def test_72_layouts_read_back_in_sequence(self):
        for count in range(1, 13):
            pages = Canvas(str(self.source), pagesize=(150, 220))
            for n in range(1, count + 1):
                pages.drawString(30, 50, f"PAGE {n}")
                pages.showPage()
            pages.save()
            for sheets, binding in itertools.product((1, 2, 4), ("left", "right")):
                with self.subTest(count=count, sheets=sheets, binding=binding):
                    impose(self.source, self.output, max_sheets=sheets, binding=binding)
                    ordered = fold_and_read(panels(self.output), sheets, binding)
                    self.assertEqual(ordered, list(range(1, count + 1)) + [None] * (-count % 4))

    def test_rotated_offset_crop_matches_source_pixels(self):
        raw = self.root / "colors.pdf"
        c = Canvas(str(raw), pagesize=(280, 300))
        c.setFillColorRGB(1, 0, 1)
        c.rect(0, 0, 280, 300, fill=1, stroke=0)
        for x, y, rgb in [(30, 40, (1, 0, 0)), (110, 40, (0, 1, 0)),
                          (30, 150, (0, 0, 1)), (110, 150, (1, 1, 0))]:
            c.setFillColorRGB(*rgb)
            c.rect(x, y, 80, 110, fill=1, stroke=0)
        c.showPage()
        c.save()
        for angle in (0, 90, 180, 270):
            with self.subTest(angle=angle), PdfWriter() as w:
                page = w.add_page(PdfReader(raw).pages[0])
                page.cropbox = RectangleObject((30, 40, 190, 260))
                page.rotate(angle)
                w.write(self.source)
                impose(self.source, self.output, paper="letter", margin_mm=0)
                with pdfium.PdfDocument(self.source) as src, pdfium.PdfDocument(self.output) as dst:
                    original = src[0].render().to_pil().convert("RGB")
                    image = dst[0].render().to_pil().convert("RGB")
                self.assertEqual(image.size, (792, 612))
                cell = image.width // 2
                scale = min(cell / original.width, image.height / original.height)
                wpx, hpx = original.width * scale, original.height * scale
                left, top = cell + (cell - wpx) / 2, (image.height - hpx) / 2
                for fx, fy in itertools.product((.1, .8), repeat=2):
                    self.assertEqual(image.getpixel((int(left + fx * wpx), int(top + fy * hpx))),
                                     original.getpixel((int(fx * original.width), int(fy * original.height))))
                colors = {rgb for _, rgb in image.getcolors(image.width * image.height)}
                self.assertNotIn((255, 0, 255), colors)
                self.assertEqual(image.crop((0, 0, cell, image.height)).getextrema(), ((255, 255),) * 3)

    def test_cli_and_committed_print_sample(self):
        result = subprocess.run([sys.executable, ROOT / "fold_proof.py", ROOT / "examples/reading.pdf",
                                 self.output, "--sheets-per-signature", "2"], capture_output=True, check=True)
        self.assertEqual(json.loads(result.stdout), {"source_pages": 11, "signatures": 2,
                                                     "sheets": 3, "blank_panels": 1})
        expected = [[8, 1], [2, 7], [6, 3], [4, 5], [None, 9], [10, 11]]
        self.assertEqual(panels(self.output), expected)
        self.assertEqual(panels(ROOT / "examples/booklet.pdf"), expected)
        for out in (self.output, ROOT / "examples/booklet.pdf"):
            prefs = PdfReader(out).viewer_preferences
            self.assertEqual((prefs.duplex, prefs.print_scaling), ("/DuplexFlipShortEdge", "/None"))

    def test_interactive_field_requires_a_static_print_copy(self):
        page = Canvas(str(self.source))
        page.acroForm.textfield(name="notes", x=100, y=200, width=80, height=15)
        page.showPage()
        page.save()
        self.output.write_bytes(b"prior copy")
        with self.assertRaisesRegex(ValueError, "flatten"):
            impose(self.source, self.output)
        self.assertEqual(self.output.read_bytes(), b"prior copy")

    def test_failure_preserves_output_and_source(self):
        self.output.write_bytes(b"existing job")
        empty = PdfWriter()
        empty.write(self.source)
        with self.assertRaisesRegex(ValueError, "no pages"):
            impose(self.source, self.output)
        self.assertEqual(self.output.read_bytes(), b"existing job")
        before = self.source.read_bytes()
        with self.assertRaisesRegex(ValueError, "different path"):
            impose(self.source, self.source)
        self.assertEqual(self.source.read_bytes(), before)
        for margin in (106, float("nan"), -1):
            with self.assertRaisesRegex(ValueError, "margin"):
                impose(self.source, self.output, margin_mm=margin)


if __name__ == "__main__":
    unittest.main()
