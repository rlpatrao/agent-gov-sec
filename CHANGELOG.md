# Changelog

All notable changes to the Galaxy governance platform are recorded here. The format
follows [Keep a Changelog](https://keepachangelog.com/); versions follow semantic
versioning of the platform wheel (`galaxy-agentkit`).

## [Unreleased]

### Added
- **`docs/BACKLOG.md`** — the tracked list of open work. Previously open items were
  split across CHANGELOG known-gaps entries, `agent-registration-plan.md`, and
  conversation history; this consolidates them (twelve items, prioritized, with
  acceptance criteria). Includes the 2026-09-10 decision to persist the dashboard
  decision log in SQLite (B-4), with the hash-chained ledger remaining the audit
  record.
- **The Governance Dashboard** — `GET /dashboard` on the enforcement service. A
  self-contained HTML page (inline CSS, no JavaScript, no external requests, 15-second
  meta refresh) with four sections: the service header (version, revision, uptime, the
  loaded policy registry), recent agent runs, guardrail decisions aggregated per control
  code and per chokepoint route, and the full control-to-standards crosswalk. It is the
  read path for the centralised compliance tracker; the hash-chained trace ledger
  (`core/trace_ledger.py`) remains the underlying tamper-evident record, and the page says
  so. There was previously no way to see what the service had decided other than reading
  its logs.
- **`governance/remote/decision_log.py`** — the bounded, thread-safe, in-memory decision
  buffer the dashboard reads. Capacity is set by `GOV_DASHBOARD_DECISION_BUFFER` (default
  1000); aggregate counters are held separately from the ring, so per-control and
  per-route totals cover the whole process lifetime even after the run list has wrapped.
  This is observability, not audit: it is not durable, not hash-chained, and resets on
  restart. It retains request metadata only — timestamp, route, agent type, NHI id,
  outcome, control code, HTTP status — and no prompt, model response, tool argument, data
  row, or request or response body of any kind.
- **`scripts/gen_crosswalk.py`** compiles `docs/shared/standards-crosswalk.md` into
  `governance/remote/_crosswalk.py` (44 controls). The service image copies `governance/`,
  `core/` and `cloud_adapters/` but not `docs/`, so the dashboard imports the compiled
  module rather than parsing markdown at runtime. `--check` is the staleness gate, mirroring
  `scripts/gen_third_party_notices.py`.
- **`galaxy_agentkit` — the client-side package.** What an agent team installs and
  imports. `govern()` returns one handle carrying the agent's identity, a client for the
  enforcement service (`/llm`, `/data`, `/a2a`, `/health`), and the in-process
  `GuardPipeline` on request. Identity and endpoint are resolved from the environment
  (`GALAXY_AGENT_TYPE`, `GALAXY_NHI_ID`, `GALAXY_ENFORCEMENT_ENDPOINT`, `GALAXY_MODE`)
  and validated on load, so an agent cannot assert its own identity from code. There was
  previously no client path to the enforcement service at all: `GOV_LLM_ENDPOINT` and
  `GOV_DATA_ENDPOINT` were documented in `deploy/docker-compose.yml` but nothing read
  them. The distribution is renamed `galaxy-governance` → `galaxy-agentkit` so the
  install name matches the import name.
- **`galaxy init <project>`** scaffolds a complete governed-agent project —
  `.env.example`, the floor-safe `governance:` request config, the data-classification
  catalogue, prompt, a working governed entry point, and tests.
- **The SDK wheel now ships its package data.** `[tool.setuptools.package-data]` declares
  the guard rule files, the `galaxy-*.yaml` policies, `authz.cedar`, the data
  classification catalogue, and the per-cloud `egress.yaml`. Previously the wheel
  contained **no** non-Python files, so an installed client resolved none of them.
  Declared explicitly rather than by a blanket glob so `cloud_adapters/*/infra` —
  an importable package holding Terraform and its state — cannot be swept in.
- **Versioned enforcement service image, published to ECR.** `deploy/VERSION` is the
  single source of truth for the service image version, governance-owned via the
  `/deploy/` CODEOWNERS rule. The version is stamped into the image as an OCI label and
  as `GALAXY_SERVICE_VERSION`, and `GET /health` now reports `version` and `revision` so
  an operator can identify the running build.
  `scripts/publish_service_image.py` builds, tags, and pushes: every build gets an
  immutable `<version>-<sha>` tag, the bare `<version>` tag requires `--release` from a
  clean tree, and there is no `latest` tag. The ECR repository is declared in Terraform
  (`aws_ecr_repository.enforcement`, output `enforcement_image_repo`) with immutable
  tags, scan-on-push, AES256 encryption, and a 14-day untagged-image expiry. The
  governance gate asserts the label, the environment variable, and the service's own
  report all agree with `deploy/VERSION`. Versioning policy — including what MAJOR means
  for a component that allows and denies — is in `docs/shared/PACKAGING.md`.
- **Terraform for the enforcement service runtime.**
  `cloud_adapters/aws/infra/enforcement_service.tf` runs the published image as a scaled
  ECS Fargate service (0.5 vCPU / 1024 MB, `linux/amd64`, two tasks by default) behind an
  internal application load balancer that health-checks `GET /health`, with CloudWatch
  logs retained for 30 days, its own execution and task roles, and security groups that
  admit the load balancer only from within the VPC and the tasks only from the load
  balancer. Previously the image had a registry to be published to but nothing that ran
  it. The file is opt-in — every resource is gated on `deploy_enforcement_service`
  (default `false`) — and takes the image URI from `enforcement_image`, which is meant to
  be digest-pinned to the value `scripts/publish_service_image.py` prints. Output
  `enforcement_service_url` is the endpoint agents point at. The identity control plane
  is left disabled (`GOV_CONTROL_TOKEN` unset), which is the production default.
- **Corporate TLS support in the service build.** `deploy/Dockerfile.service` accepts a
  private TLS root as a BuildKit secret (`--secret id=pip_ca`), used only for the
  dependency-install layer and never written into the image, for networks that terminate
  TLS with a private CA. `--pip-ca` on the publish script passes it through.
- **Licensing and third-party attribution.** The platform is licensed under Apache-2.0
  (`LICENSE`, `NOTICE`); the wheel declares `License-Expression: Apache-2.0` and bundles
  `LICENSE`, `NOTICE`, and `THIRD_PARTY_NOTICES.md`, and the enforcement image copies the
  same three files. `scripts/gen_third_party_notices.py` generates the notices from the
  resolved dependency closure of both artifacts and runs as a CI staleness gate
  (`--check`). The three toolkit packages are upper-bounded (`>=3.7.0,<4`) so a rebuild
  cannot pull an unreviewed major version. See `docs/shared/PACKAGING.md`.
- **Identity Registrar (mechanism 5).** `governance/remote/registrar.py` records
  `agent_type → cloud principal` bindings in an authority-side store
  (`governance/remote/identity_store.py`), served by a control plane on the authority
  (`POST /enroll`, `GET /identity`, `GET /registry/digest`) that is disabled unless
  `GOV_CONTROL_TOKEN` is set. `core/nhi_registry.py` resolves the binding from the
  authority when `GOV_AUTHORITY_ENDPOINT` is configured and no longer consults the
  `NHI_CLIENT_ID_<TYPE>` env bridge in that case, so an agent can no longer assert its
  own identity. Enrollment binds identity only and grants no capability: an enrolled
  agent without an approved control policy reports `pending_policy` and is still denied
  with `403 no_governance_policy`.
- **Read-only principal verification.** `cloud_adapters/aws/principal_verify.py` confirms
  a claimed IAM role exists using `sts:GetCallerIdentity` and `iam:GetRole` only. No code
  path creates or mutates an identity; roles are provisioned by Terraform or by the
  developer's own AWS SSO session.
- **Developer commands.** `galaxy enroll <Type>` (bind an identity under an SSO login),
  `galaxy export-registry` (emit the policy registry plus derived provisioning inputs,
  with `--check` as a CI staleness gate), and `galaxy verify [<Type>]` (report which of
  the two keys — identity, policy — are turned).
- **Centralized policy store.** The policy registry is now one versioned object every
  enforcement tier reads, instead of a copy per deployment. New in
  `governance/shared/policy_registry.py`: `resolve_registry()`, the single loading
  contract, with precedence `GOV_POLICY_REGISTRY` (inline JSON) → `GOV_POLICY_REGISTRY_URI`
  (`s3://bucket/key`) → `GOV_POLICY_REGISTRY_PATH` (a file baked into the image), and a
  `GOV_POLICY_REGISTRY_TTL_SECONDS` cache (default 300). The enforcement authority and the
  LLM and data chokepoints call it instead of each reimplementing the environment lookup.
  A refresh that fails past the TTL keeps serving the last good document and logs its age
  and the S3 `VersionId` it came from; when no document has ever loaded, resolution raises
  and the reader denies. `galaxy export-registry --publish` uploads the exported registry
  to the store and prints the returned `VersionId` and the sha256 digest of the bytes it
  wrote. The bucket is declared in `cloud_adapters/aws/infra/policy_store.tf` — versioning
  enabled, public access blocked, AES256 — behind the opt-in `deploy_policy_store`
  variable, with the bucket name and the suggested `GOV_POLICY_REGISTRY_URI` as outputs.

### Known gaps
- **The service image's dependencies are not pinned.** `requirements-proxy.txt` carries
  ranges, so each image build resolves transitive dependencies afresh: the pushed image
  contains `cryptography` 48.0.1 while `THIRD_PARTY_NOTICES.md`, generated from the
  development venv, records 46.0.7. The package list is the same either way, so
  attribution is complete, but the version column can lag a given image and two builds of
  the same commit are not byte-identical. A lock file installed by
  `deploy/Dockerfile.service` would fix reproducibility, notices accuracy, and CVE
  attribution together.
- **Base-image CVEs in the enforcement image.** ECR enhanced scanning on the first push
  reported 4 CRITICAL and 10 HIGH findings. All four criticals are `python:3.14-slim` OS
  packages (perl, glibc) with no fix available upstream yet; the actionable ones are
  `cryptography` (fixed in 49/50) and `sqlite3`, which pinning plus a rebuild cadence
  would address.
- **Provenance is not wired.** SBOM (N5) and artifact signing (N6) exist as flag-gated
  runtime controls in `agent_sre`; neither runs against the wheel or the service image.
- The identity control plane is **not production-ready**. `GET /identity` and
  `POST /enroll` share one bearer token, and `GOV_CONTROL_TRUST_HEADER=1` (set by
  `deploy/docker-compose.yml` for local development) makes the enrolling identity a
  forgeable request header. The store's write lock is process-local, so the authority
  is single-replica only. Enrollment does not inspect the role's trust policy, is not
  written to the tamper-evident ledger, and revocation does not reach an agent that
  already resolved its NHI. Full list with severities and sequencing in
  `docs/shared/agent-registration-plan.md`.

### Added (refactor hygiene)
- **Documentation-reference gate.** `scripts/check_doc_refs.py` verifies every path-like
  and module reference in tracked Markdown against the tree (1,200+ references across 40
  documents), with `scripts/docref-allow.txt` for deliberate forward references such as
  scaffold output. Runs in the governance gate in CI, as a Claude Code Stop hook
  (`.claude/settings.json`), and as a PR-template checklist item. The first run found and
  fixed 60+ stale references left by earlier reorganizations.
- **`docs/archive/` (tracked).** Retired documents move here rather than being deleted or
  placed in the local-only `archive/`, so history stays visible in every checkout. Policy
  in `docs/archive/README.md`; the reference checker exempts the directory.

### Changed
- **Documentation content pass after the reorg week.** The reference checker no
  longer lets a `pkg/removed.py` reference pass when a `pkg/removed/` directory
  exists (file-suffix references must resolve to files), which surfaced eight
  stale `payload_agents/config.py` links — all repointed to
  `galaxy_gov/agent_config.py`. Remaining old-name labels fixed in the deck/docx
  generator strings, the mermaid diagram sources, and — as a text-level patch —
  the four rendered/hand-authored SVGs (a puppeteer re-render of the two
  generated ones is pending an environment where Chrome launches;
  `scripts/render_diagrams.sh`). `agent-engine.md`, `agentkit.md`, and
  `.env.example` document the injection variables (`GALAXY_AGENT_PACKAGE`,
  `GALAXY_AGENT_CONFIG_DIR`, `GOV_DATA_SOURCE_MODULE`). Archive review: no
  tracked document describes removed functionality, so `docs/archive/` stays
  empty; `agent-registration-plan.md` remains the live tracker for the open
  identity-control-plane gaps.
- **Platform code no longer imports the demo application — the dependency arrow
  is application → platform, enforced by test.** Remaining inversions:
  `core.framework_factory` defaults to `framework_adapters.<name>` and takes the
  application's builder package as an argument (`package=` /
  `GALAXY_FRAMEWORK_PACKAGE_<NAME>`); the GCP Agent Engine app takes
  `agent_package=` / `GALAXY_AGENT_PACKAGE` and discovers `build_<name>_agent`
  callables instead of importing the three personas; both data-proxy chokepoints
  take `GOV_DATA_SOURCE_MODULE` and answer `501 data_source_unconfigured` when
  unset instead of importing the demo fixtures (which the enforcement container
  never shipped); `default_config_dir()` resolves `GALAXY_AGENT_CONFIG_DIR` only,
  and `payload_agents/__init__` registers its own config directory on import
  (`GOV_AGENT_CONFIG_DIR` is folded into the one variable). `galaxy
  export-registry` gains `--config-dir` and fails loudly instead of deriving an
  empty registry. A boundary test asserts no platform package imports
  `payload_agents`. Deliberate exception: `galaxy new-agent` still writes into
  `payload_agents/` — it is the monorepo generator and that is its output path.
