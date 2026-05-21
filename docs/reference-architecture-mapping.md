# Galaxy SDLC — Enterprise Agentic Security Reference Architecture Mapping

> **Status:** ✅ Active &nbsp;|&nbsp; 🔄 Partial &nbsp;|&nbsp; ⏸ Roadmap &nbsp;|&nbsp; ⚪ N/A

---

<table style="width:100%; border-collapse:collapse; font-family:sans-serif; font-size:0.82em;">

<!-- ══════════════════════════════════════════════════════════════════════════ -->
<!-- GOVERNANCE BAR                                                             -->
<!-- ══════════════════════════════════════════════════════════════════════════ -->
<thead>
<tr>
  <td colspan="6" style="background:#111827; color:#f9fafb; text-align:center; font-weight:bold; font-size:1em; padding:10px; letter-spacing:1px;">
    GOVERNANCE
  </td>
</tr>
<tr>
  <td colspan="2" style="background:#1f2937; color:#d1d5db; text-align:center; padding:6px; font-weight:bold;">
    Risk Classification &amp; Red-teaming
  </td>
  <td colspan="2" style="background:#1f2937; color:#d1d5db; text-align:center; padding:6px; font-weight:bold;">
    Compliance &nbsp;·&nbsp; EU AI Act &nbsp;·&nbsp; ISO 42001 &nbsp;·&nbsp; NIST AI RMF
  </td>
  <td colspan="2" style="background:#1f2937; color:#d1d5db; text-align:center; padding:6px; font-weight:bold;">
    Policy Lifecycle &amp; Exception Management
  </td>
</tr>
<tr>
  <td colspan="2" style="background:#374151; color:#e5e7eb; text-align:center; padding:8px;">
    ✅ <strong>agent_os</strong> Prompt Injection Detector<br/>
    <em>7-vector taxonomy · OWASP LLM01</em><br/><br/>
    ✅ <strong>agent_os</strong> Credential Redactor<br/>
    <em>PCI-DSS 3.4 · secrets never reach LLM</em><br/><br/>
    🔄 <strong>agent_compliance</strong> Red-team eval harness<br/>
    <em>available in toolkit · not yet scheduled</em>
  </td>
  <td colspan="2" style="background:#374151; color:#e5e7eb; text-align:center; padding:8px;">
    ✅ <strong>agent_os</strong> Audit Logger<br/>
    <em>hash-chained SHA-256 · SOC 2 CC7.2</em><br/><br/>
    ✅ <strong>Azure Entra ID</strong> NHI (18 principals)<br/>
    <em>ISO 27001 A.9 · per-agent blast radius</em><br/><br/>
    ✅ <strong>agent_os</strong> Context Budget Guard<br/>
    <em>OWASP LLM04 · Denial of Wallet</em>
  </td>
  <td colspan="2" style="background:#374151; color:#e5e7eb; text-align:center; padding:8px;">
    ✅ <strong>YAML Policy Engine</strong> (OPA-equivalent)<br/>
    <em>agent_os GovernancePolicyMiddleware<br/>
    declarative rules · no code change to update</em><br/><br/>
    ✅ <strong>agentmesh-platform</strong> policy compose<br/><br/>
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
  <em>Orchestrator</em>
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
    ✅ <strong>Azure OpenAI</strong> · gpt-5-3-codex<br/>
    <em>via APIM — never direct</em><br/><br/>
    5-agent migration pipeline<br/>
    Analyzer · Coder (×3) · Tester · Reviewer · SecurityReviewer<br/>
    + Scanner · ASTAnalyzer (pre-migration)
  </td>
  <td style="vertical-align:top; padding:8px; border:1px solid #ddd; background:#faf5ff;">
    <strong>Custom &amp; Team agents</strong><br/>
    ✅ <strong>MAF Agent</strong> (same runtime)<br/><br/>
    5-agent Discovery pipeline<br/>
    DiscoveryScanner · DiscoveryGrapher · DiscoveryBRD<br/>
    DiscoveryArchitect · DiscoveryStories<br/><br/>
    10 codebase-type Coder variants<br/>
    <em>python_serverless · java_spring_boot · node_lambda …</em>
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
        ✅ <strong>MAF</strong> AgentMiddleware chain<br/>
        ✅ <strong>MAF</strong> ChatTelemetryLayer<br/>
        ✅ <strong>MAF</strong> AgentTelemetryLayer<br/>
        ✅ YAML agent config schema<br/>
        <em>Pydantic-validated · typo-safe</em>
      </td>
      <td style="width:25%; vertical-align:top; padding:8px; border-right:1px solid #ddd;">
        <strong>Registries</strong><br/>
        ✅ <strong>agentmesh-platform</strong> NHI registry<br/>
        <em>18 agent principals</em><br/>
        ✅ <strong>agent_os</strong> CapabilityGuard<br/>
        <em>tool allow-list · deny-unknown</em><br/>
        🔄 <strong>Azure Container Registry</strong><br/>
        <em>image store · provisioned</em>
      </td>
      <td style="width:25%; vertical-align:top; padding:8px; border-right:1px solid #ddd;">
        <strong>Templates &amp; utilities</strong><br/>
        ✅ Prompt library (Markdown)<br/>
        <em>shared rules + per-stack variants</em><br/>
        ✅ AWS→Azure mapping registry<br/>
        <em>YAML · codebase_type → strategy</em><br/>
        ✅ <strong>A2A</strong> typed envelope schema<br/>
        <em>run_id · module_id correlation</em>
      </td>
      <td style="width:25%; vertical-align:top; padding:8px;">
        <strong>Memory management</strong><br/>
        ✅ Isolated context window<br/>
        <em>per agent · per run · no bleed</em><br/>
        ✅ Pydantic artifact models<br/>
        <em>passed between discovery agents</em><br/>
        ⚪ Vector / long-term memory<br/>
        <em>not required for migration</em>
      </td>
    </tr>
    </table>
  </td>
