"""Offline tests for per-run output isolation; no Azure calls are made."""

import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import test_agent


ROOT = Path(__file__).resolve().parent
RUN_ID = "47a836d9-61f5-41ba-a6f3-55338348dae0"


class RunOutputIsolationTests(unittest.TestCase):
    def test_run_id_is_required_and_normalized(self):
        self.assertEqual(test_agent.normalize_run_id(RUN_ID.upper()), RUN_ID)
        with self.assertRaises(ValueError):
            test_agent.normalize_run_id("../../part-merged.csv")
        with self.assertRaises(SystemExit):
            test_agent.main([])

    def test_prefix_is_scoped_to_one_run(self):
        self.assertEqual(test_agent.run_output_prefix(RUN_ID), f"runs/{RUN_ID}/")

    def test_reads_all_csv_shards_from_only_selected_run(self):
        container = Mock()
        container.list_blobs.return_value = [
            SimpleNamespace(name=f"runs/{RUN_ID}/part-000.csv"),
            SimpleNamespace(name=f"runs/{RUN_ID}/part-001.csv"),
            SimpleNamespace(name=f"runs/{RUN_ID}/_SUCCESS"),
            SimpleNamespace(name="runs/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa/part-000.csv"),
        ]
        payloads = {
            f"runs/{RUN_ID}/part-000.csv": b"order_id,customer_id,product,quantity,unit_price,duplicate_rank\n1001,C01,Widget,1,2.5,1\n",
            f"runs/{RUN_ID}/part-001.csv": b"order_id,customer_id,product,quantity,unit_price,duplicate_rank\n1002,C02,Gadget,2,4.5,1\n",
        }
        container.get_blob_client.side_effect = lambda name: Mock(
            download_blob=lambda: Mock(readall=lambda: payloads[name])
        )

        records = test_agent.read_run_records(container, RUN_ID)

        container.list_blobs.assert_called_once_with(name_starts_with=f"runs/{RUN_ID}/")
        self.assertEqual([record["order_id"] for record in records], ["1001", "1002"])

    def test_missing_run_output_fails_without_shared_file_fallback(self):
        container = Mock()
        container.list_blobs.return_value = [SimpleNamespace(name="part-merged.csv")]
        with self.assertRaises(FileNotFoundError):
            test_agent.read_run_records(container, RUN_ID)

    def test_dataset_and_pipeline_definitions_bind_run_id_to_both_sinks(self):
        pipeline = json.loads((ROOT / "pipeline_PL_RunOrderQualityChecks.json").read_text(encoding="utf-8"))
        params = pipeline["activities"][0]["typeProperties"]["dataflow"]["datasetParameters"]
        for sink in ("sinkValidRecords", "sinkInvalidRecords"):
            self.assertEqual(params[sink]["runFolder"]["value"], "@concat('runs/', pipeline().RunId)")
            self.assertEqual(params[sink]["runFolder"]["type"], "Expression")

        for filename, filesystem in (("dataset_DS_ValidOrders.json", "processed"),
                                     ("dataset_DS_InvalidOrders.json", "reports")):
            dataset = json.loads((ROOT / filename).read_text(encoding="utf-8"))
            self.assertEqual(dataset["typeProperties"]["location"]["fileSystem"], filesystem)
            self.assertEqual(dataset["typeProperties"]["location"]["folderPath"], {
                "value": "@dataset().runFolder", "type": "Expression"
            })
            self.assertEqual(dataset["parameters"]["runFolder"]["type"], "String")


if __name__ == "__main__":
    unittest.main()
