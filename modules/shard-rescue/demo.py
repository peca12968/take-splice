"""Recover a generated WAV after a volume disappears and two parts are damaged."""
import json
from pathlib import Path
import struct
from tempfile import TemporaryDirectory
import wave

from shard_rescue import pack, part, rebuild_volume, restore, scrub


def demo(root):
    source = root / "recording.wav"
    with wave.open(str(source), "wb") as wav:
        wav.setparams((1, 2, 48000, 0, "NONE", "not compressed"))
        cycle = b"".join(struct.pack("<h", (abs(i - 48) - 24) * 400) for i in range(96))
        wav.writeframes(cycle * 500)
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


if __name__ == "__main__":
    with TemporaryDirectory() as directory:
        print(json.dumps(demo(Path(directory)), indent=2))