- **The framework axis is now a shipped package: `framework_adapters/`.** The
  glue binding each agent framework to the `GuardPipeline` — the LangChain
  `GalaxyGuardMiddleware` + `build_langgraph_agent` (with the gateway-backed and
  scripted chat models), the Pydantic AI `GovernedModel`, the provider-native raw
  loop, and the MAF middleware stack (moved out of `cloud_adapters/azure/`, which
  had a framework filed under a cloud) — moves out of the demo package into a
  top-level `framework_adapters/`, the counterpart of `cloud_adapters/`. It ships
  in the wheel, so `pip install "galaxy-agentkit[langgraph]"` now delivers the
  middleware rather than only its dependencies. `payload_agents/` keeps only the
  demo personas, which compose the adapters. Governance-owned via CODEOWNERS.
- **Agent-config schema moved to the platform: `galaxy_gov/agent_config.py`.**
  `AgentConfigModel`, `GovernanceConfig`, and the floor-clamped loaders lived in
  `payload_agents/config.py` — platform mechanism in demo code, and a
  platform-to-demo import from `galaxy_gov` (floor, policy_export, registrar,
  scaffold). The YAML documents stay with the agents; the default directory
  resolves via `GALAXY_AGENT_CONFIG_DIR`, falling back to the in-tree
  `payload_agents/config/`. The MAF-era `payload_agents/_base.py` factory,
  imported by nothing since the reorg, is removed (git history retains it).
