# Autonomous-Data-Pipeline-Optimization-with-AI-Agents

Azure Data Factory demo project for order-quality processing, pipeline failure analysis, and evidence-based performance reporting.

## Project contents

- Python workflows for failure analysis, performance analysis, quality reporting, and optional report publishing.
- Agent instruction files under `agents/`.
- Local ADF, dataset, dashboard, and Workbook definitions for review and reproducibility.
- Offline tests and `EVIDENCE.md`, which distinguishes live Azure observations from synthetic tests and controlled validation.

The definitions in this repository are project artifacts. Review them against the target Azure environment before deployment. The controlled failure-probe definition is retained as a test artifact; its deployed ADF objects were removed after the approved validation run.

## Local validation

Run the offline test suite with:

```powershell
python -m unittest -v test_publish_reports.py test_analyze_pipeline_failures.py test_run_output_isolation.py test_order_quality_workflow.py test_pipeline_performance.py
```

Compile the Python workflows and tests with:

```powershell
python -m py_compile analyze_pipeline_failures.py analyze_pipeline_performance.py build_report.py publish_reports.py test_agent.py test_analyze_pipeline_failures.py test_run_output_isolation.py test_order_quality_workflow.py test_pipeline_performance.py test_publish_reports.py
```

See `EVIDENCE.md` for recorded results, Azure resource context, known limitations, and the distinction between synthetic and controlled-test findings.
