"""
Unified LLM Client — provider-agnostic adapter for the code reviewer pipeline.

Select a backend via the MODEL_TYPE environment variable:

  MODEL_TYPE=FREE         → ZhipuAI GLM-5.2-free  (free tier, OpenAI-compat API)
                            model:   z-ai/glm-5.2-free  (override with MODEL_NAME)
                            api key: ZHIPUAI_API_KEY  or  LLM_API_KEY

  MODEL_TYPE=OPENAI       → OpenAI GPT  (default model: gpt-4o)
                            model:   gpt-4o  (override with MODEL_NAME)
                            api key: OPENAI_API_KEY  or  LLM_API_KEY

  MODEL_TYPE=ANTHROPIC    → Anthropic Claude Opus 4.8  (default, production)
  (or unset)                model:   claude-opus-4-8  (override with MODEL_NAME)
                            api key: ANTHROPIC_API_KEY  or  LLM_API_KEY

Optional overrides:
  MODEL_NAME=<id>         Override the default model ID for the selected provider.
  LLM_BASE_URL=<url>      Override the API base URL (OpenAI-compat custom deployments).
  LLM_API_KEY=<key>       Fallback API key (checked after the provider-specific var).

The unified clients expose a `.messages` namespace whose `.create()` accepts
Anthropic-style parameters (system, messages, tools, tool_choice) and returns an
Anthropic-compatible response object.  Existing Stage 1/3/4 code requires zero
changes to its call or response parsing logic.

Usage::

    from tools.llm_client import LLMClientFactory

    # Sync (Stage 3, Stage 4, main.py)
    client = LLMClientFactory.create()
    resp   = client.messages.create(model=..., max_tokens=..., system=..., messages=[...])
    text   = resp.content[0].text

    # Async (Stage 1h summary_generator)
    aclient = LLMClientFactory.create_async()
    resp    = await aclient.messages.create(...)
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from tools.pricing import estimate_cost

logger = logging.getLogger(__name__)

# ── Provider defaults ─────────────────────────────────────────────────────────
_DEFAULTS = {
    "FREE":       {
        "model":    "z-ai/glm-5.2-free",
        "base_url": "https://api.tokenrouter.com/v1",
        "key_env":  "ZHIPUAI_API_KEY",
    },
    "OPENAI":     {
        "model":    "gpt-4o",
        "base_url": None,
        "key_env":  "OPENAI_API_KEY",
    },
    "ANTHROPIC":  {
        "model":    "claude-opus-4-8",
        "base_url": None,
        "key_env":  "ANTHROPIC_API_KEY",
    },
}

# ── Anthropic-compatible response objects ─────────────────────────────────────
# These mirror the Anthropic SDK's response shape so existing code that does
# `response.content[0].text` or `block.type == "tool_use"` keeps working.

@dataclass
class _TextBlock:
    """A plain-text content block, matching the shape of an Anthropic text block."""
    type: str = "text"
    text: str = ""


@dataclass
class _ToolUseBlock:
    """A tool-call content block, matching the shape of an Anthropic tool_use block."""
    type:  str  = "tool_use"
    name:  str  = ""
    input: Dict = field(default_factory=dict)


@dataclass
class _UnifiedResponse:
    """Anthropic-compatible response returned by every backend's messages.create()."""
    content:     List       # List[_TextBlock | _ToolUseBlock]
    model:       str = ""
    stop_reason: str = "end_turn"
    # Normalized across backends regardless of the provider's own field names
    # (Anthropic: input_tokens/output_tokens; OpenAI: prompt_tokens/completion_tokens).
    usage: Optional[Dict[str, int]] = None


# ── Schema translation helpers ────────────────────────────────────────────────

def _to_openai_tools(anthropic_tools: Optional[List[Dict]]) -> Optional[List[Dict]]:
    """Convert Anthropic tool schema → OpenAI function-calling schema."""
    if not anthropic_tools:
        return None
    return [
        {
            "type": "function",
            "function": {
                "name":        t["name"],
                "description": t.get("description", ""),
                "parameters":  t.get("input_schema", {"type": "object", "properties": {}}),
            },
        }
        for t in anthropic_tools
    ]


