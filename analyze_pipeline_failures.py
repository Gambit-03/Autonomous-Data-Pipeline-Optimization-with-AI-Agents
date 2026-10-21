"""Read ADF failure telemetry and ask the dedicated Foundry agent to analyze it.

Normal runs write a local JSON report. Blob upload is opt-in because it can incur
storage transaction and capacity charges. Synthetic mode never queries Azure.
"""

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

RESOURCE_GROUP = "rg-autonomous-pipeline-demo"
WORKSPACE_NAME = "la-autonomous-pipeline-demo"
STORAGE_URL = "https://stautopipe097654.blob.core.windows.net"
CONTAINER_NAME = "reports"
FOUNDRY_ENDPOINT = "https://ameicanbro-4228-resource.services.ai.azure.com/api/projects/ameicanbro-4228"
AGENT_NAME = "PipelineFailureAnalysisAgent"
MODEL_DEPLOYMENT_NAME = "gpt-5-mini-1"
REPORT_FILE = Path(__file__).resolve().parent / "pipeline_failure_analysis.json"
SYNTHETIC_REPORT_FILE = Path(__file__).resolve().parent / "pipeline_failure_analysis_synthetic.json"
MAX_FAILURES = 50
MAX_TEXT = 2000
LAST_AGENT_USAGE = None
LAST_AGENT_RESPONSES = []

ANALYSIS_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "overall_summary": {"type": "string"},
        "failure_analyses": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "pipeline_name": {"type": ["string", "null"]},
                    "run_id": {"type": ["string", "null"]},
                    "status": {"type": ["string", "null"]},
                    "error_code": {"type": ["string", "null"]},
                    "failure_type": {"type": ["string", "null"]},
                    "observed_facts": {"type": "array", "items": {"type": "string"}},
                    "likely_cause": {"type": "string"},
                    "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
                    "recommended_action": {"type": "string"},
                    "safe_to_retry": {"type": "boolean"},
                    "retry_preconditions": {"type": "array", "items": {"type": "string"}},
                },
                "required": [
                    "pipeline_name", "run_id", "status", "error_code", "failure_type",
                    "observed_facts", "likely_cause", "confidence", "recommended_action",
                    "safe_to_retry", "retry_preconditions",
                ],
                "additionalProperties": False,
            },
        },
    },
    "required": ["overall_summary", "failure_analyses"],
    "additionalProperties": False,
}

PIPELINE_QUERY = """
ADFPipelineRun
| where TimeGenerated > ago(7d) and Status == 'Failed'
| summarize arg_max(TimeGenerated, *) by RunId
| project TimeGenerated, PipelineName, RunId, Status, ErrorCode, FailureType, ErrorMessage
| order by TimeGenerated desc
| take 50
"""

ACTIVITY_QUERY = """
ADFActivityRun
| where TimeGenerated > ago(7d) and Status == 'Failed'
| project TimeGenerated, PipelineName, PipelineRunId, ActivityName, ActivityRunId,
          Status, ErrorCode, FailureType, ErrorMessage
| order by TimeGenerated desc
| take 200
"""

# Redact common credential forms before any diagnostic text reaches the model.
SECRET_PATTERNS = (
    re.compile(r"(?i)(accountkey|access[_-]?token|clientsecret|password|sig)=([^&\s;]+)"),
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/-]+=*"),
    re.compile(r"(?i)(https?://[^\s:]+):([^@\s]+)@"),
)


class PipelineAnalysisError(RuntimeError):
    pass


class AgentResponseValidationError(PipelineAnalysisError):
    """The model responded, but its content failed the required contract."""