</tr>

<!-- LAYER 03 ───────────────────────────────────────────────────────────────── -->
<tr>
  <td style="width:90px; background:#fef3c7; text-align:center; vertical-align:middle; padding:6px; border:1px solid #ddd; color:#92400e; font-weight:bold; font-size:0.9em;">
    LAYER 03<br/><strong>Security<br/>&amp; Control<br/>Plane</strong>
  </td>
  <td colspan="2" style="padding:0; border:1px solid #ddd; background:#fffbeb;">
    <table style="width:100%; border-collapse:collapse;">
    <tr>
      <td style="width:25%; vertical-align:top; padding:8px; border-right:1px solid #ddd;">
        <strong>Security gateway &amp; LLM router</strong><br/>
        ✅ <strong>Azure APIM</strong> Consumption<br/>
        <em>sole LLM egress path</em><br/>
        ✅ Sub-key validation<br/>
        ✅ Required-headers guard<br/>
        <em>x-agent-type · x-galaxy-run-id</em><br/>
        ✅ AOAI key injection<br/>
        <em>from Key Vault named value</em><br/>
        ✅ 100 RPM rate-limit
      </td>
      <td style="width:25%; vertical-align:top; padding:8px; border-right:1px solid #ddd;">
        <strong>Guardrails &amp; policy enforcement</strong><br/>
        ✅ <strong>agent_os</strong> PromptInjectionDetector<br/>
        ✅ <strong>agent_os</strong> CredentialRedactor<br/>
        ✅ <strong>agent_os</strong> ContextScheduler<br/>
        ✅ <strong>YAML Policy Engine</strong><br/>
        <em>OPA-equivalent · first-match-wins<br/>
        no container rebuild to update</em><br/>
        🔄 PII patterns<br/>
        <em>placeholder in galaxy-pii.yaml</em>
      </td>
      <td style="width:25%; vertical-align:top; padding:8px; border-right:1px solid #ddd;">
        <strong>NHI &amp; Identity</strong><br/>
        ✅ <strong>Azure Entra ID</strong><br/>
        <em>18 App Registrations</em><br/>
        ✅ <strong>Workload Identity Federation</strong><br/>
        <em>federated OIDC · no passwords</em><br/>
        ✅ <strong>ManagedIdentityCredential</strong><br/>
        ✅ <strong>Azure Key Vault</strong> secrets<br/>
        <em>TokenProvider · 5-min TTL cache</em><br/>
        ✅ <code>x-nhi-id</code> on every APIM request
      </td>
      <td style="width:25%; vertical-align:top; padding:8px;">
        <strong>Circuit breakers</strong><br/>
        ✅ <strong>agent_os</strong> ContextScheduler<br/>
        <em>per-agent token cap · hard cutoff</em><br/>
        ✅ <strong>agent_sre</strong> RogueAgentDetector<br/>
        <em>tool-use anomaly detection</em><br/>
        ✅ <strong>APIM</strong> 100 RPM throttle<br/>
        ✅ Orchestrator attempt cap<br/>
        <em>Coder max 3 retries</em><br/>
        🔴 <strong>agent_sre</strong> CircuitBreaker<br/>
        <em>AOAI resilience · deferred</em>
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
        ✅ <strong>Azure Container Apps</strong> Job<br/>
        ✅ <strong>MAF</strong> Agent runtime<br/>
        ✅ <strong>A2A protocol</strong><br/>
        <em>typed envelopes · OTel spans</em><br/>
        ✅ mTLS via APIM (agent→LLM)<br/>
        ✅ Sandboxed tool execution<br/>
        <em>closure-bound · path-limited</em>
      </td>
      <td style="width:33%; vertical-align:top; padding:8px; border-right:1px solid #ddd;">
        <strong>Deployment pipeline</strong><br/>
        🔄 <strong>Azure Container Registry</strong><br/>
        <em>image store · provisioned</em><br/>
        ⏸ Azure DevOps / GitHub Actions<br/>
        <em>CI/CD · not yet wired</em><br/>
        ⏸ 4-eyes promotion gate<br/>
        ⏸ Signed artifact verification
      </td>
      <td style="width:33%; vertical-align:top; padding:8px;">
        <strong>AI-SBOM &amp; provenance</strong><br/>
        ✅ <strong>OpenTelemetry</strong> trace correlation<br/>
        <em>run_id ties all artifacts</em><br/>
        ✅ Versioned output<br/>
        <em>migrated/&lt;repo&gt;/vN · never overwritten</em><br/>
        ✅ run-summary.json<br/>
        <em>machine-readable per-run snapshot</em><br/>
        ⏸ Formal AI-SBOM JSON<br/>
        <em>model + agent versions + YAML hashes</em>
      </td>
    </tr>
    </table>
  </td>
