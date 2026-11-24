from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
import wave
from zipfile import ZipFile

from demo import HEADER, ROOT, make_take


CUE = ROOT / "modules/cue-conform"
CLI = CUE / "deliver.py"
TRACKS = CUE / "examples/tracks.json"


class DeliveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        temporary = TemporaryDirectory()
        cls.addClassCleanup(temporary.cleanup)
        cls.root = Path(temporary.name)
        make_take(cls.root / "first.wav", 120, 160)
        make_take(cls.root / "second.wav", 200, 240)
        cls.plan = cls.root / "edit.tsv"
        cls.plan.write_text(HEADER + "first.wav\t24000\t48000\nsecond.wav\t99\t24100\nfirst.wav\t24000\t36000\n",
                            encoding="utf-8")
        cls.release = cls.root / "release.zip"
        result = cls.cli("build", cls.plan, TRACKS, cls.release, "--edge-fade-frames", 240)
        if result.returncode:
            raise RuntimeError(result.stderr)
        cls.report = json.loads(result.stdout)
        with ZipFile(cls.release) as archive:
            cls.members = {name: archive.read(name) for name in archive.namelist()}

    @staticmethod
    def cli(*args):
        return subprocess.run([sys.executable, "-X", "utf8", CLI, *map(str, args)], capture_output=True, text=True,
                              encoding="utf-8", timeout=30)

    def test_repeated_build_is_identical_and_verified_without_extraction(self):
        output = self.root / "second-release.zip"
        result = self.cli("build", self.plan, TRACKS, output, "--edge-fade-frames", 240)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(output.read_bytes(), self.release.read_bytes())
        self.assertEqual(set(self.members), {"manifest.json", "audio.wav", "captions.vtt"})
        self.assertEqual(self.members["captions.vtt"], (CUE / "examples/edited.vtt").read_text(encoding="utf-8").encode())
        self.assertNotEqual(self.members["audio.wav"], (ROOT / "examples/preview.wav").read_bytes())
        self.assertEqual(self.report["manifest"]["audio"], {"frames": 60001, "sample_rate": 48000,
                                                            "channels": 2, "bits": 16})
        self.assertEqual(self.report["manifest"]["captions"], {"cues": 6, "last_end_ms": 1251})
        digest = hashlib.sha256(self.release.read_bytes()).hexdigest()
        original_files = set(self.root.iterdir())
        result = self.cli("verify", self.release, "--sha256", digest.upper())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), self.report)
        self.assertEqual(set(self.root.iterdir()), original_files)

    def test_odd_size_24_bit_mono_delivery_can_omit_captions(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            with wave.open(str(root / "mono.wav"), "wb") as wav:
                wav.setparams((1, 3, 44100, 0, "NONE", "not compressed"))
                wav.writeframes(b"\x00\x00\x01" * 4)
            plan = root / "edit.tsv"
            plan.write_text(HEADER + "mono.wav\t0\t3\n", encoding="utf-8")
            tracks = root / "tracks.json"
            tracks.write_text('{"mono.wav": null}', encoding="utf-8")
            output = root / "mono.zip"
            result = self.cli("build", plan, tracks, output)
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(result.stdout)
            self.assertEqual(report["manifest"]["audio"], {"frames": 3, "sample_rate": 44100, "channels": 1, "bits": 24})
            self.assertEqual(report["manifest"]["captions"], {"cues": 0, "last_end_ms": 0})
            self.assertEqual(self.cli("verify", output).returncode, 0)

    def rewrite(self, name, replacements):
        path = self.root / name
        with ZipFile(path, "x") as archive:
            for member, contents in (self.members | replacements).items():
                archive.writestr(member, contents)
        return path

    def test_replaced_captions_fail_asset_digest_and_trusted_archive_digest(self):
        edited = self.members["captions.vtt"].replace("高音".encode(), "旁白".encode())
        output = self.rewrite("swapped.zip", {"captions.vtt": edited})
        result = self.cli("verify", output)
        self.assertEqual(result.returncode, 2)
        self.assertIn("captions.vtt: byte length or SHA-256 differs", result.stderr)
        result = self.cli("verify", output, "--sha256", self.report["sha256"])
        self.assertEqual(result.returncode, 2)
        self.assertIn("archive SHA-256 differs", result.stderr)
        fixture = ROOT / "examples/delivery-demo.json"
        self.assertEqual(json.loads(fixture.read_text(encoding="utf-8")), {
            **self.report,
            "altered": {"file": "captions.vtt", "expected_sha256": self.report["manifest"]["files"]["captions.vtt"]["sha256"],
                        "observed_sha256": hashlib.sha256(edited).hexdigest(), "verified": False},
        })

    def test_manifest_with_correct_hashes_still_checks_caption_timeline_and_metadata(self):
        edited = self.members["captions.vtt"].replace(b"00:00:01.251", b"00:00:09.251")
        updated = json.loads(self.members["manifest.json"])
        updated["files"]["captions.vtt"]["sha256"] = hashlib.sha256(edited).hexdigest()
        output = self.rewrite("overrun.zip", {"captions.vtt": edited, "manifest.json": json.dumps(updated)})
        result = self.cli("verify", output)
        self.assertEqual(result.returncode, 2)
        self.assertIn("cue ends after the audio timeline", result.stderr)
        stale = json.loads(self.members["manifest.json"])
        stale["audio"]["frames"] -= 1
        output = self.rewrite("stale.zip", {"manifest.json": json.dumps(stale)})
        result = self.cli("verify", output)
        self.assertEqual(result.returncode, 2)
        self.assertIn("manifest does not match packaged media metadata", result.stderr)

    def test_rejects_unexpected_members_and_truncated_archive(self):
        expanded = self.rewrite("extra.zip", {"../out.txt": b"unexpected"})
        truncated = self.root / "truncated.zip"
        truncated.write_bytes(self.release.read_bytes()[:-9])
        for package in (expanded, truncated):
            with self.subTest(package=package.name):
                result = self.cli("verify", package)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, "")

    def test_failed_and_concurrent_exports_never_publish_an_incomplete_zip(self):
        output = self.root / "shared.zip"
        missing_track = self.root / "missing-track.json"
        missing_track.write_text('{"first.wav": "missing.vtt", "second.wav": null}', encoding="utf-8")
        failure = self.cli("build", self.plan, missing_track, output)
        self.assertEqual(failure.returncode, 2)
        self.assertIn("missing.vtt", failure.stderr)
        self.assertFalse(output.exists())
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.cli("build", self.plan, TRACKS, output), range(2)))
        self.assertEqual(sorted(result.returncode for result in results), [0, 2])
        original = output.read_bytes()
        self.assertEqual(self.cli("verify", output).returncode, 0)
        self.assertEqual(self.cli("build", self.plan, TRACKS, output, "--edge-fade-frames", 240).returncode, 2)
        self.assertEqual(output.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
