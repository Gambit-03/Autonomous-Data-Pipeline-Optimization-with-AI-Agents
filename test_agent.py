"""Generate a run-scoped AI quality report; upload is a separate explicit step."""

import argparse
import csv
import io
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

STORAGE_URL = "https://stautopipe097654.blob.core.windows.net"
CONTAINER_NAME = "reports"
FOUNDRY_ENDPOINT = (
    "https://ameicanbro-4228-resource.services.ai.azure.com/"
    "api/projects/ameicanbro-4228"
)
AGENT_NAME = "OrderQualityAgent"
EXPECTED_FIELDS = ("order_id", "customer_id", "product", "quantity", "unit_price", "duplicate_rank")


def normalize_run_id(value):
    """Accept only a GUID-shaped ADF pipeline RunId and return canonical form."""
    try:
        return str(UUID(str(value)))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError("RunId must be a valid Azure Data Factory pipeline GUID") from exc


def run_output_prefix(run_id):
    return f"runs/{normalize_run_id(run_id)}/"


def report_file(run_id):
    return Path(__file__).resolve().parent / f"ai_quality_report_{normalize_run_id(run_id)}.json"


def read_run_records(container_client, run_id):
    """Read and validate CSV shards for one run only; never use shared/latest files."""
    prefix = run_output_prefix(run_id)
    blob_names = sorted(
        blob.name for blob in container_client.list_blobs(name_starts_with=prefix)
        if blob.name.startswith(prefix) and blob.name.lower().endswith(".csv")
    )
    if not blob_names:
        raise FileNotFoundError(f"No CSV output found for RunId {normalize_run_id(run_id)} under {prefix}")

    records = []
    expected_fields = None
    for blob_name in blob_names:
        csv_text = container_client.get_blob_client(blob_name).download_blob().readall().decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(csv_text))
        fields = tuple(reader.fieldnames or ())
        if not fields:
            raise ValueError(f"Run output CSV has no header: {blob_name}")
        if fields != EXPECTED_FIELDS:
            raise ValueError(f"Unexpected CSV schema in {blob_name}; expected {', '.join(EXPECTED_FIELDS)}")
        if expected_fields is None:
            expected_fields = fields
        elif fields != expected_fields:
            raise ValueError(f"Run output CSV schema differs between shards: {blob_name}")
        for record in reader:
            if None in record or any(value is None for value in record.values()):
                raise ValueError(f"Malformed CSV row in {blob_name}")
            records.append(record)
    if not records:
        raise ValueError(f"Run output contains no records for RunId {normalize_run_id(run_id)}")
    return records


def decode_agent_report(text, records=None):
    """Decode the agent's JSON and enforce the report contract before saving."""
    if not isinstance(text, str):
        raise ValueError("Agent returned no text response")
    candidate = text.strip()
    if candidate.startswith("```json") and candidate.endswith("```"):
        candidate = candidate[7:-3].strip()
    elif candidate.startswith("```") and candidate.endswith("```"):
        candidate = candidate[3:-3].strip()
    try:
        report = json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise ValueError("Agent returned malformed JSON") from exc
    if not isinstance(report, dict):
        raise ValueError("Agent report must be a JSON object")
    for key in ("confirmed_errors", "potential_anomalies"):
        if not isinstance(report.get(key), list):
            raise ValueError(f"Agent report must contain a {key} array")
    if not isinstance(report.get("summary"), str) or not report["summary"].strip():
        raise ValueError("Agent report must contain a non-empty summary")
    for group in ("confirmed_errors", "potential_anomalies"):
        for index, issue in enumerate(report[group]):
            if not isinstance(issue, dict):
                raise ValueError(f"{group}[{index}] must be an object")
            for field in ("order_id", "issue_type", "severity", "explanation", "evidence", "recommended_action"):
                if not isinstance(issue.get(field), str) or not issue[field].strip():
                    raise ValueError(f"{group}[{index}] is missing {field}")
            if issue["severity"].lower() not in {"low", "medium", "high"}:
                raise ValueError(f"{group}[{index}].severity must be low, medium, or high")
            confidence = issue.get("confidence")
            if confidence is not None and (not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1):
                raise ValueError(f"{group}[{index}].confidence must be between 0 and 1 or null")
            if group == "confirmed_errors" and "duplicat" in issue["issue_type"].lower():
                raise ValueError("Duplicate findings cannot be confirmed from duplicate_rank alone")
    if records is not None:
        ranked_duplicates = {str(row.get("order_id")) for row in records if str(row.get("duplicate_rank", "")).strip().isdigit() and int(row["duplicate_rank"]) > 1}
        expected_ids = {issue["order_id"] for issue in report["potential_anomalies"] if "duplicat" in issue["issue_type"].lower()}
        if not ranked_duplicates.issubset(expected_ids):
            raise ValueError("Every duplicate_rank greater than 1 must remain a potential anomaly")
    return report


def build_prompt(records, run_id):
    return {
        "task": "Analyze the supplied invalid order output for quality issues.",
        "run_id": run_id,
        "records": records,
        "rules": [
            "Treat blank customer_id and quantity less than or equal to zero as confirmed errors.",
            "Treat duplicate_rank greater than 1 only as a potential duplicate; rank alone does not prove a duplicate.",
            "Do not infer a unit_price error without an authoritative pricing rule.",
            "Separate facts visible in the records from hypotheses.",
            "Do not claim that you changed or repaired source data; recommend human review.",
            "Use confidence from 0 to 1 or null when evidence is insufficient.",
        ],
        "response_schema": {
            "summary": "string",
            "confirmed_errors": [{"order_id": "string", "issue_type": "string", "severity": "low|medium|high", "explanation": "string", "evidence": "string", "confidence": 0.0, "recommended_action": "string"}],
            "potential_anomalies": [{"order_id": "string", "issue_type": "string", "severity": "low|medium|high", "explanation": "string", "evidence": "string", "confidence": 0.0, "recommended_action": "string"}],
        },
    }