def _to_openai_tool_choice(anthropic_tc: Optional[Dict]) -> Optional[Any]:
    """Convert Anthropic tool_choice → OpenAI tool_choice."""
    if not anthropic_tc:
        return None
    tc_type = anthropic_tc.get("type")
    if tc_type == "any":
        return "required"         # force a tool call (Anthropic "any" = OpenAI "required")
    if tc_type == "auto":
        return "auto"
    if tc_type == "tool":
        return {"type": "function", "function": {"name": anthropic_tc["name"]}}
    return None


def _extract_usage(raw_usage: Any, prompt_key: str, completion_key: str) -> Optional[Dict[str, int]]:
    """Normalize a provider's usage object into {"prompt_tokens", "completion_tokens"}."""
    if raw_usage is None:
        return None
    prompt = getattr(raw_usage, prompt_key, None)
    completion = getattr(raw_usage, completion_key, None)
    if prompt is None and completion is None:
        return None
    return {"prompt_tokens": prompt or 0, "completion_tokens": completion or 0}


def _openai_to_unified(response: Any, model: str) -> _UnifiedResponse:
    """Convert an OpenAI ChatCompletion → _UnifiedResponse."""
    message = response.choices[0].message
    blocks: List = []

    if message.content:
        blocks.append(_TextBlock(text=message.content))

    for tc in (message.tool_calls or []):
        try:
            args = json.loads(tc.function.arguments)
        except (json.JSONDecodeError, AttributeError):
            args = {}
        blocks.append(_ToolUseBlock(name=tc.function.name, input=args))

    usage = _extract_usage(getattr(response, "usage", None), "prompt_tokens", "completion_tokens")
    return _UnifiedResponse(content=blocks, model=model, usage=usage)


def _anthropic_to_unified(response: Any, model: str) -> _UnifiedResponse:
    """Wrap a raw Anthropic SDK response → _UnifiedResponse.

    `response.content` already duck-types as List[_TextBlock | _ToolUseBlock]
    (both expose .type/.text/.name/.input), so callers that do
    `resp.content[0].text` or check `block.type == "tool_use"` are unaffected.
    """
    usage = _extract_usage(getattr(response, "usage", None), "input_tokens", "output_tokens")
    return _UnifiedResponse(
        content=response.content,
        model=getattr(response, "model", model) or model,
        stop_reason=getattr(response, "stop_reason", None) or "end_turn",
        usage=usage,
    )


# ── Sync message namespaces ───────────────────────────────────────────────────

class _AnthropicSyncMessages:
    """Thin proxy around the real anthropic.messages namespace (sync)."""

    def __init__(self, raw_client, model: str) -> None:
        self._raw   = raw_client
        self._model = model

    def create(
        self,
        model       = None,
        max_tokens  = 1024,
        system      = "",
        messages    = None,
        tools       = None,
        tool_choice = None,
        **kwargs,
    ):
        """
        Forward an Anthropic-style request straight to anthropic.messages.create().

        Args:
            model: Model ID override; defaults to the client's configured model.
            max_tokens: Maximum tokens to generate.
            system: System prompt.
            messages: Anthropic-style message list.
            tools: Optional tool schema list (Anthropic format).
            tool_choice: Optional tool_choice directive (Anthropic format).

        Returns:
            _UnifiedResponse wrapping the raw Anthropic SDK response (see
            _anthropic_to_unified — content stays duck-type compatible with
            the raw response, so existing callers are unaffected).
        """
        m = model or self._model
        kwargs_extra = {}
        if tools:
            kwargs_extra["tools"] = tools
        if tool_choice:
            kwargs_extra["tool_choice"] = tool_choice
        raw_resp = self._raw.messages.create(
            model      = m,
            max_tokens = max_tokens,
            system     = system,
            messages   = messages or [],
            **kwargs_extra,
            **kwargs,
        )
        return _anthropic_to_unified(raw_resp, m)


