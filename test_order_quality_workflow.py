"""Offline validation tests for run-scoped quality reports and explicit upload."""

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch

import test_agent


RUN_ID = "0ab4f5e6-cd71-11f1-a72d-f9fc223d98b0"
HEADER = ",".join(test_agent.EXPECTED_FIELDS)
ISSUE = {
    "order_id": "101",
    "issue_type": "potential_duplicate",
    "severity": "low",
    "explanation": "Rank is greater than one, which is a duplicate signal but not proof.",
    "evidence": "duplicate_rank=2",
    "confidence": 0.7,
    "recommended_action": "Review the matching source rows.",
}
AGENT_REPORT = {"summary": "One possible duplicate needs human review.", "confirmed_errors": [], "potential_anomalies": [ISSUE]}


class QualityWorkflowTests(unittest.TestCase):
    def test_agent_report_contract_and_fenced_json(self):
        self.assertEqual(test_agent.decode_agent_report("```json\n" + json.dumps(AGENT_REPORT) + "\n```"), AGENT_REPORT)
        for bad in ("not json", json.dumps({"summary": "x"}), json.dumps({**AGENT_REPORT, "potential_anomalies": [None]})):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                test_agent.decode_agent_report(bad)
        confirmed_duplicate = {"summary": "bad classification", "confirmed_errors": [ISSUE], "potential_anomalies": []}
        with self.assertRaisesRegex(ValueError, "cannot be confirmed"):
            test_agent.decode_agent_report(json.dumps(confirmed_duplicate))
        ranked_rows = [{"order_id": "101", "duplicate_rank": "2"}]
        with self.assertRaisesRegex(ValueError, "must remain a potential anomaly"):
            test_agent.decode_agent_report(json.dumps({"summary": "omitted", "confirmed_errors": [], "potential_anomalies": []}), ranked_rows)

    def test_run_csv_rejects_missing_schema_and_empty_data(self):
        for payload, error in ((b"", ValueError), (b"order_id,wrong\n1,x\n", ValueError), (f"{HEADER}\n".encode(), ValueError)):
            container = Mock()
            name = f"runs/{RUN_ID}/part.csv"
            container.list_blobs.return_value = [SimpleNamespace(name=name)]
            container.get_blob_client.return_value.download_blob.return_value.readall.return_value = payload
            with self.subTest(payload=payload), self.assertRaises(error):
                test_agent.read_run_records(container, RUN_ID)

    def test_upload_is_create_only_and_verifies_readback(self):
        blob = Mock()
        blob.exists.return_value = False
        blob.download_blob.return_value.readall.return_value = json.dumps({"run_id": RUN_ID}).encode()
        service = Mock()
        service.get_blob_client.return_value = blob
        storage_module = ModuleType("azure.storage.blob")
        storage_module.BlobServiceClient = Mock(return_value=service)
        storage_module.ContentSettings = lambda **kwargs: kwargs
        report = {"run_id": RUN_ID}
        with patch.dict("sys.modules", {"azure.storage.blob": storage_module}):
            uri = test_agent.upload_existing_report(report, RUN_ID, object())
        blob.upload_blob.assert_called_once()
        self.assertFalse(blob.upload_blob.call_args.kwargs["overwrite"])
        self.assertTrue(uri.endswith(f"reports/runs/{RUN_ID}/ai_quality_report.json"))

        blob.exists.return_value = True
        with patch.dict("sys.modules", {"azure.storage.blob": storage_module}), self.assertRaises(FileExistsError):
            test_agent.upload_existing_report(report, RUN_ID, object())

    def test_saved_report_requires_matching_run_and_complete_metadata(self):
        report = {
            "run_id": RUN_ID,
            "agent": test_agent.AGENT_NAME,
            "data_mutated": False,
            "source": {"record_count": 5, "schema": list(test_agent.EXPECTED_FIELDS)},
            **AGENT_REPORT,
        }
        self.assertEqual(test_agent.validate_saved_report(report, RUN_ID), report)
        with self.assertRaisesRegex(ValueError, "RunId"):
            test_agent.validate_saved_report(report, "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")

    def test_upload_readback_mismatch_is_rejected(self):
        blob = Mock()
        blob.exists.return_value = False
        blob.download_blob.return_value.readall.return_value = b'{"run_id":"wrong"}'
        service = Mock()
        service.get_blob_client.return_value = blob
        storage_module = ModuleType("azure.storage.blob")
        storage_module.BlobServiceClient = Mock(return_value=service)
        storage_module.ContentSettings = lambda **kwargs: kwargs
        with patch.dict("sys.modules", {"azure.storage.blob": storage_module}), self.assertRaises(ValueError):
            test_agent.upload_existing_report({"run_id": RUN_ID}, RUN_ID, object())

    def test_upload_permission_error_is_actionable(self):
        class Denied(Exception):
            status_code = 403

        blob = Mock()
        blob.exists.side_effect = Denied("permission mismatch")
        service = Mock()
        service.get_blob_client.return_value = blob
        storage_module = ModuleType("azure.storage.blob")
        storage_module.BlobServiceClient = Mock(return_value=service)
        storage_module.ContentSettings = lambda **kwargs: kwargs
        with patch.dict("sys.modules", {"azure.storage.blob": storage_module}), self.assertRaisesRegex(PermissionError, "blob data-plane write access"):
            test_agent.upload_existing_report({"run_id": RUN_ID}, RUN_ID, object())


if __name__ == "__main__":
    unittest.main()