</tr>

<!-- LAYER 05 ───────────────────────────────────────────────────────────────── -->
<tr>
  <td style="width:90px; background:#f3f4f6; text-align:center; vertical-align:middle; padding:6px; border:1px solid #ddd; color:#374151; font-weight:bold; font-size:0.9em;">
    LAYER 05<br/><strong>Infrastructure</strong>
  </td>
  <td colspan="2" style="padding:0; border:1px solid #ddd; background:#f9fafb;">
    <table style="width:100%; border-collapse:collapse;">
    <tr>
      <td style="width:25%; vertical-align:top; padding:8px; border-right:1px solid #ddd;">
        <strong>Cloud &amp; Compute</strong><br/>
        ✅ <strong>Azure Container Apps</strong><br/>
        <em>Consumption tier · serverless</em><br/>
        ✅ <strong>Azure Container Registry</strong><br/>
        🔄 VNet integration<br/>
        <em>APIM internal mode ready</em>
      </td>
      <td style="width:25%; vertical-align:top; padding:8px; border-right:1px solid #ddd;">
        <strong>Model access</strong><br/>
        ✅ <strong>Azure OpenAI</strong><br/>
        <em>gpt-5-3-codex · Responses API</em><br/>
        ✅ Via <strong>Azure APIM</strong> only<br/>
        <em>no direct endpoint in any agent</em>
      </td>
      <td style="width:25%; vertical-align:top; padding:8px; border-right:1px solid #ddd;">
        <strong>Vector DB &amp; Storage</strong><br/>
        🔄 <strong>Azure PostgreSQL Flex</strong><br/>
        <em>audit ledger · DSN unset → stdout</em><br/>
        ✅ Filesystem versioned output<br/>
        <em>migrated/ · no RAG needed</em><br/>
        ⚪ Vector DB<br/>
        <em>not required for migration pattern</em>
      </td>
      <td style="width:25%; vertical-align:top; padding:8px;">
        <strong>Secret &amp; Key Management</strong><br/>
        ✅ <strong>Azure Key Vault</strong><br/>
        <em>all secrets · APIM named values</em><br/>
        ✅ <strong>Workload Identity</strong><br/>
        <em>zero standing privilege</em><br/>
        ✅ <strong>TokenProvider</strong> 5-min TTL<br/>
        <em>KV fetch with env-var fallback</em>
      </td>
    </tr>
    </table>
  </td>
