"""Black-box real HTTP tests; each invocation starts a new .NET process."""
import json
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
from threading import Event
from time import monotonic, sleep
import unittest

from example_origin import DATA, PREFIX, TAG, digest, origin, partial, reply, stall_body

DLL = Path(__file__).resolve().parent / "src/bin/Release/net10.0/RangeFetch.dll"
OLD = b"prior published artifact"


class DownloadTests(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.destination = Path(tmp.name) / "release.bin"
        self.destination.write_bytes(OLD)

    def cmd(self, url, expected=DATA):
        return ["dotnet", str(DLL), url, str(self.destination), digest(expected)]

    def invoke(self, url, success=True, expected=DATA):
        result = subprocess.run(self.cmd(url, expected), capture_output=True, timeout=12)
        self.assertEqual(result.returncode, 0 if success else 1, result.stderr)
        if success:
            return json.loads(result.stdout)
        self.assertEqual(result.stdout, b"")
        self.assertTrue(result.stderr)

    def test_disconnect_resume_only_suffix_then_skip_verified_file(self):
        with origin(reply(200, DATA[:PREFIX], length=len(DATA)), partial(PREFIX)) as (url, calls):
            self.invoke(url, False)
            self.assertEqual(self.destination.read_bytes(), OLD)
            receipt = self.invoke(url)
            self.assertEqual(receipt, {"status": "verified", "fileBytes": len(DATA),
                                       "reusedBytes": PREFIX, "downloadedBytes": len(DATA) - PREFIX})
            self.assertEqual(self.destination.read_bytes(), DATA)
            self.assertEqual(self.invoke(url)["status"], "already_verified")
            self.assertEqual(calls, [(None, None), (f"bytes={PREFIX}-", TAG)])

    def test_changed_version_full_response_replaces_prefix(self):
        old_prefix = b"X" * PREFIX
        with origin(reply(200, old_prefix, length=len(DATA), tag='"old"'), reply(200, DATA)) as (url, calls):
            self.invoke(url, False)
            receipt = self.invoke(url)
            self.assertEqual((receipt["reusedBytes"], receipt["downloadedBytes"]), (0, len(DATA)))
            self.assertEqual(self.destination.read_bytes(), DATA)
            self.assertEqual(calls, [(None, None), (f"bytes={PREFIX}-", '"old"')])

    def test_first_window_with_weak_tag_or_empty_range_falls_back_to_full_get(self):
        for first in [partial(0, PREFIX-1, tag='W/"weak"'), reply(416, content_range="bytes */0")]:
            with origin(first, reply(200, DATA)) as (url, calls):
                self.destination.write_bytes(OLD)
                result = subprocess.run(self.cmd(url) + ["--max-range-bytes", str(PREFIX)], capture_output=True, timeout=12)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.destination.read_bytes(), DATA)
                self.assertEqual(calls, [(f"bytes=0-{PREFIX-1}", None), (None,None)])

    def test_server_overruns_window_and_retry_keeps_verified_prefix(self):
        with origin(partial(0, PREFIX-1), partial(PREFIX), partial(PREFIX)) as (url, calls):
            result = subprocess.run(self.cmd(url) + ["--max-range-bytes",str(PREFIX)], capture_output=True, timeout=12)
            self.assertEqual(result.returncode, 1)
            self.assertEqual(self.destination.read_bytes(),OLD)
            self.assertEqual(self.invoke(url)["reusedBytes"],PREFIX)
            self.assertEqual(calls, [(f"bytes=0-{PREFIX-1}",None),(f"bytes={PREFIX}-{2*PREFIX-1}",TAG),(f"bytes={PREFIX}-",TAG)])

    def test_stalled_full_and_partial_reads_resume_from_last_written_byte(self):
        release = Event()
        with origin(stall_body(release), stall_body(release, PREFIX, 2 * PREFIX),
                    partial(2 * PREFIX)) as (url, calls):
            try:
                for _ in range(2):
                    result = subprocess.run(self.cmd(url) + ["--idle-timeout-ms", "500"],
                                            capture_output=True, timeout=10)
                    self.assertEqual(result.returncode, 1, result.stderr)
                    self.assertIn(b"No response body data received for 500 ms", result.stderr)
                    self.assertEqual(self.destination.read_bytes(), OLD)
                receipt = self.invoke(url)
                self.assertEqual(receipt["reusedBytes"], 2 * PREFIX)
                self.assertEqual(receipt["downloadedBytes"], len(DATA) - 2 * PREFIX)
                self.assertEqual(self.destination.read_bytes(), DATA)
                self.assertEqual(calls, [(None, None), (f"bytes={PREFIX}-", TAG),
                                         (f"bytes={2 * PREFIX}-", TAG)])
            finally:
                release.set()

    def test_headers_without_any_body_time_out_and_next_run_uses_full_get(self):
        release = Event()
        with origin(stall_body(release, stop=0), reply(200, DATA)) as (url, calls):
            try:
                result = subprocess.run(self.cmd(url) + ["--idle-timeout-ms", "500"],
                                        capture_output=True, timeout=10)
                self.assertIn(b"No response body data received", result.stderr)
                self.assertEqual(result.returncode, 1)
                self.assertEqual(self.destination.read_bytes(), OLD)
                self.assertEqual(self.invoke(url)["reusedBytes"], 0)
                self.assertEqual(self.destination.read_bytes(), DATA)
                self.assertEqual(calls, [(None, None)] * 2)
            finally:
                release.set()

    def test_progressing_body_can_take_longer_than_idle_limit(self):
        def paced(handler):
            handler.send_response(200)
            handler.send_header("Content-Length", str(len(DATA)))
            handler.end_headers()
            for start in range(0, len(DATA), 16384):
                if start:
                    sleep(.1)
                handler.wfile.write(DATA[start:start + 16384])
                handler.wfile.flush()

        with origin(paced) as (url, calls):
            start = monotonic()
            result = subprocess.run(self.cmd(url) + ["--idle-timeout-ms", "1000"],
                                    capture_output=True, timeout=12)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertGreater(monotonic() - start, 1)
            self.assertEqual(json.loads(result.stdout)["downloadedBytes"], len(DATA))
            self.assertEqual(self.destination.read_bytes(), DATA)
            self.assertEqual(calls, [(None, None)])

    def test_invalid_idle_limit_rejected_before_contacting_source(self):
        with origin() as (url, calls):
            for value in ("0", "-1", "1.5", "2147483648"):
                with self.subTest(value=value):
                    result = subprocess.run(self.cmd(url) + ["--idle-timeout-ms", value],
                                            capture_output=True, timeout=10)
                    self.assertEqual(result.returncode, 2)
            self.assertEqual(calls, [])

    def test_weak_or_absent_tag_forces_full_get(self):
        for tag in ('W/"weak"', None):
            with self.subTest(tag=tag), origin(reply(200, DATA[:PREFIX], tag=tag, length=len(DATA)),
                                              reply(200, DATA, tag=tag)) as (url, calls):
                self.destination.write_bytes(OLD)
                self.invoke(url, False)
                self.assertEqual(self.invoke(url)["reusedBytes"], 0)
                self.assertEqual(calls, [(None, None), (None, None)])
                self.assertEqual(self.destination.read_bytes(), DATA)

    def test_invalid_partial_cannot_modify_destination_or_prefix(self):
        for invalid in [partial(PREFIX + 1), partial(PREFIX, tag='"wrong"'),
                        reply(206, b"a", content_range=f"bytes {PREFIX}-{PREFIX+1}/{len(DATA)}")]:
            with self.subTest(invalid=invalid), origin(reply(200, DATA[:PREFIX], length=len(DATA)), invalid,
                                                       partial(PREFIX)) as (url, calls):
                self.destination.write_bytes(OLD)
                self.invoke(url, False)
                self.invoke(url, False)
                self.assertEqual(self.destination.read_bytes(), OLD)
                self.assertEqual(self.invoke(url)["reusedBytes"], PREFIX)
                self.assertEqual(calls[-2:], [(f"bytes={PREFIX}-", TAG)] * 2)

    def test_short_valid_ranges_and_unsatisfiable_range(self):
        for responses, offsets, reused in [
            ([partial(PREFIX, PREFIX+4095), partial(PREFIX+4096)], [PREFIX, PREFIX+4096], PREFIX),
            ([reply(416, content_range="bytes */0"), reply(200, DATA)], [PREFIX, None], 0),
        ]:
            with origin(reply(200, DATA[:PREFIX], length=len(DATA)), *responses) as (url, calls):
                self.destination.write_bytes(OLD)
                self.invoke(url, False)
                self.assertEqual(self.invoke(url)["reusedBytes"], reused)
                self.assertEqual(self.destination.read_bytes(), DATA)
                self.assertEqual(calls[1:], [(f"bytes={n}-", TAG) if n else (None, None) for n in offsets])

    def test_checksum_failure_preserves_target_and_next_attempt_starts_clean(self):
        with origin(reply(200, b"bad release"), reply(200, DATA)) as (url, calls):
            self.invoke(url, False)
            self.assertEqual(self.destination.read_bytes(), OLD)
            self.assertEqual(self.invoke(url)["status"], "verified")
            self.assertEqual(calls, [(None, None)] * 2)
            self.assertEqual(self.destination.read_bytes(), DATA)

    def test_two_processes_cannot_write_same_destination_together(self):
        entered, finish = Event(), Event()

        def slow(handler):
            entered.set()
            if finish.wait(10):
                reply(200, DATA)(handler)

        with origin(slow) as (url, calls):
            with subprocess.Popen(self.cmd(url), stdout=subprocess.PIPE, stderr=subprocess.PIPE) as first:
                try:
                    self.assertTrue(entered.wait(7))
                    self.invoke(url, False)
                    self.assertEqual(self.destination.read_bytes(), OLD)
                finally:
                    finish.set()
                out, err = first.communicate(timeout=8)
            self.assertEqual(first.returncode, 0, err)
            self.assertEqual(json.loads(out)["status"], "verified")
            self.assertEqual(self.destination.read_bytes(), DATA)
            self.assertEqual(calls, [(None, None)])

    def test_interrupted_after_copy_can_publish_without_network(self):
        with origin(reply(200, DATA[:PREFIX], length=len(DATA))) as (url, calls):
            self.invoke(url, False)
            # Simulate a process ending after the last write and before publishing.
            stage = Path(str(self.destination) + ".range-fetch") / "data"
            with stage.open("ab") as stream:
                stream.write(DATA[PREFIX:])
            receipt = self.invoke(url)
            self.assertEqual((receipt["reusedBytes"], receipt["downloadedBytes"]), (len(DATA), 0))
            self.assertEqual(self.destination.read_bytes(), DATA)
            self.assertEqual(calls, [(None, None)])

    def test_different_url_or_expected_digest_cannot_adopt_prefix(self):
        for change in ("url", "digest"):
            with self.subTest(change=change), origin(reply(200, DATA[:PREFIX], length=len(DATA)),
                                                     reply(200, DATA)) as (url, calls):
                self.destination.write_bytes(OLD)
                self.invoke(url, False, expected=DATA if change == "url" else b"another release")
                receipt = self.invoke(url + "?v=2" if change == "url" else url)
                self.assertEqual(receipt["reusedBytes"], 0)
                self.assertEqual(calls, [(None, None)] * 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
