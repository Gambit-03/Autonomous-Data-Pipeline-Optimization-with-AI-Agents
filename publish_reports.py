"""Publish explicitly selected local JSON/Markdown reports to the existing container."""

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

STORAGE_URL = "https://stautopipe097654.blob.core.windows.net"
CONTAINER_NAME = "reports"


def validate_report(path, content):
    """Require structured report JSON to identify its provenance before publishing."""
    if path.suffix.lower() != ".json":
        return
    report = json.loads(content.decode("utf-8"))
    if not isinstance(report, dict):
        raise ValueError(f"{path.name} must contain a JSON object")
    if report.get("synthetic") is True or report.get("source") == "synthetic_fixture":
        raise ValueError(f"Refusing to publish synthetic test output: {path.name}")


def publish_files(paths, credential, timestamp=None):
    """Upload selected report files create-only and verify exact read-back bytes."""
    from azure.storage.blob import BlobServiceClient, ContentSettings

    stamp = timestamp or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    service = BlobServiceClient(account_url=STORAGE_URL, credential=credential)
    published = []
    for path in paths:
        path = Path(path)
        content = path.read_bytes()
        validate_report(path, content)
        blob_name = f"demo-reports/{stamp}/{path.name}"
        blob = service.get_blob_client(container=CONTAINER_NAME, blob=blob_name)
        blob.upload_blob(
            content,
            overwrite=False,
            content_settings=ContentSettings(content_type="application/json" if path.suffix.lower() == ".json" else "text/markdown"),
        )
        if blob.download_blob().readall() != content:
            raise ValueError(f"Uploaded report read-back did not match: {blob_name}")
        published.append(f"{STORAGE_URL}/{CONTAINER_NAME}/{blob_name}")
    return published


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("files", nargs="+", type=Path, help="Explicit local JSON or Markdown report files")
    args = parser.parse_args(argv)
    missing = [str(path) for path in args.files if not path.is_file()]
    if missing:
        parser.error("report file(s) not found: " + ", ".join(missing))
    from azure.identity import DefaultAzureCredential
    with DefaultAzureCredential() as credential:
        for uri in publish_files(args.files, credential):
            print(uri)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
