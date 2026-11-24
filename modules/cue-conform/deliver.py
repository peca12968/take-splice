"""Render, verify, or excerpt a WAV and WebVTT delivery."""
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


def caption_timeline(track, audio):
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


def publish_package(wav_path, caption_text, audio, output):
    package = wav_path.parent / "delivery.zip"
    caption_bytes = caption_text.encode("utf-8")
    with wav_path.open("rb") as wav, ZipFile(package, "x") as archive:
        size = wav_path.stat().st_size
        assets = {AUDIO: {"bytes": size, "sha256": sha256(wav)}}
        wav.seek(0)
        with archive.open(archive_entry(AUDIO, size), "w") as member:
            copyfileobj(wav, member, BLOCK)
        archive.writestr(archive_entry(CAPTIONS, len(caption_bytes)), caption_bytes)
        assets[CAPTIONS] = {"bytes": len(caption_bytes), "sha256": hashlib.sha256(caption_bytes).hexdigest()}
        manifest = {"version": 1, "audio": audio, "captions": caption_timeline(parse_track(caption_text, CAPTIONS), audio),
                    "files": assets}
        archive.writestr(archive_entry(MANIFEST, 0), json_bytes(manifest))
    result = verify(package)
    os.link(package, output)
    return result


def build(plan, tracks, output, fade_frames):
    if fade_frames < 0:
        raise ValueError("--edge-fade-frames must be nonnegative")
    if not EDITOR.is_file():
        raise ValueError("build the editor first with dotnet build src -c Release")
    with TemporaryDirectory(dir=output.parent) as directory:
        wav_path = Path(directory) / AUDIO
        result = subprocess.run(["dotnet", EDITOR, plan, wav_path, "--edge-fade-frames", str(fade_frames)],
                                capture_output=True, check=True)
        edit = json.loads(result.stdout)
        caption_text, _ = conform(edit, load_tracks(tracks))
        audio = {key: edit["format"][key] for key in ("sample_rate", "channels", "bits")}
        return publish_package(wav_path, caption_text, {**audio, "frames": edit["frames"]}, output)


def delivery_sha(stream, expected_sha):
    digest = sha256(stream)
    if expected_sha is not None and digest != expected_sha:
        raise ValueError("archive SHA-256 differs from the expected digest")
    stream.seek(0)
    return digest


def verify_contents(archive):
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
    track = parse_track(archive.read(CAPTIONS).decode("utf-8"), CAPTIONS)
    observed = {"version": 1, "audio": audio, "captions": caption_timeline(track, audio), "files": assets}
    if json_bytes(manifest) != json_bytes(observed):
        raise ValueError("manifest does not match packaged media metadata")
    return observed, track


def verify(package, expected_sha=None):
    with package.open("rb") as stream:
        digest = delivery_sha(stream, expected_sha)
        with ZipFile(stream) as archive:
            manifest, _ = verify_contents(archive)
    return {"sha256": digest, "manifest": manifest}


def copy_excerpt(member, output, start, frames):
    with wave.open(member, "rb") as source, output.open("w+b") as file:
        frame_bytes = source.getnchannels() * source.getsampwidth()
        source.setpos(start)
        with wave.open(file, "wb") as target:
            target.setparams(source.getparams()._replace(nframes=frames))
            remaining = frames
            while remaining:
                count = min(remaining, BLOCK // frame_bytes)
                block = source.readframes(count)
                if len(block) != count * frame_bytes:
                    raise ValueError("audio.wav: truncated excerpt")
                target.writeframesraw(block)
                remaining -= count
        pcm_bytes = frames * frame_bytes
        # Include the alignment byte in the RIFF length, outside the data chunk.
        if pcm_bytes % 2:
            file.write(b"\0")
            file.seek(4)
            file.write((36 + pcm_bytes + 1).to_bytes(4, "little"))


def excerpt(package, start, end, output, expected_sha=None):
    if not 0 <= start < end:
        raise ValueError("expected a nonempty half-open frame range")
    with TemporaryDirectory(dir=output.parent) as directory:
        wav_path = Path(directory) / AUDIO
        with package.open("rb") as stream:
            source_sha = delivery_sha(stream, expected_sha)
            with ZipFile(stream) as archive:
                manifest, track = verify_contents(archive)
                if end > manifest["audio"]["frames"]:
                    raise ValueError("excerpt end exceeds audio length")
                audio = {**manifest["audio"], "frames": end - start}
                with archive.open(AUDIO) as member:
                    copy_excerpt(member, wav_path, start, end - start)
        plan = {"format": audio, "frames": end - start,
                "clips": [{"source": AUDIO, "start": start, "end": end, "output_start": 0}]}
        caption_text, stats = conform(plan, {AUDIO: track})
        result = publish_package(wav_path, caption_text, audio, output)
    return {**result, "source_sha256": source_sha, "range": {"start_frame": start, "end_frame": end},
            "sub_ms_omitted": stats["sub_ms_omitted"]}


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
    trim = commands.add_parser("excerpt", help="verify a package and copy a frame range with retimed captions")
    trim.add_argument("package", type=Path)
    trim.add_argument("start_frame", type=int)
    trim.add_argument("end_frame", type=int)
    trim.add_argument("output", type=Path, help="new ZIP file")
    trim.add_argument("--sha256", type=digest_arg, help="expected digest of the source package")
    args = parser.parse_args()
    try:
        if args.command == "build":
            result = build(args.plan, args.tracks, args.output, args.edge_fade_frames)
        elif args.command == "excerpt":
            result = excerpt(args.package, args.start_frame, args.end_frame, args.output, args.sha256)
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