class _OpenAISyncMessages:
    """Translates Anthropic-style calls → OpenAI chat completions (sync)."""

    def __init__(self, raw_client, model: str) -> None:
        self._raw   = raw_client
        self._model = model

    def create(
        self,
        model       = None,
        max_tokens  = 1024,
        system      = "",
        messages    = None,
        tools       = None,
        tool_choice = None,
        **kwargs,
    ):
        """
        Translate an Anthropic-style request into an OpenAI chat completion call.

        Folds `system` into the message list as a leading "system" message,
        converts `tools`/`tool_choice` to OpenAI's function-calling schema, and
        wraps the OpenAI response back into an Anthropic-compatible shape.

        Args:
            model: Model ID override; defaults to the client's configured model.
            max_tokens: Maximum tokens to generate.
            system: System prompt, injected as the first message.
            messages: Anthropic-style message list.
            tools: Optional tool schema list (Anthropic format).
            tool_choice: Optional tool_choice directive (Anthropic format).

        Returns:
            _UnifiedResponse mirroring the Anthropic SDK response shape.
        """
        m = model or self._model
        oai_msgs = []
        if system:
            oai_msgs.append({"role": "system", "content": system})
        oai_msgs.extend(messages or [])

        extra: Dict[str, Any] = {}
        oai_tools = _to_openai_tools(tools)
        if oai_tools:
            extra["tools"] = oai_tools
        oai_tc = _to_openai_tool_choice(tool_choice)
        if oai_tc is not None:
            extra["tool_choice"] = oai_tc

        response = self._raw.chat.completions.create(
            model      = m,
            max_tokens = max_tokens,
            messages   = oai_msgs,
            **extra,
        )
        return _openai_to_unified(response, m)


# ── Async message namespaces ──────────────────────────────────────────────────

class _AnthropicAsyncMessages:
    """Thin async proxy around anthropic.AsyncAnthropic.messages."""

    def __init__(self, raw_client, model: str) -> None:
        self._raw   = raw_client
        self._model = model

    async def create(
        self,
        model       = None,
        max_tokens  = 1024,
        system      = "",
        messages    = None,
        tools       = None,
        tool_choice = None,
        **kwargs,
    ):
        """
        Await an Anthropic-style request forwarded to anthropic.AsyncAnthropic.messages.create().

        Args:
            model: Model ID override; defaults to the client's configured model.
            max_tokens: Maximum tokens to generate.
            system: System prompt.
            messages: Anthropic-style message list.
            tools: Optional tool schema list (Anthropic format).
            tool_choice: Optional tool_choice directive (Anthropic format).

        Returns:
            _UnifiedResponse wrapping the raw Anthropic SDK response (see
            _anthropic_to_unified).
        """
        m = model or self._model
        kwargs_extra = {}
        if tools:
            kwargs_extra["tools"] = tools
        if tool_choice:
            kwargs_extra["tool_choice"] = tool_choice
        raw_resp = await self._raw.messages.create(
            model      = m,
            max_tokens = max_tokens,
            system     = system,
            messages   = messages or [],
            **kwargs_extra,
            **kwargs,
        )
        return _anthropic_to_unified(raw_resp, m)


class _OpenAIAsyncMessages:
    """Translates Anthropic-style async calls → OpenAI async chat completions."""

    def __init__(self, raw_client, model: str) -> None:
        self._raw   = raw_client
        self._model = model

    async def create(
        self,
        model       = None,
        max_tokens  = 1024,
        system      = "",
        messages    = None,
        tools       = None,
        tool_choice = None,
        **kwargs,
    ):
        """
        Await the async equivalent of _OpenAISyncMessages.create().

        Translates the Anthropic-style request into an OpenAI async chat
        completion call and wraps the result back into an Anthropic-compatible
        shape. See _OpenAISyncMessages.create() for parameter details.

        Returns:
            _UnifiedResponse mirroring the Anthropic SDK response shape.
        """
        m = model or self._model
        oai_msgs = []
        if system:
            oai_msgs.append({"role": "system", "content": system})
        oai_msgs.extend(messages or [])

        extra: Dict[str, Any] = {}
        oai_tools = _to_openai_tools(tools)
        if oai_tools:
            extra["tools"] = oai_tools
        oai_tc = _to_openai_tool_choice(tool_choice)
        if oai_tc is not None:
            extra["tool_choice"] = oai_tc

        response = await self._raw.chat.completions.create(
            model      = m,
            max_tokens = max_tokens,
            messages   = oai_msgs,
            **extra,
        )
        return _openai_to_unified(response, m)


