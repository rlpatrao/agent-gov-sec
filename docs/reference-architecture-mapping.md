# Galaxy SDLC — Enterprise Agentic Security Reference Architecture Mapping

> **Status key:** ✅ Active &nbsp;|&nbsp; 🔄 Partial / in-progress &nbsp;|&nbsp; ⏸ Roadmap &nbsp;|&nbsp; ⚪ N/A by design

---

<table style="width:100%; border-collapse:collapse; font-family:sans-serif; font-size:0.82em;">

<!-- ══════════════════════════════════════════════════════════════════════════ -->
<!-- GOVERNANCE BAR                                                             -->
<!-- ══════════════════════════════════════════════════════════════════════════ -->
<thead>
<tr>
  <td colspan="6" style="background:#111827; color:#f9fafb; text-align:center; font-weight:bold; font-size:1em; padding:10px; letter-spacing:1px;">
    GOVERNANCE &nbsp;·&nbsp; Risk Classification &amp; Red-teaming &nbsp;·&nbsp; Compliance (EU AI Act · ISO 42001 · NIST AI RMF) &nbsp;·&nbsp; Policy Lifecycle &amp; Exception Management
  </td>
</tr>
<tr>
  <td colspan="2" style="background:#374151; color:#e5e7eb; text-align:center; padding:8px;">
    <strong>Risk Classification &amp; Red-teaming</strong><br/><br/>
    ✅ <strong>agent_os</strong> — Prompt Injection Detector<br/>
    <em>7-vector taxonomy · OWASP LLM01</em><br/><br/>
    ✅ <strong>agent_os</strong> — Credential Redactor<br/>
    <em>PCI-DSS 3.4 · secrets never reach LLM</em><br/><br/>
    ⏸ <strong>agent_compliance</strong> — Red-team eval harness<br/>
    <em>available in toolkit · not yet scheduled</em>
  </td>
  <td colspan="2" style="background:#374151; color:#e5e7eb; text-align:center; padding:8px;">
    <strong>Compliance &amp; Audit</strong><br/><br/>
    ✅ <strong>agent_os</strong> — SHA-256 Hash-chained Audit Logger<br/>
    <em>tamper-evident · SOC 2 CC7.2</em><br/><br/>
    ✅ <strong>Azure Entra ID</strong> — 18 NHI Managed Identities<br/>
    <em>per-agent blast radius · ISO 27001 A.9</em><br/><br/>
    ✅ <strong>agent_os</strong> — Context Budget Guard<br/>
    <em>OWASP LLM04 · Denial of Wallet prevention</em>
  </td>
  <td colspan="2" style="background:#374151; color:#e5e7eb; text-align:center; padding:8px;">
    <strong>Policy Lifecycle &amp; Exception Management</strong><br/><br/>
    ✅ <strong>YAML Policy Engine</strong> (OPA-equivalent)<br/>
    <em>Declarative rules · no container rebuild to update<br/>
    first-match-wins · deny by default</em><br/><br/>
    ✅ <strong>agentmesh-platform</strong> — Policy composition<br/><br/>
    ⏸ Human-in-the-loop exception gate<br/>
    <em>Azure Logic Apps · roadmap</em>
  </td>
</tr>
</thead>

<tbody>
<tr>
<!-- ── CONSUMERS ──────────────────────────────────────────────────────────── -->
<td style="background:#f3f4f6; width:90px; text-align:center; vertical-align:middle; padding:6px; border:1px solid #d1d5db;">
  <strong>CONSUMERS</strong><br/><br/>
  <em>CLI</em><br/>
  <em>A2A API</em><br/>
  <em>ACA Orchestrator</em>
</td>

<!-- ── LAYERS GRID ─────────────────────────────────────────────────────────── -->
<td colspan="4" style="padding:0; vertical-align:top; border:1px solid #d1d5db;">
<table style="width:100%; border-collapse:collapse;">

