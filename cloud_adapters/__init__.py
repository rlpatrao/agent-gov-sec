"""
cloud_adapters — per-cloud platform bindings behind the agnostic ``core.interfaces`` seam.

One sub-package per cloud, each exposing identity, secrets, the LLM-egress gateway,
the hash-chained audit backend, tracing, and egress allow-list for that cloud:

  cloud_adapters/azure/   Azure bindings (+ MAF framework glue under azure/maf/)
  cloud_adapters/aws/     AWS bindings (IAM / Bedrock gateway / DynamoDB ledger)
  cloud_adapters/gcp/     GCP bindings (SA / Vertex·Gemini / BigQuery)
  cloud_adapters/local/   cloud-neutral (env secrets, in-memory ledger, no egress)

Selected at runtime by ``core.provider_factory.get_provider()`` (keyed on the
``CLOUD_PROVIDER`` env var); each package exposes a module-level ``PROVIDER``.
This axis is orthogonal to the agent-framework axis (``payload_agents/<framework>``).
``core`` and ``governance`` do not import this package or any cloud SDK.
"""
