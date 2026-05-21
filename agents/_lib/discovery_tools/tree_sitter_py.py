"""Deterministic Python source extraction using stdlib ast.

Naming stays stable — tree_sitter_py is the interface name; the implementation
swaps to the real tree-sitter when cross-language support is added.

Ported from agentrepo discovery/tools/tree_sitter_py.py with no logic changes.
"""
from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Import:
    module: str
    file: str
    line: int


@dataclass
class Boto3Call:
    service: str
    method: str
    resource_name: str | None
    file: str
    line: int


def parse_imports(path: str) -> list[Import]:
    """Return every import statement in the file. Returns [] on parse error."""
    src = Path(path).read_text(encoding="utf-8", errors="replace")
    try:
        tree = ast.parse(src, filename=path)
    except SyntaxError:
        return []
    out: list[Import] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                out.append(Import(module=alias.name, file=path, line=node.lineno))
        elif isinstance(node, ast.ImportFrom):
            level = "." * (node.level or 0)
            mod = (node.module or "")
            out.append(Import(module=f"{level}{mod}", file=path, line=node.lineno))
    return out


_BOTO3_FACTORIES = {"client", "resource"}
_NAME_KWARGS = {
    "TableName": "dynamodb",
    "Bucket": "s3",
    "QueueUrl": "sqs",
    "TopicArn": "sns",
    "StreamName": "kinesis",
    "SecretId": "secretsmanager",
    "FunctionName": "lambda",
}


def extract_boto3_calls(path: str) -> list[Boto3Call]:
    """Find every boto3/aioboto3 call site. Returns [] on parse error."""
    src = Path(path).read_text(encoding="utf-8", errors="replace")
    try:
        tree = ast.parse(src, filename=path)
    except SyntaxError:
        return []

    service_of: dict[str, str] = {}
    assigns: list[tuple[str, ast.AST | None]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and \
           isinstance(node.targets[0], ast.Name):
            assigns.append((node.targets[0].id, node.value))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            assigns.append((node.target.id, node.value))

    for name, value in assigns:
        svc = _service_from_factory(value)
        if svc is None:
            svc = _service_from_chained_call(value, service_of) if isinstance(value, ast.Call) else None
        if svc is None and isinstance(value, ast.Call) and isinstance(value.func, ast.Attribute) \
           and isinstance(value.func.value, ast.Name):
            svc = service_of.get(value.func.value.id)
        if svc:
            service_of[name] = svc

    calls: list[Boto3Call] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name):
            svc = service_of.get(node.func.value.id)
            if svc:
                calls.append(Boto3Call(
                    service=svc,
                    method=node.func.attr,
                    resource_name=_extract_resource_name(node),
                    file=path,
                    line=node.lineno,
                ))
                continue
        svc = _service_from_chained_call(node, service_of)
        if svc:
            calls.append(Boto3Call(
                service=svc,
                method=node.func.attr if isinstance(node.func, ast.Attribute) else "<call>",
                resource_name=_extract_resource_name(node),
                file=path,
                line=node.lineno,
            ))
    return calls


def _service_from_factory(value: ast.AST | None) -> str | None:
    if not isinstance(value, ast.Call):
        return None
    func = value.func
    if isinstance(func, ast.Attribute) and func.attr in _BOTO3_FACTORIES \
       and isinstance(func.value, ast.Name) and func.value.id in {"boto3", "aioboto3"}:
        if value.args and isinstance(value.args[0], ast.Constant) \
           and isinstance(value.args[0].value, str):
            return value.args[0].value
    return None


def _service_from_chained_call(call: ast.Call, service_of: dict[str, str] | None = None) -> str | None:
    cur: ast.AST = call.func
    while isinstance(cur, ast.Attribute):
        cur = cur.value
        if isinstance(cur, ast.Call):
            svc = _service_from_factory(cur)
            if svc:
                return svc
        if service_of is not None and isinstance(cur, ast.Name):
            svc = service_of.get(cur.id)
            if svc:
                return svc
    return None


def _extract_resource_name(call: ast.Call) -> str | None:
    if call.args and isinstance(call.args[0], ast.Constant) \
       and isinstance(call.args[0].value, str):
        return call.args[0].value
    for kw in call.keywords:
        if kw.arg in _NAME_KWARGS and isinstance(kw.value, ast.Constant) \
           and isinstance(kw.value.value, str):
            return kw.value.value
    return None
