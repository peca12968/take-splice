from concurrent.futures import ThreadPoolExecutor
from fractions import Fraction
import json
from pathlib import Path
import random
import struct
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
import wave

from demo import DLL, HEADER, ROOT, make_take


def chunk(tag, data):
    return tag + struct.pack("<I", len(data)) + data + bytes(len(data) % 2)


def riff(chunks):
    body = b"WAVE" + b"".join(chunks)
    return b"RIFF" + struct.pack("<I", len(body)) + body


def fmt(channels, bits, rate=48000):
    align = channels * bits // 8
    return struct.pack("<HHIIHH", 1, channels, rate, rate*align, align, bits)


def seam_samples(path, start):
    with wave.open(str(path)) as audio:
        audio.setpos(start - 1)
        left_before, right_before, left_after, right_after = struct.unpack("<hhhh", audio.readframes(2))
    return {"before": [left_before, right_before], "after": [left_after, right_after]}


class SpliceTests(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.plan = self.root / "edit.tsv"
        self.output = self.root / "out.wav"

    def render(self, *flags):
        return subprocess.run(["dotnet", DLL, self.plan, self.output, *flags], capture_output=True, timeout=15)

    def test_32_edit_lists_against_independent_byte_slices(self):
        rng = random.Random(416)
        for channels in (1, 2):
            for bits in (16, 24):
                frame_size = channels * bits // 8
                original = {}
                for name in ("one.wav", "take two.wav"):
                    data = rng.randbytes(301*frame_size)
                    original[name] = data
                    chunks = [chunk(b"JUNK", b"abc"), chunk(b"data", data),
                              chunk(b"fmt ", fmt(channels, bits) + b"\0\0")]
                    (self.root / name).write_bytes(riff(chunks))
                for _ in range(8):
                    clips = []
                    expected = bytearray()
                    offsets = []
                    for _ in range(rng.randrange(2, 12)):
                        name = rng.choice(list(original))
                        start = rng.randrange(301)
                        end = rng.randint(start+1, 301)
                        offsets.append(len(expected)//frame_size)
                        clips.append(f"{name}\t{start}\t{end}\n")
                        expected += original[name][start*frame_size:end*frame_size]
                    self.plan.write_text(HEADER + "".join(clips), encoding="utf-8")
                    result = self.render()
                    self.assertEqual(result.returncode, 0, result.stderr)
                    report = json.loads(result.stdout)
                    self.assertEqual([row["output_start"] for row in report["clips"]], offsets)
                    with wave.open(str(self.output)) as out:
                        self.assertEqual(out.getparams()[:4], (channels, bits//8, 48000, len(expected)//frame_size))
                        self.assertEqual(out.readframes(out.getnframes()), expected)
                    self.assertEqual(self.output.stat().st_size, 44 + len(expected) + len(expected)%2)
                    self.output.unlink()

    def test_edge_fades_match_signed_pcm_across_formats_and_buffer_boundaries(self):
        for channels in (1, 2):
            for bits in (16, 24):
                with self.subTest(channels=channels, bits=bits):
                    width = bits // 8
                    first_length = 65536 // (channels * width) + 2
                    lengths = (first_length, 3, 9)
                    minimum, maximum = -(2 ** (bits - 1)), 2 ** (bits - 1) - 1
                    values = (minimum, maximum, -97, 35)
                    source = [tuple(values[(frame + channel) % 4] for channel in range(channels))
                              for frame in range(first_length + 2)]
                    encode = lambda samples: b"".join(sample.to_bytes(width, "little", signed=True)
                                                       for frame in samples for sample in frame)
                    (self.root / "source.wav").write_bytes(riff([
                        chunk(b"fmt ", fmt(channels, bits)), chunk(b"data", encode(source)),
                    ]))
                    windows = [source[2:2 + first_length], source[:3], source[1:10]]
                    self.plan.write_text(HEADER + f"source.wav\t2\t{2 + first_length}\n"
                                         "source.wav\t0\t3\nsource.wav\t1\t10\n", encoding="utf-8")

                    hard = self.render()
                    self.assertEqual(hard.returncode, 0, hard.stderr)
                    original = self.output.read_bytes()
                    self.output.unlink()
                    zero = self.render("--edge-fade-frames", "0")
                    self.assertEqual((zero.returncode, zero.stdout), (0, hard.stdout), zero.stderr)
                    self.assertEqual(self.output.read_bytes(), original)
                    self.output.unlink()

                    faded = self.render("--edge-fade-frames", "5")
                    self.assertEqual((faded.returncode, faded.stdout), (0, hard.stdout), faded.stderr)
                    expected = []
                    for index, window in enumerate(windows):
                        for frame, samples in enumerate(window):
                            gain = min(5, frame if index else 5,
                                       len(window) - 1 - frame if index < 2 else 5)
                            expected.append(tuple(int(Fraction(sample * gain, 5)) for sample in samples))
                    with wave.open(str(self.output), "rb") as rendered:
                        self.assertEqual(rendered.getnframes(), sum(lengths))
                        self.assertEqual(rendered.readframes(sum(lengths)), encode(expected))
                    self.output.unlink()

    def test_caption_exports_keep_the_same_offsets_after_fading(self):
        make_take(self.root / "first.wav", 120, 160)
        make_take(self.root / "second.wav", 200, 240)
        self.plan.write_text(HEADER + "first.wav\t24000\t48000\nsecond.wav\t99\t24100\nfirst.wav\t24000\t36000\n",
                             encoding="utf-8")
        tool = ROOT / "modules/cue-conform"
        fade = 240
        result = self.render("--edge-fade-frames", str(fade))
        self.assertEqual(result.returncode, 0, result.stderr)
        manifest = json.loads(result.stdout)
        self.assertEqual(manifest, json.loads((tool / "examples/edit.json").read_bytes()))
        path = self.root / "actual.json"
        path.write_bytes(result.stdout)
        captions = self.root / "captions.vtt"
        subprocess.run([sys.executable, tool / "conform.py", path, tool / "examples/tracks.json", captions],
                       check=True, capture_output=True, timeout=15)
        self.assertEqual(captions.read_text(encoding="utf-8"),
                         (tool / "examples/edited.vtt").read_text(encoding="utf-8"))
        joints = []
        for clip in manifest["clips"][1:]:
            frame = clip["output_start"]
            hard = seam_samples(ROOT / "examples/preview.wav", frame)
            faded = seam_samples(self.output, frame)
            self.assertNotEqual(hard["before"], hard["after"])
            self.assertEqual(faded, {"before": [0, 0], "after": [0, 0]})
            joints.append({"frame": frame, "hard": hard, "faded": faded})
        with wave.open(str(self.output), "rb") as audio:
            self.assertEqual((audio.getframerate(), audio.getnframes()), (48000, 60001))
        demo = json.loads((ROOT / "examples/edge-fade-demo.json").read_bytes())
        self.assertEqual(demo, {"sample_rate": 48000, "frames": 60001, "fade_frames": fade, "joints": joints})

    def test_invalid_fade_arguments_leave_the_output_unpublished(self):
        for flags in [("--edge-fade-frames",), ("--edge-fade-frames", "-1"),
                      ("--edge-fade-frames", "4294967296"), ("--unrecognized", "5")]:
            command = self.render(*flags)
            self.assertEqual((command.returncode, command.stdout), (2, b""))
            self.assertIn(b"--edge-fade-frames", command.stderr)
            self.assertFalse(self.output.exists())

    def test_rejects_malformed_riff_and_bad_later_clip_without_publication(self):
        form = chunk(b"fmt ", fmt(1, 16))
        audio = chunk(b"data", b"\0\0")
        valid = riff([form, audio])
        (self.root / "good.wav").write_bytes(valid)
        bad = self.root / "bad.wav"
        self.plan.write_text(HEADER + "good.wav\t0\t1\nbad.wav\t0\t1\n", encoding="utf-8")
        for payload in [valid[:-1], valid + b"x", riff([form, form, audio]), riff([form, audio, audio]),
                        riff([form, chunk(b"data", b"x")]), riff([form]), riff([audio]),
                        riff([chunk(b"fmt ", fmt(1, 16, 44100)), audio]),
                        riff([chunk(b"fmt ", fmt(1, 16)[:8] + bytes(8)), audio]),
                        riff([chunk(b"fmt ", fmt(1, 16) + b"\x01\x00"), audio]),
                        riff([form, audio, b"JUNK\x01\0\0\0x"])]:
            bad.write_bytes(payload)
            result = self.render()
            self.assertEqual((result.returncode, result.stdout), (2, b""), result.stderr)
            self.assertFalse(self.output.exists())
        for bounds in ["0\t0", "-1\t1", "0\t2", "1\t0", "0.0\t1"]:
            self.plan.write_text(HEADER + f"good.wav\t{bounds}\n", encoding="utf-8")
            self.assertEqual(self.render().returncode, 2)
            self.assertFalse(self.output.exists())
        self.assertEqual((self.root / "good.wav").read_bytes(), valid)
        self.assertEqual(list(self.root.glob(".take-*.tmp")), [])

    def test_unrepresentable_riff_is_rejected_before_copy(self):
        # A tiny source repeated enough times would make a >4 GiB RIFF.
        samples = bytes(1 << 20)
        (self.root / "a.wav").write_bytes(riff([chunk(b"fmt ", fmt(1, 16)), chunk(b"data", samples)]))
        self.plan.write_text(HEADER + "a.wav\t0\t524288\n"*4096, encoding="utf-8")
        result = self.render()
        self.assertEqual((result.returncode, result.stdout), (2, b""))
        self.assertFalse(self.output.exists())
        self.assertEqual(list(self.root.glob(".take-*.tmp")), [])

    def test_concurrent_publish_and_existing_output(self):
        pcm = b"\x01\x02" * 5000
        valid = riff([chunk(b"fmt ", fmt(1, 16)), chunk(b"data", pcm)])
        (self.root / "a.wav").write_bytes(valid)
        self.plan.write_text(HEADER + "a.wav\t0\t5000\n", encoding="utf-8")
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.render().returncode, range(2)))
        self.assertEqual(sorted(results), [0, 2])
        self.assertEqual(self.output.read_bytes(), valid)
        self.assertEqual(self.render().returncode, 2)
        self.assertEqual(self.output.read_bytes(), valid)
