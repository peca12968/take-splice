"""Verify captions against a manifest and an actual WAV produced by Take Splice."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import wave


ROOT = Path(__file__).resolve().parent


def verify(upstream, root):
    listening = root / "audio"
    subprocess.run([sys.executable, upstream / "demo.py", listening], check=True, stdout=subprocess.PIPE)
    dll = upstream / "src/bin/Release/net10.0/take-splice.dll"
    audio = root / "output.wav"
    manifest = subprocess.check_output(["dotnet", dll, listening / "edit.tsv", audio])
    if json.loads(manifest) != json.loads((ROOT / "examples/edit.json").read_bytes()):
        raise ValueError("example differs from the pinned editor's manifest")
    path = root / "actual.json"
    path.write_bytes(manifest)
    captions = root / "captions.vtt"
    stats = json.loads(subprocess.check_output([sys.executable, ROOT / "conform.py", path,
                                                ROOT / "examples/tracks.json", captions]))
    with wave.open(str(audio), "rb") as wav:
        if (wav.getframerate(), wav.getnframes()) != (48000, stats["frames"]):
            raise ValueError("WAV timeline differs from caption timeline")
    if stats != {"frames": 60001, "clips": 3, "cues": 6, "sub_ms_omitted": 0}:
        raise ValueError(stats)
    if captions.read_text(encoding="utf-8") != (ROOT / "examples/edited.vtt").read_text(encoding="utf-8"):
        raise ValueError("exported captions differ from expected clip boundaries")
    print(json.dumps(stats))


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as directory:
        verify(Path(sys.argv[1]).resolve(), Path(directory))
