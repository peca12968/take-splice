"""Impose static PDF pages into nested, duplex-printable booklet signatures."""
import argparse
import json
from math import isfinite
import os
from pathlib import Path
from tempfile import NamedTemporaryFile

from pypdf import PdfReader, PdfWriter, Transformation
from pypdf.errors import PyPdfError
from pypdf.generic import RectangleObject


PT_PER_MM = 72 / 25.4
PAPERS = {"a4": (297 * PT_PER_MM, 210 * PT_PER_MM), "letter": (792, 612)}


def signatures(pages, max_sheets):
    """Yield signatures in binding order and sheets from outside to inside."""
    for first in range(0, pages, 4 * max_sheets):
        count = min(pages - first, 4 * max_sheets)
        slots = list(range(first, first + count)) + [None] * (-count % 4)
        sheets = []
        for i in range(len(slots) // 4):
            low, high = 2 * i, len(slots) - 1 - 2 * i
            sheets.append(((slots[high], slots[low]), (slots[low + 1], slots[high - 1])))
        yield sheets


def place(side, page, x, cell_w, height, margin):
    if "/Annots" in page:
        for ref in page["/Annots"]:
            if ref.get_object().get("/Subtype") != "/Link":
                raise ValueError("flatten printable annotations and forms before imposing")
        page.pop("/Annots")
    crop, media = page.cropbox, page.mediabox
    left, right = max(crop.left, media.left), min(crop.right, media.right)
    bottom, top = max(crop.bottom, media.bottom), min(crop.top, media.top)
    if right <= left or top <= bottom:
        raise ValueError("page has no visible crop area")
    page.cropbox = RectangleObject((left, bottom, right, top))
    # Rotate the visible rectangle clockwise and move its lower-left corner to (0, 0).
    rotation = page.rotation % 360
    matrices = {0: (1, 0, 0, 1, -left, -bottom), 90: (0, -1, 1, 0, -bottom, right),
                180: (-1, 0, 0, -1, right, top), 270: (0, 1, -1, 0, top, -left)}
    if rotation not in matrices:
        raise ValueError("PDF page rotation must be a multiple of 90 degrees")
    w, h = float(right - left), float(top - bottom)
    if rotation in (90, 270):
        w, h = h, w
    scale = min((cell_w - 2 * margin) / w, (height - 2 * margin) / h)
    tx = x + (cell_w - w * scale) / 2
    ty = (height - h * scale) / 2
    transform = Transformation(matrices[rotation]).scale(scale).translate(tx, ty)
    side.merge_transformed_page(page, transform)


def impose(source, output, *, max_sheets=4, paper="a4", binding="left", margin_mm=6):
    if source.resolve() == output.resolve():
        raise ValueError("choose a different path for the output PDF")
    if max_sheets < 1 or binding not in ("left", "right") or paper not in PAPERS:
        raise ValueError("invalid signature size, binding or paper")
    width, height = PAPERS[paper]
    margin = margin_mm * PT_PER_MM
    if not isfinite(margin) or margin < 0 or 2 * margin >= min(width / 2, height):
        raise ValueError("margin leaves no printable area")
    with source.open("rb") as stream, PdfWriter() as writer:
        writer.pdf_header = "%PDF-1.7"
        writer.create_viewer_preferences()
        writer.viewer_preferences.duplex = "/DuplexFlipShortEdge"
        writer.viewer_preferences.print_scaling = "/None"
        reader = PdfReader(stream)
        if reader.is_encrypted:
            raise ValueError("provide an unencrypted print copy")
        pages = len(reader.pages)
        if not pages:
            raise ValueError("input has no pages")
        for signature in signatures(pages, max_sheets):
            for sheet in signature:
                for pair in sheet:
                    side = writer.add_blank_page(width, height)
                    for half, index in enumerate(pair if binding == "left" else reversed(pair)):
                        if index is not None:
                            place(side, reader.pages[index], half * width / 2, width / 2, height, margin)
        tmp = None
        try:
            with NamedTemporaryFile(dir=output.parent, suffix=".pdf", delete=False) as staged:
                tmp = Path(staged.name)
                writer.write(staged)
            os.replace(tmp, output)
        finally:
            if tmp is not None:
                tmp.unlink(missing_ok=True)
    return {"source_pages": pages, "signatures": (pages - 1) // (4 * max_sheets) + 1,
            "sheets": (pages + 3) // 4, "blank_panels": -pages % 4}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--sheets-per-signature", type=int, default=4)
    parser.add_argument("--binding", choices=("left", "right"), default="left")
    parser.add_argument("--paper", choices=PAPERS, default="a4")
    parser.add_argument("--margin-mm", type=float, default=6)
    args = parser.parse_args()
    try:
        result = impose(args.source, args.output, max_sheets=args.sheets_per_signature,
                        paper=args.paper, binding=args.binding, margin_mm=args.margin_mm)
    except (ValueError, OSError, PyPdfError) as exc:
        parser.exit(2, f"{exc}\n")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
