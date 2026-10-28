"""Shared moto environment for the preload tests: the projects table, the Ingest + Lab artifacts buckets,
the Lab asset-index table, and a fake Cognito admin client.

Import as ``from preload_fixtures import ...`` (the tests directory is on ``sys.path``, as for
``improve_fixtures``).
"""
from __future__ import annotations

import io
import json
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterator, Mapping, Optional

import boto3
from moto import mock_aws

from ingestion.pipeline_store import PipelineStore
from lab.asset_index import AssetIndex

REGION = "us-west-2"
TABLE = "projects-test"
INGEST = "ingest"
LAB_TABLE = "lab-asset-index"
ARTIFACTS = "lab-artifacts"
POOL = "us-west-2_pool"
EMAIL = "owner@example.com"
OWNER_SUB = "owner-sub"


def skill_md(name: str, body: str = "Body.", description: str = "A helper skill.") -> str:
    return f"---\nname: {name}\ndescription: {description}\nlicense: Apache-2.0\n---\n# {name}\n{body}\n"


class FakeIdp:
    """``ListUsers`` finds nobody on the first call; ``AdminCreateUser`` returns a fixed ``sub``."""

    def __init__(self) -> None:
        self.created: list[str] = []

    def list_users(self, UserPoolId, Filter, Limit):  # noqa: N803 — boto3 kwarg names
        if self.created:
            return {"Users": [{"Username": self.created[0], "Attributes": [{"Name": "sub", "Value": OWNER_SUB}]}]}
        return {"Users": []}

    def admin_create_user(self, UserPoolId, Username, UserAttributes, DesiredDeliveryMediums):  # noqa: N803
        self.created.append(Username)
        return {"User": {"Username": Username, "Attributes": [{"Name": "sub", "Value": OWNER_SUB}]}}

    def admin_add_user_to_group(self, UserPoolId, Username, GroupName):  # noqa: N803
        pass


@dataclass
class Env:
    store: PipelineStore
    s3: Any
    index: AssetIndex

    def put(self, key: str, body: Any) -> None:
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.s3.put_object(Bucket=INGEST, Key=key, Body=body)

    def put_tree(self, root: str, files: Mapping[str, Any]) -> None:
        for rel, body in files.items():
            self.put(f"{root.rstrip('/')}/{rel}", body)

    def delete(self, key: str) -> None:
        self.s3.delete_object(Bucket=INGEST, Key=key)

    def keys(self, prefix: str, bucket: str = INGEST) -> list[str]:
        resp = self.s3.list_objects_v2(Bucket=bucket, Prefix=prefix)
        return sorted(o["Key"] for o in resp.get("Contents", []) or [])

    def lab_rows(self) -> list[dict]:
        return self.index._table.scan()["Items"]

    def published(self) -> list[dict]:
        return [r for r in self.lab_rows() if r.get("status") == "published"]


def zip_bytes(files: Mapping[str, Any]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for rel, body in files.items():
            if isinstance(body, (dict, list)):
                body = json.dumps(body)
            zf.writestr(rel, body)
    return buf.getvalue()


@contextmanager
def moto_env(monkeypatch) -> Iterator[Env]:
    for k, v in (("AWS_ACCESS_KEY_ID", "t"), ("AWS_SECRET_ACCESS_KEY", "t"), ("AWS_DEFAULT_REGION", REGION)):
        monkeypatch.setenv(k, v)
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name=REGION)
        t = ddb.create_table(
            TableName=TABLE, BillingMode="PAY_PER_REQUEST",
            KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": n, "AttributeType": "S"}
                                  for n in ("PK", "SK", "assetListPk", "assetListSk")],
            GlobalSecondaryIndexes=[{"IndexName": "assetsByRegistered", "Projection": {"ProjectionType": "ALL"},
                                     "KeySchema": [{"AttributeName": "assetListPk", "KeyType": "HASH"},
                                                   {"AttributeName": "assetListSk", "KeyType": "RANGE"}]}])
        s3 = boto3.client("s3", region_name=REGION)
        s3.create_bucket(Bucket=INGEST, CreateBucketConfiguration={"LocationConstraint": REGION})
        s3.create_bucket(Bucket=ARTIFACTS, CreateBucketConfiguration={"LocationConstraint": REGION})
        # The Lab asset-index single table (PK/SK + byPublishedId GSI) the skill publish writes to.
        lab = ddb.create_table(
            TableName=LAB_TABLE, BillingMode="PAY_PER_REQUEST",
            KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": n, "AttributeType": "S"}
                                  for n in ("PK", "SK", "GSI1PK", "GSI1SK")],
            GlobalSecondaryIndexes=[{"IndexName": "byPublishedId", "Projection": {"ProjectionType": "ALL"},
                                     "KeySchema": [{"AttributeName": "GSI1PK", "KeyType": "HASH"},
                                                   {"AttributeName": "GSI1SK", "KeyType": "RANGE"}]}])
        yield Env(store=PipelineStore(t), s3=s3, index=AssetIndex(ddb_table=lab))


def run(env: Env, root: str, idp: Optional[FakeIdp] = None):
    from preload.runner import run_preload

    return run_preload(s3=env.s3, store=env.store, idp=idp or FakeIdp(), bucket=INGEST, package_root=root,
                       user_pool_id=POOL, owner_email=EMAIL, skill_index=env.index, artifacts_bucket=ARTIFACTS)
