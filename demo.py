"""Generate two stereo takes, assemble three excerpts, and check every PCM frame."""
import argparse
import hashlib
import json
from pathlib import Path
import struct
import subprocess
from tempfile import TemporaryDirectory
import wave


ROOT = Path(__file__).resolve().parent
DLL = ROOT / "src/bin/Release/net10.0/take-splice.dll"
HEADER = "source\tstart_frame\tend_frame\n"


def triangle(frame, period):
    phase = frame % period
    return 12000 - 48000 * abs(phase - period // 2) // period


def make_take(path, left_period, right_period):
    pcm = b"".join(struct.pack("<hh", triangle(i, left_period), triangle(i, right_period))
                   for i in range(48000))
    with wave.open(str(path), "wb") as out:
        out.setparams((2, 2, 48000, 0, "NONE", "not compressed"))
        out.writeframes(pcm)
    return pcm


def demonstrate(directory):
    a = make_take(directory / "first.wav", 120, 160)
    b = make_take(directory / "second.wav", 200, 240)
    plan = directory / "edit.tsv"
    plan.write_text(HEADER + "first.wav\t24000\t48000\nsecond.wav\t99\t24100\nfirst.wav\t24000\t36000\n",
                    encoding="utf-8")
    output = directory / "edit.wav"
    manifest = json.loads(subprocess.check_output(["dotnet", DLL, plan, output]))
    expected = a[24000*4:] + b[99*4:24100*4] + a[24000*4:36000*4]
    with wave.open(str(output), "rb") as wav:
        actual = wav.readframes(wav.getnframes())
        assert (wav.getnchannels(), wav.getsampwidth(), wav.getframerate(), wav.getnframes()) == (2, 2, 48000, 60001)
    assert actual == expected
    assert output.read_bytes() == (ROOT / "examples/preview.wav").read_bytes()
    assert [c["output_start"] for c in manifest["clips"]] == [0, 24000, 48001]
    print(json.dumps({"frames": manifest["frames"], "pcm_bytes": len(actual),
                      "sha256": hashlib.sha256(actual).hexdigest(), "clips": manifest["clips"]}, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", nargs="?", type=Path, help="new directory for the WAVs and edit list")
    destination = parser.parse_args().directory
    if destination is None:
        with TemporaryDirectory() as temporary:
            demonstrate(Path(temporary))
    else:
        destination.mkdir()
        demonstrate(destination)
