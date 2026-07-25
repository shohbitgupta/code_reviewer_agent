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
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

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
    type: str = "text"
    text: str = ""


@dataclass
class _ToolUseBlock:
    type:  str  = "tool_use"
    name:  str  = ""
    input: Dict = field(default_factory=dict)


@dataclass
class _UnifiedResponse:
    content:     List       # List[_TextBlock | _ToolUseBlock]
    model:       str = ""
    stop_reason: str = "end_turn"


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

    return _UnifiedResponse(content=blocks, model=model)


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
        kwargs_extra = {}
        if tools:
            kwargs_extra["tools"] = tools
        if tool_choice:
            kwargs_extra["tool_choice"] = tool_choice
        return self._raw.messages.create(
            model      = model or self._model,
            max_tokens = max_tokens,
            system     = system,
            messages   = messages or [],
            **kwargs_extra,
            **kwargs,
        )


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
        kwargs_extra = {}
        if tools:
            kwargs_extra["tools"] = tools
        if tool_choice:
            kwargs_extra["tool_choice"] = tool_choice
        return await self._raw.messages.create(
            model      = model or self._model,
            max_tokens = max_tokens,
            system     = system,
            messages   = messages or [],
            **kwargs_extra,
            **kwargs,
        )


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
    def create(cls) -> UnifiedLLMClient:
        """
        Build and return a sync unified client.

        Used by:  main.py · stage3_review · stage4_comments
        """
        model_type, model_id, api_key, base_url = cls._resolve()
        logger.info(
            "[LLMClientFactory] Sync client — provider=%s  model=%s",
            model_type, model_id,
        )

        if model_type == "ANTHROPIC":
            import anthropic
            raw = anthropic.Anthropic(api_key=api_key)
            ns  = _AnthropicSyncMessages(raw, model_id)
        else:
            import openai
            kw: Dict[str, Any] = {}
            if api_key:
                kw["api_key"] = api_key
            if base_url:
                kw["base_url"] = base_url
            raw = openai.OpenAI(**kw)
            ns  = _OpenAISyncMessages(raw, model_id)

        return UnifiedLLMClient(ns, provider=model_type.lower(), model=model_id)

    @classmethod
    def create_async(cls) -> AsyncUnifiedLLMClient:
        """
        Build and return an async unified client.

        Used by:  stage1_ingestion/summary_generator.py
        """
        model_type, model_id, api_key, base_url = cls._resolve()
        logger.info(
            "[LLMClientFactory] Async client — provider=%s  model=%s",
            model_type, model_id,
        )

        if model_type == "ANTHROPIC":
            import anthropic
            raw = anthropic.AsyncAnthropic(api_key=api_key)
            ns  = _AnthropicAsyncMessages(raw, model_id)
        else:
            import openai
            kw: Dict[str, Any] = {}
            if api_key:
                kw["api_key"] = api_key
            if base_url:
                kw["base_url"] = base_url
            raw = openai.AsyncOpenAI(**kw)
            ns  = _OpenAIAsyncMessages(raw, model_id)

        return AsyncUnifiedLLMClient(ns, provider=model_type.lower(), model=model_id)
