import hashlib
import re

import numpy as np
import pytest

from app import db
from app.llm import LLMError


class FakeLLM:
    """Deterministic stand-in for Gemini. Embeddings are hashed bags of words, so texts
    sharing words are close; `replies` maps a schema name (or "text") to a canned reply."""

    def __init__(self, replies=None, fail_embed=False):
        self.replies = replies or {}
        self.fail_embed = fail_embed
        self.calls = []

    def embed(self, texts, *, query=False):
        if self.fail_embed:
            raise LLMError("503: busy")
        out = []
        for text in texts:
            v = np.zeros(64, dtype=np.float32)
            for w in re.findall(r"\w+", text.lower()):
                v[int(hashlib.md5(w.encode()).hexdigest(), 16) % 64] += 1
            out.append((v / (np.linalg.norm(v) or 1)).tolist())
        return out

    def json(self, system, prompt, schema, *, fast=False, think="minimal", usage=None):
        self.calls.append((schema.__name__, prompt))
        reply = self.replies.get(schema.__name__)
        if isinstance(reply, Exception):
            raise reply
        if callable(reply):
            reply = reply(prompt)
        if reply is None:
            raise LLMError(f"no fake reply for {schema.__name__}")
        return schema.model_validate(reply)

    def text(self, system, prompt, *, think="minimal", usage=None):
        self.calls.append(("text", prompt))
        reply = self.replies.get("text", "")
        return reply(prompt) if callable(reply) else reply


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    yield c
    c.close()


@pytest.fixture
def fake():
    return FakeLLM()
