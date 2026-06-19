"""
governance.shared.state — externalized state for stateful controls.

Controls with memory across requests — data-access drift baselines, circuit-breaker
counters, cost accumulators, rate-limit windows — cannot be verified from a
stateless chokepoint (a Lambda/interceptor has no per-request memory). They need a
shared store. `StateBackend` is that seam: `InMemoryState` for in-process/offline
runs and tests; `DynamoDbState` for the deployed chokepoints, so every horizontally
scaled node sees the same counters.

The interface is intentionally small (get/put/incr namespaced by control + key);
each control maps its own shape onto it. The audit ledger already has a DynamoDB
backend; this mirrors that choice for control state.
"""

from __future__ import annotations

from typing import Any, Optional, Protocol


class StateBackend(Protocol):
    def get(self, namespace: str, key: str) -> Optional[Any]: ...
    def put(self, namespace: str, key: str, value: Any) -> None: ...
    def incr(self, namespace: str, key: str, amount: float = 1.0) -> float: ...


class InMemoryState:
    """Process-local state. Correct for a single in-process run or a test; for a
    scaled-out chokepoint use DynamoDbState so nodes share counters."""

    def __init__(self) -> None:
        self._d: dict[tuple[str, str], Any] = {}

    def get(self, namespace: str, key: str) -> Optional[Any]:
        return self._d.get((namespace, key))

    def put(self, namespace: str, key: str, value: Any) -> None:
        self._d[(namespace, key)] = value

    def incr(self, namespace: str, key: str, amount: float = 1.0) -> float:
        new = float(self._d.get((namespace, key)) or 0.0) + amount
        self._d[(namespace, key)] = new
        return new


class DynamoDbState:
    """DynamoDB-backed shared state for deployed chokepoints. Lazy boto3 import so
    the module stays importable offline. Table schema: partition key ``pk`` =
    ``"{namespace}#{key}"``, attribute ``v``; ``incr`` uses an atomic ADD."""

    def __init__(self, table_name: str, region: Optional[str] = None) -> None:
        self._table_name = table_name
        self._region = region
        self._table = None

    def _t(self):
        if self._table is None:
            import boto3
            self._table = boto3.resource("dynamodb", region_name=self._region).Table(self._table_name)
        return self._table

    @staticmethod
    def _pk(namespace: str, key: str) -> str:
        return f"{namespace}#{key}"

    def get(self, namespace: str, key: str) -> Optional[Any]:
        item = self._t().get_item(Key={"pk": self._pk(namespace, key)}).get("Item")
        return item.get("v") if item else None

    def put(self, namespace: str, key: str, value: Any) -> None:
        self._t().put_item(Item={"pk": self._pk(namespace, key), "v": value})

    def incr(self, namespace: str, key: str, amount: float = 1.0) -> float:
        from decimal import Decimal
        resp = self._t().update_item(
            Key={"pk": self._pk(namespace, key)},
            UpdateExpression="ADD v :a",
            ExpressionAttributeValues={":a": Decimal(str(amount))},
            ReturnValues="UPDATED_NEW",
        )
        return float(resp["Attributes"]["v"])
