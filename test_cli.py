import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from demo import generate
from locate import locate


class DelayTests(unittest.TestCase):
    def test_signed_stereo_delay_and_cli_failure_modes(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "demo"
            generate(folder)
            marker, capture = folder / "marker.wav", folder / "stereo.wav"
            cli = [sys.executable, Path(__file__).with_name("locate.py"), marker]
            def run(recording, other, expected):
                result = subprocess.run([*cli, recording, "--compare-channel", str(other)],
                                        text=True, capture_output=True)
                self.assertEqual(result.returncode, expected, result.stderr)
                if expected == 2:
                    self.assertEqual(result.stdout, "")
                    return result.stderr
                return json.loads(result.stdout)

            report = run(capture, 2, 0)
            first, second = (report[name]["candidates"][0] for name in ("reference", "comparison"))
            self.assertEqual((first["frame"], second["frame"]), (65545, 65962))
            self.assertLess(second["correlation"], -0.99)
            self.assertEqual((report["delay_frames"], report["delay_seconds"]), (417, 417 / 48000))
            reverse = locate(marker, capture, channel=2, compare_channel=1)
            self.assertEqual(reverse["delay_frames"], -417)

            echo = run(folder / "echo.wav", 2, 1)
            self.assertIsNone(echo["delay_frames"])
            self.assertEqual(echo["comparison"]["status"], "ambiguous")
            self.assertEqual({c["frame"] for c in echo["comparison"]["candidates"]}, {65962, 81000})
            self.assertIn("channels", run(capture, 1, 2))
