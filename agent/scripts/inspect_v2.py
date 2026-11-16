"""Quick inspection of migrated v2 rows."""
import boto3

ddb = boto3.resource("dynamodb", region_name="us-west-2")
table = ddb.Table("mediaworks-harness-lab-asset-index")
resp = table.query(
    KeyConditionExpression="PK = :pk",
    ExpressionAttributeValues={":pk": "USER#local-dev-user#OWNED"},
)
print(f"Owned rows for local-dev-user: {len(resp['Items'])}")
for item in resp["Items"][:5]:
    pk = item["PK"]
    sk = item["SK"]
    print(f"  {pk} / {sk}")
    print(f"    s3Prefix: {item.get('s3Prefix')}")
    print(f"    status: {item.get('status')}")
    print(f"    version: {item.get('version', '(working copy)')}")
    print(f"    deprecated: {item.get('deprecated')}")
