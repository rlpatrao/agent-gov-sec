# GCP stack (placeholder)

Placeholder for the GCP documentation stack. The AWS stack (`../aws/`) is the populated
reference; this stack will mirror its structure once the GCP binding is documented.

The platform's governance core is cloud-neutral (see `../shared/`). The GCP binding
exists in code under `cloud_adapters/gcp/` — Workload Identity service accounts,
Secret Manager, a Vertex/Gemini egress path, and Cloud Trace tracing — but is not yet
written up here.

## Planned contents

| File | Status |
|---|---|
| `README.md` | this placeholder |
| `deck.md` | to be authored (mirror of `../aws/deck.md`, GCP bindings) |
| `narrative.md` | to be authored |
| `architecture.md` | to be authored — Workload Identity · Secret Manager · Vertex · Cloud Trace |
| `diagrams/` | to be authored — GCP infrastructure topology |

For the cloud-neutral platform reference (guard catalogue, delta classification,
governance authority, standards crosswalk), see `../shared/`.
