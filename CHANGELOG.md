# Changelog

All notable changes to the Galaxy governance platform are recorded here. The format
follows [Keep a Changelog](https://keepachangelog.com/); versions follow semantic
versioning of the platform wheel (`galaxy-governance`).

## [Unreleased]

### Added
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
