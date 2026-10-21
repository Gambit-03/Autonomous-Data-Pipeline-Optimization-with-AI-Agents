import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import publish_reports


class PublishReportsTests(unittest.TestCase):
    def test_uploads_create_only_and_verifies_readback(self):
        content = b'{"synthetic": false}'
        blob = Mock()
        blob.download_blob.return_value.readall.return_value = content
        service = Mock()
        service.get_blob_client.return_value = blob
        storage = Mock()
        storage.BlobServiceClient.return_value = service
        storage.ContentSettings.side_effect = lambda **kwargs: kwargs
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "report.json"
            path.write_bytes(content)
            with patch.dict("sys.modules", {"azure.storage.blob": storage}):
                result = publish_reports.publish_files([path], object(), "20261021T000000Z")
        self.assertEqual(result, [f"{publish_reports.STORAGE_URL}/reports/demo-reports/20261021T000000Z/report.json"])
        self.assertFalse(blob.upload_blob.call_args.kwargs["overwrite"])
        blob.download_blob.assert_called_once_with()

    def test_synthetic_json_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "synthetic.json"
            path.write_text(json.dumps({"source": "synthetic_fixture"}), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "synthetic"):
                publish_reports.validate_report(path, path.read_bytes())


if __name__ == "__main__":
    unittest.main()
