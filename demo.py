"""Generate a mono sync marker and two deterministic stereo captures."""
import argparse
from pathlib import Path
import wave

import numpy as np


def save(path, frames):
    with wave.open(str(path), "wb") as stream:
        stream.setparams((1 if frames.ndim == 1 else frames.shape[1], 2, 48000, 0, "NONE", "PCM"))
        stream.writeframes(frames.astype("<i2").tobytes())


def generate(directory):
    directory.mkdir(parents=True)
    rng = np.random.default_rng(12968)
    marker = rng.integers(-3000, 3001, 257, dtype=np.int16)
    capture = rng.integers(-90, 91, (96000, 2), dtype=np.int16)
    capture[65545:65802, 0] += marker
    capture[65962:66219, 1] += 1700 - 2 * marker
    save(directory / "marker.wav", marker)
    save(directory / "stereo.wav", capture)
    capture[81000:81257, 1] = marker
    save(directory / "echo.wav", capture)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path, help="new output directory")
    generate(parser.parse_args().directory)
