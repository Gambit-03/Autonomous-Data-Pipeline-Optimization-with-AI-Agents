"""Offline tests for performance telemetry normalization."""

import json
import unittest

import analyze_pipeline_performance as performance


class PerformanceReportTests(unittest.TestCase):
    def test_final_activity_reports_acquisition_quality_counts_and_units(self):
        rows = [{
            "TimeGenerated": "2026-10-21T17:05:39Z",
            "PipelineName": "PL_RunOrderQualityChecks",
            "PipelineRunId": "run-1",
            "ActivityName": "Data flow1",
            "ActivityRunId": "activity-1",
            "ActivityType": "ExecuteDataFlow",
            "Status": "Succeeded",
            "Start": "2026-10-21T17:01:31Z",
            "End": "2026-10-21T17:05:39Z",
            "Output": json.dumps({
                "runStatus": {
                    "computeAcquisitionDuration": 147259,
                    "metrics": {
                        "sinkValidRecords": {"rowsWritten": 95},
                        "sinkInvalidRecords": {"rowsWritten": 5},
                    },
                },
                "billingReference": {"billableDuration": [{"duration": 0.5104, "unit": "coreHour"}]},
            }),
        }]
        report = performance.build_report(activity_rows=rows, pipeline_rows=[])
        activity = report["activity_runs"][0]
        self.assertEqual(activity["duration_seconds"], 248)
        self.assertEqual(activity["compute_acquisition_duration_seconds"], 147.259)
        self.assertEqual(activity["sink_rows_written"], {"sinkValidRecords": 95, "sinkInvalidRecords": 5})
        self.assertEqual(activity["billing_unit"], "coreHour")
        self.assertIsNone(report["estimated_currency_cost"])

    def test_queued_only_and_empty_telemetry_do_not_become_final_runs(self):
        self.assertEqual(performance.normalize_activities([{
            "ActivityRunId": "activity-1", "Status": "InProgress",
            "TimeGenerated": "2026-10-21T17:04:00Z",
            "Start": "2026-10-21T17:01:31Z", "End": "1601-01-01T00:00:00Z",
        }]), [])
        with self.assertRaisesRegex(RuntimeError, "no final activity or pipeline runs"):
            performance.build_report(activity_rows=[], pipeline_rows=[])


if __name__ == "__main__":
    unittest.main()
