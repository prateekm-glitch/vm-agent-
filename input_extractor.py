# input_extractor.py
# ─────────────────────────────────────────────────────────────
# Uses the shared Claude client to extract a VM config (as a dict)
# from a natural-language user message.
# ─────────────────────────────────────────────────────────────

import json

from llm_client import ask, strip_code_fences
from prompts import EXTRACT_PROMPT


def extract_vm_config(user_input: str) -> dict:
    """Send the user message to the LLM and return a dict of extracted fields.

    Raises json.JSONDecodeError if the model returns non-JSON content.
    """
    prompt = EXTRACT_PROMPT.format(user_input=user_input)
    raw = ask(prompt, max_tokens=500, temperature=0.0)

    # Model often wraps JSON in ```json ... ``` fences — strip them.
    cleaned = strip_code_fences(raw)
    return json.loads(cleaned)