<!-- LAYER 01 ───────────────────────────────────────────────────────────────── -->
<tr>
  <td style="width:90px; background:#ede9fe; text-align:center; vertical-align:middle; padding:6px; border:1px solid #ddd; color:#5b21b6; font-weight:bold; font-size:0.9em;">
    LAYER 01<br/><strong>Agent<br/>Application</strong>
  </td>
  <td style="vertical-align:top; padding:8px; border:1px solid #ddd; background:#faf5ff;">
    <strong>Core platform agents</strong><br/>
    ✅ <strong>Microsoft Agent Framework (MAF)</strong><br/>
    <em>agent-framework-core · agent-framework-foundry</em><br/><br/>
    ✅ <strong>5-agent Migration pipeline</strong><br/>
    <em>Analyzer · Coder · Tester · Reviewer · SecurityReviewer</em><br/><br/>
    ✅ <strong>Pre-migration agents</strong><br/>
    <em>Scanner · ASTAnalyzer · Classifier</em>
  </td>
  <td style="vertical-align:top; padding:8px; border:1px solid #ddd; background:#faf5ff;">
    <strong>Custom &amp; Team agents</strong><br/>
    ✅ <strong>MAF Agent</strong> (same runtime, same governance)<br/><br/>
    🔄 <strong>5-agent Discovery pipeline</strong><br/>
    <em>Agents built &amp; tested individually<br/>
    End-to-end orchestrator is a stub</em><br/><br/>
    ✅ <strong>10 Coder stack variants</strong><br/>
    <em>Per codebase-type YAML config<br/>
    python · java · node · .net · go …</em>
  </td>
</tr>

<!-- LAYER 02 ───────────────────────────────────────────────────────────────── -->
<tr>
  <td style="width:90px; background:#dbeafe; text-align:center; vertical-align:middle; padding:6px; border:1px solid #ddd; color:#1e40af; font-weight:bold; font-size:0.9em;">
    LAYER 02<br/><strong>Agent<br/>Services</strong>
  </td>
  <td colspan="2" style="padding:0; border:1px solid #ddd; background:#eff6ff;">
    <table style="width:100%; border-collapse:collapse;">
    <tr>
      <td style="width:25%; vertical-align:top; padding:8px; border-right:1px solid #ddd;">
        <strong>Harness &amp; configurator</strong><br/>
        ✅ <strong>MAF</strong> — AgentMiddleware chain<br/>
        <em>Governance stack wired at build time</em><br/><br/>
        ✅ <strong>MAF</strong> — Telemetry layers<br/>
        <em>Chat + Agent OTel instrumentation</em><br/><br/>
        ✅ YAML agent configuration schema<br/>
        <em>Per-agent budgets, tools, policies</em>
      </td>
      <td style="width:25%; vertical-align:top; padding:8px; border-right:1px solid #ddd;">
        <strong>Registries</strong><br/>
        ✅ <strong>agentmesh-platform</strong> — NHI registry<br/>
        <em>18 agent principals · real Entra IDs</em><br/><br/>
        ✅ <strong>agent_os</strong> — Capability Guard<br/>
        <em>Tool allow-list · deny-unknown enforced</em><br/><br/>
        ✅ <strong>Azure Container Registry</strong><br/>
        <em>galaxy-scanner:0.2.1 · deployed to 18 jobs</em>
      </td>
      <td style="width:25%; vertical-align:top; padding:8px; border-right:1px solid #ddd;">
        <strong>Templates &amp; utilities</strong><br/>
        ✅ Prompt library (Markdown)<br/>
        <em>Shared rules + per-stack variants</em><br/><br/>
        ✅ AWS → Azure mapping registry<br/>
        <em>YAML · codebase_type → migration strategy</em><br/><br/>
        ✅ <strong>A2A</strong> typed envelope schema<br/>
        <em>run_id · module_id correlation</em>
      </td>
      <td style="width:25%; vertical-align:top; padding:8px;">
        <strong>Memory management</strong><br/>
        ✅ Isolated context window<br/>
        <em>Per agent · per run · no cross-agent bleed</em><br/><br/>
        ✅ Typed artifact handoff<br/>
        <em>Structured outputs passed between agents<br/>
        via Azure Files in ACA mode</em><br/><br/>
        ⚪ Vector / long-term memory<br/>
        <em>Not required — migration is stateless per run</em>
      </td>
    </tr>
    </table>
  </td>
</tr>