- **`a2a/` folded into `core/a2a/`.** The agent-to-agent protocol (envelope + audited
  dispatcher) is seam code used by both sides, so it now lives inside the agnostic core
  rather than as a top-level package. Imports move from `a2a.…` to `core.a2a.…`; the
  standalone packaging glob is dropped. A side effect worth noting: the enforcement
  containers COPY `core/` but never copied `a2a/`, so the protocol modules now actually
  ship in the images.
- **Package renamed: `governance/` → `galaxy_gov/`.** The authority-side package now
  matches the architecture's naming (the `Galaxy_gov` containers). Import paths,
  packaging globs, container COPY paths, CODEOWNERS rules, and documentation move with
  it. Unchanged on purpose: the `governance:` block key in agent configs, telemetry
  attribute names (`governance.agent_id`, …), AWS resource names (`galaxy-governance-gw`,
  the `galaxy_governance` Cedar engine), and historical CHANGELOG entries.
- **One command line.** The two scaffolders are consolidated into the `galaxy` console
  script. `galaxy init <project>` replaces `galaxy-agentkit init <project>` and sits
  alongside `new-agent`, `enroll`, `verify`, and `export-registry`; the generated project
  is unchanged. The `galaxy-agentkit` console script and `galaxy_agentkit/scaffold.py` are
  removed. The distribution is still named `galaxy-agentkit` and the import path is still
  `galaxy_agentkit`; only the second entry point is gone.
