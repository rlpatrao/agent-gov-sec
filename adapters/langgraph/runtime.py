"""
adapters.langgraph.runtime — chat-model factory for the LangGraph axis.

Two model sources, mirroring the egress logic in ``payload_agents/_base.py``:

  - ``FakeToolCallingModel`` — an **offline, no-credentials** chat model that
    replays a scripted list of ``AIMessage`` turns (including ``tool_calls``).
    LangChain's bundled ``GenericFakeChatModel`` cannot drive a tool-using
    ``create_agent`` because it raises ``NotImplementedError`` from
    ``bind_tools``; this subclass implements ``bind_tools`` as a no-op (the tool
    calls are already scripted) so the full plan→tool→observe→answer loop runs
    deterministically in tests, CI, and the offline demo.

  - ``build_chat_model`` — returns a live ``langchain_openai`` chat model when
    real credentials/endpoint are resolved (via the cloud provider's LLM
    gateway), else falls back to a ``FakeToolCallingModel``. The demo always uses
    the fake model; live mode is an env-gated upgrade, never a requirement.
"""

from __future__ import annotations

import logging
from typing import Any, List, Optional, Sequence

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult

logger = logging.getLogger(__name__)


class FakeToolCallingModel(BaseChatModel):
    """Deterministic offline chat model that replays scripted ``AIMessage`` turns.

    Each ``_generate`` call returns the next message in ``responses`` (clamping to
    the last one once exhausted, so an over-eager agent loop can't IndexError).
    ``bind_tools`` returns ``self`` unchanged — tool calls are pre-scripted on the
    ``AIMessage.tool_calls``, so no real tool-binding is needed.
    """

    responses: List[AIMessage] = []
    cursor: int = 0

    # BaseChatModel is a Pydantic model; allow the mutable cursor field.
    model_config = {"arbitrary_types_allowed": True}

    @property
    def _llm_type(self) -> str:
        return "galaxy-fake-tool-calling"

    def bind_tools(self, tools: Sequence[Any], **kwargs: Any) -> "FakeToolCallingModel":
        # create_agent calls bind_tools(); the scripted tool_calls don't need it.
        return self

    def _generate(
        self,
        messages: List[BaseMessage],
        stop: Optional[List[str]] = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        if not self.responses:
            msg: AIMessage = AIMessage(content="")
        else:
            idx = min(self.cursor, len(self.responses) - 1)
            msg = self.responses[idx]
            self.cursor += 1
        return ChatResult(generations=[ChatGeneration(message=msg)])


def scripted_model(*messages: AIMessage) -> FakeToolCallingModel:
    """Build a ``FakeToolCallingModel`` that replays ``messages`` in order.

    Example::

        m = scripted_model(
            AIMessage(content="", tool_calls=[{"name": "query_billing",
                                               "args": {"rows": 2}, "id": "c1"}]),
            AIMessage(content="Summary: 2 rows of billing."),
        )
    """
    return FakeToolCallingModel(responses=list(messages), cursor=0)


def build_chat_model(
    *,
    deployment: Optional[str] = None,
    api_key: Optional[str] = None,
    endpoint: Optional[str] = None,
    api_version: Optional[str] = None,
    default_headers: Optional[dict] = None,
    offline_fallback: Optional[FakeToolCallingModel] = None,
    use_responses_api: bool = False,
) -> BaseChatModel:
    """Return a live ``langchain_openai`` model when credentials resolve, else the
    offline fallback.

    The demo and tests pass ``offline_fallback=scripted_model(...)`` and never set
    credentials, so this returns the fake model deterministically. When
    ``AZURE_OPENAI_*`` / ``OPENAI_API_KEY`` are present (resolved through the cloud
    provider's LLM gateway in ``_base.build_langgraph_agent``), a real
    ``AzureChatOpenAI`` / ``ChatOpenAI`` is constructed instead.

    ``use_responses_api`` routes Azure calls through the **Responses API** instead
    of ``/chat/completions`` — required for reasoning/codex deployments (o-series,
    gpt-5*, *-codex) that don't support chat completions. The Responses API needs
    ``api-version`` ``2025-03-01-preview`` or later, so the version is bumped to
    that floor when an older/placeholder value is supplied.
    """
    if not api_key:
        if offline_fallback is None:
            raise ValueError(
                "build_chat_model: no api_key resolved and no offline_fallback supplied. "
                "Pass scripted_model(...) for offline runs."
            )
        logger.info("langgraph.model.offline", extra={"reason": "no api_key; using FakeToolCallingModel"})
        return offline_fallback

    # Live path — only imported when credentials exist, so offline runs never
    # require a configured OpenAI/AOAI client.
    if endpoint:
        from langchain_openai import AzureChatOpenAI

        _RESPONSES_FLOOR = "2025-03-01-preview"
        ver = api_version
        if use_responses_api and (not ver or ver == "preview" or ver < _RESPONSES_FLOOR):
            ver = _RESPONSES_FLOOR
        logger.info("langgraph.model.live_azure",
                    extra={"endpoint": endpoint, "deployment": deployment,
                           "responses_api": use_responses_api, "api_version": ver})
        return AzureChatOpenAI(
            azure_endpoint=endpoint,
            azure_deployment=deployment,
            api_key=api_key,
            api_version=ver or "preview",
            default_headers=default_headers or {},
            use_responses_api=use_responses_api,
        )

    from langchain_openai import ChatOpenAI

    logger.info("langgraph.model.live_openai", extra={"model": deployment, "responses_api": use_responses_api})
    return ChatOpenAI(model=deployment or "gpt-4o", api_key=api_key,
                      default_headers=default_headers or {}, use_responses_api=use_responses_api)


def build_gemini_model(
    *,
    model: Optional[str] = None,
    project: Optional[str] = None,
    location: Optional[str] = None,
    api_key: Optional[str] = None,
    offline_fallback: Optional[FakeToolCallingModel] = None,
) -> BaseChatModel:
    """Return a live Google **Gemini** chat model (the GCP counterpart to
    ``build_chat_model``), else the offline fallback.

    Uses the maintained ``langchain_google_genai.ChatGoogleGenerativeAI`` for both
    backends (it supersedes the now-deprecated ``ChatVertexAI``):

      - **Vertex AI** when ``project`` is set — authorized by ADC / the agent's
        Service-Account token (no api key). Selected via the google-genai client's
        ``GOOGLE_GENAI_USE_VERTEXAI=True`` + ``GOOGLE_CLOUD_PROJECT`` /
        ``GOOGLE_CLOUD_LOCATION``, which this sets from the args when absent.
      - **Gemini Developer API** when only an ``api_key`` (``GOOGLE_API_KEY``) is
        available.

    Falls back to ``ChatVertexAI`` if only ``langchain-google-vertexai`` is
    installed, and to ``offline_fallback`` when no creds resolve or no
    ``langchain-google-*`` package is present.
    """
    import os

    model = model or "gemini-2.5-pro"
    use_vertex = bool(project)

    if use_vertex or api_key:
        try:
            from langchain_google_genai import ChatGoogleGenerativeAI
        except ImportError:
            ChatGoogleGenerativeAI = None
        if ChatGoogleGenerativeAI is not None:
            if use_vertex:
                # The google-genai client reads these from the environment.
                os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "True"
                os.environ.setdefault("GOOGLE_CLOUD_PROJECT", project)
                os.environ.setdefault("GOOGLE_CLOUD_LOCATION", location or "us-central1")
                logger.info("langgraph.model.live_vertex_genai",
                            extra={"project": project, "location": location, "model": model})
                return ChatGoogleGenerativeAI(model=model)
            logger.info("langgraph.model.live_genai", extra={"model": model})
            return ChatGoogleGenerativeAI(model=model, google_api_key=api_key)

    # Fallback: only the (deprecated) Vertex SDK is installed.
    if use_vertex:
        try:
            from langchain_google_vertexai import ChatVertexAI
        except ImportError:
            logger.warning("langgraph.model.gcp_missing — pip install '.[gcp]'")
        else:
            logger.info("langgraph.model.live_vertex", extra={"project": project, "location": location, "model": model})
            return ChatVertexAI(model=model, project=project, location=location or "us-central1")

    if offline_fallback is None:
        raise ValueError(
            "build_gemini_model: no Vertex project or GOOGLE_API_KEY resolved and no "
            "offline_fallback supplied. Set GOOGLE_CLOUD_PROJECT or GOOGLE_API_KEY."
        )
    logger.info("langgraph.model.offline", extra={"reason": "no gcp creds; using FakeToolCallingModel"})
    return offline_fallback


def build_bedrock_model(
    *,
    endpoint: Optional[str] = None,
    api_key: Optional[str] = None,
    model_id: Optional[str] = None,
    default_headers: Optional[dict] = None,
    offline_fallback: Optional[FakeToolCallingModel] = None,
) -> BaseChatModel:
    """Return a live **Bedrock-via-API-Gateway** chat model (the AWS counterpart to
    ``build_chat_model`` / ``build_gemini_model``), else the offline fallback.

    Routes through the ``apigw-bedrock`` egress chokepoint: a
    ``BedrockGatewayChatModel`` POSTs Converse requests to ``endpoint`` with the
    ``x-api-key`` (from the gateway's Secrets-Manager-backed resolution) plus the
    per-agent attribution headers. Bedrock creds stay server-side in the Lambda.

    Returns ``offline_fallback`` when ``endpoint`` or ``api_key`` is absent (the
    chokepoint refusing to hand back an egress credential offline — by design)."""
    if not endpoint or not api_key:
        if offline_fallback is None:
            raise ValueError(
                "build_bedrock_model: no gateway endpoint/key resolved and no offline_fallback. "
                "Set AWS_BEDROCK_GATEWAY_ENDPOINT + the gateway key (Secrets Manager / "
                "AWS_BEDROCK_GATEWAY_KEY)."
            )
        logger.info("langgraph.model.offline", extra={"reason": "no bedrock gateway creds; using FakeToolCallingModel"})
        return offline_fallback

    from adapters.langgraph.bedrock_gateway import BedrockGatewayChatModel

    logger.info("langgraph.model.live_bedrock", extra={"endpoint": endpoint, "model": model_id})
    return BedrockGatewayChatModel(
        endpoint=endpoint,
        api_key=api_key,
        model_id=model_id or "us.anthropic.claude-sonnet-4-6",
        default_headers=default_headers or {},
    )
