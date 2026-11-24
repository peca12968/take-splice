"""Read the CLI export through Chromium's native text track parser."""
import base64
from html import unescape
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parent


def check(browser, directory):
    output = directory / "captions.vtt"
    subprocess.run([sys.executable, ROOT / "conform.py", ROOT / "examples/edit.json",
                    ROOT / "examples/tracks.json", output], check=True)
    page = directory / "test.html"
    page.write_text("""<!doctype html><meta charset="utf-8">
<video></video><pre id="result">waiting</pre><script>
const track = document.createElement("track");
track.kind = "captions";
track.onload = () => {
  const cues = Array.from(track.track.cues);
  document.querySelector("pre").textContent = JSON.stringify(cues.map(c =>
    [c.id, c.text, c.startTime, c.endTime, c.line, c.position, c.size, c.align, c.snapToLines]));
};
track.onerror = () => document.querySelector("pre").textContent = "track load failed";
track.src = "data:text/vtt;base64,DATA";
document.querySelector("video").append(track);
track.track.mode = "hidden";
</script>""".replace(",DATA", "," + base64.b64encode(output.read_bytes()).decode("ascii")), encoding="utf-8")
    page_result = subprocess.run([browser, "--headless", "--disable-gpu", "--no-first-run",
                                  "--user-data-dir=" + str(directory / "profile"), "--dump-dom",
                                  "--virtual-time-budget=5000", page.as_uri()], check=True,
                                 capture_output=True, encoding="utf-8", errors="replace", timeout=45)
    body = re.search(r'<pre id="result">(.*?)</pre>', page_result.stdout, re.DOTALL)
    if not body:
        raise RuntimeError("Chromium did not return the text track result")
    cues = json.loads(unescape(body[1]))
    if len(cues) != 6:
        raise ValueError(f"Expected 6 cues, got {len(cues)}")
    for moment, clip in ((0.210, 1), (1.210, 3)):
        visible = [[cue[0], cue[1], *cue[4:]] for cue in cues if cue[2] <= moment < cue[3]]
        expected = [[f"clip-{clip}-cue-2", "高音", 15, 10, 38, "start", False],
                    [f"clip-{clip}-cue-3", "延音", 80, 90, 32, "end", False]]
        if visible != expected:
            raise ValueError(f"Unexpected cue layout at {moment}s: {visible!r}")
    print("Verified native cue layout at 210 ms and 1210 ms")


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as tmp:
        check(sys.argv[1], Path(tmp))
