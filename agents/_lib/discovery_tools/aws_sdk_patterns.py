"""boto3 call pattern → typed AWS resource reference.

Catalog-driven; ambiguous calls (dynamic resource names, unknown methods)
return None and are flagged for LLM disambiguation in the DependencyGrapher.

Ported from agentrepo discovery/tools/aws_sdk_patterns.py with no logic changes.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from agents._lib.discovery_tools.tree_sitter_py import Boto3Call

Access = Literal["reads", "writes", "produces", "consumes", "invokes"]
ResourceKind = Literal[
    "dynamodb_table", "s3_bucket", "sqs_queue", "sns_topic",
    "kinesis_stream", "secrets_manager_secret", "lambda_function",
]


@dataclass(frozen=True)
class ResourceRef:
    kind: ResourceKind
    name: str | None
    access: Access


_RULES: dict[str, dict] = {
    "dynamodb": {
        "kind": "dynamodb_table",
        "writes": {"put_item", "update_item", "delete_item", "batch_write_item",
                   "transact_write_items"},
        "reads": {"get_item", "query", "scan", "batch_get_item",
                  "transact_get_items", "describe_table"},
        "default_access": "reads",
    },
    "s3": {
        "kind": "s3_bucket",
        "writes": {"put_object", "delete_object", "copy_object",
                   "complete_multipart_upload", "upload_file", "upload_fileobj"},
        "reads": {"get_object", "head_object", "list_objects", "list_objects_v2",
                  "list_buckets", "head_bucket", "download_file", "download_fileobj"},
        "default_access": "reads",
    },
    "sqs": {
        "kind": "sqs_queue",
        "produces": {"send_message", "send_message_batch"},
        "consumes": {"receive_message", "delete_message", "delete_message_batch",
                     "change_message_visibility"},
        "default_access": None,
    },
    "sns": {
        "kind": "sns_topic",
        "produces": {"publish", "publish_batch"},
        "default_access": None,
    },
    "kinesis": {
        "kind": "kinesis_stream",
        "produces": {"put_record", "put_records"},
        "consumes": {"get_records", "get_shard_iterator"},
        "default_access": None,
    },
    "secretsmanager": {
        "kind": "secrets_manager_secret",
        "reads": {"get_secret_value", "describe_secret"},
        "writes": {"put_secret_value", "update_secret"},
        "default_access": None,
    },
    "lambda": {
        "kind": "lambda_function",
        "invokes": {"invoke", "invoke_async"},
        "default_access": None,
    },
}


def resolve(c: Boto3Call) -> ResourceRef | None:
    rule = _RULES.get(c.service)
    if not rule:
        return None
    kind: ResourceKind = rule["kind"]
    for access in ("reads", "writes", "produces", "consumes", "invokes"):
        if c.method in rule.get(access, set()):
            return ResourceRef(kind=kind, name=c.resource_name, access=access)
    default = rule["default_access"]
    if default is None:
        return None
    return ResourceRef(kind=kind, name=c.resource_name, access=default)
