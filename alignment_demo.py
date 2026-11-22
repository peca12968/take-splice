"""Compare exported subtitle times with two measured stereo marker positions."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile


def verify(detector, editor, root):
    audio = root / "audio"
    subprocess.run([sys.executable, detector / "demo.py", audio], check=True)
    edit = audio / "edit.tsv"
    edit.write_text("source\tstart_frame\tend_frame\nstereo.wav\t48005\t62405\nstereo.wav\t52000\t54500\n",
                    encoding="utf-8")
    manifest = root / "edit.json"
    manifest.write_bytes(subprocess.check_output(["dotnet", editor / "src/bin/Release/net10.0/take-splice.dll",
                                                   edit, root / "cut.wav"]))
    (root / "source.vtt").write_text("WEBVTT\n\n00:01.000 --> 00:01.100 line:80%\nSync\n", encoding="utf-8")
    mapping = root / "tracks.json"
    mapping.write_text(json.dumps({"stereo.wav": {"file": "source.vtt", "alignment": "measured.json"}}),
                       encoding="utf-8")
    for name, channel, comparison, expected, times in (
        ("forward", 1, 2, 417, [("00.009","00.109"), ("00.300","00.326")]),
        ("reverse", 2, 1, -417, [("00.000","00.092"), ("00.300","00.308")]),
        ("echo", 1, 2, None, []),
    ):
        capture = "echo.wav" if name == "echo" else "stereo.wav"
        detection = subprocess.run([sys.executable, detector / "locate.py", audio / "marker.wav", audio / capture,
                                    "--channel", str(channel), "--compare-channel", str(comparison)], capture_output=True)
        if detection.returncode != int(expected is None) or json.loads(detection.stdout)["delay_frames"] != expected:
            raise ValueError(f"{name}: unexpected measurement {detection.stdout!r} {detection.stderr!r}")
        measured = root / "measured.json"
        measured.write_bytes(detection.stdout)
        out = root / f"{name}.vtt"
        export = [sys.executable, Path(__file__).with_name("conform.py"), manifest, mapping, out]
        result = subprocess.run(export, capture_output=True)
        if expected is None:
            if result.returncode != 2 or out.exists() or b"matched comparison" not in result.stderr:
                raise ValueError("ambiguous capture must fail before publishing")
        else:
            result.check_returncode()
            cues = out.read_text(encoding="utf-8").strip().split("\n\n")[1:]
            wanted = [f"clip-{i}-cue-1\n00:00:{a} --> 00:00:{b} line:80%\nSync"
                      for i, (a, b) in enumerate(times, 1)]
            if cues != wanted:
                raise ValueError(f"{name}: {cues!r} differs from {wanted!r}")
        print(f"{name}: delay={expected}, cues={times}")


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as directory:
        verify(Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve(), Path(directory))
