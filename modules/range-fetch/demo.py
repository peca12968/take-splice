"""Exercise two separate downloader processes against the same loopback origin."""
import json
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory

from example_origin import DATA, PREFIX, TAG, digest, origin, partial, reply


def main():
    dll = Path(__file__).resolve().parent / "src/bin/Release/net10.0/RangeFetch.dll"
    if not dll.exists():
        raise SystemExit("Build first with: dotnet build src -c Release")
    with TemporaryDirectory() as temp, origin(reply(200, DATA[:PREFIX], length=len(DATA)),
                                              partial(PREFIX)) as (url, requests):
        path = Path(temp) / "release.bin"
        previous = b"previous release"
        path.write_bytes(previous)
        cmd = ["dotnet", str(dll), url, str(path), digest(DATA)]
        first = subprocess.run(cmd, capture_output=True, timeout=15)
        if first.returncode != 1 or path.read_bytes() != previous:
            raise RuntimeError("Interrupted transfer did not preserve previous release")
        final = subprocess.run(cmd, capture_output=True, check=True, timeout=15)
        receipt = json.loads(final.stdout)
        if (path.read_bytes() != DATA or requests != [(None, None), (f"bytes={PREFIX}-", TAG)]
                or receipt["downloadedBytes"] != len(DATA) - PREFIX):
            raise RuntimeError("Resume evidence disagrees with the fixture")
        print(json.dumps({"previous_file_preserved": True, "retry_range": requests[1][0],
                          "retry_if_range": requests[1][1], "receipt": receipt}, indent=2))


if __name__ == "__main__":
    main()
