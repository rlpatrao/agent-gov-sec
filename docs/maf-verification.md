# Phase A.1 — Microsoft Agent Framework verification

**Verified on:** 2026-04-24 in throwaway venv `/tmp/maf-probe` (Python 3.13.13).

## Install

```bash
uv pip install agent-framework
# → Installs: agent-framework 1.2.0, agent-framework-core, plus 25+ provider sub-packages
```

Pulls through the corporate proxy cleanly (unlike Docker Hub). MIT licensed.

## Top-level API

`agent_framework.__init__` is empty; imports must target submodules directly.

| Symbol | Module | Role |
|---|---|---|
| `Agent` | `agent_framework._agents` | Core agent class (plan assumed `ChatAgent`; actual name is `Agent`) |
| `BaseAgent`, `RawAgent` | `agent_framework._agents` | Lower-level building blocks |
| `AgentMiddleware` | `agent_framework._middleware` | Pre/post hook base class |
| `ChatMiddleware`, `FunctionMiddleware` | same | Chat-level and tool-call-level variants |
| `agent_middleware` / `chat_middleware` / `function_middleware` | same | Decorator form |
| `AgentContext`, `ChatContext`, `FunctionInvocationContext` | same | Runtime contexts passed to middleware |
| `AgentMiddlewarePipeline` | same | Pipeline composition |
| `MiddlewareTermination` | same | Raise from middleware to short-circuit |

**Conclusion:** middleware API is exactly what the plan needs for policy / audit / circuit-breaker hooks.

## Azure AI Foundry client

`agent_framework_foundry` sub-package — ships with `agent-framework`:

| Class | Purpose |
|---|---|
| `FoundryChatClient` | Low-level Foundry chat completion client |
| `FoundryAgent` | Higher-level MAF agent pre-wired to Foundry |
| `FoundryAgentSettings` / `FoundryAgentOptions` | Typed config |
| `FoundryEmbeddingClient` | Embeddings path |

Also: `agent_framework_foundry_local` for local emulation.

**Conclusion:** no custom wrapper needed. Plan's `foundry_client.py` — including the `_AzureOpenAIProvider` / `_AnthropicProvider` split — is superseded by `FoundryChatClient`.

## Provider sub-packages (for reference)

`agent_framework_openai`, `agent_framework_anthropic`, `agent_framework_claude`, `agent_framework_bedrock`, `agent_framework_ollama`, `agent_framework_meta`, `agent_framework_github_copilot`, `agent_framework_copilotstudio`, plus infra: `agent_framework_azure_cosmos`, `agent_framework_redis`, `agent_framework_mem0`, `agent_framework_durabletask`, `agent_framework_orchestrations`, **`agent_framework_purview`** (see toolkit doc), `agent_framework_declarative`, `agent_framework_devui`.

## Python support

Installed cleanly on 3.13.13. Plan target was 3.11+; requirement satisfied.

## Minimal construction snippet (illustrative — untested end-to-end)

```python
from agent_framework import Agent
from agent_framework_foundry import FoundryChatClient, FoundryAgentSettings

client = FoundryChatClient(
    endpoint="https://galaxyscanner-openai.openai.azure.com/",
    deployment_name="gpt-5-3-codex",
    credential=ManagedIdentityCredential(client_id=os.environ["NHI_CLIENT_ID_SCANNER"]),
)

agent = Agent(
    name="Scanner",
    instructions=SYSTEM_PROMPT,
    chat_client=client,
    middleware=[  # list of AgentMiddleware instances
        # PurviewPolicyMiddleware(...)  if Purview is available
        # custom PolicyYamlMiddleware(...) otherwise
        # CircuitBreakerMiddleware(agent_sre.CircuitBreaker(...))
        # PostgresLedgerMirrorMiddleware(...)
    ],
)

response = await agent.run(user_prompt)
```

## Gaps vs. plan assumptions

| Plan assumed | Actual |
|---|---|
| `ChatAgent` is the class name | It's `Agent` |
| `from agent_framework.azure_ai import AzureAIChatClient` | Correct package is `agent_framework_foundry`, class is `FoundryChatClient` |
| `ChatAgent.from_foundry(...)` factory | No such factory; use `Agent(chat_client=FoundryChatClient(...))` |

Minor naming drift only. No blockers.