# ── Cost/event instrumentation wrappers ────────────────────────────────────────
# Wrap whatever messages namespace a backend produced so every call — sync or
# async, Anthropic or OpenAI-shaped — passes through the same pre-flight budget
# check and post-call cost/event recording exactly once, regardless of which of
# the 3 call sites (stage1 summaries, stage3 review, stage4 comment polish)
# triggered it. Both budget_guard and event_spine are optional and duck-typed
# (only .check_before_call()/.record_call()/.record() are ever called on them)
# so this module never needs to import tools.budget_guard/tools.event_spine.

class _InstrumentedSyncMessages:
    """Decorates a sync messages namespace with budget-guard + event-spine hooks."""

    def __init__(self, inner, model: str, budget_guard: Any = None, event_spine: Any = None) -> None:
        self._inner = inner
        self._model = model
        self._budget_guard = budget_guard
        self._event_spine = event_spine

    def create(self, model=None, max_tokens=1024, **kwargs):
        m = model or self._model
        if self._budget_guard is not None:
            self._budget_guard.check_before_call()
        t0 = time.monotonic()
        resp = self._inner.create(model=model, max_tokens=max_tokens, **kwargs)
        duration_ms = (time.monotonic() - t0) * 1000
        usage = getattr(resp, "usage", None)
        cost_usd = 0.0
        if usage is not None:
            cost_usd = estimate_cost(m, usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0))
            if self._budget_guard is not None:
                self._budget_guard.record_call(m, usage, cost_usd)
        if self._event_spine is not None:
            self._event_spine.record(
                "llm_call", model=m, usage=usage, cost_usd=cost_usd, duration_ms=duration_ms,
            )
        return resp


class _InstrumentedAsyncMessages:
    """Async counterpart of _InstrumentedSyncMessages."""

    def __init__(self, inner, model: str, budget_guard: Any = None, event_spine: Any = None) -> None:
        self._inner = inner
        self._model = model
        self._budget_guard = budget_guard
        self._event_spine = event_spine

    async def create(self, model=None, max_tokens=1024, **kwargs):
        m = model or self._model
        if self._budget_guard is not None:
            self._budget_guard.check_before_call()
        t0 = time.monotonic()
        resp = await self._inner.create(model=model, max_tokens=max_tokens, **kwargs)
        duration_ms = (time.monotonic() - t0) * 1000
        usage = getattr(resp, "usage", None)
        cost_usd = 0.0
        if usage is not None:
            cost_usd = estimate_cost(m, usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0))
            if self._budget_guard is not None:
                self._budget_guard.record_call(m, usage, cost_usd)
        if self._event_spine is not None:
            self._event_spine.record(
                "llm_call", model=m, usage=usage, cost_usd=cost_usd, duration_ms=duration_ms,
            )
        return resp


# ── Public client wrappers ────────────────────────────────────────────────────

class UnifiedLLMClient:
    """
    Sync LLM client.  Drop-in replacement for anthropic.Anthropic().

    Exposes .messages with a .create() that accepts Anthropic-style parameters
    and returns an Anthropic-compatible response regardless of the backend.
    """

    def __init__(self, messages_ns, provider: str = "", model: str = "") -> None:
        self._messages = messages_ns
        self.provider  = provider   # "anthropic" | "openai" | "openai_compat"
        self.model_name = model     # resolved model ID for this session

    @property
    def messages(self):
        return self._messages


class AsyncUnifiedLLMClient:
    """
    Async LLM client.  Drop-in replacement for anthropic.AsyncAnthropic().

    Used by stage1_ingestion/summary_generator.py which awaits messages.create().
    """

    def __init__(self, messages_ns, provider: str = "", model: str = "") -> None:
        self._messages = messages_ns
        self.provider  = provider
        self.model_name = model

    @property
    def messages(self):
        return self._messages


# ── Factory ───────────────────────────────────────────────────────────────────