</tr>

</table>
</td>

<!-- ── OBSERVE COLUMN ─────────────────────────────────────────────────────── -->
<td style="width:150px; vertical-align:top; background:#faf5ff; padding:8px; border:1px solid #d1d5db; font-size:0.8em;">
  <div style="text-align:center; font-weight:bold; font-size:1em; margin-bottom:8px; color:#5b21b6;">OBSERVE</div>

  <strong>Audit &amp; SIEM</strong><br/>
  ✅ <strong>OpenTelemetry</strong><br/>
  ✅ <strong>Azure Application Insights</strong><br/>
  ✅ <strong>agent_os</strong> AuditLogger<br/>
  <em>hash-chained SHA-256</em><br/>
  ✅ <strong>Azure PostgreSQL</strong><br/>
  <em>tamper-evident ledger</em><br/>
  🔄 Azure Sentinel<br/>
  <em>App Insights → Sentinel export</em><br/><br/>

  <strong>Behavioural Monitoring</strong><br/>
  ✅ <strong>agent_sre</strong> RogueAgentDetector<br/>
  <em>tool-use anomaly detection</em><br/>
  ⏸ Cross-run behavioral baseline<br/>
  <em>data available · alert not wired</em><br/><br/>

  <strong>Policy Compliance</strong><br/>
  ✅ <strong>YAML Policy Engine</strong><br/>
  <em>every call evaluated</em><br/>
  ✅ KQL governance queries<br/>
  <em>Azure Application Insights</em><br/>
  ⏸ Continuous gap reporting<br/><br/>

  <strong>Telemetry</strong><br/>
  ✅ <strong>OpenTelemetry</strong> (OTel)<br/>
  ✅ <strong>Azure Monitor</strong><br/>
  ✅ <strong>Azure Application Insights</strong><br/>
  ✅ 3-channel JSONL<br/>
  <em>orchestration · agents · a2a</em>
</td>

</tr>
</tbody>
</table>

---

## Key frameworks at a glance

| Framework / Service | Role in Galaxy SDLC |
|---|---|
| **Microsoft Agent Framework (MAF)** | Agent runtime — `Agent`, `AgentMiddleware`, telemetry layers |
| **Microsoft Agent OS (`agent_os`)** | Governance toolkit — prompt injection, credential redactor, context budget, policy engine, audit logger |
| **Microsoft Agent SRE (`agent_sre`)** | Operational layer — `RogueAgentDetector` anomaly detection, circuit breaker (roadmap) |
| **agentmesh-platform** | NHI registry, platform governance primitives |
| **YAML Policy Engine** | OPA-equivalent — declarative rules evaluated per call, no code change to update |
| **Azure APIM** | Sole LLM egress path — sub-key validation, rate-limit, AOAI key injection |
| **Azure Entra ID** | 18 per-agent Non-Human Identities (Managed Identity + Workload Identity Federation) |
| **Azure Key Vault** | All secrets — TokenProvider with 5-min TTL, APIM named values |
| **Azure OpenAI** | LLM inference — `gpt-5-3-codex` deployment, accessed via APIM only |
| **Azure Container Apps** | Agent runtime host (Consumption tier) |
| **Azure Application Insights** | OTel span sink + governance audit event store + KQL dashboard |
| **OpenTelemetry (OTel)** | Instrumentation standard — `pipeline.run` root span → `a2a.dispatch.*` children |
| **Azure PostgreSQL Flex** | Hash-chained audit ledger (provisioned; stdout mode until DSN set) |

---

*Galaxy SDLC Platform — Reference Architecture Mapping v1.1 — 2026-05-15*
