# Governance Dashboard

The enforcement service serves an operational dashboard at `GET /dashboard`. It is the
read path for the centralised compliance tracker: what the governance chokepoints have
decided, which controls fired, and how those controls map to external frameworks.

## Where it runs

The dashboard is part of the enforcement service
([`governance/remote/server.py`](../../governance/remote/server.py)), so it is available
wherever that service runs — the local container from
[`deploy/docker-compose.yml`](../../deploy/docker-compose.yml), or the deployed service.
It is served from the same port as the data plane:

```
http://<enforcement-endpoint>/dashboard
```

The page is self-contained HTML: inline CSS, no JavaScript, no external requests. It
refreshes itself every 15 seconds with a `<meta http-equiv="refresh">`, so it works in a
network-isolated environment.

## What it shows

| Section | Content |
|---|---|
| Header | Service version and revision, uptime, the loaded policy registry (its version and how many agent types it covers), decisions recorded, buffer occupancy |
| Agent runs | The most recent enforcement decisions, newest first: time, agent type, NHI, route, outcome, control code, HTTP status. Allow, deny and error rows are visually distinct |
| Guardrail decisions | Cumulative allowed and denied counts per control code and per chokepoint route |
| Controls crosswalk | Every governance control mapped to OWASP, NIST AI RMF, ISO/IEC 42001, the EU AI Act and MITRE ATLAS |

## The buffer is not the ledger

The dashboard reads
[`governance/remote/decision_log.py`](../../governance/remote/decision_log.py): a bounded,
in-memory, process-local ring buffer written to by the server on every data-plane request.
It is observability, not audit.

| | Decision buffer | Trace ledger |
|---|---|---|
| Location | Enforcement service process memory | `core/trace_ledger.py`, PostgreSQL |
| Durability | Lost on restart | Append-only, retained |
| Integrity | None | Hash-chained, tamper-evident |
| Retention | Most recent N decisions | Every entry |
| Use | Live operational view | Audit evidence |

Aggregate counters are held separately from the ring, so the guardrail totals cover the
whole process lifetime even after the run list has wrapped. Audit questions — what
happened last quarter, has the record been altered — are answered from the ledger, not
from this page.

## What is not stored

The buffer holds request metadata only: timestamp, route, agent type, NHI id, outcome,
control code and HTTP status. No prompt, model response, tool argument, data row, or
request or response body of any kind is retained or rendered.

The page is nonetheless an operational view of which agents are calling which chokepoints
and which controls are firing. Reach it from the governing team's network path; do not
expose it to the agent runtimes the service enforces against.

## Configuration

| Variable | Default | Effect |
|---|---|---|
| `GOV_DASHBOARD_DECISION_BUFFER` | `1000` | Ring capacity, in decision records. A non-numeric or non-positive value falls back to the default |

The header's version and revision come from `GALAXY_SERVICE_VERSION` and
`GALAXY_SERVICE_REVISION`, which the publish path stamps into the image; the registry
line reflects `GOV_POLICY_REGISTRY` / `GOV_POLICY_REGISTRY_PATH`.

## Regenerating the crosswalk

The service image does not contain `docs/`, so the crosswalk table is compiled into
`governance/remote/_crosswalk.py` at development time and imported at runtime. After
editing [`standards-crosswalk.md`](standards-crosswalk.md):

```bash
python scripts/gen_crosswalk.py            # rewrite the module
python scripts/gen_crosswalk.py --check    # staleness gate; non-zero if out of date
```

Commit the regenerated module alongside the document.
