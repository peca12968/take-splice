import json
import os
from pathlib import Path
import random
import struct
import subprocess
import tempfile
import unittest
import zlib
from zipfile import ZipFile

from PIL import Image

from demo import demo


class AtlasTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root = tempfile.TemporaryDirectory()
        cls.addClassCleanup(root.cleanup)
        cls.exe = Path(root.name) / ("sprite-crate.exe" if os.name == "nt" else "sprite-crate")
        subprocess.run(["go", "build", "-o", cls.exe, "."], cwd=Path(__file__).parent, check=True)

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.bundle = self.root / "atlas.zip"

    def pack(self, files, size, ok=True, dest=None):
        p = subprocess.run([self.exe, "-out", dest or self.bundle, "-size", str(size), *files],
                           capture_output=True)
        self.assertEqual(p.returncode, 0 if ok else 1, p.stderr)
        if ok:
            return json.loads(p.stdout)
        self.assertFalse(p.stdout)

    def test_replay_moving_parcel(self):
        directory = self.root / "replay"
        self.assertEqual(demo(self.exe, directory),
                         {"frames": 9, "source_pixels": 36864, "pages": 1, "atlas_pixels": 2304})
        examples = Path(__file__).parent / "examples"
        with Image.open(directory / "replayed.png") as replay, Image.open(examples / "parcel-replay.png") as sample:
            self.assertEqual(replay.tobytes(), sample.tobytes())
        with ZipFile(directory / "parcel.zip") as bundle, bundle.open("page-000.png") as raw:
            with Image.open(raw) as page, Image.open(examples / "parcel-atlas.png") as sample:
                self.assertEqual(page.tobytes(), sample.tobytes())

    def test_16_bit_straight_alpha_channels(self):
        def chunk(kind, data):
            return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

        source = self.root / "rgba16.png"
        header = struct.pack(">IIBBBBB", 2, 1, 16, 6, 0, 0, 0)
        samples = struct.pack(">8H", 14000, 3000, 100, 255, 0x7190, 0xBEE1, 0x0160, 0x0100)
        source.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header)
                           + chunk(b"IDAT", zlib.compress(b"\x00" + samples)) + chunk(b"IEND", b""))
        m = self.pack([source], 3)
        self.assertEqual(m["frames"][0]["offset"], [1,0])
        with ZipFile(self.bundle) as archive, archive.open(m["pages"][0]) as raw, Image.open(raw) as page:
            self.assertEqual(page.getpixel((1,1)), (0x71,0xBE,0x01,0x01))

    def test_crops_gutters_and_reconstruction_across_pages(self):
        rng = random.Random(9031)
        sources = {}
        for i in range(64):
            w,h = rng.randint(1,12), rng.randint(1,12)
            left,top = rng.randrange(9), rng.randrange(9)
            source = Image.new("RGBA", (left+w+3,top+h+2), (255,37,18,0))
            for y in range(h):
                for x in range(w):
                    if rng.random() < .7:
                        source.putpixel((left+x,top+y), tuple(rng.randrange(256) for _ in range(3))
                                        + (rng.choice([1,77,128,255]),))
            path = self.root / f"f{i:02}.png"
            source.save(path)
            sources[path] = source
        m = self.pack(sources, 18)
        original = self.bundle.read_bytes()
        self.assertGreater(len(m["pages"]), 1)
        occupied = [set() for _ in m["pages"]]
        self.assertEqual([f["name"] for f in m["frames"]], sorted(p.name for p in sources))
        with ZipFile(self.bundle) as archive:
            self.assertEqual(json.loads(archive.read("manifest.json")), m)
            pages = []
            for name in m["pages"]:
                with archive.open(name) as raw, Image.open(raw) as page:
                    self.assertEqual(page.size, (18,18))
                    pages.append(page.convert("RGBA"))
            for frame in m["frames"]:
                source = sources[self.root / frame["name"]]
                self.assertEqual(tuple(frame["source_size"]), source.size)
                box = source.getchannel("A").getbbox()
                if box is None:
                    self.assertEqual(frame["page"], -1)
                    continue
                l,t,r,b = box
                x,y,w,h = frame["atlas"]
                self.assertEqual((w,h), (r-l,b-t))
                self.assertEqual(frame["offset"], [l,t])
                idx = frame["page"]
                self.assertTrue(0 <= idx < len(pages))
                for dy in range(-1,h+1):
                    for dx in range(-1,w+1):
                        pos = x+dx, y+dy
                        self.assertTrue(all(0 <= coord < 18 for coord in pos))
                        self.assertNotIn(pos, occupied[idx])
                        occupied[idx].add(pos)
                        rgba = source.getpixel((l+max(0,min(w-1,dx)),t+max(0,min(h-1,dy))))
                        self.assertEqual(pages[idx].getpixel(pos), rgba if rgba[3] else (0,0,0,0))
        self.pack(reversed(list(sources)),18)
        self.assertEqual(self.bundle.read_bytes(), original)

    def test_blank_inputs_palette_and_exact_page_fit(self):
        p = self.root / "pal.png"
        img = Image.new("P", (3,3), 0)
        img.putpalette([0,0,0,19,214,73] + [0]*762)
        img.save(p, transparency=0)
        blank = self.pack([p],5)
        self.assertEqual(blank["pages"], [])
        self.assertEqual(blank["frames"][0]["page"], -1)
        img.putpixel((0,0),1)
        img.putpixel((2,2),1)
        img.save(p, transparency=0)
        m = self.pack([p],5)
        self.assertEqual(m["frames"][0]["atlas"], [1,1,3,3])
        with ZipFile(self.bundle) as archive, archive.open(m["pages"][0]) as raw, Image.open(raw) as page:
            self.assertEqual(page.convert("RGBA").getpixel((0,0)), (19,214,73,255))

    def test_errors_preserve_inputs_and_prior_archive(self):
        p = self.root / "source.png"
        Image.new("RGB", (6,4), "red").save(p)
        original = p.read_bytes()
        self.bundle.write_bytes(b"previous")
        bad = self.root / "bad.png"
        bad.write_bytes(b"truncated")
        for files, size in [([p],7), ([p,p],16), ([p,bad],16), ([],16), ([p],4097)]:
            self.pack(files,size,ok=False)
            self.assertEqual(self.bundle.read_bytes(), b"previous")
        self.pack([p],16,ok=False,dest=p)
        self.assertEqual(p.read_bytes(), original)
        self.assertFalse(list(self.root.glob(".sprite-crate-*.zip")))


if __name__ == "__main__":
    unittest.main()
