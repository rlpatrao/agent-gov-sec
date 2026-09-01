# Packaging & delivery — how the platform reaches the agent team

This document describes how the Galaxy governance platform is packaged and delivered to the
team that builds payload agents, and how the developer/governance boundary is enforced both
at merge time and at runtime. It is the companion to
[`governance-authority.md`](governance-authority.md) (who controls what) and
[`adding-an-agent.md`](adding-an-agent.md) (how a developer builds an agent).

## Two artifacts, two owners

The platform ships as two artifacts with two different owners and lifecycles.

| Artifact | Owner | What it is | How it is consumed |
|---|---|---|---|
| **Galaxy agentkit** (`galaxy-agentkit` wheel) | platform team | The client-side package `galaxy_agentkit` (governed-agent wrapper, enforcement client) and the `galaxy` command line, whose `init` subcommand scaffolds a project, over the agnostic core, the guard/enforcement library, the A2A protocol, and the cloud adapters. Excludes `payload_agents`, tests, and docs. | The agent team installs it (`pip install "galaxy-agentkit[aws,langgraph]"`), imports it, and builds agents on top. See [`agentkit.md`](agentkit.md). |
| **Galaxy Enforcement Service** (container) | governance team | The out-of-process chokepoints (LLM / data / A2A). Ships only `governance/` + `core/` + the handlers. | Deployed in a governance-owned environment; agents *call* it and cannot modify it. |

The SDK is what a developer builds *with*; the enforcement service is what governs them at
runtime. They are versioned and released independently.

The SDK wheel ships **package data as well as code**: the guards read their rule files
(`prompt-injection.yaml`, the `galaxy-*.yaml` policies, `authz.cedar`, the data
classification catalogue, the per-cloud `egress.yaml`) from the installed package at
runtime. A wheel built without them imports and constructs cleanly but governs on the
upstream toolkit's *sample* rules, so the packaging is a control surface in its own
right. `galaxy_agentkit.check_install()` verifies the set, the guard raises rather than
falling back, and CI installs the wheel into a clean virtualenv outside the source tree
and asserts both.

## The boundary — enforced at merge time and at runtime

The developer/governance split is not a convention; it is enforced by four mechanisms (see
[`governance-authority.md`](governance-authority.md)):

| # | Mechanism | Where | Effect |
|---|---|---|---|
| 1 | CODEOWNERS | source, merge-time | the governing team approves every change to the control surface |
| 2 | Non-overridable floor | in-process (in the SDK) | per-agent config can tighten, never weaken |
| 3 | NHI-keyed policy registry | resolved per request | one authoritative posture, resolved — never request-supplied |
| 4 | Enforcement service | separate process / identity / environment | controls hold even if the agent runtime is hostile |

Mechanism 1 is the merge-time gate; mechanism 4 is its runtime counterpart. A developer can
neither merge a weakened control (CODEOWNERS) nor bypass one at runtime (the enforcement
service runs under an identity the developer cannot assume).

## For the agent team — consuming the SDK

The on-ramp is [`ONBOARDING.md`](../../ONBOARDING.md). In short:

```bash
pip install "galaxy-agentkit[langgraph]"      # add [aws] or [azure] for live runs
galaxy new-agent Payroll                        # scaffold config · prompt · test (floor-safe defaults)
# … implement tools + prompt, tighten the governance: block …
scripts/demo_agents.py --fake --extended        # full control matrix, offline, no cloud creds
```