- **Guards fail closed on missing configuration instead of degrading.**
  `_build_injection_detector()` raises `GovernanceConfigError` when
  `prompt-injection.yaml` is absent, and the Azure MAF guard now defaults to the packaged
  rules rather than to `None`. Both previously constructed `PromptInjectionDetector(None)`,
  which falls back to the upstream toolkit's **sample** rules and keeps reporting success
  — so a packaging mistake silently downgraded the control while every guard still looked
  green. This is what made the missing package data invisible.
- **Python 3.14 throughout the components this repo controls.** The documented
  prerequisite is now 3.14 only (README, AWS and Azure user guides), and the enforcement
  service image builds on `python:3.14-slim` (was 3.12). The Lambda base image, the
  AgentCore runtimes, and the Azure Function App stay on 3.12 — those runtimes are set by
  the cloud provider; they copy platform source rather than installing the wheel, so
  `requires-python` does not gate them.
- **One source of truth for which agents exist.** `governance.policy_export` now derives
  agent types from `payload_agents/config/*.yaml` via `discover_agent_types()` instead of
  the hand-maintained `KNOWN_AGENT_TYPES` tuple. That tuple was consumed by the registry
  export, the Terraform `agent_types` variable, AgentCore provisioning (workload
  identities, runtimes, Cedar policies), and a separate hardcoded list in
  `payload_agents/__init__.py` — none of which was checked against the filesystem, so a
  scaffolded agent was silently absent from all of them. `KNOWN_AGENT_TYPES` is retained
  as a derived value for compatibility. Verified to produce a byte-identical
  `agent-controls.json`.
