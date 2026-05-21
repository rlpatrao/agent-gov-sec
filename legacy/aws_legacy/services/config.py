"""Service URL registry and environment configuration.

Mirrors `src/config.ts`: resolves downstream microservice URLs dynamically
based on `PLATFORM`, and the three ELB DNS env vars.
"""
from __future__ import annotations

import os


def _host(kind: str = "internal") -> str:
    if os.environ.get("PLATFORM") != "AWS":
        return "localhost"
    return {
        "internal": os.environ.get("ELB_INTERNAL_DNS", ""),
        "internal_ar": os.environ.get("ELB_INTERNAL_AR_DNS", ""),
        "internet_facing": os.environ.get("ELB_INTERNET_FACING_DNS", ""),
    }[kind]


def _url(host_kind: str, port: int, path: str = "") -> str:
    host = _host(host_kind)
    return f"http://{host}:{port}{path}"


# Port convention from docs §11.
FILER_URLS = {
    # Annual Report scrapers
    ("ANNUAL_REPORT", "FLORIDA"): _url("internal_ar", 7000),
    ("ANNUAL_REPORT", "CALIFORNIA"): _url("internal_ar", 7001),
    # Independent-service filers
    ("EIN", None): _url("internal", 5000),
    ("WRITTEN_CONSENT", None): _url("internal", 5001),
    ("CID_PIN", None): _url("internal", 5002),
    # COGS
    ("COGS", "FLORIDA"): _url("internal", 7050),
    ("COGS", "CALIFORNIA"): _url("internal", 7051),
    ("COGS", "NEW_YORK"): _url("internal", 7052),
    # Initial report, kit docs
    ("INITIAL_REPORT", "CALIFORNIA"): _url("internal", 9121),
    ("KIT_DOCUMENTS", None): _url("internal", 4001),
}


def resolve_filer_url(service_type: str, jurisdiction: str | None) -> str | None:
    return FILER_URLS.get((service_type, jurisdiction)) or FILER_URLS.get(
        (service_type, None)
    )


ENV = os.environ.get("ENV", "local")
TABLE_NAME = os.environ.get("TABLE_NAME", "AutografRequests")
SETTING_TABLE_NAME = os.environ.get("SETTING_TABLE_NAME", "ApplicationData")
AUDIT_TABLE_NAME = os.environ.get("AUDIT_TABLE_NAME", "AuditLogs")
ORDER_SHEET_TABLE_NAME = os.environ.get("ORDER_SHEET_TABLE_NAME", "orderSheetUpload")
BUCKET_NAME = os.environ.get("BUCKET_NAME", "autograf")
SQS_QUEUE_URL = os.environ.get("SQS_QUEUE_URL", "")
STD_SQS_QUEUE_URL = os.environ.get("STD_SQS_QUEUE_URL", "")
EVENT_BUS_NAME = os.environ.get("EVENT_BUS_NAME", "autografTaskDispatcher")
AWS_REGION = os.environ.get("AWS_REGION", "us-west-1")

COGNITO_USER_POOL_ID = os.environ.get("COGNITO_USER_POOL_ID", "")
COGNITO_CLIENT_ID = os.environ.get("COGNITO_CLIENT_ID", "")
COGNITO_REGION = os.environ.get("COGNITO_REGION", AWS_REGION)
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "")
PDF_MERGER_API_URL = os.environ.get("PDF_MERGER_API_URL", "")

JWT_BYPASS_PATHS = {"/get/errorscreenshot/filing", "/get/botStatus"}
