"""Generate an eleven-page printer's field guide in reading order."""
from pathlib import Path

from reportlab.lib.colors import HexColor
from reportlab.pdfgen.canvas import Canvas


GUIDE = [
    ("The fold", "proof", "A pocket guide to checking a booklet", "before you print the full run."),
    ("Count the", "panels", "One folded sheet carries four pages.", "Include any final blank in your count."),
    ("Choose", "the stock", "Select the same paper size in the", "print dialog and the imposition tool."),
    ("Find the", "spine", "A left-bound cover opens to the left.", "Check the edge before your test print."),
    ("Leave", "room", "Keep ink away from the printer margin", "and give each half space at the fold."),
    ("Look at", "the crop", "Only the visible part of the source", "page should appear on the sheet."),
    ("Read it", "upright", "Check the top of both halves.", "Rotated source pages should stay upright."),
    ("Turn the", "sheet", "Print the landscape sheet on both sides.", "Use short-edge duplex for this layout."),
    ("Nest the", "sheets", "The outer sheet wraps the inner sheet.", "Keep each gathering in print order."),
    ("Test the", "sequence", "Fold each gathering and turn its pages.", "The printed folios should climb by one."),
    ("Keep the", "proof", "Check the last folio and the blank panel.", "Save a folded copy with the print job."),
]


def make_sample(path):
    canvas = Canvas(str(path), pagesize=(288, 396), invariant=True, pageCompression=1)
    canvas.setTitle("The fold proof")
    for n, (line1, line2, copy1, copy2) in enumerate(GUIDE, start=1):
        dark = n == 1
        ink, ground = ("#f3ecdd", "#153d3e") if dark else ("#153d3e", "#f3ecdd")
        accent = "#edaf65" if dark else "#99512b"
        canvas.setFillColor(HexColor(ground))
        canvas.rect(0, 0, 288, 396, fill=1, stroke=0)
        canvas.setFillColor(HexColor(ink))
        canvas.setFont("Helvetica-Bold", 9)
        canvas.drawString(24, 361, "PRINT WORKSHOP   /   FIELD GUIDE")
        canvas.setStrokeColor(HexColor(ink))
        canvas.line(24, 346, 264, 346)
        canvas.setFont("Times-Roman", 49)
        canvas.drawString(22, 270, line1)
        canvas.drawString(22, 221, line2)
        canvas.setFillColor(HexColor(accent))
        canvas.setFont("Courier-Bold", 10)
        canvas.drawString(24, 172, f"PROOF NOTE   {n:02d} / 11")
        canvas.setFillColor(HexColor(ink))
        canvas.setFont("Helvetica", 11)
        canvas.drawString(24, 135, copy1)
        canvas.drawString(24, 119, copy2)
        canvas.line(24, 56, 264, 56)
        canvas.setFont("Courier", 9)
        canvas.drawString(24, 36, f"PAGE {n:02d}")
        canvas.showPage()
    canvas.save()


if __name__ == "__main__":
    make_sample(Path(__file__).parent / "examples/reading.pdf")
