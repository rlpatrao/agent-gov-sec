"""
payload_agents._lib.demo_data — in-memory sample rows for the LangGraph demo.

Column names match the classification catalog
(``governance/extensions/configs/data-classification.example.yaml``) so the FGAC
mediator's mask/row-filter decisions land on real columns. No secrets here — the
``customer_email`` / ``ssn`` / ``tax_id`` values are obviously synthetic.
"""

from __future__ import annotations

BILLING: list[dict] = [
    {"account_id": "acct-001", "cost_usd": 1200, "region": "us-east-1",
     "customer_email": "alice@example.com", "tax_id": "TAX-1001"},
    {"account_id": "acct-002", "cost_usd": 3400, "region": "us-west-2",
     "customer_email": "bob@example.com", "tax_id": "TAX-1002"},
    {"account_id": "acct-003", "cost_usd": 900, "region": "eu-west-1",
     "customer_email": "carol@example.com", "tax_id": "TAX-1003"},
    {"account_id": "acct-004", "cost_usd": 5600, "region": "ap-south-1",
     "customer_email": "dan@example.com", "tax_id": "TAX-1004"},
]

EMPLOYEES: list[dict] = [
    {"employee_id": "emp-01", "salary": 145000, "ssn": "000-00-0001"},
    {"employee_id": "emp-02", "salary": 98000, "ssn": "000-00-0002"},
]

DATASETS: dict[str, dict[str, list[dict]]] = {
    "finops": {"billing": BILLING},
    "hr": {"employees": EMPLOYEES},
}


def rows_for(dataset: str, table: str) -> list[dict]:
    """Return the sample rows for a dataset.table, or [] if unknown."""
    return DATASETS.get(dataset, {}).get(table, [])
