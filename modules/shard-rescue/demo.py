"""Recover a generated WAV after a volume disappears and two parts are damaged."""
import argparse
import json
from pathlib import Path
import struct
from tempfile import TemporaryDirectory
import wave

from shard_rescue import STRIPE_BYTES, pack, part, rebuild_volume, restore, scrub


def write_recording(source, cycles):
    with wave.open(str(source), "wb") as wav:
        wav.setparams((1, 2, 48000, 0, "NONE", "not compressed"))
        cycle = b"".join(struct.pack("<h", (abs(i - 48) - 24) * 400) for i in range(96))
        wav.writeframes(cycle * cycles)


def demo(root):
    source = root / "recording.wav"
    write_recording(source, 500)
    archive = root / "archive"
    packed = pack(source, archive)
    # Move a whole volume offline. Two different volumes also have localized damage.
    (archive / "d1").rename(root / "offline-d1")
    part(archive, 0, "d3").write_bytes(b"truncated")
    damaged = part(archive, 1, "q")
    with damaged.open("r+b") as stream:
        original = stream.read(1)[0]
        stream.seek(0)
        stream.write(bytes([original ^ 1]))
    output = root / "restored.wav"
    restored = restore(archive, output)
    if output.read_bytes() != source.read_bytes():
        raise RuntimeError("restored audio differs")
    with wave.open(str(output), "rb") as wav:
        frames = wav.getnframes()
    before = scrub(archive)
    spare = root / "replacement-d1"
    rebuilt = rebuild_volume(archive, "d1", spare)
    for shard in spare.iterdir():
        if shard.read_bytes() != (root / "offline-d1" / shard.name).read_bytes():
            raise RuntimeError("replacement volume differs")
    spare.rename(archive / "d1")
    after = scrub(archive)
    return {"stripes": packed["stripes"], "frames": frames, **restored,
            "before_rebuild": before["issues"], "replacement": rebuilt, "after_rebuild": after["issues"]}


def range_demo(root):
    source, archive, excerpt = (root / name for name in ("recording.wav", "archive", "excerpt.pcm"))
    write_recording(source, 1000)
    packed = pack(source, archive)
    for stripe, slots in enumerate((("d3", "p"), ("d0", "q"), ("d0", "d1", "p"))):
        for slot in slots:
            part(archive, stripe, slot).unlink()

    whole = root / "whole.wav"
    try:
        restore(archive, whole)
    except ValueError as error:
        if str(error) != "stripe 2: 3 unusable shards, at most 2 recoverable" or whole.exists():
            raise
    else:
        raise RuntimeError("full restoration unexpectedly succeeded")

    offset, length = 49152, 32768
    selected = restore(archive, excerpt, interval=(offset, length))
    if excerpt.read_bytes() != source.read_bytes()[offset:offset + length]:
        raise RuntimeError("restored excerpt differs")
    audit = scrub(archive)
    return {"archive_bytes": packed["bytes"], "stripe_bytes": STRIPE_BYTES,
            "archive_status": audit["status"], "issues": audit["issues"], "selected": selected}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--range", action="store_true", help="restore an excerpt from a partly unrecoverable archive")
    args = parser.parse_args()
    with TemporaryDirectory() as directory:
        execute = range_demo if args.range else demo
        print(json.dumps(execute(Path(directory)), indent=2))
