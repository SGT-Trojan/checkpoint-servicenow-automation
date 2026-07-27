from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

MODULE_PATH = Path(__file__).resolve().parents[1] / "cpuse_jhf_fetch.py"
SPEC = importlib.util.spec_from_file_location("cpuse_jhf_fetch", MODULE_PATH)
assert SPEC and SPEC.loader
m = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = m
SPEC.loader.exec_module(m)

CATALOG = """
<h3>Take 107 - <span>Recommended</span></h3>
<table><tr><td>Gateways and Management</td><td>
<a href="https://support.checkpoint.com/results/download/143762"><img></a> (TAR)
</td><td><a href="https://support.checkpoint.com/results/download/999999">EXE</a></td></tr></table>
<h3>Take 118 - Latest</h3>
<table><tr><td><a href="https://support.checkpoint.com/results/download/144486">TAR</a></td></tr></table>
"""


def detail(version="R82", take=107, filename=None, sha1="a" * 40, sha256="b" * 64):
    item = {
        "title": f"{version} JHF Take {take}",
        "version": version,
        "fileName": filename or f"Check_Point_{version}_jumbo_hf_main_Bundle_T{take}_FULL.tar",
        "datePublished": "2026-06-15",
        "size": "2.3 GB",
        "sha1": sha1,
        "sha256": sha256,
    }
    payload = {"props": {"pageProps": {"data": item, "publicFile": True}}}
    return f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(payload)}</script>'


class JhfFetchTests(unittest.TestCase):
    def test_catalog_keeps_recommended_and_latest_separate(self):
        catalog = m.parse_catalog(CATALOG, "R82")
        self.assertEqual(catalog["recommended"]["take"], 107)
        self.assertEqual(catalog["recommended"]["download_id"], "143762")
        self.assertEqual(catalog["latest"]["take"], 118)
        self.assertEqual(catalog["latest"]["download_id"], "144486")

    def test_metadata_is_structured_and_fail_closed(self):
        expected = {"version": "R82", "take": 107, "download_id": "143762"}
        parsed = m.parse_detail(detail(), expected)
        self.assertEqual(parsed["filename"], "Check_Point_R82_jumbo_hf_main_Bundle_T107_FULL.tar")
        self.assertEqual(parsed["sha256"], "b" * 64)
        with self.assertRaises(m.FetchError):
            m.parse_detail(detail(version="R81.20"), expected)
        with self.assertRaises(m.FetchError):
            m.parse_detail(detail(filename="unexpected.tar"), expected)
        with self.assertRaises(m.FetchError):
            m.parse_detail(detail(sha256="bad"), expected)

    def test_catalog_requires_recommended_take(self):
        with self.assertRaises(m.FetchError):
            m.parse_catalog("<h3>Take 118 - Latest</h3><a href='/results/download/1'>TAR</a>", "R82")

    def test_release_url_mapping(self):
        self.assertTrue(m.version_path("R82").endswith("/R82/R82.00/R82_Downloads.htm"))
        self.assertTrue(m.version_path("R81.20").endswith("/R81.20/R81.20/R81.20_Downloads.htm"))
        with self.assertRaises(m.FetchError):
            m.version_path("../../bad")

    def test_signed_url_rejects_untrusted_host(self):
        with mock.patch.object(m, "fetch_bytes", return_value=b'{"filePath":"https://evil.example/pkg"}'):
            with self.assertRaises(m.FetchError):
                m.signed_url("123")
        with mock.patch.object(m, "fetch_bytes", return_value=b'{"filePath":"https://dl3.checkpoint.com/paid/pkg"}'):
            self.assertEqual(m.signed_url("123"), "https://dl3.checkpoint.com/paid/pkg")

    def test_file_hashes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "package.tar"
            path.write_bytes(b"verified package")
            self.assertEqual(
                m.file_hashes(path),
                (
                    "0db9d1cf0b887897bee8f2cb41e429ce5431c2ec",
                    "5b365b709602bee45e8db24117a4f631efd738927cd9e85e95f765d6d58d909d",
                ),
            )



    def test_discovery_retries_transient_invalid_detail(self):
        recommended = detail(take=107).encode()
        latest = detail(take=118).encode()
        responses = [CATALOG.encode(), b"temporary response", recommended, latest]
        with mock.patch.object(m, "fetch_bytes", side_effect=responses), mock.patch.object(m.time, "sleep"):
            catalog = m.discover("R82")
        self.assertEqual(catalog["recommended"]["take"], 107)
        self.assertEqual(catalog["latest"]["take"], 118)


    def test_existing_verified_package_is_reused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = root / "package.tar"
            package.write_bytes(b"verified package")
            record = {
                "filename": package.name,
                "sha1": "0db9d1cf0b887897bee8f2cb41e429ce5431c2ec",
                "sha256": "5b365b709602bee45e8db24117a4f631efd738927cd9e85e95f765d6d58d909d",
            }
            with mock.patch.object(m, "signed_url") as signer:
                result = m.download(record, root)
            signer.assert_not_called()
            self.assertTrue(result["verified"])
            self.assertTrue(result["reused"])



if __name__ == "__main__":
    unittest.main()
