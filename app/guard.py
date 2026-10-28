"""Checks applied to text going into and coming out of the model."""

import re

# Phrases that try to talk to the model rather than to the meeting. Flagged turns
# stay visible but are never accepted as evidence for a decision or task update.
_INJECTION = re.compile(
    r"ignore (all |any |the |your )?(previous |prior |above )?(instructions|rules|prompts?)"
    r"|disregard (all |the |your )?(previous |prior )?(instructions|rules)"
    r"|system prompt|you are now|as an ai\b|new instructions|developer mode"
    r"|\b(assistant|ai|model|chatbot)\s*[,:]\s*(mark|add|delete|say|tell|reveal|respond)",
    re.IGNORECASE,
)
_DELIMITERS = re.compile(r"</?\s*(transcript|question|material|open_tasks)\s*>", re.IGNORECASE)


def looks_like_injection(text: str) -> bool:
    return bool(_INJECTION.search(text))


def strip_delimiters(text: str) -> str:
    """Data can't close the <transcript> box early and smuggle in instructions."""
    return _DELIMITERS.sub("", text)
