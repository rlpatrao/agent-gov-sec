"""
cloud_adapters.azure.data_fgac — Azure cloud-native enforcement for Gap 1 (data-layer FGAC).

The agnostic mediator (``governance.shared.enforcement.data_fgac.DataAccessMediator``)
decides allow/mask/deny; its default ``InProcessEnforcer`` masks/filters rows
*after* they're fetched. That's correct but reads the sensitive bytes first.
This adapter pushes the decision **down to the store** so masked/denied data
never leaves it — the Azure counterpart of ``cloud_adapters/aws/data_fgac.py``:

  - ``scoped_query`` rewrites the read as Azure SQL / Synapse Serverless T-SQL
    that projects only allowed columns, replaces masked columns with a redaction
    literal, and adds the row-filter as a ``WHERE`` clause — the agent's query
    can't even express out-of-scope reads.
  - ``register_row_level_security`` emits (and optionally applies) the same scope
    as an **Azure SQL Row-Level Security** predicate + a column include-list
    ``GRANT``, so the database enforces it for *any* client that connects as the
    agent's principal, not just our generated SQL.
  - Microsoft Purview can auto-populate the classification catalog (sensitivity
    labels) that the mediator consumes — see ``catalog_hint_from_purview``.

``AzureSqlFgacEnforcer`` also implements the agnostic ``DataAccessEnforcer``
``apply()`` as post-fetch defense-in-depth (delegates to the in-process
enforcer), so wiring it never weakens the masking even if a caller fetches rows
directly. ``pyodbc`` is lazy — query rewriting needs no driver; only the live
Row-Level-Security registration does.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Optional

from governance.shared.enforcement.data_fgac import (
    DataAccessDecision,
    InProcessEnforcer,
    _MASK,
)

logger = logging.getLogger(__name__)

# Column / table / schema names must be simple identifiers. Anything else is
# rejected before it can reach the generated SQL — these names can originate from
# agent-supplied request fields (e.g. the `columns` list), so they are untrusted.
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _sql_str(value) -> str:
    """Single-quote a literal for T-SQL, escaping embedded quotes."""
    return "'" + str(value).replace("'", "''") + "'"


def _check_ident(name) -> str:
    """Validate a SQL identifier; raise on anything that isn't a simple name.

    Returns the raw (unquoted) name — for APIs/DDL that take bare identifiers."""
    s = str(name)
    if not _IDENT_RE.match(s):
        raise ValueError(f"invalid SQL identifier (rejected as injection risk): {name!r}")
    return s


def _sql_ident(name) -> str:
    """Validate and bracket-quote a SQL identifier for T-SQL (Azure SQL / Synapse)."""
    return "[" + _check_ident(name).replace("]", "]]") + "]"


class AzureSqlFgacEnforcer:
    """Store-side FGAC: scoped Azure SQL / Synapse T-SQL + Row-Level Security policies."""

    def __init__(self, schema: Optional[str] = None) -> None:
        self._schema = schema or os.environ.get("AZURE_SQL_SCHEMA", "dbo")
        self._fallback = InProcessEnforcer()

    # ── DataAccessEnforcer protocol (post-fetch defense-in-depth) ─────────
    def apply(self, decision: DataAccessDecision, rows: list[dict]) -> list[dict]:
        # If rows were fetched directly (not via scoped_query), still enforce.
        return self._fallback.apply(decision, rows)

    # ── pushdown: rewrite the read as a scoped query ──────────────────────
    def scoped_query(self, decision: DataAccessDecision, *, database: str, table: str) -> str:
        """Build a T-SQL SELECT that enforces the decision at query time.

        - allowed columns are projected as-is
        - masked columns are replaced with the redaction literal (bytes never read)
        - dropped columns are omitted
        - the row filter becomes a WHERE clause
        Raises PermissionError when the whole request is out of scope.

        ``database`` is the schema-qualifier here (Azure SQL is single-catalog per
        connection); it maps to the T-SQL schema, defaulting to ``dbo``.
        """
        if decision.denied:
            raise PermissionError(f"data access denied: {decision.reason}")

        select_parts: list[str] = [_sql_ident(col) for col in decision.allowed_columns]
        select_parts += [f"{_sql_str(_MASK)} AS {_sql_ident(col)}" for col in decision.masked_columns]
        if not select_parts:
            select_parts = [f"{_sql_str(_MASK)} AS {_sql_ident('redacted')}"]

        schema = _sql_ident(database or self._schema)
        sql = f"SELECT {', '.join(select_parts)} FROM {schema}.{_sql_ident(table)}"

        where = []
        for col, allowed_values in (decision.row_filter or {}).items():
            if allowed_values:
                vals = ", ".join(_sql_str(v) for v in allowed_values)
                where.append(f"{_sql_ident(col)} IN ({vals})")
        if where:
            sql += " WHERE " + " AND ".join(where)
        return sql

    # ── catalog-level enforcement (any client on this principal) ──────────
    def register_row_level_security(
        self,
        decision: DataAccessDecision,
        *,
        database: str,
        table: str,
        principal: Optional[str] = None,
        apply: bool = False,
    ) -> dict:
        """Emit (and optionally apply) the T-SQL that mirrors the decision as an
        Azure SQL **Row-Level Security** policy plus a column include-list ``GRANT``
        for this principal — so the database enforces column/row scope for *any*
        client that connects as the agent, not just our generated SQL.

        With ``apply=False`` (default) this returns the DDL for review/CI; with
        ``apply=True`` it executes it via ``pyodbc`` (lazy — raises RuntimeError if
        the driver is unavailable). Masked columns are excluded from the ``GRANT``
        so the catalog never returns them; ``scoped_query`` re-adds them as
        redaction literals.
        """
        schema = _check_ident(database or self._schema)
        tbl = _check_ident(table)
        db_principal = _check_ident(principal or f"galaxy_{decision.agent_type}")
        predicate = f"galaxy_rls_{decision.agent_type}_{tbl}"[:120]
        policy = f"galaxy_secpol_{decision.agent_type}_{tbl}"[:120]

        # Column include-list — masked columns are deliberately excluded.
        grant_cols = ", ".join(_sql_ident(c) for c in decision.allowed_columns) or _sql_ident("_none")

        # Row filter → a security predicate over the allowed values.
        conds = [
            f"{_sql_ident(col)} IN ({', '.join(_sql_str(v) for v in vals)})"
            for col, vals in (decision.row_filter or {}).items() if vals
        ]
        where = " AND ".join(conds) if conds else "1 = 1"

        ddl = "\n".join([
            f"GRANT SELECT ({grant_cols}) ON [{schema}].[{tbl}] TO [{db_principal}];",
            f"CREATE OR ALTER FUNCTION [{schema}].[{predicate}](@principal sysname)",
            "    RETURNS TABLE WITH SCHEMABINDING AS",
            f"    RETURN SELECT 1 AS granted WHERE @principal = {_sql_str(db_principal)} AND ({where});",
            f"CREATE SECURITY POLICY [{schema}].[{policy}]",
            f"    ADD FILTER PREDICATE [{schema}].[{predicate}](USER_NAME()) ON [{schema}].[{tbl}]",
            "    WITH (STATE = ON);",
        ])

        if apply:
            self._execute(ddl)
        logger.info(
            "azure_fgac.rls_registered",
            extra={"agent_type": decision.agent_type, "table": f"{schema}.{tbl}",
                   "columns": len(decision.allowed_columns), "row_filtered": bool(conds),
                   "applied": apply},
        )
        return {"principal": db_principal, "policy": policy, "predicate": predicate,
                "columns": list(decision.allowed_columns), "ddl": ddl, "applied": apply}

    def _execute(self, ddl: str) -> None:
        try:
            import pyodbc  # type: ignore
        except ImportError as e:  # pragma: no cover
            raise RuntimeError(
                "pyodbc not installed — `pip install pyodbc` (and the ODBC driver) "
                "to apply Row-Level Security registration"
            ) from e
        conn_str = os.environ.get("AZURE_SQL_CONNECTION_STRING")
        if not conn_str:
            raise RuntimeError("AZURE_SQL_CONNECTION_STRING must be set to apply RLS DDL")
        with pyodbc.connect(conn_str, autocommit=True) as conn:  # pragma: no cover - needs live DB
            cur = conn.cursor()
            for stmt in [s for s in ddl.split(";\n") if s.strip()]:
                cur.execute(stmt)


def catalog_hint_from_purview(*_args, **_kwargs):  # pragma: no cover - doc stub
    """Placeholder for Microsoft Purview-driven catalog population.

    In production, Microsoft Purview sensitive-data classifications (PII/financial
    labels discovered over Azure SQL / Synapse / ADLS) can be transformed into the
    mediator's classification catalog (column → sensitivity), so the
    data-classification YAML is generated rather than hand-maintained. This mirrors
    the Amazon Macie seam on the AWS side. Left as a documented integration point.
    """
    raise NotImplementedError("Purview catalog population is a documented integration seam (WS7 follow-up).")
