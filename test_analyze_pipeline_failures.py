"""Offline tests for pipeline_failure_analysis behavior; no Azure calls are made."""

import json
import sys
import tempfile
import types
import unittest
from unittest.mock import MagicMock, Mock, patch

import analyze_pipeline_failures as analyzer


class FailureAnalysisTests(unittest.TestCase):
    def test_zero_failures_skips_agent(self):
        def should_not_run(_):
            self.fail("agent must not run when there are no failures")

        report = analyzer.build_report([], [], agent=should_not_run)
        self.assertEqual(report["failed_run_count"], 0)
        self.assertEqual(report["analysis"]["failure_analyses"], [])
        self.assertFalse(report["synthetic"])

    def test_failure_context_and_agent_result(self):
        pipeline = [{
            "PipelineName": "orders", "RunId": "run-1", "Status": "Failed",
            "ErrorCode": "Timeout", "FailureType": "Transient",
            "ErrorMessage": "read timed out", "TimeGenerated": "2026-10-21T00:00:00Z",
        }]
        activity = [{
            "PipelineRunId": "run-1", "ActivityName": "copy", "ActivityRunId": "act-1",
            "Status": "Failed", "ErrorMessage": "timed out",
        }]
        expected = {"overall_summary": "one failure", "failure_analyses": [{
            "pipeline_name": "orders", "run_id": "run-1", "status": "Failed",
            "error_code": "Timeout", "failure_type": "Transient",
            "observed_facts": ["The orders pipeline run run-1 failed with Timeout."],
            "likely_cause": "Transient source timeout hypothesis", "confidence": "medium",
            "recommended_action": "Verify the source is available before a human-approved retry.",
            "safe_to_retry": False, "retry_preconditions": ["Confirm idempotency."],
        }]}
        seen = []
        report = analyzer.build_report(pipeline, activity, agent=lambda rows: (seen.extend(rows) or expected))
        self.assertEqual(report["analysis"], expected)
        self.assertEqual(seen[0]["observed_facts"]["failed_activities"][0]["activity_name"], "copy")
        self.assertEqual(seen[0]["observed_facts"]["error_code"], "Timeout")

    def test_secret_redaction(self):
        self.assertNotIn("abc123", analyzer.redact("connection?sig=abc123"))
        self.assertNotIn("token-value", analyzer.redact("Bearer token-value"))

    def test_log_analytics_malformed_json_fails_explicitly(self):
        with self.assertRaises(analyzer.PipelineAnalysisError):
            analyzer.parse_query_result("not json")

    def test_agent_malformed_json_fails_explicitly(self):
        with self.assertRaises(analyzer.PipelineAnalysisError):
            analyzer.parse_agent_response("not json")
        with self.assertRaises(analyzer.PipelineAnalysisError):
            analyzer.parse_agent_response(json.dumps({"summary": "missing required list"}))
        with self.assertRaisesRegex(analyzer.PipelineAnalysisError, "required fields"):
            analyzer.parse_agent_response(json.dumps({"overall_summary": "bad item", "failure_analyses": [{"run_id": "x"}]}))
        with self.assertRaisesRegex(analyzer.PipelineAnalysisError, "required fields"):
            analyzer.parse_agent_response(json.dumps({"overall_summary": "facts missing", "failure_analyses": [{
                "run_id": "x", "likely_cause": "hypothesis", "confidence": 0.5,
                "recommended_action": "inspect", "safe_to_retry": False, "retry_preconditions": [],
            }]}))
        unsafe_to_assume = [{"observed_facts": {"pipeline_name": "synthetic", "run_id": "run-1",
            "status": "Failed", "error_code": "Timeout", "failure_type": "Transient"}}]
        unsafe_claim = {"overall_summary": "test", "failure_analyses": [{
            "pipeline_name": "synthetic", "run_id": "run-1", "status": "Failed",
            "error_code": "Timeout", "failure_type": "Transient", "observed_facts": [],
            "likely_cause": "Timeout", "confidence": "low", "recommended_action": "Inspect source.",
            "safe_to_retry": True, "retry_preconditions": [],
        }]}
        with self.assertRaisesRegex(analyzer.AgentResponseValidationError, "without explicit input evidence"):
            analyzer.parse_agent_response(json.dumps(unsafe_claim), expected_records=unsafe_to_assume)

    def test_connected_agent_uses_only_supported_conversation_and_input_fields(self):
        facts = {
            "pipeline_name": "SYNTHETIC_TEST_ONLY_Pipeline",
            "run_id": "SYNTHETIC_TEST_ONLY_RUN_0001",
            "status": "Failed",
            "error_code": "SYNTHETIC_TEST_ONLY_SourceTimeout",
            "failure_type": "SYNTHETIC_TEST_ONLY_Transient",
            "error_message": "Synthetic timeout fixture.",
            "failed_activities": [],
        }
        expected = {"overall_summary": "synthetic", "failure_analyses": [{
            "pipeline_name": facts["pipeline_name"],
            "run_id": facts["run_id"],
            "status": facts["status"],
            "error_code": facts["error_code"],
            "failure_type": facts["failure_type"],
            "observed_facts": ["The synthetic record represents a failed source-timeout fixture."],
            "likely_cause": "Synthetic transient timeout hypothesis.",
            "confidence": "medium",
            "recommended_action": "Check source availability.",
            "safe_to_retry": False,
            "retry_preconditions": ["Confirm source availability and idempotency."],
        }]}
        credential = MagicMock()
        client = Mock()
        client.conversations.create.return_value.id = "synthetic-conversation"
        client.responses.create.return_value.output_text = json.dumps(expected)
        client.responses.create.return_value.id = "resp_synthetic_test"
        client.responses.create.return_value.usage = None
        project = MagicMock()
        project.__enter__.return_value = project
        project.get_openai_client.return_value = client
        identity_module = types.ModuleType("azure.identity")
        identity_module.DefaultAzureCredential = Mock(return_value=credential)
        projects_module = types.ModuleType("azure.ai.projects")
        projects_module.AIProjectClient = Mock(return_value=project)
        rows = [{"observed_facts": facts}]
        with patch.dict(sys.modules, {
            "azure.identity": identity_module,
            "azure.ai.projects": projects_module,
        }):
            actual = analyzer.call_agent(rows)
        self.assertEqual(actual, expected)
        self.assertEqual(set(client.responses.create.call_args.kwargs), {"conversation", "input"})

    def test_analyze_with_agent_allows_only_one_corrective_response(self):
        good = {"overall_summary": "synthetic", "failure_analyses": []}
        with patch.object(analyzer, "call_agent", side_effect=[
            analyzer.AgentResponseValidationError("invalid first response"), good,
        ]) as invoke:
            self.assertEqual(analyzer.analyze_with_agent([]), good)
        self.assertEqual(invoke.call_count, 2)
        self.assertIsNone(invoke.call_args_list[0].kwargs.get("correction"))
        self.assertIn("invalid first response", invoke.call_args_list[1].kwargs["correction"])

    def test_query_failure_is_propagated(self):
        with patch.object(analyzer, "run_az", side_effect=analyzer.PipelineAnalysisError("simulated CLI failure")):
            with self.assertRaisesRegex(analyzer.PipelineAnalysisError, "simulated CLI failure"):
                analyzer.query_log_analytics("print 1")

    def test_workspace_auth_or_missing_workspace_id_fails_explicitly(self):
        with patch.object(analyzer, "run_az", return_value=""):
            with self.assertRaisesRegex(analyzer.PipelineAnalysisError, "empty Log Analytics workspace ID"):
                analyzer.query_log_analytics("print 1")
        with patch.object(analyzer, "run_az", side_effect=analyzer.PipelineAnalysisError("Azure CLI failed: Please run az login")):
            with self.assertRaisesRegex(analyzer.PipelineAnalysisError, "az login"):
                analyzer.query_log_analytics("print 1")

    def test_empty_telemetry_is_not_written_as_an_empty_performance_report(self):
        import analyze_pipeline_performance as performance
        self.assertEqual(analyzer.parse_query_result(json.dumps({"tables": []})), [])
        with self.assertRaisesRegex(RuntimeError, "no final activity or pipeline runs"):
            performance.build_report(activity_rows=[], pipeline_rows=[])

    def test_synthetic_fixture_is_labelled_and_never_queried(self):
        def fake_agent(rows):
            self.assertTrue(rows[0]["observed_facts"]["run_id"].startswith("SYNTHETIC_TEST_ONLY"))
            return {"overall_summary": "simulated", "failure_analyses": []}

        with patch.object(analyzer, "query_log_analytics", side_effect=AssertionError("must not query Azure")):
            result = analyzer.synthetic_analysis(fake_agent)
        self.assertTrue(result["simulated"])
        self.assertEqual(result["source"], "synthetic_fixture")

    def test_synthetic_cli_mock_avoids_foundry_and_labels_output(self):
        with tempfile.TemporaryDirectory() as directory:
            target = analyzer.Path(directory) / "synthetic.json"
            with patch.object(analyzer, "SYNTHETIC_REPORT_FILE", target), patch.object(analyzer, "call_agent", side_effect=AssertionError("Foundry call not expected")):
                self.assertEqual(analyzer.main(["--synthetic", "--mock-agent"]), 0)
            report = json.loads(target.read_text(encoding="utf-8"))
            self.assertTrue(report["simulated"])
            self.assertTrue(report["failures"][0]["observed_facts"]["run_id"].startswith("SYNTHETIC_TEST_ONLY"))
            diagnosis = report["analysis"]["failure_analyses"][0]
            self.assertEqual(diagnosis["status"], "Failed")
            self.assertEqual(diagnosis["error_code"], "SYNTHETIC_TEST_ONLY_SourceTimeout")
            self.assertTrue(diagnosis["likely_cause"])
            self.assertIn(diagnosis["confidence"], {"low", "medium", "high"})
            self.assertTrue(diagnosis["recommended_action"])
            self.assertIsInstance(diagnosis["safe_to_retry"], bool)
            self.assertIsInstance(diagnosis["retry_preconditions"], list)


if __name__ == "__main__":
    unittest.main()
