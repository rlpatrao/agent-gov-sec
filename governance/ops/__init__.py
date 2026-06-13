"""
governance.ops — operational (fleet-level) wrappers over ``agent_sre``.

Unlike ``governance.extensions`` (per-agent-call guards on the GuardPipeline
hooks), these are operational/reporting capabilities: SLO + error-budget
evaluation, per-agent cost attribution, eval harness, golden-trace replay,
adversarial red-team, accuracy declaration, SBOM, artifact signing, and the
certification gate. They are not invoked per model/tool call; the demo drives
them as standalone sections. Each helper returns a small report object so the
demo and tests can assert on the outcome without printing the firehose.

All are cloud-neutral and have no effect unless their ``GALAXY_OPS_*`` flag is
enabled (see ``governance.extensions.flags``).
"""