- **Import-boundary rule made precise.** `tests/test_governance_tiers.py` previously
  forbade `governance/remote/` from importing itself, which blocked any internal structure
  in that tier. It now permits `governance.shared` and `governance.remote` and explicitly
  forbids the build-time producer (`governance.policy_export`) and `governance.inprocess`,
  which is what the rule was protecting.

- **Platform packaging.** The platform now builds as a versioned wheel
  (`galaxy-governance`) via a declared build system; the wheel ships the agnostic core,
  the guard/enforcement library, the A2A protocol, and the cloud adapters, and excludes
  `payload_agents`, tests, and docs.
- **Enforcement service (out-of-process, mechanism 4).** `governance/remote/server.py`
  exposes the LLM / data / A2A chokepoints over HTTP; `deploy/Dockerfile.service` and
  `deploy/docker-compose.yml` run it as a standalone, governance-owned container with a
  local hash-chain ledger for dev/prod parity.
- **Developer on-ramp.** `galaxy new-agent <Type>` scaffolds a governed agent (config,
  prompt, test) with a floor-satisfying default posture; `ONBOARDING.md` and a pull-request
  template guide the payload-agent team.
- **CI.** A payload check (tests + offline conformance matrix) and a governance gate (full
  matrix + wheel build).
- **Docs.** `docs/shared/PACKAGING.md` — how the platform is delivered to the payload team
  and how the governance boundary is enforced at merge time and at runtime.

### Fixed
- **CODEOWNERS boundary** repaired: the stale `/governance/floor.py` and
  `/docs/governance-authority.md` paths now point at their post-reorg locations, and the
  new control surfaces (Azure infra, the policy registry, the data-classification
  catalogue, the enforcement service) are covered.
- `payload_agents/_base.py` referenced an undefined `endpoint` in its structured log and
  hard-typed an Azure-specific audit backend; both corrected.
