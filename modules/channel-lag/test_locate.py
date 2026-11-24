from pathlib import Path
import tempfile
import unittest
import wave

import numpy as np

from locate import correlations, locate


def save(path, data, rate=16000):
    with wave.open(str(path), "wb") as wav:
        wav.setparams((1 if data.ndim == 1 else data.shape[1], 2, rate, 0, "NONE", "PCM"))
        wav.writeframes(data.astype("<i2").tobytes())


class AlignmentTests(unittest.TestCase):
    def test_fft_coefficients_against_individually_centered_windows(self):
        rng = np.random.default_rng(91)
        for width in (3, 16, 27):
            values = rng.integers(31998, 32002, 105, dtype=np.int16)
            values[20:55] = 32000
            marker = rng.integers(-400, 400, width, dtype=np.int16)
            expected = []
            for start in range(len(values) - width + 1):
                window = values[start:start+width]
                expected.append(np.corrcoef(marker, window)[0, 1] if window.max() != window.min() else np.nan)
            np.testing.assert_allclose(correlations(values, marker), expected, atol=1e-10)

    def test_stereo_boundary_polarity_noise_and_ambiguous_echo(self):
        with tempfile.TemporaryDirectory() as directory:
            reference, capture = (Path(directory) / name for name in ("marker.wav", "capture.wav"))
            rng = np.random.default_rng(23)
            marker = rng.integers(-2500, 2501, 129, dtype=np.int16)
            data = np.zeros((1500, 2), dtype=np.int16)
            data[:, 1] = rng.integers(-100, 101, len(data), dtype=np.int16)
            data[509:638, 1] += 3000 - 2 * marker
            save(reference, marker)
            save(capture, data)
            for block in (64, 512, 4096):
                result = locate(reference, capture, channel=2, block=block)
                self.assertEqual(result["status"], "matched")
                best = result["candidates"][0]
                self.assertEqual(best["frame"], 509)
                self.assertLess(best["correlation"], -0.99)
            self.assertEqual(locate(reference, capture)["status"], "missing")
            data[1031:1160, 1] = marker
            save(capture, data)
            echo = locate(reference, capture, channel=2, block=64)
            self.assertEqual(echo["status"], "ambiguous")
            self.assertEqual({c["frame"] for c in echo["candidates"]}, {509, 1031})
