"""Conform plain WebVTT captions to a Take Splice frame manifest."""
import argparse
from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from decimal import Decimal
import json
import os
from pathlib import Path
import re
from tempfile import NamedTemporaryFile


@dataclass(frozen=True)
class Cue:
    start: int
    end: int
    text: str
    settings: str = ""


PERCENT = r"(?P<percent>[0-9]+(?:\.[0-9]+)?)%"
LAYOUT = {
    "vertical": re.compile(r"rl|lr"),
    "line": re.compile(rf"(?:{PERCENT}|-?[0-9]+)(?:,(?:start|center|end))?"),
    "position": re.compile(rf"{PERCENT}(?:,(?:line-left|center|line-right))?"),
    "size": re.compile(PERCENT),
    "align": re.compile(r"start|center|end|left|right"),
}


def cue_settings(raw, path, number):
    seen = set()
    tokens = re.findall(r"[^ \t]+", raw)
    for token in tokens:
        name, _, value = token.partition(":")
        pattern = LAYOUT.get(name)
        match = pattern.fullmatch(value) if pattern else None
        if not match or name in seen:
            raise ValueError(f"{path}: invalid or repeated setting {token!r} in block {number}")
        percent = match.groupdict().get("percent")
        if percent is not None and Decimal(percent) > 100:
            raise ValueError(f"{path}: percentage out of range in {token!r} in block {number}")
        seen.add(name)
    return " ".join(tokens)


def timestamp(value):
    match = re.fullmatch(r"(?:([0-9]{2,}):)?([0-5][0-9]):([0-5][0-9])\.([0-9]{3})", value)
    if not match:
        raise ValueError(f"invalid WebVTT timestamp {value!r}")
    h, m, s, ms = (int(part or 0) for part in match.groups())
    return ((h * 60 + m) * 60 + s) * 1000 + ms


def format_time(ms):
    return f"{ms // 3600000:02}:{ms // 60000 % 60:02}:{ms // 1000 % 60:02}.{ms % 1000:03}"


class Track:
    def __init__(self, cues):
        self.cues = cues
        self.starts = [cue.start for cue in cues]
        self.latest_ends = []
        latest = 0
        for cue in cues:
            latest = max(latest, cue.end)
            self.latest_ends.append(latest)

    def candidates(self, start_frame, end_frame, rate):
        first = bisect_right(self.latest_ends, start_frame * 1000 // rate)
        stop = bisect_left(self.starts, ceil_div(end_frame * 1000, rate))
        return range(first, stop)


def read_track(path):
    text = path.read_text(encoding="utf-8-sig").rstrip("\n")
    blocks = re.split(r"\n{2,}", text)
    if not re.fullmatch(r"WEBVTT(?:[ \t][^\n]*)?", blocks[0]) or "-->" in blocks[0]:
        raise ValueError(f"{path}: expected WEBVTT header and blank line")
    cues = []
    for number, block in enumerate(blocks[1:], 1):
        lines = block.split("\n")
        if re.match(r"^NOTE(?:[ \t]|$)", lines[0]):
            continue
        index = 0 if "-->" in lines[0] else 1
        if len(lines) <= index + 1:
            raise ValueError(f"{path}: invalid text cue in block {number}")
        timing = re.fullmatch(r"(\S+)[ \t]+-->[ \t]+(\S+)([ \t].*)?", lines[index])
        if not timing:
            raise ValueError(f"{path}: invalid cue timings in block {number}")
        start, end = map(timestamp, timing.group(1, 2))
        settings = cue_settings(timing.group(3) or "", path, number)
        payload = "\n".join(lines[index+1:])
        if "<" in payload or "-->" in payload or "\x00" in payload:
            raise ValueError(f"{path}: expected plain caption text in block {number}")
        if end <= start or (cues and start < cues[-1].start):
            raise ValueError(f"{path}: reversed or out-of-order timing in block {number}")
        cues.append(Cue(start, end, payload, settings))
    return Track(cues)


def unique_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON field {key!r}")
        result[key] = value
    return result


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8-sig"), object_pairs_hook=unique_keys)


def integer(record, field, minimum):
    value = record.get(field) if isinstance(record, dict) else None
    if type(value) is not int or value < minimum:
        raise ValueError(f"invalid manifest field {field}")
    return value


def ceil_div(value, divisor):
    return -(-value // divisor)


def conform(manifest, tracks):
    if not isinstance(manifest, dict) or not isinstance(manifest.get("clips"), list) or not manifest["clips"]:
        raise ValueError("expected a manifest with clips")
    rate = integer(manifest.get("format"), "sample_rate", 1)
    total = integer(manifest, "frames", 1)
    offset = emitted = sub_ms = 0
    blocks = ["WEBVTT"]
    for number, clip in enumerate(manifest["clips"], 1):
        start, end = integer(clip, "start", 0), integer(clip, "end", 1)
        if end <= start or integer(clip, "output_start", 0) != offset:
            raise ValueError(f"clip {number}: invalid source range or discontinuous output")
        source = clip.get("source")
        if not isinstance(source, str) or source not in tracks:
            raise ValueError(f"clip {number}: missing caption mapping for {source!r}")
        track = tracks[source]
        if track is not None:
            for i in track.candidates(start, end, rate):
                cue = track.cues[i]
                # All boundaries use units of 1/(1000*rate) seconds until final output.
                left = max(start * 1000, cue.start * rate)
                right = min(end * 1000, cue.end * rate)
                if left >= right:
                    continue
                shift = (offset - start) * 1000
                begin, finish = ceil_div(left + shift, rate), ceil_div(right + shift, rate)
                if begin == finish:
                    sub_ms += 1
                    continue
                timing = f"{format_time(begin)} --> {format_time(finish)}"
                if cue.settings:
                    timing += " " + cue.settings
                blocks.append(f"clip-{number}-cue-{i+1}\n{timing}\n{cue.text}")
                emitted += 1
        offset += end - start
    if offset != total:
        raise ValueError("clip lengths do not match manifest frames")
    output = "\n\n".join(blocks) + ("\n" if emitted else "\n\n")
    stats = {"frames": total, "clips": len(manifest["clips"]), "cues": emitted, "sub_ms_omitted": sub_ms}
    return output, stats


def load_tracks(path):
    mapping = read_json(path)
    if not isinstance(mapping, dict):
        raise ValueError("expected an object mapping audio sources to subtitle files or null")
    tracks = {}
    for source, filename in mapping.items():
        if filename is not None and (not isinstance(filename, str) or not filename):
            raise ValueError(f"invalid caption filename for {source!r}")
        tracks[source] = read_track(path.parent / filename) if filename is not None else None
    return tracks


def publish(path, text):
    temp = None
    try:
        with NamedTemporaryFile(dir=path.parent, delete=False) as file:
            temp = Path(file.name)
            file.write(text.encode("utf-8"))
        os.link(temp, path)
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("mapping", type=Path)
    parser.add_argument("output", type=Path, help="new WebVTT filename")
    args = parser.parse_args()
    try:
        text, stats = conform(read_json(args.manifest), load_tracks(args.mapping))
        publish(args.output, text)
    except (ValueError, OSError) as error:
        parser.exit(2, f"{parser.prog}: {error}\n")
    print(json.dumps(stats))


if __name__ == "__main__":
    main()
