from __future__ import annotations

import io
import os
from pathlib import Path
import stat
import tempfile
import unittest
import zipfile

import servicenow_checkpoint_readiness_worker as readiness
import servicenow_checkpoint_runner as runner


ATTACHMENT_ID_A = "a" * 32
ATTACHMENT_ID_B = "b" * 32


class FakeServiceNow:
    def __init__(self, payloads: dict[str, bytes] | None = None):
        self.payloads = payloads or {}
        self.requests: list[str] = []

    def attachment_bytes(self, sys_id: str) -> bytes:
        self.requests.append(sys_id)
        return self.payloads.get(sys_id, b"synthetic")


class AttachmentStorageTests(unittest.TestCase):
    def test_runner_rejects_unsafe_names_before_download(self) -> None:
        unsafe_names = (
            "../../package.csv",
            "/tmp/package.csv",
            "..\\..\\package.xlsx",
            "",
            "package.txt",
            "package.csv.exe",
            " package.csv",
            "package.csv\n",
        )
        for name in unsafe_names:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                sn = FakeServiceNow()
                context = {"attachments": [{"sys_id": ATTACHMENT_ID_A, "file_name": name}]}
                with self.assertRaises((ValueError, RuntimeError)):
                    runner.download_context_attachments(sn, context, Path(directory))
                self.assertEqual(sn.requests, [])

    def test_duplicate_original_names_store_under_distinct_sys_ids(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            sn = FakeServiceNow({ATTACHMENT_ID_A: b"first", ATTACHMENT_ID_B: b"second"})
            attachments = [
                {"sys_id": ATTACHMENT_ID_A, "file_name": "CPUSE Package.csv"},
                {"sys_id": ATTACHMENT_ID_B, "file_name": "CPUSE Package.csv"},
            ]
            runner.download_context_attachments(sn, {"attachments": attachments}, Path(directory))

            first = Path(attachments[0]["local_path"])
            second = Path(attachments[1]["local_path"])
            self.assertEqual(first.name, f"{ATTACHMENT_ID_A}.csv")
            self.assertEqual(second.name, f"{ATTACHMENT_ID_B}.csv")
            self.assertNotEqual(first, second)
            self.assertEqual(first.read_bytes(), b"first")
            self.assertEqual(second.read_bytes(), b"second")
            self.assertEqual(stat.S_IMODE(first.stat().st_mode), 0o600)
            self.assertEqual([row["file_name"] for row in attachments], ["CPUSE Package.csv"] * 2)

    def test_readiness_worker_uses_the_same_sys_id_storage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            sn = FakeServiceNow({ATTACHMENT_ID_A: b"xlsx"})
            attachments = [{"sys_id": ATTACHMENT_ID_A, "file_name": "CPUSE Package.XLSX"}]
            readiness.download_attachments(sn, attachments, Path(directory))
            stored = Path(attachments[0]["local_path"])
            self.assertEqual(stored.name, f"{ATTACHMENT_ID_A}.xlsx")
            self.assertEqual(stored.read_bytes(), b"xlsx")

    def test_existing_regular_destination_is_forced_to_mode_0600(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            out_dir = run_dir / "attachments"
            out_dir.mkdir()
            destination = out_dir / f"{ATTACHMENT_ID_A}.csv"
            destination.write_bytes(b"old")
            destination.chmod(0o666)
            sn = FakeServiceNow({ATTACHMENT_ID_A: b"new"})
            attachments = [{"sys_id": ATTACHMENT_ID_A, "file_name": "package.csv"}]
            runner.download_context_attachments(sn, {"attachments": attachments}, run_dir)
            self.assertEqual(destination.read_bytes(), b"new")
            self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o600)

    def test_invalid_sys_id_is_rejected_before_download(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            sn = FakeServiceNow()
            context = {"attachments": [{"sys_id": "../outside", "file_name": "package.csv"}]}
            with self.assertRaisesRegex(ValueError, "sys_id"):
                runner.download_context_attachments(sn, context, Path(directory))
            self.assertEqual(sn.requests, [])

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks are unavailable")
    def test_preexisting_attachment_directory_symlink_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as outside:
            run_dir = Path(directory)
            os.symlink(outside, run_dir / "attachments", target_is_directory=True)
            sn = FakeServiceNow()
            context = {
                "attachments": [{"sys_id": ATTACHMENT_ID_A, "file_name": "package.csv"}]
            }
            with self.assertRaisesRegex(RuntimeError, "directory must not be a symlink"):
                runner.download_context_attachments(sn, context, run_dir)
            self.assertEqual(sn.requests, [])

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks are unavailable")
    def test_preexisting_destination_symlink_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as outside:
            run_dir = Path(directory)
            out_dir = run_dir / "attachments"
            out_dir.mkdir()
            outside_file = Path(outside) / "outside.csv"
            outside_file.write_bytes(b"unchanged")
            os.symlink(outside_file, out_dir / f"{ATTACHMENT_ID_A}.csv")
            sn = FakeServiceNow()
            context = {
                "attachments": [{"sys_id": ATTACHMENT_ID_A, "file_name": "package.csv"}]
            }
            with self.assertRaisesRegex(RuntimeError, "destination must not be a symlink"):
                runner.download_context_attachments(sn, context, run_dir)
            self.assertEqual(sn.requests, [])
            self.assertEqual(outside_file.read_bytes(), b"unchanged")



class XlsxParsingTests(unittest.TestCase):
    WORKBOOK_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <sheets>{sheets}</sheets>
</workbook>"""
    RELATIONSHIPS_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  {relationships}
</Relationships>"""
    WORKSHEET_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <sheetData>{rows}</sheetData>
</worksheet>"""

    def workbook_bytes(
        self,
        *,
        sheets: str,
        relationships: str,
        worksheets: dict[str, str],
        shared_strings: str | None = None,
    ) -> bytes:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("xl/workbook.xml", self.WORKBOOK_TEMPLATE.format(sheets=sheets))
            archive.writestr(
                "xl/_rels/workbook.xml.rels",
                self.RELATIONSHIPS_TEMPLATE.format(relationships=relationships),
            )
            for path, rows in worksheets.items():
                archive.writestr(path, self.WORKSHEET_TEMPLATE.format(rows=rows))
            if shared_strings is not None:
                archive.writestr("xl/sharedStrings.xml", shared_strings)
        return buffer.getvalue()

    def test_sparse_reordered_inline_cells_keep_their_columns(self) -> None:
        rows = """
<row r="1">
  <c r="E1" t="inlineStr"><is><t>action</t></is></c>
  <c r="A1" t="inlineStr"><is><t>package_name</t></is></c>
  <c r="C1" t="inlineStr"><is><t>sha256</t></is></c>
</row>
<row r="2">
  <c r="E2" t="inlineStr"><is><t>remove</t></is></c>
  <c r="A2" t="inlineStr"><is><t>synthetic.tgz</t></is></c>
</row>"""
        data = self.workbook_bytes(
            sheets='<sheet name="Ticket" sheetId="1" r:id="rId7"/>',
            relationships=(
                '<Relationship Id="rId7" '
                'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
                'Target="worksheets/ticket-data.xml"/>'
            ),
            worksheets={"xl/worksheets/ticket-data.xml": rows},
        )
        self.assertEqual(
            runner.parse_xlsx_bytes(data),
            [{"package_name": "synthetic.tgz", "sha256": "", "action": "remove"}],
        )

    def test_first_workbook_sheet_is_resolved_through_relationships(self) -> None:
        ticket_rows = """
<row r="1"><c r="A1" t="inlineStr"><is><t>package_name</t></is></c></row>
<row r="2"><c r="A2" t="inlineStr"><is><t>approved.tgz</t></is></c></row>"""
        decoy_rows = """
<row r="1"><c r="A1" t="inlineStr"><is><t>package_name</t></is></c></row>
<row r="2"><c r="A2" t="inlineStr"><is><t>wrong.tgz</t></is></c></row>"""
        data = self.workbook_bytes(
            sheets=(
                '<sheet name="Ticket" sheetId="1" r:id="rIdTicket"/>'
                '<sheet name="Decoy" sheetId="2" r:id="rIdDecoy"/>'
            ),
            relationships=(
                '<Relationship Id="rIdDecoy" '
                'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
                'Target="worksheets/sheet1.xml"/>'
                '<Relationship Id="rIdTicket" '
                'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
                'Target="worksheets/ticket.xml"/>'
            ),
            worksheets={
                "xl/worksheets/sheet1.xml": decoy_rows,
                "xl/worksheets/ticket.xml": ticket_rows,
            },
        )
        self.assertEqual(runner.parse_xlsx_bytes(data), [{"package_name": "approved.tgz"}])

    def test_shared_strings_remain_supported(self) -> None:
        shared = """<?xml version="1.0" encoding="UTF-8"?>
<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <si><t>package_name</t></si><si><t>shared.tgz</t></si>
</sst>"""
        rows = '<row r="1"><c r="A1" t="s"><v>0</v></c></row><row r="2"><c r="A2" t="s"><v>1</v></c></row>'
        data = self.workbook_bytes(
            sheets='<sheet name="Ticket" sheetId="1" r:id="rId1"/>',
            relationships=(
                '<Relationship Id="rId1" '
                'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
                'Target="worksheets/sheet4.xml"/>'
            ),
            worksheets={"xl/worksheets/sheet4.xml": rows},
            shared_strings=shared,
        )
        self.assertEqual(runner.parse_xlsx_bytes(data), [{"package_name": "shared.tgz"}])

    def test_cell_without_coordinate_fails_closed(self) -> None:
        rows = '<row r="1"><c t="inlineStr"><is><t>package_name</t></is></c></row>'
        data = self.workbook_bytes(
            sheets='<sheet name="Ticket" sheetId="1" r:id="rId1"/>',
            relationships=(
                '<Relationship Id="rId1" '
                'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
                'Target="worksheets/sheet.xml"/>'
            ),
            worksheets={"xl/worksheets/sheet.xml": rows},
        )
        with self.assertRaisesRegex(ValueError, "cell reference"):
            runner.parse_xlsx_bytes(data)

if __name__ == "__main__":
    unittest.main()
