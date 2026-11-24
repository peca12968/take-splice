import hashlib
import itertools
import json
from pathlib import Path
import random
import subprocess
import sys
import tempfile
import unittest

from shard_rescue import BLOCK, SLOTS, STRIPE_BYTES, pack, part, rebuild_volume, restore
from demo import demo, range_demo


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
            "before_rebuild": [
                {"stripe": 0, "faults": {"d1": "missing", "d3": "wrong_length"}, "recoverable": True},
                {"stripe": 1, "faults": {"d1": "missing", "q": "digest_mismatch"}, "recoverable": True}],
            "replacement": {"slot": "d1", "copied": 0, "rebuilt": 2, "bytes": 32768},
            "after_rebuild": [
                {"stripe": 0, "faults": {"d3": "wrong_length"}, "recoverable": True},
                {"stripe": 1, "faults": {"q": "digest_mismatch"}, "recoverable": True}],
        })

    def test_every_volume_rebuild_with_each_possible_second_erasure(self):
        self.prepare(BLOCK * 4 + 37)
        for slot, other in itertools.permutations(SLOTS, 2):
            with self.subTest(slot=slot, other=other):
                expected = part(self.archive, 1, slot).read_bytes()
                second = part(self.archive, 1, other)
                original = second.read_bytes()
                part(self.archive, 1, slot).unlink()
                second.write_bytes(b"bad")
                with tempfile.TemporaryDirectory(dir=self.root) as tmp:
                    spare = Path(tmp) / "spare"
                    result = rebuild_volume(self.archive, slot, spare)
                    self.assertEqual((result["copied"], result["rebuilt"]), (1, 1))
                    self.assertEqual((spare / "00000001.bin").read_bytes(), expected)
                    self.assertEqual((spare / "00000000.bin").read_bytes(), part(self.archive, 0, slot).read_bytes())
                    self.assertFalse(part(self.archive, 1, slot).exists())
                part(self.archive, 1, slot).write_bytes(expected)
                second.write_bytes(original)

    def test_scrub_exit_codes_and_late_failure_cleanup(self):
        self.prepare(8 * BLOCK + 1)
        def audit(code):
            command = self.cli("scrub", self.archive)
            self.assertEqual(command.returncode, code, command.stderr)
            return json.loads(command.stdout)

        self.assertEqual(audit(0), {"status": "healthy", "stripes": 3, "issues": []})
        part(self.archive, 1, "q").unlink()
        self.assertEqual(audit(2)["status"], "degraded")
        part(self.archive, 1, "d0").write_bytes(b"short")
        part(self.archive, 1, "d2").unlink()
        part(self.archive, 2, "q").write_bytes(b"short")
        report = audit(3)
        self.assertEqual(report["status"], "unrecoverable")
        self.assertEqual([issue["recoverable"] for issue in report["issues"]], [False, True])
        proc = self.cli("rebuild-volume", self.archive, "q", self.output)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("stripe 1: 3 unusable", proc.stderr)
        self.assertFalse(self.output.exists())

    def test_verified_copy_even_when_other_slots_are_lost_and_digest_failure(self):
        self.prepare(4 * BLOCK)
        for slot in ("d0", "p", "q"):
            part(self.archive, 0, slot).unlink()
        answer = self.cli("rebuild-volume", self.archive, "d2", self.output)
        self.assertEqual(answer.returncode, 0, answer.stderr)
        self.assertEqual(json.loads(answer.stdout)["copied"], 1)
        with self.assertRaises(FileExistsError):
            rebuild_volume(self.archive, "d2", self.output)
        self.assertEqual((self.output / "00000000.bin").read_bytes(), part(self.archive, 0, "d2").read_bytes())
        with self.assertRaisesRegex(ValueError, "outside"):
            rebuild_volume(self.archive, "d2", self.archive / "extra")
        # Repeat with coherent surviving parity but a wrong expected digest for the lost shard.
        clean = self.root / "clean"
        pack(self.source, clean)
        part(clean, 0, "p").unlink()
        path = clean / "manifest.json"
        manifest = json.loads(path.read_text())
        manifest["stripes"][0][4] = "0" * 64
        path.write_text(json.dumps(manifest))
        spare = self.root / "spare"
        with self.assertRaisesRegex(ValueError, "digest mismatch"):
            rebuild_volume(clean, "p", spare)
        self.assertFalse(spare.exists())

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

    def test_range_demo_matches_recorded_result_after_whole_file_fails(self):
        fixture = Path(__file__).parent / "examples" / "range-demo.json"
        self.assertEqual(range_demo(self.root), json.loads(fixture.read_text(encoding="utf-8")))

    def test_range_at_stripe_boundary_skips_unrelated_failure_and_excludes_padding(self):
        content = self.prepare(3 * STRIPE_BYTES - 7)
        for slot in ("d0", "p", "q"):
            part(self.archive, 1, slot).unlink()
        for offset, length in ((STRIPE_BYTES - 11, 11), (2 * STRIPE_BYTES, STRIPE_BYTES - 7), (len(content), 0)):
            with self.subTest(offset=offset, length=length):
                result = self.cli("restore-range", self.archive, self.output, str(offset), str(length))
                self.assertEqual(result.returncode, 0, result.stderr)
                expected = content[offset:offset + length]
                self.assertEqual(self.output.read_bytes(), expected)
                self.assertEqual(json.loads(result.stdout), {
                    "offset": offset, "bytes": length, "unusable_shards": 0,
                    "stripes_read": int(bool(length)), "verification": "shard_digests",
                    "sha256": hashlib.sha256(expected).hexdigest(),
                })

        self.output.write_bytes(b"previously verified")
        before = set(self.root.iterdir())
        for offset, length, diagnostic in ((STRIPE_BYTES - 1, 2, "stripe 1: 3 unusable"),
                                           (len(content), 1, "outside"), (-1, 1, "outside"),
                                           (0, -1, "outside")):
            result = self.cli("restore-range", self.archive, self.output, str(offset), str(length))
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertIn(diagnostic, result.stderr)
            self.assertEqual(result.stdout, "")
            self.assertEqual(self.output.read_bytes(), b"previously verified")
            self.assertEqual(set(self.root.iterdir()), before)

    def test_whole_interval_retains_whole_file_integrity_check(self):
        self.prepare(3)
        result = restore(self.archive, self.output, interval=(0, 3))
        self.assertEqual(result["verification"], "whole_file")
        path = self.archive / "manifest.json"
        record = json.loads(path.read_text())
        path.write_text(json.dumps(record | {"sha256": "0" * 64}))
        with self.assertRaisesRegex(ValueError, "digest or length mismatch"):
            restore(self.archive, self.output, interval=(0, 3))

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
        return subprocess.run([sys.executable, "-X", "utf8", str(Path(__file__).with_name("shard_rescue.py")), *args],
                              capture_output=True, encoding="utf-8")

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