<!-- LAYER 03 ───────────────────────────────────────────────────────────────── -->
<tr>
  <td style="width:90px; background:#fef3c7; text-align:center; vertical-align:middle; padding:6px; border:1px solid #ddd; color:#92400e; font-weight:bold; font-size:0.9em;">
    LAYER 03<br/><strong>Security<br/>&amp; Control</strong>
  </td>
  <td colspan="2" style="padding:0; border:1px solid #ddd; background:#fffbeb;">
    <table style="width:100%; border-collapse:collapse;">
    <tr>
      <td style="width:25%; vertical-align:top; padding:8px; border-right:1px solid #ddd;">
        <strong>Security gateway &amp; LLM router</strong><br/>
        ✅ <strong>Azure APIM</strong> — Consumption tier<br/>
        <em>Sole LLM egress path · all agents route through it</em><br/><br/>
        ✅ Subscription key validation<br/>
        ✅ Required-headers enforcement<br/>
        <em>x-agent-type · x-galaxy-run-id · x-nhi-id</em><br/><br/>
        ✅ AOAI key injection from Key Vault<br/>
        <em>Key never leaves Azure control plane</em><br/><br/>
        ✅ 100 RPM rate-limit (per subscription)<br/>
        🔄 JWT token enforcement<br/>
        <em>Stub wired · not yet activated</em>
      </td>
      <td style="width:25%; vertical-align:top; padding:8px; border-right:1px solid #ddd;">
        <strong>Guardrails &amp; policy enforcement</strong><br/>
        ✅ <strong>agent_os</strong> — Prompt Injection Detector<br/>
        <em>Blocks 7 injection pattern families</em><br/><br/>
        ✅ <strong>agent_os</strong> — Credential Redactor<br/>
        <em>Strips secrets before LLM call</em><br/><br/>
        ✅ <strong>agent_os</strong> — Context Budget Guard<br/>
        <em>Hard token cap per agent</em><br/><br/>
        ✅ <strong>YAML Policy Engine</strong><br/>
        <em>OPA-equivalent · declarative deny rules<br/>
        No rebuild needed to update policy</em>
      </td>
      <td style="width:25%; vertical-align:top; padding:8px; border-right:1px solid #ddd;">
        <strong>NHI &amp; Identity</strong><br/>
        ✅ <strong>Azure Entra ID</strong> — 18 Managed Identities<br/>
        <em>One per agent type · real Entra principals</em><br/><br/>
        ✅ <strong>Azure Managed Identity</strong><br/>
        <em>MI token auto-injected by ACA at job runtime<br/>
        No passwords · no stored credentials</em><br/><br/>
        ✅ <strong>Azure Key Vault</strong> — Token provider<br/>
        <em>5-min TTL credential cache</em><br/><br/>
        ✅ <code>x-nhi-id</code> header on every APIM call<br/>
        <em>Per-agent attribution in gateway logs</em>
      </td>
      <td style="width:25%; vertical-align:top; padding:8px;">
        <strong>Circuit breakers</strong><br/>
        ✅ <strong>agent_os</strong> — Context Budget Guard<br/>
        <em>Hard token cap · fails fast before overspend</em><br/><br/>
        🔄 <strong>agent_sre</strong> — Rogue Agent Detector<br/>
        <em>Tool-use anomaly detection<br/>
        Active: Coder + Tester (tool-bearing agents)<br/>
        No-op: read-only agents by design</em><br/><br/>
        ✅ <strong>APIM</strong> — 100 RPM throttle<br/>
        <em>Hard egress cap at gateway level</em><br/><br/>
        ✅ Orchestrator retry cap<br/>
        <em>Coder: max 3 self-healing attempts</em><br/><br/>
        ⏸ <strong>agent_sre</strong> — Circuit Breaker<br/>
        <em>AOAI resilience · roadmap</em>
      </td>
    </tr>
    </table>
  </td>
</tr>

<!-- LAYER 04 ───────────────────────────────────────────────────────────────── -->
<tr>
  <td style="width:90px; background:#d1fae5; text-align:center; vertical-align:middle; padding:6px; border:1px solid #ddd; color:#065f46; font-weight:bold; font-size:0.9em;">
    LAYER 04<br/><strong>Runtime<br/>&amp; Platform</strong>
  </td>
  <td colspan="2" style="padding:0; border:1px solid #ddd; background:#ecfdf5;">
    <table style="width:100%; border-collapse:collapse;">
    <tr>
      <td style="width:33%; vertical-align:top; padding:8px; border-right:1px solid #ddd;">
        <strong>Agent runtime &amp; orchestration</strong><br/>
        ✅ <strong>Azure Container Apps</strong> — 18 Jobs<br/>
        <em>One job per agent type · each with own MI<br/>
        Azure Files share for artifact handoff<br/>
        Deployed via Bicep IaC</em><br/><br/>
        ✅ <strong>MAF</strong> — Agent runtime<br/>
        <em>Same runtime locally and in ACA</em><br/><br/>
        ✅ <strong>A2A protocol</strong><br/>
        <em>Typed envelopes · OTel-correlated spans<br/>
        File-based handoff between jobs in ACA mode</em><br/><br/>
        ✅ Sandboxed tool execution<br/>
        <em>Path-limited · secret-scrubbed subprocess</em>
      </td>
      <td style="width:33%; vertical-align:top; padding:8px; border-right:1px solid #ddd;">
        <strong>Deployment pipeline</strong><br/>
        ✅ <strong>Azure Container Registry</strong><br/>
        <em>Image published · deployed to all 18 jobs</em><br/><br/>
        ✅ <strong>Bicep IaC</strong><br/>
        <em>Full 18-job stack reproducible from repo<br/>
        Re-deploy with --image-tag to update</em><br/><br/>
        ⏸ GitHub Actions / Azure DevOps CI<br/>
        <em>Build → push → re-deploy pipeline · roadmap</em><br/><br/>
        ⏸ Signed artifact verification
      </td>
      <td style="width:33%; vertical-align:top; padding:8px;">
        <strong>AI-SBOM &amp; provenance</strong><br/>
        ✅ <strong>OpenTelemetry</strong> — trace correlation<br/>
        <em>run_id ties all spans, logs, and artifacts</em><br/><br/>
        ✅ Versioned output tree<br/>
        <em>migrated/&lt;repo&gt;/vN · immutable per run</em><br/><br/>
        ✅ run-summary.json<br/>
        <em>Machine-readable per-run provenance</em><br/><br/>
        ⏸ Formal AI-SBOM document<br/>
        <em>Model versions + agent versions + policy hashes</em>
      </td>
    </tr>
    </table>
  </td>
