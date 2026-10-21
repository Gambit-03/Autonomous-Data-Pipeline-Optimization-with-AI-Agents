# Azure Data Factory Performance Analysis

Generated (UTC): 2026-10-21T17:16:46.651980+00:00
Source: Azure Log Analytics ADFPipelineRun and ADFActivityRun; lookback: 7 days.

## Pipeline health

| Pipeline | Final statuses |
|---|---|
| PL_AutonomousPipeline_Demo | Succeeded: 1 |
| PL_RunOrderQualityChecks | Succeeded: 5 |

## Activity measurements

| Pipeline / activity | n succeeded | wall seconds (min / median / mean / max) | compute acquisition seconds (median) | observed billable units |
|---|---:|---:|---:|---|
| PL_AutonomousPipeline_Demo / Copy_xp6 | 1 | 23 / 23 / 23.00 / 23 | not available | 0.066667 DIUHours |
| PL_RunOrderQualityChecks / Data flow1 | 5 | 218 / 248 / 247.00 / 279 | 155.48 | 2.496572 coreHour |

Activity wall time includes service runtime. `computeAcquisitionDuration` is shown separately when present; it is not treated as a cold-start timer, and the residual is not labeled processing time because other runtime overhead may remain.
Observed compute acquisition for Data flow1: 138.859 to 184.220 seconds (median 155.479); 59.4 to 71.3% of activity wall time across n=5. This measures acquisition only; the remaining interval is not a direct processing-time measurement.

## Data Flow output counts

| Run ID | Sink rows written |
|---|---|
| 47a836d9-61f5-41ba-a6f3-55338348dae0 | sinkValidRecords: 97, sinkInvalidRecords: 3 |
| 640ddbe7-18ea-4229-95c3-ebcd53c6752b | sinkValidRecords: 97, sinkInvalidRecords: 3 |
| 4d63e171-c3a7-4f7b-b91b-bcd82ca0b4f8 | sinkValidRecords: 97, sinkInvalidRecords: 3 |
| 2c28a3b4-2393-42de-bfeb-e7a55bd10607 | sinkValidRecords: 95, sinkInvalidRecords: 5 |
| 0ab4f5e6-cd71-11f1-a72d-f9fc223d98b0 | sinkValidRecords: 95, sinkInvalidRecords: 5 |

## Evidence-based recommendations

- **Compare measured compute acquisition with Data Flow Monitor source, transformation, and sink timings before changing compute or partition settings.**
  Confidence: high for observed acquisition measurement; insufficient evidence for a tuning change.
  Log Analytics Output includes computeAcquisitionDuration. This measured acquisition is reported separately from wall time; the residual is not assumed to be pure processing time and the metric is not treated as a cold-start measurement. Inspect Data Flow Monitor stage timings before tuning.
- **Do not flag duration anomalies or scale Copy_xp6 from this sample; collect more final-status runs first.**
  Confidence: high.
- **Do not flag duration anomalies or scale Data flow1 from this sample; collect more final-status runs first.**
  Confidence: high.

## Limits

- Outlier screening requires at least 8 successful samples per activity group; current groups below that threshold are descriptive only.
- Currency cost is not estimated because no subscription rate card or billing export was used.
- No performance improvement is claimed. See the JSON report for run IDs, statuses, and raw normalized measurements.
