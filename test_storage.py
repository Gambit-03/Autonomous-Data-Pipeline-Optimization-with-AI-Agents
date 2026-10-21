
from azure.identity import DefaultAzureCredential
from azure.storage.blob import BlobServiceClient

account_url = "https://stautopipe097654.blob.core.windows.net"
container_name = "reports"
blob_name = "part-merged.csv"

with DefaultAzureCredential() as credential:
    service = BlobServiceClient(
        account_url=account_url,
        credential=credential,
    )

    blob = service.get_blob_client(
        container=container_name,
        blob=blob_name,
    )

    content = blob.download_blob().readall().decode("utf-8-sig")
    print(content)