</tr>

<!-- LAYER 05 ───────────────────────────────────────────────────────────────── -->
<tr>
  <td style="width:90px; background:#f3f4f6; text-align:center; vertical-align:middle; padding:6px; border:1px solid #ddd; color:#374151; font-weight:bold; font-size:0.9em;">
    LAYER 05<br/><strong>Infra&shy;structure</strong>
  </td>
  <td colspan="2" style="padding:0; border:1px solid #ddd; background:#f9fafb;">
    <table style="width:100%; border-collapse:collapse;">
    <tr>
      <td style="width:25%; vertical-align:top; padding:8px; border-right:1px solid #ddd;">
        <strong>Cloud &amp; Compute</strong><br/>
        ✅ <strong>Azure Container Apps</strong><br/>
        <em>Environment provisioned · 18 jobs deployed<br/>
        Each job: own MI + ACR pull + Azure Files<br/>
        Fully deployed via Bicep</em><br/><br/>
        ✅ <strong>Azure Container Registry</strong><br/>
        <em>galaxy-scanner:0.2.1 image · Basic SKU</em><br/><br/>
        🔄 <strong>Azure VNet integration</strong><br/>
        <em>Not yet configured · roadmap</em>
      </td>
      <td style="width:25%; vertical-align:top; padding:8px; border-right:1px solid #ddd;">
        <strong>Model access</strong><br/>
        ✅ <strong>Azure OpenAI</strong><br/>
        <em>gpt-5-3-codex · Responses API</em><br/><br/>
        ✅ Accessed via <strong>Azure APIM</strong> only<br/>
        <em>No agent holds a direct endpoint or key</em><br/><br/>
        ✅ <strong>Azure Log Analytics</strong><br/>
        <em>Linked to Application Insights<br/>
        Span + governance event store</em>
      </td>
      <td style="width:25%; vertical-align:top; padding:8px; border-right:1px solid #ddd;">
        <strong>Storage &amp; Persistence</strong><br/>
        ✅ <strong>Azure Files</strong> — Artifact share<br/>
        <em>galaxy-runs share · mounted at /data<br/>
        Artifact handoff between per-agent jobs</em><br/><br/>
        ✅ Versioned filesystem output<br/>
        <em>migrated/ · local and downloadable from ACA</em><br/><br/>
        🔄 <strong>Azure PostgreSQL Flex</strong><br/>
        <em>Hash-chain ledger · DDL ready<br/>
        Not yet provisioned · stdout mode today</em><br/><br/>
        ⚪ Vector DB<br/>
        <em>Not required — migration is stateless per run</em>
      </td>
      <td style="width:25%; vertical-align:top; padding:8px;">
        <strong>Secret &amp; Key Management</strong><br/>
        ✅ <strong>Azure Key Vault</strong><br/>
        <em>All secrets · APIM named values<br/>
        No secret in any container image or env var</em><br/><br/>
        ✅ <strong>Azure Managed Identity</strong><br/>
        <em>18 per-agent MIs · token auto-injected by ACA<br/>
        Zero standing privilege</em><br/><br/>
        ✅ <strong>TokenProvider</strong> — 5-min TTL<br/>
        <em>KV-backed credential cache<br/>
        Env-var fallback for local dev only</em>
      </td>
    </tr>
    </table>
  </td>
