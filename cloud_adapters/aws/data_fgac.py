"""
cloud_adapters.aws.data_fgac — AWS cloud-native enforcement for Gap 1 (data-layer FGAC).

The agnostic mediator (``governance.extensions.data_fgac.DataAccessMediator``)
decides allow/mask/deny; its default ``InProcessEnforcer`` masks/filters rows
*after* they're fetched. That's correct but reads the sensitive bytes first.
This adapter pushes the decision **down to the store** so masked/denied data
never leaves it:

  - ``scoped_query`` rewrites the read as Athena/Trino SQL that projects only
    allowed columns, replaces masked columns with a redaction literal, and adds
    the row-filter as a ``WHERE`` clause — the agent's query can't even express
    out-of-scope reads.
  - ``register_data_cells_filter`` registers the same column/row scope as a
    **Lake Formation data-cells filter** for the agent's IAM principal, so the
    catalog enforces it for *any* engine (Athena, Redshift Spectrum, EMR), not
    just our generated SQL.
  - Macie can auto-populate the classification catalog (sensitivity labels) that
    the mediator consumes — see ``catalog_hint_from_macie`` (doc-level helper).

``AwsLakeFormationEnforcer`` also implements the agnostic ``DataAccessEnforcer``
``apply()`` as post-fetch defense-in-depth (delegates to the in-process
enforcer), so wiring it never weakens the masking even if a caller fetches rows
directly. boto3 is lazy — query rewriting needs no AWS SDK; only the live
Lake-Formation registration does.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

from governance.extensions.data_fgac import (
    DataAccessDecision,
    InProcessEnforcer,
    _MASK,
)

logger = logging.getLogger(__name__)


def _sql_str(value) -> str:
    """Single-quote a literal for Athena/Trino, escaping embedded quotes."""
    return "'" + str(value).replace("'", "''") + "'"


class AwsLakeFormationEnforcer:
    """Store-side FGAC: scoped Athena SQL + Lake Formation data-cells filters."""

    def __init__(self, region: Optional[str] = None) -> None:
        self._region = region or os.environ.get("AWS_REGION", "us-east-1")
        self._fallback = InProcessEnforcer()

    # ── DataAccessEnforcer protocol (post-fetch defense-in-depth) ─────────
    def apply(self, decision: DataAccessDecision, rows: list[dict]) -> list[dict]:
        # If rows were fetched directly (not via scoped_query), still enforce.
        return self._fallback.apply(decision, rows)

    # ── pushdown: rewrite the read as a scoped query ──────────────────────
    def scoped_query(self, decision: DataAccessDecision, *, database: str, table: str) -> str:
        """Build an Athena/Trino SELECT that enforces the decision at query time.

        - allowed columns are projected as-is
        - masked columns are replaced with the redaction literal (bytes never read)
        - dropped columns are omitted
        - the row filter becomes a WHERE clause
        Raises PermissionError when the whole request is out of scope.
        """
        if decision.denied:
            raise PermissionError(f"data access denied: {decision.reason}")

        select_parts: list[str] = list(decision.allowed_columns)
        select_parts += [f"{_sql_str(_MASK)} AS {col}" for col in decision.masked_columns]
        if not select_parts:
            select_parts = [f"{_sql_str(_MASK)} AS redacted"]

        sql = f"SELECT {', '.join(select_parts)} FROM {database}.{table}"

        where = []
        for col, allowed_values in (decision.row_filter or {}).items():
            if allowed_values:
                vals = ", ".join(_sql_str(v) for v in allowed_values)
                where.append(f"{col} IN ({vals})")
        if where:
            sql += " WHERE " + " AND ".join(where)
        return sql

    # ── catalog-level enforcement (any engine) ────────────────────────────
    def register_data_cells_filter(
        self,
        decision: DataAccessDecision,
        *,
        database: str,
        table: str,
        catalog_id: Optional[str] = None,
    ) -> dict:
        """Register a Lake Formation data-cells filter mirroring the decision
        (column include-list + row-filter expression) for this principal.

        Lazy boto3 — raises RuntimeError if the AWS SDK is unavailable.
        """
        try:
            import boto3
        except ImportError as e:  # pragma: no cover
            raise RuntimeError("boto3 not installed — `pip install '.[aws]'` for Lake Formation registration") from e

        lf = boto3.client("lakeformation", region_name=self._region)
        row_filter_expr = " AND ".join(
            f"{col} IN ({', '.join(_sql_str(v) for v in vals)})"
            for col, vals in (decision.row_filter or {}).items() if vals
        )
        # Masked columns are excluded from the column include-list so the catalog
        # never returns them; our scoped_query re-adds them as redaction literals.
        column_names = list(decision.allowed_columns)
        table_data = {
            "TableCatalogId": catalog_id or os.environ.get("AWS_ACCOUNT_ID", ""),
            "DatabaseName": database,
            "TableName": table,
            "Name": f"galaxy-{decision.agent_type}-{table}"[:255],
            "ColumnNames": column_names,
            "RowFilter": {"FilterExpression": row_filter_expr} if row_filter_expr else {"AllRowsWildcard": {}},
        }
        resp = lf.create_data_cells_filter(TableData=table_data)
        logger.info(
            "aws_fgac.data_cells_filter_registered",
            extra={"agent_type": decision.agent_type, "table": f"{database}.{table}",
                   "columns": len(column_names), "row_filtered": bool(row_filter_expr)},
        )
        return resp


def catalog_hint_from_macie(*_args, **_kwargs):  # pragma: no cover - doc stub
    """Placeholder for Macie-driven catalog population.

    In production, Amazon Macie sensitive-data discovery findings (PII/financial
    classifications on S3/Glue) can be transformed into the mediator's
    classification catalog (column → sensitivity), so the data-classification
    YAML is generated rather than hand-maintained. Left as a documented seam.
    """
    raise NotImplementedError("Macie catalog population is a documented integration seam (WS7 follow-up).")
