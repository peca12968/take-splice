"""Render, package, and verify a WAV and WebVTT delivery."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
from shutil import copyfileobj
import subprocess
from tempfile import TemporaryDirectory
import wave
from zipfile import BadZipFile, ZIP_STORED, ZipFile, ZipInfo

from conform import ceil_div, conform, load_tracks, parse_track, unique_keys


EDITOR = Path(__file__).resolve().parents[2] / "src/bin/Release/net10.0/take-splice.dll"
AUDIO = "audio.wav"
CAPTIONS = "captions.vtt"
MANIFEST = "manifest.json"
BLOCK = 64 * 1024


def sha256(stream):
    return hashlib.file_digest(stream, "sha256").hexdigest()


def json_bytes(value):
    return (json.dumps(value, sort_keys=True, indent=2) + "\n").encode("utf-8")


def wav_timeline(stream):
    with wave.open(stream, "rb") as wav:
        details = {"frames": wav.getnframes(), "sample_rate": wav.getframerate(),
                   "channels": wav.getnchannels(), "bits": 8 * wav.getsampwidth()}
        if (details["frames"] < 1 or details["sample_rate"] < 1 or
                details["channels"] not in (1, 2) or details["bits"] not in (16, 24)):
            raise ValueError("audio.wav: expected nonempty mono/stereo 16/24-bit PCM")
        frame_bytes = wav.getnchannels() * wav.getsampwidth()
        remaining = details["frames"]
        while remaining:
            count = min(remaining, BLOCK // frame_bytes)
            if len(wav.readframes(count)) != count * frame_bytes:
                raise ValueError("audio.wav: truncated PCM")
            remaining -= count
    return details


def caption_timeline(text, audio):
    track = parse_track(text, CAPTIONS)
    end = max((cue.end for cue in track.cues), default=0)
    if end > ceil_div(audio["frames"] * 1000, audio["sample_rate"]):
        raise ValueError("captions.vtt: cue ends after the audio timeline")
    return {"cues": len(track.cues), "last_end_ms": end}


def archive_entry(name, size):
    info = ZipInfo(name)
    info.create_system = 3
    info.external_attr = 0o100644 << 16
    info.file_size = size
    return info


def build(plan, tracks, output, fade_frames):
    if fade_frames < 0:
        raise ValueError("--edge-fade-frames must be nonnegative")
    if not EDITOR.is_file():
        raise ValueError("build the editor first with dotnet build src -c Release")
    with TemporaryDirectory(dir=output.parent) as directory:
        wav_path = Path(directory) / AUDIO
        package = Path(directory) / "delivery.zip"
        result = subprocess.run(["dotnet", EDITOR, plan, wav_path, "--edge-fade-frames", str(fade_frames)],
                                capture_output=True, check=True)
        edit = json.loads(result.stdout)
        caption_text, _ = conform(edit, load_tracks(tracks))
        caption_bytes = caption_text.encode("utf-8")
        with wav_path.open("rb") as wav, ZipFile(package, "x") as archive:
            audio = {key: edit["format"][key] for key in ("sample_rate", "channels", "bits")}
            audio["frames"] = edit["frames"]
            size = wav_path.stat().st_size
            assets = {AUDIO: {"bytes": size, "sha256": sha256(wav)}}
            wav.seek(0)
            with archive.open(archive_entry(AUDIO, size), "w") as member:
                copyfileobj(wav, member, BLOCK)
            archive.writestr(archive_entry(CAPTIONS, len(caption_bytes)), caption_bytes)
            assets[CAPTIONS] = {"bytes": len(caption_bytes), "sha256": hashlib.sha256(caption_bytes).hexdigest()}
            manifest = {"version": 1, "audio": audio, "captions": caption_timeline(caption_text, audio),
                        "files": assets}
            archive.writestr(archive_entry(MANIFEST, 0), json_bytes(manifest))
        result = verify(package)
        os.link(package, output)
    return result


def verify(package, expected_sha=None):
    with package.open("rb") as stream:
        digest = sha256(stream)
        if expected_sha is not None and digest != expected_sha:
            raise ValueError("archive SHA-256 differs from the expected digest")
        stream.seek(0)
        with ZipFile(stream) as archive:
            if sorted(archive.namelist()) != sorted([AUDIO, CAPTIONS, MANIFEST]):
                raise ValueError("expected one audio.wav, captions.vtt and manifest.json")
            if any(info.compress_type != ZIP_STORED or info.flag_bits & 1 for info in archive.infolist()):
                raise ValueError("expected uncompressed, unencrypted ZIP entries")
            if archive.getinfo(MANIFEST).file_size > 8192:
                raise ValueError("manifest.json exceeds 8192 bytes")
            manifest = json.loads(archive.read(MANIFEST), object_pairs_hook=unique_keys)
            if not isinstance(manifest, dict) or type(manifest.get("version")) is not int or manifest["version"] != 1:
                raise ValueError("unsupported delivery manifest version")
            assets = {}
            for name in (AUDIO, CAPTIONS):
                with archive.open(name) as member:
                    assets[name] = {"bytes": archive.getinfo(name).file_size, "sha256": sha256(member)}
                if not isinstance(manifest.get("files"), dict) or json_bytes(manifest["files"].get(name)) != json_bytes(assets[name]):
                    raise ValueError(f"{name}: byte length or SHA-256 differs from manifest")
            with archive.open(AUDIO) as member:
                audio = wav_timeline(member)
            captions = caption_timeline(archive.read(CAPTIONS).decode("utf-8"), audio)
            observed = {"version": 1, "audio": audio, "captions": captions, "files": assets}
            if json_bytes(manifest) != json_bytes(observed):
                raise ValueError("manifest does not match packaged media metadata")
    return {"sha256": digest, "manifest": observed}


def digest_arg(value):
    if not re.fullmatch(r"[0-9a-fA-F]{64}", value):
        raise argparse.ArgumentTypeError("expected 64 hexadecimal SHA-256 digits")
    return value.lower()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    render = commands.add_parser("build", help="render a WAV and VTT package from source files")
    render.add_argument("plan", type=Path)
    render.add_argument("tracks", type=Path)
    render.add_argument("output", type=Path, help="new ZIP file")
    render.add_argument("--edge-fade-frames", type=int, default=0)
    check = commands.add_parser("verify", help="check a package without extracting it")
    check.add_argument("package", type=Path)
    check.add_argument("--sha256", type=digest_arg, help="digest received independently from the sender")
    args = parser.parse_args()
    try:
        if args.command == "build":
            result = build(args.plan, args.tracks, args.output, args.edge_fade_frames)
        else:
            result = verify(args.package, args.sha256)
    except subprocess.CalledProcessError as error:
        message = error.stderr.decode("utf-8", errors="replace").strip()
        parser.exit(2, f"{parser.prog}: WAV render failed: {message}\n")
    except (ValueError, OSError, BadZipFile, EOFError, wave.Error) as error:
        parser.exit(2, f"{parser.prog}: {error}\n")
    print(json_bytes(result).decode("utf-8"), end="")


if __name__ == "__main__":
    main()
