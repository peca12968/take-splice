from collections import Counter
from fractions import Fraction
import json
from pathlib import Path
import random
import subprocess
import sys
import tempfile
import unittest

from conform import Cue, Track, conform, read_track


ROOT = Path(__file__).resolve().parent


def as_ms(time):
    hours, minutes, rest = time.split(":")
    seconds, ms = rest.split(".")
    return int(hours)*3600000 + int(minutes)*60000 + int(seconds)*1000 + int(ms)


def parse_export(text):
    blocks = text.strip().split("\n\n")
    assert blocks[0] == "WEBVTT"
    result = []
    for block in blocks[1:]:
        identity, times, caption = block.split("\n", 2)
        a, b = map(as_ms, times.split(" --> "))
        assert a < b
        result.append((identity, a, b, caption))
    assert len(set(r[0] for r in result)) == len(result)
    assert [r[1] for r in result] == sorted(r[1] for r in result)
    return result


class ConformTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    def cli(self, manifest, mapping, output):
        return subprocess.run([sys.executable, ROOT / "conform.py", manifest, mapping, output],
                              capture_output=True)

    def test_example_and_no_clobber(self):
        manifest, mapping = [ROOT / "examples" / n for n in ("edit.json", "tracks.json")]
        out = self.root / "export.vtt"
        result = self.cli(manifest, mapping, out)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["cues"], 6)
        self.assertEqual(parse_export(out.read_text(encoding="utf-8")),
                         parse_export((ROOT / "examples/edited.vtt").read_text(encoding="utf-8")))
        out.write_bytes(b"prior export")
        self.assertEqual(self.cli(manifest, mapping, out).returncode, 2)
        self.assertEqual(out.read_bytes(), b"prior export")
        self.assertEqual(list(self.root.iterdir()), [out])

    def test_each_playback_millisecond_against_source_timeline(self):
        rng = random.Random(81923)
        for case in range(120):
            rate = rng.choice((8000, 11025, 44100, 48000))
            tracks = {"silent": None}
            for source in ("a", "b"):
                cues = sorted((Cue(t, t+rng.randint(1, 55), f"{source}-{i}") for i, t in
                               enumerate(rng.choices(range(75), k=9))), key=lambda c: c.start)
                tracks[source] = Track(cues)
            clips, offset = [], 0
            for _ in range(rng.randint(2, 8)):
                start = rng.randrange(rate//12)
                length = rng.randint(1, rate//20)
                clips.append({"start": start, "end": start+length, "source": rng.choice(list(tracks)),
                              "output_start": offset})
                offset += length
            manifest = {"format": {"sample_rate": rate}, "clips": clips, "frames": offset}
            text, _ = conform(manifest, tracks)
            actual = parse_export(text)
            with self.subTest(case=case):
                for ms in range(offset*1000//rate + 3):
                    frame = Fraction(ms*rate, 1000)
                    expected = Counter()
                    for clip in clips:
                        source_frame = frame - clip["output_start"] + clip["start"]
                        track = tracks[clip["source"]]
                        if track and clip["start"] <= source_frame < clip["end"]:
                            source_ms = source_frame * 1000 / rate
                            expected.update(c.text for c in track.cues if c.start <= source_ms < c.end)
                    self.assertEqual(Counter(c for _, a, b, c in actual if a <= ms < b), expected)

    def test_submillisecond_slices_and_null_track(self):
        clips = [{"source": key, "start": i, "end": i+1, "output_start": i}
                 for i, key in enumerate(["quiet", "a", "a", "a", "a"])]
        manifest = {"clips": clips, "frames": 5, "format": {"sample_rate": 4000}}
        track = Track([Cue(0, 10, "text")])
        text, stats = conform(manifest, {"quiet": None, "a": track})
        self.assertEqual(parse_export(text), [("clip-5-cue-1", 1, 2, "text")])
        self.assertEqual(stats["sub_ms_omitted"], 3)

    def test_bom_comments_multiline_and_invalid_blocks(self):
        path = self.root / "caption.vtt"
        body = "WEBVTT\r\n\r\nNOTE source\r\ncomment\r\n\r\nID\r\n00:01.000 --> 00:02.000\r\n第一行\r\n第二行"
        path.write_bytes(body.encode("utf-8-sig"))
        self.assertEqual(read_track(path).cues, [Cue(1000, 2000, "第一行\n第二行")])
        for old, new in [("WEBVTT", "VTT"), ("00:02.000", "00:01.000"),
                         ("00:02.000", "00:02.000 align:left"), ("第一行", "<00:01.500>第一行")]:
            path.write_bytes(body.replace(old, new).encode("utf-8"))
            with self.assertRaises(ValueError):
                read_track(path)

    def test_invalid_late_clip_or_unmapped_source_creates_no_output(self):
        m = json.loads((ROOT / "examples/edit.json").read_bytes())
        out, doc = self.root / "out", self.root / "edit"
        for patch in ({"source": "unmapped.wav"}, {"end": 7}, {"output_start": 48000}, {"start": False}):
            item = dict(m, clips=[*m["clips"][:-1], m["clips"][-1] | patch])
            doc.write_text(json.dumps(item), encoding="utf-8")
            self.assertEqual(self.cli(doc, ROOT / "examples/tracks.json", out).returncode, 2)
            self.assertFalse(out.exists())


if __name__ == "__main__":
    unittest.main()
