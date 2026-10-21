"""Build an evidence-based ADF performance report from final Log Analytics runs."""

import argparse
import base64
import json
import statistics
import subprocess
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

RESOURCE_GROUP = "rg-autonomous-pipeline-demo"
WORKSPACE_ID = "83a5ea77-eab7-46f4-8930-9bf6e6bc9632"
DEFAULT_OUTPUT = Path(__file__).resolve().parent / "pipeline_performance_analysis.json"
FINAL_STATUSES = {"Succeeded", "Failed", "Cancelled", "Canceled"}
MIN_OUTLIER_SAMPLE = 8  # Conservative report policy, not an Azure requirement.
SENTINEL_END = datetime(2000, 1, 1, tzinfo=timezone.utc)


def run_az(args):
    # Azure CLI is a Windows command script. Pass every argument as a quoted
    # PowerShell literal so KQL pipes and operators reach az unchanged.
    quote_ps = lambda value: "'" + str(value).replace("'", "''") + "'"
    script = "$ErrorActionPreference='Stop'; & az " + " ".join(quote_ps(x) for x in args) + "; exit $LASTEXITCODE"
    encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        detail = (result.stderr or result.stdout or "Azure CLI failed").strip()
        raise RuntimeError(f"Azure CLI failed ({result.returncode}): {detail[:1500]}")
    return result.stdout.strip()


def parse_rows(raw):
    result = json.loads(raw or "[]")
    if isinstance(result, list):
        return [r for r in result if isinstance(r, dict)]
    tables = result.get("tables", []) if isinstance(result, dict) else []
    if not tables:
        return []
    columns = [c["name"] for c in tables[0].get("columns", [])]
    return [dict(zip(columns, row)) for row in tables[0].get("rows", [])]


def query(query_text, days):
    return parse_rows(run_az([
        "monitor", "log-analytics", "query",
        "--workspace", WORKSPACE_ID,
        "--analytics-query", query_text.strip(),
        "--timespan", f"P{days}D",
        "-o", "json",
    ]))


def activity_query(days):
    return f"""
ADFActivityRun
| where TimeGenerated > ago({days}d)
"""


def pipeline_query(days):
    return f"""
ADFPipelineRun
| where TimeGenerated > ago({days}d)
"""


