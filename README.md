# Galaxy — Scanner Agent

Discovery Pipeline agent (Tier 1) for the Galaxy migration platform.

## Architecture

```
repo_path
    │
    ▼
Claude Agent SDK              autonomous file traversal (Read / Glob / Bash)
    │ file_map
    ▼
FoundryClient.call()          Hard Contract P32-V2 — single LLM egress
    ├── TokenProvider         short-lived credentials (Key Vault + Workload Identity)
    ├── NHI identity          per-agent Entra service principal
    ├── Input sanitisation    injection pattern stripping
    ├── Cost ceiling          pre-flight token estimate — hard stop
    ├── APIM headers          x-agent-type, x-galaxy-run-id, x-nhi-id + W3C traceparent
    ├── Circuit breaker       exponential back-off, 3 retries max
    └── Response safety       empty response guard
    │ structured JSON
    ▼
TraceLedger.record()          append-only hash-chained PostgreSQL ledger
    │
    ▼
ScannerOutput                 consumed by Architect agent
```

## Security model

| Concern | Implementation |
|---|---|
| Short-lived credentials | `TokenProvider` — Key Vault via Workload Identity, refreshed every 5 min |
| No static secrets | No API keys in env vars, Dockerfiles, or code in AKS |
| Per-agent identity | `NHIRegistry` — each agent type has its own Entra service principal (NHI) |
| Least privilege | Each NHI has scoped permissions only — Scanner can read files, not write |
| Trace correlation | W3C TraceContext injected into all APIM calls — full tree in AppInsights |
| Immutable audit | Hash-chained trace ledger — tamper-evident, append-only |
| Injection defence | 7 known injection patterns stripped from all prompts |
| Cost control | Hard token ceiling — run killed before dispatch if exceeded |

## Agent 365 SDK note

Agent 365 SDK (`microsoft-agents-a365-*`) is **not a dependency** in this version.
The developer SDK and autonomous agent identity remain in Frontier preview as of April 2026.
The OTel, NHI, and Blueprint patterns here are designed to be forward-compatible —
Agent 365 SDK wraps the same primitives and will drop in when it reaches production GA.

## Local setup

```bash
cp .env.example .env
# Set ANTHROPIC_API_KEY for local dev (Key Vault takes over in AKS)

pip install -r requirements.txt
```

## Run

```bash
python run_scanner.py \
  --repo /path/to/legacy/service \
  --run-id run-20260422-001 \
  --module-id payments-service
```

## Test

```bash
pytest tests/ -v
```

## Database

Apply the schema before running against PostgreSQL:

```bash
psql $POSTGRES_DSN -f infra/ledger_schema.sql
```

## File structure

```
galaxy-scanner/
├── foundry_client.py           Hard Contract P32-V2 — single LLM egress
├── token_provider.py           Short-lived credentials via Key Vault + Workload Identity
├── nhi_identity.py             Non-Human Identity registry (per-agent Entra service principals)
├── run_tracer.py               OTel trace context — W3C propagation, agent spans, LLM attributes
├── trace_ledger.py             Immutable hash-chained audit ledger (PostgreSQL)
├── agents/
│   └── scanner_agent.py        Scanner agent — repo traversal + structured analysis
├── tests/
│   └── test_security_traceability.py   Tests for all security + traceability components
├── infra/
│   └── ledger_schema.sql       PostgreSQL schema + compliance queries
├── run_scanner.py              Local CLI runner
├── requirements.txt
└── .env.example
```

## Adding the next agent

1. Create `agents/your_agent.py`
2. Get identity: `self._identity = NHIRegistry.get("YourAgentType")`
3. Build context: `ctx = CallContext(agent_type=..., identity=..., tracer=..., ledger=...)`
4. Call: `await self._foundry.call(system=..., user=..., ctx=ctx)`
5. Record actions in ledger: `await ledger.record(agent_type=..., action=..., nhi_id=...)`
6. Register NHI in Entra and add `NHI_CLIENT_ID_YOURAGENTTYPE` to env config
EOF
