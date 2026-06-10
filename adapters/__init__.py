"""
adapters — cloud- and framework-specific bindings behind the agnostic
``core.interfaces`` seam.

  adapters/azure/  Azure cloud bindings + MAF framework glue (the only fully
                   implemented provider today; the platform's current binding)
  adapters/aws/    AWS bindings — skeleton, WS5
  adapters/gcp/    GCP bindings — skeleton, WS6

Resolve a provider via ``core.provider_factory.get_provider()`` (by
``CLOUD_PROVIDER``); each package exposes a module-level ``PROVIDER``.
"""
