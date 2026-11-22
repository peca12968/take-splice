import hashlib
import itertools
import json
from pathlib import Path
import random
import subprocess
import sys
import tempfile
import unittest

from shard_rescue import BLOCK, SLOTS, pack, part, restore
from demo import demo


class RescueTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.source = self.root / "source"
        self.archive = self.root / "archive"
        self.output = self.root / "output"

    def prepare(self, size):
        content = random.Random(2026).randbytes(size)
        self.source.write_bytes(content)
        pack(self.source, self.archive)
        return content

    def test_all_15_pairs_and_every_single_loss(self):
        content = self.prepare(BLOCK * 4)
        for count in (0, 1, 2):
            for slots in itertools.combinations(SLOTS, count):
                with self.subTest(slots=slots):
                    moved = [(part(self.archive, 0, slot), self.root / slot) for slot in slots]
                    for src, dest in moved:
                        src.rename(dest)
                    try:
                        result = restore(self.archive, self.output)
                        self.assertEqual(result["unusable_shards"], count)
                        self.assertEqual(self.output.read_bytes(), content)
                    finally:
                        for src, dest in moved:
                            dest.rename(src)

    def test_golden_parity_bytes(self):
        # Hand-calculated P=01^80^40^ff=3e. Q=01^1d^1d^ab=aa.
        data = b"".join(bytes([v]) * BLOCK for v in (1, 128, 64, 255))
        self.source.write_bytes(data)
        pack(self.source, self.archive)
        for slot, expected in (("p", 0x3e), ("q", 0xaa)):
            self.assertEqual(part(self.archive, 0, slot).read_bytes(), bytes([expected]) * BLOCK)

    def test_audio_recovery_across_two_stripes(self):
        report = demo(self.root)
        self.assertEqual(report, {
            "stripes": 2, "frames": 48000, "bytes": 96044, "unusable_shards": 4,
            "sha256": "e50c0905655e4884108eeea28e5ba22fc2012de6e0f7fc0ca4541d4e27e8d44a",
        })

    def test_three_bad_shards_in_last_stripe_preserve_prior_output(self):
        self.prepare(4 * BLOCK + 1)
        part(self.archive, 1, "d0").unlink()
        part(self.archive, 1, "d1").write_bytes(bytes([1]) * BLOCK)
        part(self.archive, 1, "p").write_bytes(b"short")
        self.output.write_bytes(b"prior output")
        before = set(self.root.iterdir())
        proc = self.cli("restore", self.archive, self.output)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("stripe 1: 3 unusable", proc.stderr)
        self.assertEqual(self.output.read_bytes(), b"prior output")
        self.assertEqual(set(self.root.iterdir()), before)

    def test_empty_and_boundary_sizes(self):
        for size in (0, 1, BLOCK-1, BLOCK, 4*BLOCK-1, 4*BLOCK+1):
            with self.subTest(size=size):
                self.archive = self.root / f"archive-{size}"
                content = self.prepare(size)
                answer = restore(self.archive, self.output)
                self.assertEqual(self.output.read_bytes(), content)
                self.assertEqual(answer["sha256"], hashlib.sha256(content).hexdigest())

    def test_manifest_and_final_digest_validation(self):
        self.prepare(321)
        path = self.archive / "manifest.json"
        record = json.loads(path.read_text())
        for patch in ({"size": True}, {"sha256": "0"*64}, {"stripes": []}, {"stripes": [["bad"]*6]}):
            with self.subTest(patch=patch):
                path.write_text(json.dumps(record | patch))
                with self.assertRaises(ValueError):
                    restore(self.archive, self.output)
                self.assertFalse(self.output.exists())

    def cli(self, *args):
        return subprocess.run([sys.executable, str(Path(__file__).with_name("shard_rescue.py")), *args],
                              capture_output=True, text=True)

    def test_cli_pack_restore_and_archive_preservation(self):
        self.source.write_bytes(b"\x00\xff" * 100)
        self.assertEqual(self.cli("pack", self.source, self.archive).returncode, 0)
        self.assertNotEqual(self.cli("pack", self.source, self.archive).returncode, 0)
        metadata = self.archive / "manifest.json"
        with self.assertRaisesRegex(ValueError, "outside"):
            restore(self.archive, metadata)
        answer = self.cli("restore", self.archive, self.output)
        self.assertEqual(answer.returncode, 0, answer.stderr)
        self.assertEqual(json.loads(answer.stdout)["bytes"], 200)
        self.assertEqual(self.output.read_bytes(), self.source.read_bytes())


if __name__ == "__main__":
    unittest.main()
