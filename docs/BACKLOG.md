# Backlog

The tracked list of open work on the platform. One row per item; when an item
ships, move its substance to `CHANGELOG.md` and delete the row. Items that
graduate to GitHub issues keep their ID here until closed. The governing team
owns this file (via the `docs/shared/` CODEOWNERS pattern applied repo-wide);
anyone may propose a row in a PR.

Priorities: P1 = blocks release or correctness; P2 = product gap with a decided
design; P3 = improvement with a design still open.

| ID | P | Item | Detail and acceptance | Also tracked in |
|---|---|---|---|---|
| B-1 | P1 | Merge PR #38 | CI was mid-run when the working session ended (2026-09-04). Confirm the `fc82707` run is green end to end (both jobs, including the Docker steps), then merge with a merge commit to preserve the curated history. | [PR #38](https://github.com/rlpatrao/agent-gov-sec/pull/38) |
| B-2 | P1 | Pin the enforcement image's dependencies | `requirements-proxy.txt` carries ranges, so image builds are not reproducible and `THIRD_PARTY_NOTICES.md` versions can lag a given image. A lock file installed by `deploy/Dockerfile.service` fixes reproducibility, notices accuracy, and CVE attribution together. | CHANGELOG known gaps |
| B-3 | P1 | Fresh ECR publish | The published image (`1.0.0-8791fd1-dirty`) predates the `galaxy_gov` rename, `framework_adapters/`, the dashboard, and the injected data source. After B-1, bump `deploy/VERSION`, run `scripts/publish_service_image.py --release` from a clean tree. | — |
| B-4 | P2 | SQLite persistence for the dashboard decision log | Decided 2026-09-10: the dashboard's runs table must survive service restarts, backed by SQLite (stdlib `sqlite3`, file path via env, e.g. `GOV_DASHBOARD_DB`). Ring semantics and the bounded size stay; counters rebuild from the table on start; unset env keeps today's in-memory behavior. The hash-chained ledger remains the audit record — the SQLite file is an operational view and must never be presented as evidence. Alternative considered and not chosen: reading recent entries back from the ledger. | — |
| B-5 | P2 | Identity control plane production readiness | Shared bearer token, `GOV_CONTROL_TRUST_HEADER` development shortcut, process-local write lock (single replica), no revocation propagation. Severities and sequencing are maintained in the plan document. | [agent-registration-plan.md](shared/agent-registration-plan.md) |
| B-6 | P2 | Publish the wheel to an index | `pip install galaxy-agentkit` does not resolve anywhere; docs state the intended command. Decide CodeArtifact (private) vs PyPI (public), wire a release step, and pin the ONBOARDING/agentkit install lines to reality. | — |
| B-7 | P2 | Registry resolver for the Azure functions and the A2A broker | `cloud_adapters/azure/infra/functions/` and the `a2a_broker` handlers still use the old inline registry lookup; the AWS LLM/data chokepoints moved to the TTL-cached `resolve_registry()` (policy store) in WS3. | CHANGELOG |
| B-8 | P2 | Wire SBOM and artifact signing into the release pipeline | `agent_sre` provides SBOM (N5) and Ed25519 signing (N6) as flag-gated runtime controls; neither runs against the wheel or the service image today. | CHANGELOG known gaps |
| B-9 | P3 | Base-image CVE cadence | ECR enhanced scanning: the actionable findings (`cryptography`, `sqlite3`) are addressed by B-2 plus a rebuild cadence; four criticals in `python:3.14-slim` OS packages (perl, glibc) await upstream fixes. Re-scan on each publish. | CHANGELOG known gaps |
| B-10 | P3 | Re-render the two generated diagrams | `docs/diagrams/src/*.mmd` sources are current; `delta-over-agentos.svg` and `framework-cloud-axes.svg` carry a text-level patch. Run `scripts/render_diagrams.sh` where puppeteer can launch Chrome. | CHANGELOG |
| B-11 | P3 | MCP path for the agentkit | The architecture shows an MCP workload (`mcp3`) inside a governed application; the MCP guard family exists flag-gated in-process, but `galaxy_agentkit` has no MCP wrapper. Design open. | — |
| B-12 | P3 | Extract `payload_agents/` to its own repository | The architecture places applications in their own repos via `galaxy init`. Blocked until a real application exists to carry the conformance-matrix role that `payload_agents/` serves in CI. | — |