</tr>

</table>
</td>

<!-- ── OBSERVE COLUMN ─────────────────────────────────────────────────────── -->
<td style="width:160px; vertical-align:top; background:#faf5ff; padding:8px; border:1px solid #d1d5db; font-size:0.8em;">
  <div style="text-align:center; font-weight:bold; font-size:1em; margin-bottom:8px; color:#5b21b6;">OBSERVE</div>

  <strong>Tracing &amp; Telemetry</strong><br/>
  ✅ <strong>OpenTelemetry</strong> SDK<br/>
  <em>pipeline.run root span →<br/>a2a.dispatch.* children</em><br/>
  ✅ <strong>Azure Application Insights</strong><br/>
  <em>OTel span sink + KQL</em><br/>
  ✅ <strong>Azure Monitor</strong><br/>
  ✅ 3-channel JSONL<br/>
  <em>orchestration · agents · a2a</em><br/><br/>

  <strong>Audit &amp; Compliance</strong><br/>
  ✅ <strong>agent_os</strong> — Audit Logger<br/>
  <em>SHA-256 hash-chained events</em><br/>
  🔄 <strong>Azure PostgreSQL</strong> ledger<br/>
  <em>Code wired · not provisioned<br/>
  Stdout mode today</em><br/>
  🔄 <strong>Azure Sentinel</strong> export<br/>
  <em>App Insights → Sentinel · roadmap</em><br/><br/>

  <strong>Behavioural Monitoring</strong><br/>
  🔄 <strong>agent_sre</strong> — Rogue Agent Detector<br/>
  <em>Active: Coder + Tester<br/>
  No-op: read-only agents</em><br/>
  ⏸ Cross-run baseline alerts<br/>
  <em>Data available · alert not wired</em><br/><br/>

  <strong>Policy Compliance</strong><br/>
  ✅ <strong>YAML Policy Engine</strong><br/>
  <em>Every LLM call evaluated</em><br/>
  ✅ KQL governance queries<br/>
  <em>Governance events in App Insights</em><br/>
  ⏸ Automated gap reporting<br/>
</td>

</tr>
</tbody>
</table>

---

## Key frameworks at a glance

| Framework / Service | Role in Galaxy SDLC | Status |
|---|---|---|
| **Microsoft Agent Framework (MAF)** | Agent runtime — middleware chain, telemetry layers, typed A2A envelopes | ✅ |
| **Microsoft Agent OS (agent_os)** | Governance toolkit — prompt injection, credential redaction, context budgeting, YAML policy engine, audit logger | ✅ |
| **Microsoft Agent SRE (agent_sre)** | Operational layer — Rogue Agent Detector (tool-bearing agents); Circuit Breaker (roadmap) | 🔄 |
| **agentmesh-platform** | NHI registry, 18 per-agent identity principals | ✅ |
| **YAML Policy Engine** | OPA-equivalent — declarative deny rules, no container rebuild to update policy | ✅ |
| **Azure APIM** | Sole LLM egress — sub-key validation, required-headers, rate-limit, AOAI key injection | ✅ |
| **Azure Entra ID** | 18 per-agent Managed Identities — each agent has its own Entra principal | ✅ |
| **Azure Managed Identity** | Token auto-injected by ACA at job runtime — zero passwords, zero standing privilege | ✅ |
| **Azure Key Vault** | All secrets — TokenProvider with 5-min TTL, APIM named values | ✅ |
| **Azure OpenAI** | LLM inference — gpt-5-3-codex, Responses API, accessed via APIM only | ✅ |
| **Azure Container Apps** | 18 Container App Jobs (one per agent) — own MI, ACR pull, Azure Files mount; deployed via Bicep | ✅ |
| **Azure Files** | Artifact handoff share between per-agent jobs (galaxy-runs) | ✅ |
| **Azure Application Insights** | OTel span sink + governance audit events + KQL dashboard | ✅ |
| **OpenTelemetry (OTel)** | Instrumentation standard — run_id correlates all spans, logs, and artifacts | ✅ |
| **Azure PostgreSQL Flex** | Hash-chained audit ledger — DDL ready, code wired, not yet provisioned | 🔄 |
| **GitHub Actions / Azure DevOps** | CI/CD — build, push image, re-deploy Bicep | ⏸ |

---

*Galaxy SDLC Platform — Reference Architecture Mapping v1.2 — 2026-05-22*