def add_report_metadata(analysis, run_id, records):
    return {
        "run_id": normalize_run_id(run_id),
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "source": {
            "container": CONTAINER_NAME,
            "blob_prefix": run_output_prefix(run_id),
            "record_count": len(records),
            "schema": list(EXPECTED_FIELDS),
        },
        "agent": AGENT_NAME,
        **analysis,
        "data_mutated": False,
    }


def validate_saved_report(report, run_id):
    """Validate the local artifact again before upload-only mode can publish it."""
    run_id = normalize_run_id(run_id)
    if not isinstance(report, dict) or report.get("run_id") != run_id:
        raise ValueError("Local report RunId does not match --run-id")
    source = report.get("source")
    if (not isinstance(source, dict) or not isinstance(source.get("record_count"), int)
            or source["record_count"] < 1 or tuple(source.get("schema", ())) != EXPECTED_FIELDS):
        raise ValueError("Local report source metadata is missing or invalid")
    if report.get("agent") != AGENT_NAME or report.get("data_mutated") is not False:
        raise ValueError("Local report agent/mutation metadata is invalid")
    analysis = {key: report.get(key) for key in ("summary", "confirmed_errors", "potential_anomalies")}
    decode_agent_report(json.dumps(analysis, ensure_ascii=False))
    return report


def upload_existing_report(report, run_id, credential):
    """Create the run's report blob only if absent, then read and verify it."""
    from azure.storage.blob import BlobServiceClient, ContentSettings

    run_id = normalize_run_id(run_id)
    blob_name = f"runs/{run_id}/ai_quality_report.json"
    service = BlobServiceClient(account_url=STORAGE_URL, credential=credential)
    blob = service.get_blob_client(container=CONTAINER_NAME, blob=blob_name)
    try:
        if blob.exists():
            raise FileExistsError(f"Refusing to overwrite existing report blob: {blob_name}")
        blob.upload_blob(
            json.dumps(report, indent=2, ensure_ascii=False).encode("utf-8"),
            overwrite=False,
            content_settings=ContentSettings(content_type="application/json"),
        )
        downloaded = json.loads(blob.download_blob().readall().decode("utf-8"))
    except FileExistsError:
        raise
    except Exception as exc:
        if getattr(exc, "status_code", None) == 403:
            raise PermissionError("Storage upload denied (HTTP 403); grant blob data-plane write access to the signed-in identity.") from None
        raise
    if downloaded.get("run_id") != run_id or downloaded != report:
        raise ValueError("Uploaded report read-back did not match the local report")
    return f"{STORAGE_URL}/{CONTAINER_NAME}/{blob_name}"


def main(argv=None):
    parser = argparse.ArgumentParser(description="Analyze invalid order rows for one ADF pipeline run.")
    parser.add_argument("--run-id", required=True, type=normalize_run_id,
                        help="The Azure Data Factory pipeline RunId whose isolated output/report to use")
    parser.add_argument("--upload", action="store_true",
                        help="Upload the previously generated local report (does not call the AI agent)")
    args = parser.parse_args(argv)

    from azure.identity import DefaultAzureCredential
    with DefaultAzureCredential() as credential:
        if args.upload:
            path = report_file(args.run_id)
            if not path.exists():
                raise FileNotFoundError(f"Generate the local report first: {path}")
            try:
                report = validate_saved_report(json.loads(path.read_text(encoding="utf-8")), args.run_id)
            except (json.JSONDecodeError, ValueError) as exc:
                print(f"Local report validation failed: {exc}", file=sys.stderr)
                return 2
            try:
                uri = upload_existing_report(report, args.run_id, credential)
            except FileExistsError as exc:
                print(str(exc), file=sys.stderr)
                return 2
            except PermissionError as exc:
                print(str(exc), file=sys.stderr)
                return 2
            print(f"Uploaded and read-back verified: {uri}")
            return 0

        from azure.storage.blob import BlobServiceClient
        from azure.ai.projects import AIProjectClient
        container = BlobServiceClient(account_url=STORAGE_URL, credential=credential).get_container_client(CONTAINER_NAME)
        records = read_run_records(container, args.run_id)
        print(f"Validated {len(records)} invalid records for RunId {args.run_id}.")
        with AIProjectClient(endpoint=FOUNDRY_ENDPOINT, credential=credential, allow_preview=True) as project:
            client = project.get_openai_client(agent_name=AGENT_NAME)
            conversation = client.conversations.create()
            response = client.responses.create(
                conversation=conversation.id,
                input=[{"role": "user", "content": json.dumps(build_prompt(records, args.run_id), ensure_ascii=False)}],
            )
        analysis = decode_agent_report(response.output_text, records)
        report = add_report_metadata(analysis, args.run_id, records)
        path = report_file(args.run_id)
        if path.exists():
            raise FileExistsError(f"Refusing to overwrite existing local report: {path}")
        path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"AI report saved locally: {path}")
        print(json.dumps(report, indent=2, ensure_ascii=False))
        print("Run with --upload to publish this report without another AI call.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