def parse_datetime(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except ValueError:
        return None


def normalize_activities(rows):
    # Log Analytics may return multiple status events for one activity run.
    # Select the latest event in Python so queued/in-progress events cannot
    # shadow a final event when the CLI flattens the query result.
    latest = {}
    for row in rows:
        run_id = row.get("ActivityRunId")
        if not run_id:
            continue
        prior = latest.get(run_id)
        generated = parse_datetime(row.get("TimeGenerated")) or SENTINEL_END
        prior_generated = parse_datetime(prior.get("TimeGenerated")) if prior else None
        if prior is None or generated > (prior_generated or SENTINEL_END):
            latest[run_id] = row
    normalized = []
    for row in latest.values():
        status = str(row.get("Status", ""))
        start, end = parse_datetime(row.get("Start")), parse_datetime(row.get("End"))
        if status not in FINAL_STATUSES or not start or not end or end <= SENTINEL_END or end < start:
            continue
        duration = int((end - start).total_seconds())
        try:
            output = json.loads(row.get("Output") or "{}")
        except (TypeError, ValueError):
            output = {}
        billing = output.get("billingReference", {}).get("billableDuration", [])
        billable = None
        billing_unit = None
        if billing:
            billable = float(billing[0].get("duration"))
            billing_unit = billing[0].get("unit")
        run_status = output.get("runStatus") or {}
        acquisition_ms = run_status.get("computeAcquisitionDuration")
        acquisition_seconds = float(acquisition_ms) / 1000 if acquisition_ms is not None else None
        metrics = run_status.get("metrics") or {}
        quality_counts = {}
        for metric_name, metric in metrics.items():
            rows = metric.get("rowsWritten")
            if rows is not None:
                quality_counts[metric_name] = int(rows)
        copy_throughput = output.get("throughput")
        used_dius = output.get("usedDataIntegrationUnits")
        normalized.append({
            "pipeline_name": row.get("PipelineName"),
            "pipeline_run_id": row.get("PipelineRunId"),
            "activity_name": row.get("ActivityName"),
            "activity_run_id": row.get("ActivityRunId"),
            "activity_type": row.get("ActivityType"),
            "status": status,
            "start_utc": start.isoformat(),
            "end_utc": end.isoformat(),
            "duration_seconds": duration,
            "billable_duration": billable,
            "billing_unit": billing_unit,
            "effective_integration_runtime": output.get("effectiveIntegrationRuntime") or None,
            "compute_acquisition_duration_seconds": acquisition_seconds,
            "duration_after_compute_acquisition_seconds": (
                max(0.0, duration - acquisition_seconds) if acquisition_seconds is not None else None
            ),
            "sink_rows_written": quality_counts,
            "copy_throughput": copy_throughput,
            "used_data_integration_units": used_dius,
        })
    return normalized


def normalize_pipelines(rows):
    latest = {}
    for row in rows:
        run_id = row.get("RunId")
        if not run_id:
            continue
        prior = latest.get(run_id)
        generated = parse_datetime(row.get("TimeGenerated")) or SENTINEL_END
        prior_generated = parse_datetime(prior.get("TimeGenerated")) if prior else None
        if prior is None or generated > (prior_generated or SENTINEL_END):
            latest[run_id] = row
    normalized = []
    for row in latest.values():
        status = str(row.get("Status", ""))
        start, end = parse_datetime(row.get("Start")), parse_datetime(row.get("End"))
        if status not in FINAL_STATUSES or not start or not end or end <= SENTINEL_END or end < start:
            continue
        normalized.append({
            "pipeline_name": row.get("PipelineName"),
            "run_id": row.get("RunId"),
            "status": status,
            "start_utc": start.isoformat(),
            "end_utc": end.isoformat(),
            "duration_seconds": int((end - start).total_seconds()),
        })
    return normalized


def activity_baselines(activities):
    groups = defaultdict(list)
    for activity in activities:
        if activity["status"] == "Succeeded":
            key = (activity["pipeline_name"], activity["activity_name"], activity["activity_type"])
            groups[key].append(activity)
    results, outliers = [], []
    for key, items in sorted(groups.items()):
        durations = [r["duration_seconds"] for r in items]
        median = statistics.median(durations)
        mean = statistics.mean(durations)
        mad = statistics.median(abs(x - median) for x in durations)
        flagged = []
        if len(durations) >= MIN_OUTLIER_SAMPLE and mad > 0:
            upper = median + (3 * 1.4826 * mad)
            flagged = [r for r in items if r["duration_seconds"] > upper]
            outliers.extend(flagged)
            outlier_state = "evaluated_median_absolute_deviation"
        else:
            upper = None
            outlier_state = "insufficient_sample" if len(durations) < MIN_OUTLIER_SAMPLE else "not_evaluated_zero_mad"
        billables = [r for r in items if r["billable_duration"] is not None and r["billing_unit"]]
        billable_totals = defaultdict(float)
        for row in billables:
            billable_totals[row["billing_unit"]] += row["billable_duration"]
        results.append({
            "pipeline_name": key[0],
            "activity_name": key[1],
            "activity_type": key[2],
            "successful_sample_size": len(durations),
            "duration_seconds": {
                "min": min(durations),
                "median": median,
                "mean": mean,
                "max": max(durations),
                "sample_stdev": statistics.stdev(durations) if len(durations) > 1 else None,
            },
            "outlier_assessment": {
                "status": outlier_state,
                "minimum_sample_required": MIN_OUTLIER_SAMPLE,
                "method": "median + 3 * 1.4826 * MAD" if upper is not None else None,
                "upper_threshold_seconds": upper,
                "flagged_runs": [r["pipeline_run_id"] for r in flagged],
            },
            "observed_billable_duration_totals_by_unit": dict(billable_totals),
        })
    return results, outliers


def build_recommendations(activities, baselines):
    recs = []
    dataflows = [r for r in activities if r["activity_type"] == "ExecuteDataFlow" and r["status"] == "Succeeded"]
    if dataflows:
        durations = [r["duration_seconds"] for r in dataflows]
        units = defaultdict(float)
        for row in dataflows:
            if row["billable_duration"] is not None and row["billing_unit"]:
                units[row["billing_unit"]] += row["billable_duration"]
        recs.append({
            "recommendation": "Compare measured compute acquisition with Data Flow Monitor source, transformation, and sink timings before changing compute or partition settings.",
            "evidence": {
                "successful_run_count": len(dataflows),
                "activity_duration_seconds": durations,
                "compute_acquisition_duration_seconds": [r["compute_acquisition_duration_seconds"] for r in dataflows if r["compute_acquisition_duration_seconds"] is not None],
                "compute_acquisition_share_percent_of_wall_time": [
                    round(100.0 * r["compute_acquisition_duration_seconds"] / r["duration_seconds"], 1)
                    for r in dataflows if r["compute_acquisition_duration_seconds"] is not None and r["duration_seconds"]
                ],
                "observed_billable_duration_totals_by_unit": dict(units),
                "effective_integration_runtimes": sorted({r["effective_integration_runtime"] for r in dataflows if r["effective_integration_runtime"]}),
            },
            "interpretation": "Log Analytics Output includes computeAcquisitionDuration for these runs. This measured acquisition is reported separately from wall time; its residual is not assumed to be pure processing time and the metric is not treated as a cold-start measurement. Inspect Data Flow Monitor stage timings before tuning.",
            "confidence": "high for observed acquisition measurement; insufficient evidence for a tuning change",
            "source": "https://learn.microsoft.com/en-us/azure/data-factory/concepts-data-flow-performance and https://learn.microsoft.com/en-us/azure/data-factory/concepts-data-flow-monitoring",
        })
    for baseline in baselines:
        if baseline["successful_sample_size"] < MIN_OUTLIER_SAMPLE:
            recs.append({
                "recommendation": f"Do not flag duration anomalies or scale {baseline['activity_name']} from this sample; collect more final-status runs first.",
                "evidence": {
                    "pipeline_name": baseline["pipeline_name"],
                    "activity_name": baseline["activity_name"],
                    "successful_sample_size": baseline["successful_sample_size"],
                    "minimum_sample_required_by_report_policy": MIN_OUTLIER_SAMPLE,
                    "duration_seconds": baseline["duration_seconds"],
                },
                "confidence": "high",
            })
    if not any(r["activity_type"] == "Copy" and r["status"] == "Succeeded" for r in activities):
        recs.append({"recommendation": "Collect successful Copy activity runs before setting a Copy duration baseline.", "evidence": {"successful_sample_size": 0}, "confidence": "high"})
    return recs


def build_report(days=7, activity_rows=None, pipeline_rows=None):
    if activity_rows is None:
        activity_rows = query(activity_query(days), days)
    if pipeline_rows is None:
        pipeline_rows = query(pipeline_query(days), days)
    activities = normalize_activities(activity_rows)
    pipelines = normalize_pipelines(pipeline_rows)
    if not activities and not pipelines:
        raise RuntimeError("Log Analytics returned no final activity or pipeline runs; refusing to write an empty report.")
    baselines, outliers = activity_baselines(activities)
    status_counts = defaultdict(lambda: defaultdict(int))
    for row in pipelines:
        status_counts[row["pipeline_name"]][row["status"]] += 1
    pipeline_counts = {name: dict(counts) for name, counts in sorted(status_counts.items())}
    billable_totals = defaultdict(float)
    for row in activities:
        if row["billable_duration"] is not None and row["billing_unit"]:
            billable_totals[row["billing_unit"]] += row["billable_duration"]
    return {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "source": "Azure Log Analytics ADFPipelineRun and ADFActivityRun",
        "lookback_days": days,
        "status_filter": sorted(FINAL_STATUSES),
        "final_status_and_valid_end_time_filter": "latest row per RunId/ActivityRunId; final status only; Start and End after 2000-01-01 to exclude 1601 sentinel values",
        "pipeline_run_counts": pipeline_counts,
        "activity_baselines": baselines,
        "outlier_runs": outliers,
        "observed_billable_duration_totals_by_unit": dict(billable_totals),
        "estimated_currency_cost": None,
        "estimated_currency_cost_reason": "Log Analytics exposes measured billableDuration units, but this report has no subscription-specific rate card or billing export from which to derive currency cost.",
        "recommendations": build_recommendations(activities, baselines),
        "limitations": [
            "Activity Start-to-End is wall-clock time. computeAcquisitionDuration is available in the current ExecuteDataFlow Output and is reported separately; the residual is not a direct measure of transformation time.",
            "Throughput and used DIU values are reported where Copy Output supplies them; Data Flow run outputs supply coreHour billing units, not comparable DIU or throughput metrics.",
            f"Outlier detection requires at least {MIN_OUTLIER_SAMPLE} successful runs per pipeline/activity group; this is a conservative report rule, not an Azure guarantee.",
            "One controlled PL_RunOrderQualityChecks run was executed on 2026-10-21 and its final status and activity billing telemetry are included; no further pipeline run was started for this report.",
        ],
        "activity_runs": activities,
        "pipeline_runs": pipelines,
    }


def markdown_report(report):
    """Render a readable companion without inferring currency cost or causes."""
    lines = [
        "# Azure Data Factory Performance Analysis",
        "",
        f"Generated (UTC): {report['generated_at_utc']}",
        f"Source: {report['source']}; lookback: {report['lookback_days']} days.",
        "",
        "## Pipeline health",
        "",
        "| Pipeline | Final statuses |",
        "|---|---|",
    ]
    for name, counts in report["pipeline_run_counts"].items():
        lines.append(f"| {name} | " + ", ".join(f"{status}: {count}" for status, count in counts.items()) + " |")
    lines += ["", "## Activity measurements", "", "| Pipeline / activity | n succeeded | wall seconds (min / median / mean / max) | compute acquisition seconds (median) | observed billable units |", "|---|---:|---:|---:|---|"]
    for baseline in report["activity_baselines"]:
        matching = [a for a in report["activity_runs"] if a["pipeline_name"] == baseline["pipeline_name"] and a["activity_name"] == baseline["activity_name"] and a["status"] == "Succeeded"]
        acquisitions = [a["compute_acquisition_duration_seconds"] for a in matching if a["compute_acquisition_duration_seconds"] is not None]
        d = baseline["duration_seconds"]
        wall = f"{d['min']} / {d['median']} / {d['mean']:.2f} / {d['max']}"
        acquisition = f"{statistics.median(acquisitions):.2f}" if acquisitions else "not available"
        units = ", ".join(f"{value:.6f} {unit}" for unit, value in baseline["observed_billable_duration_totals_by_unit"].items()) or "not reported"
        lines.append(f"| {baseline['pipeline_name']} / {baseline['activity_name']} | {baseline['successful_sample_size']} | {wall} | {acquisition} | {units} |")
    lines += ["", "Activity wall time includes service runtime. `computeAcquisitionDuration` is shown separately when present; it is not treated as a cold-start timer, and the residual is not labeled processing time because other runtime overhead may remain."]
    for baseline in report["activity_baselines"]:
        matching = [a for a in report["activity_runs"] if a["pipeline_name"] == baseline["pipeline_name"] and a["activity_name"] == baseline["activity_name"] and a["status"] == "Succeeded"]
        acquisitions = [a["compute_acquisition_duration_seconds"] for a in matching if a["compute_acquisition_duration_seconds"] is not None]
        shares = [100.0 * a["compute_acquisition_duration_seconds"] / a["duration_seconds"] for a in matching if a["compute_acquisition_duration_seconds"] is not None and a["duration_seconds"]]
        if acquisitions:
            lines.append(f"Observed compute acquisition for {baseline['activity_name']}: {min(acquisitions):.3f} to {max(acquisitions):.3f} seconds (median {statistics.median(acquisitions):.3f}); {min(shares):.1f} to {max(shares):.1f}% of activity wall time across n={len(acquisitions)}. This measures acquisition only; the remaining interval is not a direct processing-time measurement.")
    lines += ["", "## Data Flow output counts", "", "| Run ID | Sink rows written |", "|---|---|" ]
    for activity in report["activity_runs"]:
        if activity["sink_rows_written"]:
            lines.append(f"| {activity['pipeline_run_id']} | " + ", ".join(f"{name}: {count}" for name, count in activity["sink_rows_written"].items()) + " |")
    lines += ["", "## Evidence-based recommendations", ""]
    for item in report["recommendations"]:
        lines.append(f"- **{item['recommendation']}**")
        if item.get("confidence"):
            lines.append(f"  Confidence: {item['confidence']}.")
        if item.get("interpretation"):
            lines.append(f"  {item['interpretation']}")
    lines += ["", "## Limits", "", "- Outlier screening requires at least 8 successful samples per activity group; current groups below that threshold are descriptive only.", "- Currency cost is not estimated because no subscription rate card or billing export was used.", "- No performance improvement is claimed. See the JSON report for run IDs, statuses, and raw normalized measurements.", ""]
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    if args.days < 1 or args.days > 90:
        parser.error("--days must be between 1 and 90")
    report = build_report(args.days)
    args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    markdown_path = args.output.with_suffix(".md")
    markdown_path.write_text(markdown_report(report), encoding="utf-8")
    print(f"Readable report saved to {markdown_path}")
    print(f"Report saved to {args.output}")
    print(json.dumps({
        "pipeline_run_counts": report["pipeline_run_counts"],
        "activity_baselines": report["activity_baselines"],
        "observed_billable_duration_totals_by_unit": report["observed_billable_duration_totals_by_unit"],
        "recommendation_count": len(report["recommendations"]),
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
