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
        start, _, end, *layout = times.split()
        a, b = map(as_ms, [start, end])
        assert a < b
        result.append((identity, a, b, caption, " ".join(layout)))
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
                layouts = ("", "line:20%,center align:left", "vertical:lr position:80%,center")
                cues = sorted((Cue(t, t+rng.randint(1, 55), f"{source}-{i}", layouts[i % 3]) for i, t in
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
                            expected.update((c.text, c.settings) for c in track.cues if c.start <= source_ms < c.end)
                    self.assertEqual(Counter((c, s) for _, a, b, c, s in actual if a <= ms < b), expected)

    def test_submillisecond_slices_and_null_track(self):
        clips = [{"source": key, "start": i, "end": i+1, "output_start": i}
                 for i, key in enumerate(["quiet", "a", "a", "a", "a"])]
        manifest = {"clips": clips, "frames": 5, "format": {"sample_rate": 4000}}
        track = Track([Cue(0, 10, "text")])
        text, stats = conform(manifest, {"quiet": None, "a": track})
        self.assertEqual(parse_export(text), [("clip-5-cue-1", 1, 2, "text", "")])
        self.assertEqual(stats["sub_ms_omitted"], 3)

    def test_bom_comments_multiline_and_invalid_blocks(self):
        path = self.root / "caption.vtt"
        body = "WEBVTT\r\n\r\nNOTE source\r\ncomment\r\n\r\nID\r\n00:01.000 --> 00:02.000\r\n第一行\r\n第二行"
        path.write_bytes(body.encode("utf-8-sig"))
        self.assertEqual(read_track(path).cues, [Cue(1000, 2000, "第一行\n第二行")])
        for old, new in [("WEBVTT", "VTT"), ("00:02.000", "00:01.000"),
                         ("00:02.000", "00:02.000 align:middle"), ("第一行", "<00:01.500>第一行")]:
            path.write_bytes(body.replace(old, new).encode("utf-8"))
            with self.assertRaises(ValueError):
                read_track(path)

    def test_layout_grammar_and_rejected_settings(self):
        path = self.root / "layout.vtt"
        def parse(settings):
            path.write_text(f"WEBVTT\n\n00:00.000 --> 00:01.000 {settings}\nx", encoding="utf-8")
            return read_track(path).cues[0].settings

        for layout in ("vertical:rl line:-1,end position:0%,line-left size:100.000% align:right",
                       "line:00.125%,center position:99.9%,line-right", "line:0,start vertical:lr"):
            self.assertEqual(parse(layout.replace(" ", "\t  ")), layout)
        for invalid in ("line:+2", "line:1.5", "line:-1%,end", "align:left align:right",
                        "size:100.00000000000000000001%", "position:50%,end", "region:lower",
                        "vertical:tb", "line:1,auto", "size:.5%", "align:left\u00a0size:20%"):
            with self.subTest(invalid=invalid), self.assertRaisesRegex(ValueError, "block 1"):
                parse(invalid)

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
