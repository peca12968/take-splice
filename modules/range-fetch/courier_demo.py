"""Upload a binary artifact to Chunk Courier, interrupt download, and resume it."""
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from urllib.request import Request, urlopen

from example_origin import origin, reply, digest


def run(courier):
    dll = Path(__file__).resolve().parent / "src/bin/Release/net10.0/RangeFetch.dll"
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        payload = (bytes(range(256)) * 600)[:150001]
        source, target = root / "source.bin", root / "target.bin"
        source.write_bytes(payload)
        target.write_bytes(b"previous release")
        env = {**os.environ, "PORT": "0", "COURIER_DB": str(root / "db.sqlite")}
        with subprocess.Popen(["node", courier / "server.mjs"], env=env, stdout=subprocess.PIPE, text=True) as server:
            try:
                base = server.stdout.readline().strip()
                if not base:
                    raise RuntimeError("Chunk Courier exited before listening")
                upload = subprocess.check_output(["node", courier / "upload.mjs", base, "release", source], text=True)
                assert json.loads(upload)["sha256"] == digest(payload)
                upstream = base + "/uploads/release/content"
                tag = '"' + digest(payload) + '"'
                def forward(handler, *, interrupt=False):
                    headers = {name: handler.headers[name] for name in ("Range","If-Range") if name in handler.headers}
                    with urlopen(Request(upstream,headers=headers),timeout=10) as response:
                        body = response.read()
                        assert response.status == 206 and response.headers["ETag"] == tag
                        reply(206,body[:4096] if interrupt else body, tag=tag, length=len(body),
                              content_range=response.headers["Content-Range"])(handler)

                with origin(forward, lambda h: forward(h,interrupt=True), forward, forward) as (url,calls):
                    command = ["dotnet",str(dll),url,str(target),digest(payload),"--max-range-bytes","65536"]
                    first = subprocess.run(command,capture_output=True,timeout=15)
                    assert first.returncode == 1 and target.read_bytes() == b"previous release"
                    result = json.loads(subprocess.check_output(command,text=True,timeout=15))
                    assert result == {"status":"verified","fileBytes":150001,"reusedBytes":69632,"downloadedBytes":80369}
                    assert calls == [("bytes=0-65535",None),("bytes=65536-131071",tag),
                                     ("bytes=69632-135167",tag),("bytes=135168-200703",tag)]
                    assert target.read_bytes() == payload
                    print(json.dumps(result))
            finally:
                server.terminate()
                server.wait(timeout=10)


if __name__ == "__main__":
    run(Path(sys.argv[1]).resolve())
