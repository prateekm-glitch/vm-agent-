# llm_client.py
# ─────────────────────────────────────────────────────────────
# Single shared Anthropic (Claude) client used by all modules.
# Also provides a helper to send a prompt and get back clean text,
# stripping any markdown code fences the model may add.
# ─────────────────────────────────────────────────────────────

import anthropic
import httpx

from settings import ANTHROPIC_API_KEY, ANTHROPIC_BASE_URL, MODEL

# One client for the whole app — created lazily so the app can boot even
# when ANTHROPIC_API_KEY is not set yet (LLM calls fail at call time instead).
_client = None


def _get_client():
    global _client
    if _client is None:
        _client = anthropic.Anthropic(
            api_key=ANTHROPIC_API_KEY,
            base_url=ANTHROPIC_BASE_URL,
            http_client=httpx.Client(verify=False),  # internal gateway; SSL verify off
        )
    return _client


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
