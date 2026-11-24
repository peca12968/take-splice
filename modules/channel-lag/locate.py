"""Locate a short PCM marker in one channel of a longer PCM recording."""
import argparse
import json
import wave

import numpy as np
from scipy.signal import correlate


def correlations(samples, marker):
    """Signed Pearson coefficients for every complete window, NaN for silence."""
    width = len(marker)
    centered = marker.astype(np.float64) - marker.mean()
    norm = np.linalg.norm(centered)
    if width < 2 or not norm:
        raise ValueError("marker must contain at least two distinct sample values")
    pcm = samples.astype(np.int64)

    def window_sum(values):
        prefix = np.empty(len(values) + 1, dtype=np.int64)
        prefix[0] = 0
        np.cumsum(values, out=prefix[1:])
        return prefix[width:] - prefix[:-width]

    sums = window_sum(pcm)
    # Subtract the integer part of each window's mean in exact PCM arithmetic.
    # This retains low-amplitude variation even on a large DC offset.
    base, remainder = np.divmod(sums, width)
    energy = window_sum(pcm * pcm) - base * sums - base * remainder
    variance_sum = energy - remainder.astype(np.float64) ** 2 / width
    numerator = correlate(pcm.astype(np.float64) - pcm[0], centered, mode="valid", method="fft")
    scores = np.full(len(sums), np.nan)
    np.divide(numerator, norm * np.sqrt(variance_sum), out=scores, where=variance_sum > 0)
    return np.clip(scores, -1, 1)


def read_pcm(stream, frames):
    channels = stream.getnchannels()
    if stream.getsampwidth() != 2 or stream.getcomptype() != "NONE":
        raise ValueError("expected uncompressed 16-bit PCM")
    data = stream.readframes(frames)
    if len(data) != frames * channels * 2:
        raise ValueError("truncated PCM data")
    return np.frombuffer(data, dtype="<i2").reshape(-1, channels)


def locate(marker_file, recording_file, channel=1, minimum=0.8, gap=0.05, block=65536, compare_channel=None):
    if not 0 < minimum <= 1 or not 0 <= gap <= 1 or block < 1:
        raise ValueError("invalid match threshold, margin or block size")
    with wave.open(str(marker_file), "rb") as source:
        if source.getnchannels() != 1 or source.getnframes() < 2:
            raise ValueError("expected a mono marker of at least two frames")
        marker = read_pcm(source, source.getnframes())[:, 0]
        if marker.min() == marker.max():
            raise ValueError("marker has no signal variation")
        rate = source.getframerate()
    channels = [channel] if compare_channel is None else [channel, compare_channel]
    leaders = {number: [] for number in channels}
    with wave.open(str(recording_file), "rb") as stream:
        if not rate or stream.getframerate() != rate:
            raise ValueError("expected equal positive WAV sample rates")
        if len(leaders) != len(channels) or any(not 1 <= n <= stream.getnchannels() for n in channels):
            raise ValueError("expected distinct, valid one-based channels")
        total = stream.getnframes()
        width = len(marker)
        if total < width:
            raise ValueError("recording is shorter than marker")
        tail = read_pcm(stream, width - 1)
        for start in range(0, total - width + 1, block):
            count = min(block, total - width + 1 - start)
            samples = np.concatenate((tail, read_pcm(stream, count)))
            for number in channels:
                scores = correlations(samples[:, number - 1], marker)
                keep_best(leaders[number], scores, start)
            tail = samples[-(width - 1):]
    primary = summarize(leaders[channel], channel, rate, minimum, gap)
    if compare_channel is None:
        return primary
    other = summarize(leaders[compare_channel], compare_channel, rate, minimum, gap)
    offset = None
    if primary["status"] == other["status"] == "matched":
        offset = other["candidates"][0]["frame"] - primary["candidates"][0]["frame"]
    return {"reference": primary, "comparison": other, "delay_frames": offset,
            "delay_seconds": offset / rate if offset is not None else None}


def keep_best(leaders, scores, start):
    magnitudes = np.nan_to_num(np.abs(scores), nan=-1)
    for _ in range(min(2, len(scores))):
        index = int(np.argmax(magnitudes))
        if magnitudes[index] < 0:
            break
        leaders.append((float(magnitudes[index]), start + index, float(scores[index])))
        magnitudes[index] = -1
    leaders.sort(key=lambda match: (-match[0], match[1]))
    del leaders[2:]


def summarize(leaders, channel, rate, minimum, gap):
    # FFT roundoff can separate two identical copies by a few ulps.
    tolerance = 1e-12
    status = "missing"
    if leaders and leaders[0][0] >= minimum - tolerance:
        status = "matched"
        if len(leaders) == 2 and leaders[0][0] - leaders[1][0] <= gap + tolerance:
            status = "ambiguous"
    candidates = [{"frame": frame, "seconds": frame / rate, "correlation": value}
                  for _, frame, value in leaders]
    return {"status": status, "channel": channel, "sample_rate": rate, "candidates": candidates}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("marker")
    parser.add_argument("recording")
    parser.add_argument("--channel", type=int, default=1)
    parser.add_argument("--compare-channel", type=int, help="measure the signed delay relative to --channel")
    parser.add_argument("--minimum", type=float, default=0.8)
    parser.add_argument("--gap", type=float, default=0.05)
    args = parser.parse_args()
    try:
        report = locate(args.marker, args.recording, args.channel, args.minimum, args.gap,
                        compare_channel=args.compare_channel)
    except (OSError, ValueError, wave.Error, EOFError) as error:
        parser.exit(2, f"{error}\n")
    print(json.dumps(report, indent=2, allow_nan=False))
    if args.compare_channel is not None:
        return int(report["delay_frames"] is None)
    return int(report["status"] != "matched")


if __name__ == "__main__":
    raise SystemExit(main())
