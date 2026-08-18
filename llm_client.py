# llm_client.py
# ─────────────────────────────────────────────────────────────
# Single shared Anthropic (Claude) client used by all modules.
# Also provides a helper to send a prompt and get back clean text,
# stripping any markdown code fences the model may add.
# ─────────────────────────────────────────────────────────────

import contextvars
import threading
import anthropic
import httpx

from settings import ANTHROPIC_API_KEY, ANTHROPIC_BASE_URL, MODEL

# ── Per-session API key support ──────────────────────────────────
# Each user gets their own Codewise API key (fetched at login time
# from codewise.qualcomm.com).  The key lives in a ContextVar so
# concurrent requests (and background build threads that inherit the
# Flask request context via contextvars.copy_context()) never mix
# credentials.  When no session key is set, the global
# ANTHROPIC_API_KEY (from env) is used as a fallback.
_session_key_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "_session_api_key", default=None
)
_clients: dict[str, anthropic.Anthropic] = {}  # key → client cache
_clients_lock = threading.Lock()


def set_session_key(api_key: str):
    """Store the user's Codewise API key for the current request context.

    Because we use a ContextVar, this value automatically propagates
    into background build threads that use contextvars.copy_context().
    """
    _session_key_var.set(api_key)


def clear_session_key():
    """Remove the per-session key (e.g. on logout)."""
    _session_key_var.set(None)


def _active_key() -> str:
    """Return the API key to use for the current context.
    Per-session key wins; global env key is the fallback."""
    return _session_key_var.get() or ANTHROPIC_API_KEY


# Default client (uses global ANTHROPIC_API_KEY) — created lazily.
_default_client = None


def _get_client():
    """Return the Anthropic client for the current thread.
    Per-session keys get their own cached client so concurrent users
    never share credentials."""
    key = _active_key()
    # Per-session key → own client (cached by key value)
    if key and key != ANTHROPIC_API_KEY:
        with _clients_lock:
            if key not in _clients:
                _clients[key] = anthropic.Anthropic(
                    api_key=key,
                    base_url=ANTHROPIC_BASE_URL,
                    http_client=httpx.Client(verify=False),
                )
            return _clients[key]
    # Fallback: global env key
    global _default_client
    if _default_client is None:
        _default_client = anthropic.Anthropic(
            api_key=ANTHROPIC_API_KEY,
            base_url=ANTHROPIC_BASE_URL,
            http_client=httpx.Client(verify=False),
        )
    return _default_client


def strip_code_fences(text: str) -> str:
    """The model often wraps output in ```json ... ``` fences.
    Remove them so json.loads() (or the caller) gets clean content."""
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.lstrip().lower().startswith("json"):
            raw = raw.lstrip()[4:]
        raw = raw.strip()
    return raw


def ask(prompt: str, max_tokens: int = 500, temperature: float = 0.0) -> str:
    """Send a single-user-message prompt and return the model's text."""
    response = _get_client().messages.create(
        model=MODEL,
        max_tokens=max_tokens,
        temperature=temperature,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text.strip()


def ask_with_tools(
    messages: list,
    tools: list,
    system_prompt: str = "",
    max_tokens: int = 1024,
) -> object:
    """Send messages with tool definitions to Claude for the agentic loop.

    This is the core function for Phase 2 — the looping agentic loop.
    Claude can either:
      1. Call a tool → returns ToolUseBlock (type="tool_use")
      2. Write text  → returns TextBlock (type="text")
      3. Declare done → we detect this from stop_reason="end_turn" with no tool calls

    Args:
        messages:      Conversation history (list of role/content dicts)
        tools:         Tool schemas (list of dicts with name/description/input_schema)
        system_prompt: Instructions for Claude (what its goal is, what tools do)
        max_tokens:    Max tokens in response

    Returns:
        The full response object from Claude.
        Caller checks response.stop_reason and response.content to decide next action.

    Example usage:
        response = ask_with_tools(messages, TOOL_SCHEMAS, system_prompt)
        for block in response.content:
            if block.type == "tool_use":
                # Claude wants to call a tool
                tool_name = block.name
                tool_input = block.input
            elif block.type == "text":
                # Claude wrote text (explanation or final answer)
                text = block.text
    """
    kwargs = {
        "model": MODEL,
        "max_tokens": max_tokens,
        "tools": tools,
        "messages": messages,
    }
    if system_prompt:
        kwargs["system"] = system_prompt

    return _get_client().messages.create(**kwargs)
