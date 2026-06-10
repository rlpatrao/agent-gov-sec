# Test strategy

The WS1 refactor split the platform into a cloud-/framework-agnostic core and
per-cloud adapters. The tests mirror that split into **tiers**, so the bulk of
coverage runs anywhere (no Azure SDK, no Microsoft Agent Framework) and the
framework-coupled tests skip cleanly until their optional deps are installed.

```
pytest tests/          # tier 1 always runs; tiers 2+ skip if deps absent
```

## Tier 1 — Agnostic (always runs, no cloud/MAF)

The core invariant of the refactor is that `core/`, `governance/`, and `a2a/`
import no `azure.*` and no `agent_framework`. These tests exercise that surface
directly and run in any environment:

| Module | Covers |
|---|---|
| `test_provider_factory.py` | Provider selection by `CLOUD_PROVIDER`, azure default, explicit override, unknown→`ValueError`, azure accessors resolve, **aws/gcp skeletons raise `NotImplementedError`** (the WS5/WS6 contract), no-cloud-SDK-on-import |
| `test_secrets.py` | `EnvVarSecretProvider` read/cache/invalidate/missing; satisfies the `SecretProvider` Protocol |
| `test_gateway.py` | `AzureLLMGateway.resolve()` APIM-vs-direct mode, endpoint/key/header contract (driven by a fake `SecretProvider`, so no Azure SDK) |
| `test_nhi_registry.py` | Registry get/unknown/empty; `AgentIdentity`; `get_credential()` degrades to `None` without a cloud SDK |
| `test_egress.py` | Egress allow-list via explicit path **and** via the provider factory; default-deny when absent |
| `test_a2a_envelope.py` | Typed A2A envelopes + dispatch |
| `test_config.py`, `test_repo_classifier.py` | Pydantic config loading; signal-based repo classification |

## Tier 2 — Azure + MAF integration (skips without `.[azure]`)

These require the Microsoft Agent Framework (and, for a live run, the Azure
SDK). Each module starts with `pytest.importorskip("agent_framework", ...)`, so
on a bare checkout they **skip with an actionable reason** instead of erroring
at collection:

| Module | Covers | Run it with |
|---|---|---|
| `test_guards.py` | The 3 MAF `AgentMiddleware` guard wrappers (`adapters/azure/maf/guards/`) against a context stub | `pip install '.[azure]'` |
| `test_analyzer_agent.py` | `AnalyzerHandler` logic against a fake agent (currently MAF-coupled only at *import*) | `pip install '.[azure]'` |

## Tier 3 — Per-cloud adapter contract (WS5 / WS6, not yet built)

When `adapters/aws/` and `adapters/gcp/` are implemented, each gets a parallel
suite mirroring the Azure adapter against **mocked SDKs** (moto for AWS, the GCP
client test doubles for GCP): identity, secrets, tracing, audit, gateway,
egress. The factory-contract test (`test_provider_factory.py`) already asserts
they resolve; those modules will replace the `NotImplementedError` assertions
with real behavior as the impls land.

---

## What "more appropriate" replaced, and what's still owed

**Done in this pass:**
- The agnostic *egress* test was extracted out of `test_guards.py` (it never
  needed MAF) into `test_egress.py`.
- `test_guards.py` and `test_analyzer_agent.py` now `importorskip` MAF, so the
  suite is green on a bare checkout (was: 2 collection errors).
- New Tier-1 tests cover the WS1 seam — the part the MAF-dependent tests could
  never reach: factory selection, the lazy/`NotImplementedError` adapter
  contract, the gateway egress decision, the secret provider, and NHI credential
  delegation.

**Still owed (tracked, not done here):**
1. **Defer the MAF import in `payload_agents/analyzer_agent.py`** (and the chat
   client in `_base.py`) so `AnalyzerHandler`'s validation/classification/mapping
   logic — which already runs against a fake agent — can be tested in Tier 1
   without MAF. Today it skips only because the module pulls `agent_framework`
   at import. This is the highest-value follow-up: it moves ~13 handler tests
   from "skipped" to "always runs".
2. **A thin MAF stack smoke test** runnable in CI with `.[azure]` installed:
   build the Analyzer via `build_agent(...)`, assert the 7-guard middleware list
   assembles and a stubbed `agent.run()` fires the guards end-to-end.
3. **Tier-3 mocked-SDK suites** for `adapters/aws` and `adapters/gcp` (with WS5/WS6).
4. **A grep/import CI gate** asserting the agnostic invariant
   (`grep -rE "^\s*(from|import) (azure|agent_framework)" core governance a2a`
   returns nothing) so the boundary can't silently regress.
