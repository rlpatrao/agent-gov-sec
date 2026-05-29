# Galaxy SDLC — Infrastructure Connections

How every Azure component connects to every other. Verified against the live resource group `galaxyscanner-rg`.

---

## Full connection map

```mermaid
flowchart TB

    %% ── Nodes ──────────────────────────────────────────────────────────

    DEV["🖥️ Developer\nrun_pipeline_aca.py"]

    subgraph ACA["Azure Container Apps · galaxyscanner-aca-env"]
        direction LR
        J1["galaxy-classifier-job"]
        J2["galaxy-analyzer-job"]
        J3["galaxy-coder-job\n(×3 attempts)"]
        J4["galaxy-tester-job"]
        J5["galaxy-reviewer-job"]
        J6["galaxy-securityreviewer-job"]
        J1 --> J2 --> J3 --> J4 --> J5 --> J6
    end

    ACR["🐳 Azure Container Registry\ngalaxyscannercrd63cdd.azurecr.io\ngalaxy-scanner:0.2.1"]

    FILES["📁 Azure Files\ngalaxyscannersa / galaxy-runs\nmounted at /data in every job"]

    KV["🔐 Azure Key Vault\ngalaxyscanner-kv-d63cdd\nAPIM sub-key · AOAI key · App Insights conn str"]

    APIM["🛡️ Azure APIM\ngalaxyscanner-apim\nSub-key · headers guard · 100 RPM · AOAI key inject"]

    AOAI["🤖 Azure OpenAI\ngalaxyscanner-openai\ngpt-5-3-codex · Responses API"]

    ENTRA["🪪 Entra ID\n18 Managed Identity principals\ngalaxy-*-mi · one per agent"]

    AI["📊 Application Insights\ngalaxyscanner-ai\nOTel spans · governance events"]

    LAW["📋 Log Analytics\ngalaxyscanner-law\nKQL queries · long-term retention"]

    PG["🗄️ PostgreSQL Flex\nnot provisioned\nhash-chain ledger (stdout mode today)"]

    %% ── Edges ───────────────────────────────────────────────────────────

    %% Developer → platform
    DEV -->|"① upload source\naz storage file upload"| FILES
    DEV -->|"② az containerapp job start\n(sequential per phase)"| ACA
    DEV -->|"③ download results\naz storage file download"| FILES

    %% Image pull
    ACR -->|"image pull at job start\ngalaxy-scanner:0.2.1"| ACA

    %% Identity injection
    ENTRA -->|"MI token auto-injected\nby ACA runtime (IMDS)\nno password in container"| ACA

    %% Artifact handoff between jobs
    ACA <-->|"read prev artifact / write own output\n/data/runs/<run_id>/*.json"| FILES

    %% LLM egress — always through APIM
    ACA -->|"LLM call\nOcp-Apim-Subscription-Key\nx-agent-type · x-nhi-id · x-galaxy-run-id"| APIM

    %% APIM → OpenAI (key injected from KV, never in agent code)
    APIM -->|"forwarded request\nAOAI key injected\nfrom KV named value"| AOAI

    %% Key Vault connections
    KV -->|"AOAI key\n(named value)"| APIM
    ACA -->|"secret fetch\n5-min TTL token cache\nvia Managed Identity"| KV

    %% Telemetry
    ACA -->|"OTel spans\ngovernance audit events\nBatchSpanProcessor → HTTPS"| AI
    AI -->|"span & event storage\nKQL dashboard"| LAW

    %% Postgres (wired but inactive)
    ACA -.->|"hash-chain writes\n(DSN blank — stdout mode)"| PG

    %% ── Styles ──────────────────────────────────────────────────────────
    classDef live    fill:#14532d,stroke:#22c55e,color:#dcfce7
    classDef gateway fill:#713f12,stroke:#f59e0b,color:#fef3c7
    classDef obs     fill:#1e1b4b,stroke:#818cf8,color:#e0e7ff
    classDef idle    fill:#1e293b,stroke:#475569,color:#94a3b8,stroke-dasharray:4

    class J1,J2,J3,J4,J5,J6,ACA live
    class APIM,KV,ACR,FILES,ENTRA,AOAI live
    class AI,LAW obs
    class PG idle
    class DEV live
```

---

## What each connection carries

| From | To | What travels |
|---|---|---|
| Developer | Azure Files | Source repo files (uploaded before pipeline starts) |
| Developer | ACA Jobs | `az containerapp job start` CLI trigger with `RUN_ID`, `MODULE_ID` env vars |
| Azure Files | Developer | Completed artifacts downloaded after SecurityReviewer finishes |
| ACR | ACA Jobs | Container image (`galaxy-scanner:0.2.1`) pulled at job start |
| Entra ID | ACA Jobs | Managed Identity OIDC token, injected by ACA runtime via IMDS — no password ever stored |
| ACA Jobs | Azure Files | Each job reads the previous job's JSON artifact, writes its own output to `/data/runs/<run_id>/` |
| ACA Jobs | APIM | Every LLM call — carries `Ocp-Apim-Subscription-Key`, `x-agent-type`, `x-nhi-id`, `x-galaxy-run-id` headers |
| Key Vault | APIM | AOAI API key injected as a named value — key never leaves Azure control plane |
| ACA Jobs | Key Vault | Secret fetches (App Insights conn str, APIM sub-key) via Managed Identity — 5-min TTL cache |
| APIM | Azure OpenAI | Forwarded Responses API request with AOAI key injected |
| ACA Jobs | App Insights | OTel spans (`pipeline.run`, `a2a.dispatch.*`) + governance audit events — direct HTTPS, bypasses APIM |
| App Insights | Log Analytics | Span and event storage — queryable via KQL |
| ACA Jobs | PostgreSQL | Hash-chain audit writes — **inactive today** (DSN blank, falls through to stdout) |

---

## What APIM does and does NOT see

```
LLM traffic:   ACA Jobs → APIM → Azure OpenAI   ✅ APIM sees every token
OTel traffic:  ACA Jobs → App Insights (direct HTTPS)   ✅ bypasses APIM — this is intentional
Secret fetches: ACA Jobs → Key Vault (direct, via MI)   ✅ bypasses APIM — also intentional
```

APIM is the **sole LLM egress path** — no agent has a direct Azure OpenAI endpoint or key.
Everything else (telemetry, secrets) goes direct to avoid latency and unnecessary gateway coupling.

---

*Last verified: 2026-05-22 against live `galaxyscanner-rg` resources*
```
