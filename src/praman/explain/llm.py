"""Gemini, through an OpenAI-compatible gateway.

The OpenAI SDK is used as a transport, not because the model is OpenAI's --
kie.ai exposes Gemini behind that wire format, so one client shape works for
both and swapping providers is a base_url change rather than a rewrite.

Nothing here validates anything. Validation lives in `service._validate` and
runs in Python on every response, whatever the gateway claims about structured
output support -- "the API validated it" is not a fact worth depending on when
the failure mode is a confidently-wrong explanation of a money decision.

This client is also allowed to simply not exist. `from_settings()` returns None
when no API key is configured, and the service falls back to the deterministic
template. A missing key is a degraded explanation, never a broken demo.
"""

from __future__ import annotations

from praman.config import settings


class GeminiClient:
    """Thin wrapper. One method, one responsibility: return the model's text."""

    __slots__ = ("_client", "_model", "_timeout")

    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str,
        timeout: float = 20.0,
    ) -> None:
        from openai import OpenAI

        self._client = OpenAI(api_key=api_key, base_url=base_url, timeout=timeout, max_retries=1)
        self._model = model
        self._timeout = timeout

    def complete(self, system: str, user: str) -> str:
        response = self._client.chat.completions.create(
            model=self._model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            # Low but not zero: the explanation should read like prose, and the
            # archetype cache means each distinct decision shape is generated
            # once, so run-to-run variation never shows up mid-demo.
            temperature=0.2,
            max_tokens=400,
        )
        return response.choices[0].message.content or ""


def from_settings(timeout: float = 20.0) -> GeminiClient | None:
    """Build a client, or None if the environment has no key.

    Returning None rather than raising is deliberate: a judge cloning this repo
    has no Gemini key, and every explanation must still render.
    """
    if not settings.gemini_api_key:
        return None
    try:
        return GeminiClient(
            api_key=settings.gemini_api_key,
            base_url=settings.gemini_base_url,
            model=settings.gemini_model,
            timeout=timeout,
        )
    except Exception:
        # A missing openai package, or a malformed base_url. Neither is worth
        # taking the demo down for.
        return None


__all__ = ["GeminiClient", "from_settings"]