class LLMClientFactory:
    """
    Reads MODEL_TYPE (and optional MODEL_NAME / LLM_API_KEY / LLM_BASE_URL)
    from the environment and instantiates the correct backend client.
    """

    @staticmethod
    def _resolve() -> tuple:
        """Return (provider_key, model_id, api_key, base_url)."""
        model_type = os.getenv("MODEL_TYPE", "ANTHROPIC").upper().strip()
        if model_type not in _DEFAULTS:
            logger.warning(
                "[LLMClientFactory] Unknown MODEL_TYPE=%r — falling back to ANTHROPIC",
                model_type,
            )
            model_type = "ANTHROPIC"

        defaults  = _DEFAULTS[model_type]
        model_id  = os.getenv("MODEL_NAME", "").strip() or defaults["model"]
        api_key   = (
            os.getenv(defaults["key_env"], "").strip()
            or os.getenv("LLM_API_KEY", "").strip()
            or None
        )
        base_url  = (
            os.getenv("LLM_BASE_URL", "").strip()
            or defaults.get("base_url")
            or None
        )
        return model_type, model_id, api_key, base_url

    @classmethod
    def create(cls, budget_guard: Any = None, event_spine: Any = None) -> UnifiedLLMClient:
        """
        Build and return a sync unified client.

        Used by:  main.py · stage3_review · stage4_comments

        Args:
            budget_guard: Optional object exposing .check_before_call()/.record_call()
                — when given, every messages.create() call is metered against it.
            event_spine: Optional object exposing .record(event_type, **fields) —
                when given, every call emits an "llm_call" event.
        """
        model_type, model_id, api_key, base_url = cls._resolve()
        logger.info(
            "[LLMClientFactory] Sync client — provider=%s  model=%s",
            model_type, model_id,
        )

        if model_type == "ANTHROPIC":
            import anthropic
            # max_retries=0: retry/backoff/rate-limiting is owned entirely by
            # our own callers (LLMReviewer._call_with_retry, etc.) — an SDK-level
            # retry underneath ours would silently multiply request volume
            # against providers with a hard per-minute ceiling (e.g. GLM free tier).
            raw = anthropic.Anthropic(api_key=api_key, max_retries=0)
            ns  = _AnthropicSyncMessages(raw, model_id)
        else:
            import openai
            kw: Dict[str, Any] = {"max_retries": 0}
            if api_key:
                kw["api_key"] = api_key
            if base_url:
                kw["base_url"] = base_url
            raw = openai.OpenAI(**kw)
            ns  = _OpenAISyncMessages(raw, model_id)

        if budget_guard is not None or event_spine is not None:
            ns = _InstrumentedSyncMessages(ns, model_id, budget_guard=budget_guard, event_spine=event_spine)

        return UnifiedLLMClient(ns, provider=model_type.lower(), model=model_id)

    @classmethod
    def create_async(cls, budget_guard: Any = None, event_spine: Any = None) -> AsyncUnifiedLLMClient:
        """
        Build and return an async unified client.

        Used by:  stage1_ingestion/summary_generator.py

        Args: see create() — same optional budget_guard/event_spine hooks.
        """
        model_type, model_id, api_key, base_url = cls._resolve()
        logger.info(
            "[LLMClientFactory] Async client — provider=%s  model=%s",
            model_type, model_id,
        )

        if model_type == "ANTHROPIC":
            import anthropic
            # max_retries=0 — see the sync create() method for rationale.
            raw = anthropic.AsyncAnthropic(api_key=api_key, max_retries=0)
            ns  = _AnthropicAsyncMessages(raw, model_id)
        else:
            import openai
            kw: Dict[str, Any] = {"max_retries": 0}
            if api_key:
                kw["api_key"] = api_key
            if base_url:
                kw["base_url"] = base_url
            raw = openai.AsyncOpenAI(**kw)
            ns  = _OpenAIAsyncMessages(raw, model_id)

        if budget_guard is not None or event_spine is not None:
            ns = _InstrumentedAsyncMessages(ns, model_id, budget_guard=budget_guard, event_spine=event_spine)

        return AsyncUnifiedLLMClient(ns, provider=model_type.lower(), model=model_id)