The developer owns everything under `payload_agents/` except `payload_agents/config/`
(the `governance:` block and scopes are the governing team's to approve). They *request*
capabilities and data scopes; they cannot grant themselves more than the floor allows.

## For the governance team — running the enforcement service

The service is the runtime authority. Local development brings up an identical copy so the
agent team tests against what actually enforces in production (dev/prod parity):

```bash
docker compose -f deploy/docker-compose.yml up --build      # http://localhost:8080
curl localhost:8080/health
```

It exposes three routes — `POST /llm`, `POST /data`, `POST /a2a` — over the same handlers
the serverless deployments use ([`governance/remote/server.py`](../../governance/remote/server.py),
[`deploy/Dockerfile.service`](../../deploy/Dockerfile.service)).

For production, deploy the container in a **separate environment under a separate identity**
— ideally a separate cloud account (or at minimum a separate IAM/VNet boundary) the agent
team has no write access to. That separation is what makes mechanism 4 authoritative rather
than cooperative. On AWS the same handlers also run as Lambdas / AgentCore-managed infra; on
Azure they run as Functions behind API Management.

## Repository model — same-repo or separate-repo

Both are supported by the same packaging:

- **Same repo (monorepo).** The agent team works inside `payload_agents/`; CODEOWNERS +
  branch protection enforce the boundary. Lowest overhead; the team sees platform code but
  cannot merge changes to it. This is the current layout.
- **Separate repo.** The agent team's repo depends on the `galaxy-agentkit` wheel at a
  pinned version and imports the platform; they cannot edit platform internals at all, and
  upgrades are an explicit version bump. Strongest isolation; the wheel is already built to
  support this (it contains no `payload_agents`). `galaxy init <project>` generates the
  starting layout for such a repository.

## Release & CI

- **Versioning.** The wheel and the enforcement service version independently — the wheel
  from `version` in [`pyproject.toml`](../../pyproject.toml), the service image from
  [`deploy/VERSION`](../../deploy/VERSION) (see
  [Service image versioning](#service-image-versioning) below). Changes are recorded in
  [`CHANGELOG.md`](../../CHANGELOG.md).
- **CI** ([`.github/workflows/ci.yml`](../../.github/workflows/ci.yml)) has two gates: a
  **payload check** (tests + the offline conformance matrix — what an agent developer must
  pass) and a **governance gate** (full suite, the third-party notices staleness check, a
  wheel build verified to contain no `payload_agents` / tests / docs and to carry its
  license metadata, and a service image build verified to carry the notices and to report
  the version declared in `deploy/VERSION`). CI does not push: publishing is a deliberate
  act, described below.
- **Provenance.** Not yet wired into the release pipeline. `agent_sre` provides SBOM
  (N5) and Ed25519 artifact signing (N6) as flag-gated *runtime* controls over governed
  agents ([`extended-guardrails.md`](extended-guardrails.md)); neither currently runs
  against the wheel or the service image. Adding them is open work.

## Licensing and attribution

The platform is licensed under Apache-2.0 ([`LICENSE`](../../LICENSE), with the
copyright notice in [`NOTICE`](../../NOTICE)). Both artifacts carry their license
metadata: the wheel declares `License-Expression: Apache-2.0` and bundles `LICENSE`,
`NOTICE`, and `THIRD_PARTY_NOTICES.md` under `dist-info/licenses/`; the service image
copies the same three files to `/app`.

The two artifacts carry third-party code differently, and the attribution obligation
follows that difference:

| Artifact | How dependencies travel | Obligation |
|---|---|---|
| Platform wheel | declared, fetched from the index by pip | none by redistribution; the closure is recorded for auditability |
| Service image | installed into the image | the image redistributes them and carries their license texts |

[`THIRD_PARTY_NOTICES.md`](../../THIRD_PARTY_NOTICES.md) covers both sets and is
generated, not hand-written:

```bash
pip install -e '.[aws]'
python scripts/gen_third_party_notices.py            # rewrite
python scripts/gen_third_party_notices.py --check    # CI staleness gate
```

The governance gate runs `--check`, so a dependency change that is not reflected in the
notices fails CI. One limitation to be aware of: the versions recorded are those of the
generating environment, and because `requirements-proxy.txt` is unpinned, an image build
resolves transitive dependencies independently and can land on different versions. The
package list — what attribution actually depends on — is unaffected. Pinning the service
image's dependencies would make the version column exact and make image builds
reproducible; it is not done yet. The three toolkit packages (`agent-os-kernel`, `agent-sre`,
`agentmesh-platform`) are MIT, Copyright Microsoft Corporation, and are upper-bounded
(`>=3.7.0,<4`) so a rebuild cannot pull a major version whose license has not been
reviewed.

## Service image versioning

The enforcement service is the runtime authority. "Which build is enforcing?" therefore
has to be answerable at any moment, which is what this scheme is for.

### Three identifiers, three jobs

| Identifier | Example | Set by | Use it for |
|---|---|---|---|
| Declared version | `1.0.0` | [`deploy/VERSION`](../../deploy/VERSION), by hand | what humans discuss, what the CHANGELOG records, what an approval refers to |
| Per-commit tag | `1.0.0-8791fd1` | the publish script, from git | tracing a running container back to a commit |
| Digest | `sha256:7fb1da…` | the registry, on push | what deployments pin — the only identifier that cannot be moved |

`deploy/VERSION` is the single source of truth. It is a plain semver string, edited in a
reviewed commit, and it is governance-owned through the `/deploy/` rule in
[CODEOWNERS](../../.github/CODEOWNERS) — the same mechanism that protects the floor and
the policy registry. Nothing derives it from a branch name, a build number, or a
timestamp, because a version that changes without a review is not a version anyone can
be held to.

The version is stamped into the image at build time and reaches the runtime three ways,
which CI asserts are consistent: the `org.opencontainers.image.version` OCI label, the
`GALAXY_SERVICE_VERSION` environment variable, and the service's own report:

```console
$ curl -s localhost:8080/health
{"status":"ok","service":"galaxy-enforcement","version":"1.0.0","revision":"8791fd1",
 "control_plane":"disabled"}
```

An image that was not built through the publish path reports `0.0.0-dev`, so an
unversioned build is visible as one rather than passing for a release.

### When to bump what

Ordinary semver asks whether the API changed. For a component whose job is to allow and
deny, the more useful question is whether its **decisions** changed — a caller that
passed yesterday and is denied today has experienced a breaking change even though no
signature moved.

| Bump | Meaning for the enforcement service | Examples |
|---|---|---|
| MAJOR | traffic that previously passed can now be denied, or a route/request contract changes | a control moves from off to default-on; a guard's threshold tightens; `/data` requires a field it did not before |
| MINOR | new capability that cannot deny anything that previously passed | a new control added default-off; a new route; a new field in a response |
| PATCH | no change to any decision | a crash fix, a log message, a dependency bump with no behavioural change |

The asymmetry is deliberate: loosening a control is a MINOR change to the *service*, but
it is a governance event that needs the same review as a MAJOR one. Record it in the
CHANGELOG either way.

### Publishing

[`scripts/publish_service_image.py`](../../scripts/publish_service_image.py) is the only
supported path. It reads `deploy/VERSION`, resolves the git revision, builds with the
version stamped in, ensures the ECR repository exists, pushes, and prints the digest.

```bash
python scripts/publish_service_image.py --dry-run          # plan only
python scripts/publish_service_image.py                    # dev build: pushes 1.0.0-<sha>
python scripts/publish_service_image.py --release          # also pushes the bare 1.0.0
```

Four rules are enforced by the script rather than by convention:

1. **A dev build never claims the version.** Without `--release` only
   `<version>-<sha>` is pushed. A dirty tree appends `-dirty` to the tag.
2. **A release requires a clean tree.** The bare `<version>` tag must correspond to a
   commit someone can check out.
3. **A released tag is never overwritten.** The ECR repository is created with
   `IMMUTABLE` tags, and the script fails early if a tag already exists rather than
   discovering it at push time. Re-releasing means bumping `deploy/VERSION`.
4. **There is no `latest`.** A moving tag on the component that enforces the controls
   would make the question this whole section exists to answer unanswerable.

The image is built for one explicit platform, `linux/amd64` by default; pass
`--platform linux/arm64` for Graviton tasks. This is explicit rather than inherited from
the builder because an arm64 workstation would otherwise produce an image that fails to
start on an x86 task, and that failure would surface at deploy time. The build also
disables BuildKit attestations, so a push produces one manifest and one digest — see the
lifecycle-rule note in the Terraform for why that coupling matters.

Deployments should pin the digest the script prints, not a tag:

```
<account>.dkr.ecr.<region>.amazonaws.com/galaxy-rp-gov-enforcement@sha256:7fb1da…
```

The repository itself is declared in
[`cloud_adapters/aws/infra/main.tf`](../../cloud_adapters/aws/infra/main.tf)
(`aws_ecr_repository.enforcement`, output `enforcement_image_repo`) with scan-on-push,
AES256 encryption, and a lifecycle rule that expires untagged images after 14 days.
Terraform is the declarative path; `--create-repo` exists for accounts where the
governing team publishes without owning the Terraform state.

### Running the image

[`cloud_adapters/aws/infra/enforcement_service.tf`](../../cloud_adapters/aws/infra/enforcement_service.tf)
is the runtime for a published image: an ECS Fargate service behind an internal
application load balancer, with a task definition at 0.5 vCPU / 1024 MB, CloudWatch logs
retained for 30 days, and security groups that admit the load balancer only from inside
the VPC and the tasks only from the load balancer. The load balancer is internal because
the authority is called from within the deployment and should not carry an
internet-facing surface; TLS termination with an ACM certificate is the production
upgrade, alongside a customer-owned VPC and, ideally, a separate governance account. The
file is inert by default — every resource is gated on a switch — so it does not change an
existing apply of the module until it is turned on.

| Variable | Default | Purpose |
|---|---|---|
| `deploy_enforcement_service` | `false` | Opt in to the ECS cluster, service, load balancer, and IAM roles |
| `enforcement_image` | `""` | The image URI to run, pinned by digest (the value the publish script prints) |

`enforcement_desired_count` (default `2`) sets how many tasks run. The
`enforcement_service_url` output is the base URL agents point at — the endpoint
`galaxy_agentkit` resolves from `GALAXY_ENFORCEMENT_ENDPOINT`.

```bash
terraform apply \
  -var="deploy_enforcement_service=true" \
  -var="enforcement_image=<account>.dkr.ecr.<region>.amazonaws.com/galaxy-rp-gov-enforcement@sha256:7fb1da…"
```

### Behind a TLS-terminating proxy

Networks that intercept TLS present a private root the base image does not trust, which
breaks the in-container `pip install`. Pass the root and it is used for that layer only,
via a BuildKit secret, and is never written into the image:

```bash
python scripts/publish_service_image.py --pip-ca /path/to/corporate-root.pem
```

`docker compose -f deploy/docker-compose.yml up --build` has no equivalent switch, so on
such a network build the image once with the script (or `docker build --secret
id=pip_ca,src=…`) and let compose reuse it.

## Python versions

The SDK and the enforcement service target Python 3.14: `requires-python = ">=3.14"`,
and [`deploy/Dockerfile.service`](../../deploy/Dockerfile.service) builds on
`python:3.14-slim`.

Three deployment targets stay on Python 3.12 because their runtime is chosen by the
cloud provider, not by this repository:

| Target | Pin | Set by |
|---|---|---|
| Bedrock proxy Lambda image | `public.ecr.aws/lambda/python:3.12` | AWS Lambda base image tag |
| AgentCore runtimes and interceptor Lambdas | `PYTHON_3_12` / `python3.12` | AgentCore and Lambda runtime identifiers |
| Azure Function App | `Python\|3.12` | App Service `linuxFxVersion` |

Those images and runtimes copy platform source rather than installing the wheel, so
`requires-python` never gates them. The platform code carries no 3.13-or-later syntax,
which is what makes running it on 3.12 safe. Moving them to 3.14 is gated on the
provider publishing the runtime, and should be done together with a check that the
toolkit still resolves for the target interpreter.

## Environment prerequisite

The platform pins `agent-sre>=3.7.0` and `cedarpy>=4`. Provision the venv from
`requirements.txt` / the `pyproject` extras; an older `agent-sre` (for example 3.2.2) drops
the `CostGuard.check_and_charge` API and the Cedar authorizer surface the extended modules
use, and their tests will fail until the pinned versions are installed.
