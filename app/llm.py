"""The only module that talks to the model provider.

Pipeline code receives an `LLM` and never imports the SDK, so tests pass a fake
and swapping Gemini for another provider means rewriting this file only.
"""

import logging
import time
from dataclasses import dataclass, field
from typing import Protocol, TypeVar

import httpx
import numpy as np
from google import genai
from google.genai import errors, types
from pydantic import BaseModel

from app.config import settings

log = logging.getLogger(__name__)
T = TypeVar("T", bound=BaseModel)

_RETRYABLE = {429, 500, 503, 504}
_EMBED_BATCH = 100


def _retryable(e: Exception) -> bool:
    """Busy / overloaded / dropped connection: worth another try. Bad request: not."""
    return isinstance(e, httpx.HTTPError) or getattr(e, "code", None) in _RETRYABLE


class LLMError(Exception):
    """The provider failed after retries. Callers degrade instead of crashing."""


@dataclass
class Usage:
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    models: list[str] = field(default_factory=list)

    def add(self, model: str, meta) -> None:
        self.calls += 1
        self.models.append(model)
        if meta:
            self.input_tokens += meta.prompt_token_count or 0
            self.output_tokens += meta.candidates_token_count or 0


class LLM(Protocol):
    def json(
        self,
        system: str,
        prompt: str,
        schema: type[T],
        *,
        fast: bool = False,
        think: str = "minimal",
        usage: Usage | None = None,
    ) -> T: ...

    def text(
        self, system: str, prompt: str, *, think: str = "minimal", usage: Usage | None = None
    ) -> str: ...

    def embed(self, texts: list[str], *, query: bool = False) -> list[list[float]]: ...


class GeminiLLM:
    def __init__(self, api_key: str | None = None):
        self.client = genai.Client(api_key=api_key or settings.gemini_api_key)

    def _generate(self, model: str, system: str, prompt: str, think: str, usage, **config):
        cfg = types.GenerateContentConfig(
            system_instruction=system,
            temperature=0,
            thinking_config=types.ThinkingConfig(thinking_level=think),
            **config,
        )
        # Two tries on the chosen model, then one on the fallback when it's busy.
        for attempt, name in enumerate([model, model, settings.fallback_model]):
            try:
                response = self.client.models.generate_content(
                    model=name, contents=prompt, config=cfg
                )
                if usage is not None:
                    usage.add(name, response.usage_metadata)
                return response
            except (errors.APIError, httpx.HTTPError) as e:
                code = getattr(e, "code", "network")
                log.warning("llm call failed model=%s attempt=%d code=%s", name, attempt, code)
                if not _retryable(e) or attempt == 2:
                    raise LLMError(f"{code}: {getattr(e, 'message', e)}") from e
                time.sleep(1.5 * (attempt + 1))
        raise LLMError("unreachable")

    def json(self, system, prompt, schema, *, fast=False, think="minimal", usage=None):
        model = settings.fast_model if fast else settings.answer_model
        response = self._generate(
            model,
            system,
            prompt,
            think,
            usage,
            response_mime_type="application/json",
            response_schema=schema,
        )
        try:
            return schema.model_validate_json(response.text)
        except Exception as e:
            raise LLMError(f"Model returned invalid JSON: {e}") from e

    def text(self, system, prompt, *, think="minimal", usage=None):
        return self._generate(settings.answer_model, system, prompt, think, usage).text or ""

    def embed(self, texts, *, query=False):
        task = "RETRIEVAL_QUERY" if query else "RETRIEVAL_DOCUMENT"
        vectors: list[list[float]] = []
        for i in range(0, len(texts), _EMBED_BATCH):
            batch = texts[i : i + _EMBED_BATCH]
            for attempt in range(3):
                try:
                    response = self.client.models.embed_content(
                        model=settings.embedding_model,
                        contents=batch,
                        config=types.EmbedContentConfig(
                            task_type=task, output_dimensionality=settings.embedding_dims
                        ),
                    )
                    break
                except (errors.APIError, httpx.HTTPError) as e:
                    if not _retryable(e) or attempt == 2:
                        raise LLMError(f"{getattr(e, 'code', 'network')}: {e}") from e
                    time.sleep(1.5 * (attempt + 1))
            # Truncated embeddings aren't unit length; normalise so dot product = cosine.
            for e in response.embeddings:
                v = np.asarray(e.values, dtype=np.float32)
                vectors.append((v / (np.linalg.norm(v) or 1.0)).tolist())
        return vectors
