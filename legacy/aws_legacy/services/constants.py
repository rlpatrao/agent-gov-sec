"""Enums and retry schedules — port of `src/constant.ts` and `libs/enums.ts`."""
from enum import Enum


class DDBStatus(str, Enum):
    CREATED = "CREATED"
    TRANSFORMED = "TRANSFORMED"
    VALIDATED = "VALIDATED"
    QUEUED = "QUEUED"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    EXCEPTION = "EXCEPTION"
    RETRY = "RETRY"


class BotStatus(str, Enum):
    IDLE = "IDLE"
    IN_PROGRESS = "IN_PROGRESS"


class EventType(str, Enum):
    UPSTREAM = "UPSTREAM"
    MERGE_EVIDENCE = "MERGE_EVIDENCE"
    ORDER_RESUBMIT = "ORDER_RESUBMIT"


# Filer bot HTTP responses that should trigger a retry rather than completion.
BOT_RETRY_STATUSES = {429, 500, 502, 503, 504}


# Evidence retry configuration — §12.
# Days for firstRetry/retry; maxRetry is a count.
EVIDENCE_CONFIG = {
    "prod": {
        "MICHIGAN_ANNUAL_REPORT": {"firstRetry": 1, "retry": 1, "maxRetry": 10},
        "NORTH_CAROLINA_ANNUAL_REPORT": {"firstRetry": 1, "retry": 1, "maxRetry": 10},
        "NEVADA_ANNUAL_REPORT": {"firstRetry": 1, "retry": 1, "maxRetry": 10},
        "RHODE_ISLAND_ANNUAL_REPORT": {"firstRetry": 1, "retry": 1, "maxRetry": 10},
        "MASSACHUSETTS_ANNUAL_REPORT": {"firstRetry": 1, "retry": 1, "maxRetry": 10},
        "OKLAHOMA_ANNUAL_REPORT": {"firstRetry": 1, "retry": 1, "maxRetry": 10},
        "FLORIDA_FORMATION": {"firstRetry": 1, "retry": 1, "maxRetry": 10},
        "CALIFORNIA_FORMATION": {"firstRetry": 1, "retry": 1, "maxRetry": 10},
    },
    "nonProd": {
        "MICHIGAN_ANNUAL_REPORT": {"firstRetry": 0.0035, "retry": 0.0035, "maxRetry": 3},
        "NORTH_CAROLINA_ANNUAL_REPORT": {"firstRetry": 0.0035, "retry": 0.0035, "maxRetry": 3},
        "NEVADA_ANNUAL_REPORT": {"firstRetry": 0.0035, "retry": 0.0035, "maxRetry": 3},
        "RHODE_ISLAND_ANNUAL_REPORT": {"firstRetry": 0.0035, "retry": 0.0035, "maxRetry": 3},
        "MASSACHUSETTS_ANNUAL_REPORT": {"firstRetry": 0.0035, "retry": 0.0035, "maxRetry": 3},
        "OKLAHOMA_ANNUAL_REPORT": {"firstRetry": 0.0035, "retry": 0.0035, "maxRetry": 3},
        "FLORIDA_FORMATION": {"firstRetry": 0.0035, "retry": 0.0035, "maxRetry": 3},
        "CALIFORNIA_FORMATION": {"firstRetry": 0.0035, "retry": 0.0035, "maxRetry": 3},
    },
}


CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET,POST,PUT,DELETE,OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type,Authorization",
}