def run_az(args):
    """Run Azure CLI and raise a readable error without dumping credentials."""
    result = subprocess.run(
        ["cmd.exe", "/d", "/c", "az", *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        detail = (result.stderr or result.stdout or "Azure CLI returned an error").strip()
        raise PipelineAnalysisError(f"Azure CLI failed ({result.returncode}): {redact(detail)[:MAX_TEXT]}")
    return result.stdout.strip()


def redact(value):
    text = "" if value is None else str(value)
    for pattern in SECRET_PATTERNS:
        if pattern.groups == 2:
            text = pattern.sub(r"\1=[REDACTED]", text)
        else:
            text = pattern.sub(r"\1[REDACTED]", text)
    return text[:MAX_TEXT]


def parse_query_result(raw):
    """Accept Azure CLI's table envelope or its direct row-list output."""
    try:
        result = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise PipelineAnalysisError(f"Log Analytics returned malformed JSON: {exc}") from exc
    if isinstance(result, list):
        return [row for row in result if isinstance(row, dict)]
    if not isinstance(result, dict):
        raise PipelineAnalysisError("Log Analytics response was neither an object nor a row list")
    tables = result.get("tables") or []
    if not tables:
        return []
    table = tables[0]
    columns = [col.get("name") for col in table.get("columns", [])]
    return [dict(zip(columns, row)) for row in table.get("rows", [])]


def query_log_analytics(query):
    workspace_id = run_az([
        "monitor", "log-analytics", "workspace", "show",
        "--resource-group", RESOURCE_GROUP,
        "--workspace-name", WORKSPACE_NAME,
        "--query", "customerId", "-o", "tsv",
    ])
    if not workspace_id:
        raise PipelineAnalysisError("Azure CLI returned an empty Log Analytics workspace ID")
    raw = run_az([
        "monitor", "log-analytics", "query", "--workspace", workspace_id,
        "--analytics-query", query, "--timespan", "P7D", "-o", "json",
    ])
    return parse_query_result(raw)


def make_failure_records(pipeline_rows, activity_rows):
    activities = {}
    for activity in activity_rows:
        key = activity.get("PipelineRunId")
        if key:
            activities.setdefault(str(key), []).append({
                "activity_name": redact(activity.get("ActivityName")),
                "activity_run_id": redact(activity.get("ActivityRunId")),
                "error_code": redact(activity.get("ErrorCode")) if activity.get("ErrorCode") is not None else None,
                "failure_type": redact(activity.get("FailureType")) if activity.get("FailureType") is not None else None,
                "error_message": redact(activity.get("ErrorMessage")) if activity.get("ErrorMessage") is not None else None,
                "time_generated": str(activity.get("TimeGenerated", "")),
            })
    output = []
    for row in pipeline_rows:
        run_id = row.get("RunId") or row.get("PipelineRunId")
        output.append({
            "observed_facts": {
            "pipeline_name": redact(row.get("PipelineName")) if row.get("PipelineName") is not None else None,
            "run_id": redact(run_id) if run_id is not None else None,
            "status": redact(row.get("Status")) if row.get("Status") is not None else None,
            "error_code": redact(row.get("ErrorCode")) if row.get("ErrorCode") is not None else None,
            "failure_type": redact(row.get("FailureType")) if row.get("FailureType") is not None else None,
            "error_message": redact(row.get("ErrorMessage")) if row.get("ErrorMessage") is not None else None,
                "time_generated": str(row.get("TimeGenerated", "")),
                "failed_activities": activities.get(str(run_id), []),
            }
        })
    return output[:MAX_FAILURES]


def call_agent(failures, correction=None):
    """Invoke the dedicated prompt agent with only supported conversation/input fields."""
    global LAST_AGENT_USAGE
    try:
        from azure.identity import DefaultAzureCredential
        from azure.ai.projects import AIProjectClient
        with DefaultAzureCredential() as credential:
            with AIProjectClient(endpoint=FOUNDRY_ENDPOINT, credential=credential, allow_preview=True) as project:
                client = project.get_openai_client(agent_name=AGENT_NAME)
                conversation = client.conversations.create()
                prompt = {
                    "task": "Analyze these Azure Data Factory failure records using your configured contract.",
                    "records": failures,
                }
                if correction:
                    prompt["correction"] = (
                        "Your previous response failed Python validation: " + correction +
                        ". Return a corrected response that follows the agent's configured JSON schema exactly. "
                        "Do not add prose or fields."
                    )
                response = client.responses.create(
                    conversation=conversation.id,
                    input=[{"role": "user", "content": json.dumps(prompt, ensure_ascii=False)}],
                )
                usage = getattr(response, "usage", None)
                observed_usage = {}
                if usage is not None:
                    usage_fields = ("input_tokens", "output_tokens", "total_tokens")
                    observed_usage = {
                        field: getattr(usage, field)
                        for field in usage_fields
                        if isinstance(getattr(usage, field, None), (int, float))
                    }
                text = getattr(response, "output_text", None)
                LAST_AGENT_RESPONSES.append({
                    "response_id": getattr(response, "id", None),
                    "response_text": text,
                    "usage": observed_usage or None,
                })
                if observed_usage:
                    LAST_AGENT_USAGE = LAST_AGENT_USAGE or {}
                    for key, value in observed_usage.items():
                        LAST_AGENT_USAGE[key] = LAST_AGENT_USAGE.get(key, 0) + value
        return parse_agent_response(text, expected_records=failures)
    except AgentResponseValidationError:
        raise
    except (ImportError, ValueError, json.JSONDecodeError) as exc:
        raise PipelineAnalysisError(f"Agent response/configuration error: {redact(exc)}") from exc
    except Exception as exc:
        # Avoid returning SDK exception objects which may include request headers.
        name = type(exc).__name__
        if "credential" in name.lower() or "auth" in name.lower():
            raise PipelineAnalysisError(f"Foundry authentication failed ({name})") from exc
        raise PipelineAnalysisError(f"Foundry analysis failed ({name}): {redact(exc)}") from exc


def parse_agent_response(text, expected_records=None):
    try:
        decoded = json.loads(text)
    except (TypeError, json.JSONDecodeError) as exc:
        raise AgentResponseValidationError("Agent returned malformed JSON") from exc
    if (not isinstance(decoded, dict) or not isinstance(decoded.get("overall_summary"), str)
            or not decoded["overall_summary"].strip() or not isinstance(decoded.get("failure_analyses"), list)):
        raise AgentResponseValidationError("Agent response must contain overall_summary and a failure_analyses array")
    if expected_records is not None and len(decoded["failure_analyses"]) != len(expected_records):
        raise AgentResponseValidationError("Agent failure_analyses count must match the supplied failure records")
    for index, item in enumerate(decoded["failure_analyses"]):
        required = {
            "pipeline_name", "run_id", "status", "error_code", "failure_type", "observed_facts",
            "likely_cause", "confidence", "recommended_action", "safe_to_retry", "retry_preconditions",
        }
        if not isinstance(item, dict) or set(item) != required:
            raise AgentResponseValidationError(f"Agent failure_analyses[{index}] does not match the required fields")
        for field in ("pipeline_name", "run_id", "status", "error_code", "failure_type"):
            if item[field] is not None and not isinstance(item[field], str):
                raise AgentResponseValidationError(f"Agent failure_analyses[{index}].{field} must be a string or null")
        if not isinstance(item["observed_facts"], list) or any(not isinstance(value, str) for value in item["observed_facts"]):
            raise AgentResponseValidationError(f"Agent failure_analyses[{index}].observed_facts must be a string array")
        if not isinstance(item["likely_cause"], str) or not item["likely_cause"].strip():
            raise AgentResponseValidationError(f"Agent failure_analyses[{index}].likely_cause must be non-empty text")
        if item["confidence"] not in {"low", "medium", "high"}:
            raise AgentResponseValidationError(f"Agent failure_analyses[{index}].confidence must be low, medium, or high")
        if not isinstance(item["recommended_action"], str) or not item["recommended_action"].strip():
            raise AgentResponseValidationError(f"Agent failure_analyses[{index}].recommended_action must be non-empty text")
        if not isinstance(item["safe_to_retry"], bool):
            raise AgentResponseValidationError(f"Agent failure_analyses[{index}].safe_to_retry must be boolean")
        if not isinstance(item["retry_preconditions"], list) or any(not isinstance(value, str) for value in item["retry_preconditions"]):
            raise AgentResponseValidationError(f"Agent failure_analyses[{index}].retry_preconditions must be a string array")
        if expected_records is not None:
            expected_facts = expected_records[index].get("observed_facts", {})
            for field in ("pipeline_name", "run_id", "status", "error_code", "failure_type"):
                if item[field] != expected_facts.get(field):
                    raise AgentResponseValidationError(
                        f"Agent failure_analyses[{index}].{field} does not match the observed input"
                    )
            if item["safe_to_retry"] and expected_facts.get("safe_to_retry") is not True:
                raise AgentResponseValidationError(
                    f"Agent failure_analyses[{index}].safe_to_retry is true without explicit input evidence"
                )
    return decoded


def analyze_with_agent(failures):
    """Make one response call and at most one correction for a malformed response."""
    global LAST_AGENT_USAGE, LAST_AGENT_RESPONSES
    LAST_AGENT_USAGE = None
    LAST_AGENT_RESPONSES = []
    correction = None
    for attempt in range(2):
        try:
            return call_agent(failures, correction=correction)
        except AgentResponseValidationError as exc:
            if attempt == 1:
                raise
            correction = str(exc)
    raise PipelineAnalysisError("Agent analysis ended without a response")


def synthetic_analysis(agent=None):
    """A clearly simulated fixture. Pass a callable to exercise the agent boundary offline."""
    fixture = [{"observed_facts": {
        "pipeline_name": "SYNTHETIC_TEST_ONLY_Pipeline",
        "run_id": "SYNTHETIC_TEST_ONLY_RUN_0001",
        "status": "Failed",
        "error_code": "SYNTHETIC_TEST_ONLY_SourceTimeout",
        "failure_type": "SYNTHETIC_TEST_ONLY_Transient",
        "error_message": "SYNTHETIC TEST ONLY: simulated source timeout; no Azure run occurred.",
        "time_generated": "SYNTHETIC_TEST_ONLY",
        "failed_activities": [],
    }}]
    invoke = agent or analyze_with_agent
    analysis = invoke(fixture)
    return {"source": "synthetic_fixture", "simulated": True, "failures": fixture, "analysis": analysis}


def upload_report(report, blob_name):
    """Persist the report to the existing account/container; caller opts in explicitly."""
    try:
        from azure.identity import DefaultAzureCredential
        from azure.storage.blob import BlobServiceClient, ContentSettings
        with DefaultAzureCredential() as credential:
            service = BlobServiceClient(account_url=STORAGE_URL, credential=credential)
            blob = service.get_blob_client(container=CONTAINER_NAME, blob=blob_name)
            blob.upload_blob(
                json.dumps(report, ensure_ascii=False, indent=2, default=str).encode("utf-8"),
                overwrite=True,
                content_settings=ContentSettings(content_type="application/json"),
            )
    except Exception as exc:
        raise PipelineAnalysisError(f"Blob upload failed ({type(exc).__name__}): {redact(exc)}") from exc


def build_report(pipeline_rows, activity_rows, agent=None):
    failures = make_failure_records(pipeline_rows, activity_rows)
    report = {
        "checked_at_utc": datetime.now(timezone.utc).isoformat(),
        "lookback_days": 7,
        "source": "azure_log_analytics",
        "synthetic": False,
        "failed_run_count": len(failures),
        "failures": failures,
    }
    if failures:
        report["analysis"] = (agent or analyze_with_agent)(failures)
    else:
        report["analysis"] = {"overall_summary": "No failed pipeline runs were found in the last 7 days.", "failure_analyses": []}
    return report


def main(argv=None):
    global LAST_AGENT_USAGE, LAST_AGENT_RESPONSES
    LAST_AGENT_USAGE = None
    LAST_AGENT_RESPONSES = []
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--synthetic", action="store_true", help="Analyze a labelled fixture; no Azure telemetry query is performed")
    parser.add_argument("--mock-agent", action="store_true", help="With --synthetic, use a deterministic local response and make no Foundry request")
    parser.add_argument("--upload", action="store_true", help="Upload the resulting report to the existing reports container")
    parser.add_argument("--blob-name", default="pipeline_failure_analysis.json")
    parser.add_argument("--output", type=Path, help="Optional local report path; defaults to a separate file for synthetic runs")
    args = parser.parse_args(argv)
    output_file = args.output or (SYNTHETIC_REPORT_FILE if args.synthetic else REPORT_FILE)
    try:
        if args.synthetic:
            if args.mock_agent:
                report = synthetic_analysis(lambda rows: {
                    "overall_summary": "Synthetic source-timeout fixture; no Azure incident or pipeline execution occurred.",
                    "failure_analyses": [{
                        "pipeline_name": rows[0]["observed_facts"]["pipeline_name"],
                        "run_id": "SYNTHETIC_TEST_ONLY_RUN_0001",
                        "status": rows[0]["observed_facts"]["status"],
                        "error_code": rows[0]["observed_facts"]["error_code"],
                        "failure_type": rows[0]["observed_facts"]["failure_type"],
                        "observed_facts": ["The synthetic record simulates a source timeout."],
                        "likely_cause": "Simulated transient source timeout (fixture hypothesis only).",
                        "confidence": "high",
                        "recommended_action": "Inspect source availability in a real incident before retrying.",
                        "safe_to_retry": False,
                        "retry_preconditions": ["Human verifies the source is available and the operation is idempotent."],
                    }],
                })
            else:
                report = synthetic_analysis()
            if LAST_AGENT_USAGE:
                report["model_usage"] = LAST_AGENT_USAGE
            if LAST_AGENT_RESPONSES:
                report["agent_responses"] = LAST_AGENT_RESPONSES
        else:
            print("Querying pipeline and activity failures for the last 7 days...")
            report = build_report(query_log_analytics(PIPELINE_QUERY), query_log_analytics(ACTIVITY_QUERY))
        output_file.write_text(json.dumps(report, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
        if args.upload:
            upload_report(report, args.blob_name)
            report["blob_uri"] = f"{STORAGE_URL}/{CONTAINER_NAME}/{args.blob_name}"
            output_file.write_text(json.dumps(report, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
        print(f"Report saved locally: {output_file}")
        print(json.dumps(report, indent=2, ensure_ascii=False, default=str))
        return 0
    except PipelineAnalysisError as exc:
        error_report = {
            "checked_at_utc": datetime.now(timezone.utc).isoformat(),
            "source": "synthetic_fixture" if args.synthetic else "azure_log_analytics",
            "synthetic": bool(args.synthetic),
            "status": "error",
            "error": str(exc),
        }
        if args.synthetic and LAST_AGENT_USAGE:
            error_report["model_usage"] = LAST_AGENT_USAGE
        if args.synthetic and LAST_AGENT_RESPONSES:
            error_report["agent_responses"] = LAST_AGENT_RESPONSES
        output_file.write_text(json.dumps(error_report, indent=2, ensure_ascii=False), encoding="utf-8")
        print(json.dumps(error_report, indent=2), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
