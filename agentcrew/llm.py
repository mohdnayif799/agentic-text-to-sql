"""LLM abstraction and providers.

Design note: structured output, not a free-form tool loop
--------------------------------------------------------
A common agent pattern is to hand the model a bag of tools and let it decide
what to call until it declares itself done. We deliberately do not do that for
control flow. In this system **the graph decides what happens next; the model
only decides content**. Each call returns one validated Pydantic object.

The reason is failure containment. A model-driven loop can wander, repeat
itself, or never terminate, and those are the most common multi-agent failure
modes reported in the literature. A graph-driven loop has a bounded number of
states and a retry budget you can actually reason about and unit-test.

Tool *use* still happens - the agent inspects the database through real tools -
but the orchestrator sequences it.

Provider APIs verified against anthropic==1.0.0 and openai==3.3.1 on
2026-08-25 by introspecting the installed packages, not from memory. Both had
recent major-version bumps.
"""

from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

T = TypeVar("T", bound=BaseModel)

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


class LLMError(RuntimeError):
    """Unrecoverable problem talking to the provider."""


class StructuredOutputError(LLMError):
    """The model could not produce a valid object for the requested schema."""


class QuotaExhaustedError(LLMError):
    """The provider refused the call for quota or rate-limit reasons.

    Distinct from every other failure because it says nothing about the agent's
    behaviour - the request never reached a model. The evaluation harness uses
    this to stop cleanly and mark the remaining questions as *not evaluated*
    rather than scoring them as failures.

    Deliberately NOT raised for transient overload (503 / "high demand"). That
    is a real failure of the run and should be counted as one.
    """


#: Substrings that indicate quota or rate limiting across the three providers:
#: Gemini raises RESOURCE_EXHAUSTED, OpenAI insufficient_quota or
#: rate_limit_exceeded, Anthropic rate_limit_error - all under HTTP 429.
_QUOTA_MARKERS = (
    "resource_exhausted",
    "insufficient_quota",
    "rate_limit",
    "ratelimit",
    "quota exceeded",
    "quota_exceeded",
    "exceeded your current quota",
    "too many requests",
    "429",
)


def is_quota_error(exc: BaseException | str | None) -> bool:
    """True when a provider error is a quota or rate-limit refusal.

    Checks structured attributes first (SDKs expose `status_code` or `code`),
    then falls back to the message. The string path matters because
    `run_baseline` catches broadly and keeps only the text, so the harness has
    to be able to classify from that alone.
    """
    if exc is None:
        return False
    if isinstance(exc, QuotaExhaustedError):
        return True
    if isinstance(exc, BaseException):
        for attr in ("status_code", "code", "http_status"):
            if getattr(exc, attr, None) in (429, "429"):
                return True
        text = f"{type(exc).__name__}: {exc}"
    else:
        text = str(exc)
    lowered = text.lower()
    return any(marker in lowered for marker in _QUOTA_MARKERS)


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    calls: int = 0

    def add(self, other: Usage) -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.calls += other.calls


@dataclass
class LLMResponse:
    text: str
    usage: Usage = field(default_factory=Usage)
    model: str = ""


def extract_json(raw: str) -> str:
    """Pull a JSON object out of a model response.

    Handles the three things models actually do: return bare JSON, wrap it in a
    ``` fence, or prepend a sentence of commentary.
    """
    raw = raw.strip()
    fenced = _FENCE_RE.search(raw)
    if fenced:
        return fenced.group(1).strip()
    start = raw.find("{")
    end = raw.rfind("}")
    if start != -1 and end > start:
        return raw[start : end + 1]
    return raw


class LLMClient(ABC):
    """Minimal provider interface. Two methods, both easy to fake."""

    name: str = "base"

    @abstractmethod
    def complete(
        self,
        *,
        system: str,
        user: str,
        max_tokens: int = 2048,
        temperature: float = 0.0,
    ) -> LLMResponse:
        """Single-turn completion."""

    def complete_structured(
        self,
        *,
        system: str,
        user: str,
        schema: type[T],
        max_tokens: int = 2048,
        temperature: float = 0.0,
        repair_attempts: int = 1,
    ) -> tuple[T, Usage]:
        """Completion validated against a Pydantic model.

        On a validation failure the model is shown its own output and the
        validation error and asked to fix it. One repair by default: if a model
        cannot emit the schema twice, retrying more is usually wasted spend.
        """
        contract = json.dumps(schema.model_json_schema(), indent=2)
        instruction = (
            f"{system}\n\n"
            "Respond with a single JSON object and nothing else. No prose, no "
            "code fence, no explanation outside the JSON.\n"
            f"It must validate against this JSON Schema:\n{contract}"
        )

        total = Usage()
        prompt = user
        last_error = ""

        for attempt in range(repair_attempts + 1):
            response = self.complete(
                system=instruction,
                user=prompt,
                max_tokens=max_tokens,
                temperature=temperature,
            )
            total.add(response.usage)
            candidate = extract_json(response.text)
            try:
                return schema.model_validate_json(candidate), total
            except (ValidationError, ValueError) as exc:
                last_error = str(exc)[:800]
                prompt = (
                    f"{user}\n\n"
                    f"Your previous reply was rejected.\n"
                    f"--- your reply ---\n{response.text[:1500]}\n"
                    f"--- validation error ---\n{last_error}\n"
                    "Return corrected JSON only."
                )
                if attempt == repair_attempts:
                    break

        raise StructuredOutputError(
            f"{self.name} could not produce valid {schema.__name__}: {last_error}"
        )


class AnthropicClient(LLMClient):
    name = "anthropic"

    def __init__(self, api_key: str | None, model: str) -> None:
        try:
            from anthropic import Anthropic
        except ImportError as exc:  # pragma: no cover
            raise LLMError("pip install anthropic") from exc
        if not api_key:
            raise LLMError("AGENTCREW_ANTHROPIC_API_KEY is not set.")
        self._client = Anthropic(api_key=api_key)
        self._model = model

    def complete(
        self,
        *,
        system: str,
        user: str,
        max_tokens: int = 2048,
        temperature: float = 0.0,
    ) -> LLMResponse:
        try:
            msg = self._client.messages.create(
                model=self._model,
                max_tokens=max_tokens,
                temperature=temperature,
                system=system,
                messages=[{"role": "user", "content": user}],
            )
        except Exception as exc:
            if is_quota_error(exc):
                raise QuotaExhaustedError(f"Anthropic quota/rate limit: {exc}") from exc
            raise LLMError(f"Anthropic call failed: {exc}") from exc

        parts = [b.text for b in msg.content if getattr(b, "type", "") == "text"]
        return LLMResponse(
            text="".join(parts),
            usage=Usage(
                input_tokens=getattr(msg.usage, "input_tokens", 0),
                output_tokens=getattr(msg.usage, "output_tokens", 0),
                calls=1,
            ),
            model=self._model,
        )


class OpenAIClient(LLMClient):
    name = "openai"

    def __init__(
        self, api_key: str | None, model: str, base_url: str | None = None
    ) -> None:
        try:
            from openai import OpenAI
        except ImportError as exc:  # pragma: no cover
            raise LLMError("pip install openai") from exc
        if not api_key:
            raise LLMError("AGENTCREW_OPENAI_API_KEY is not set.")
        self._client = OpenAI(api_key=api_key, base_url=base_url)
        self._model = model

    def complete(
        self,
        *,
        system: str,
        user: str,
        max_tokens: int = 2048,
        temperature: float = 0.0,
    ) -> LLMResponse:
        try:
            resp = self._client.chat.completions.create(
                model=self._model,
                max_completion_tokens=max_tokens,
                temperature=temperature,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            )
        except Exception as exc:
            if is_quota_error(exc):
                raise QuotaExhaustedError(f"OpenAI quota/rate limit: {exc}") from exc
            raise LLMError(f"OpenAI call failed: {exc}") from exc

        usage = resp.usage
        return LLMResponse(
            text=resp.choices[0].message.content or "",
            usage=Usage(
                input_tokens=getattr(usage, "prompt_tokens", 0) if usage else 0,
                output_tokens=getattr(usage, "completion_tokens", 0) if usage else 0,
                calls=1,
            ),
            model=self._model,
        )


Responder = Callable[[str, str], str | dict[str, Any]]


class GeminiClient(LLMClient):
    name = "gemini"

    def __init__(self, api_key: str | None, model: str) -> None:
        try:
            from google import genai
        except ImportError as exc:  # pragma: no cover
            raise LLMError("pip install google-genai") from exc
        if not api_key:
            raise LLMError("AGENTCREW_GEMINI_API_KEY is not set.")
        self._genai = genai
        self._client = genai.Client(api_key=api_key)
        self._model = model

    def complete(
        self,
        *,
        system: str,
        user: str,
        max_tokens: int = 2048,
        temperature: float = 0.0,
    ) -> LLMResponse:
        from google.genai import types

        def call(with_sampling: bool):
            kwargs: dict[str, Any] = {
                "system_instruction": system,
                "max_output_tokens": max_tokens,
            }
            if with_sampling:
                kwargs["temperature"] = temperature
            return self._client.models.generate_content(
                model=self._model,
                contents=user,
                config=types.GenerateContentConfig(**kwargs),
            )

        try:
            resp = call(with_sampling=True)
        except Exception as exc:
            # Google deprecated temperature/top_p/top_k for Gemini 3. They are
            # currently ignored rather than rejected, but if a model starts
            # refusing them we retry without rather than failing the run.
            if is_quota_error(exc):
                raise QuotaExhaustedError(f"Gemini quota/rate limit: {exc}") from exc
            if "temperature" in str(exc).lower():
                try:
                    resp = call(with_sampling=False)
                except Exception as inner:
                    if is_quota_error(inner):
                        raise QuotaExhaustedError(
                            f"Gemini quota/rate limit: {inner}"
                        ) from inner
                    raise LLMError(f"Gemini call failed: {inner}") from inner
            else:
                raise LLMError(f"Gemini call failed: {exc}") from exc

        usage = getattr(resp, "usage_metadata", None)
        return LLMResponse(
            text=resp.text or "",
            usage=Usage(
                input_tokens=getattr(usage, "prompt_token_count", 0) or 0,
                output_tokens=getattr(usage, "candidates_token_count", 0) or 0,
                calls=1,
            ),
            model=self._model,
        )


class FakeClient(LLMClient):
    """Scripted client used by tests, CI and the offline demo.

    This is not a mock in the "assert it was called" sense - it is a real
    implementation backed by rules. It lets the entire graph, including the
    repair loop and the budget logic, be exercised deterministically with no
    API key and no network. Every end-to-end test in this repo runs on it.
    """

    name = "fake"

    def __init__(
        self,
        responses: Sequence[str | dict[str, Any]] | None = None,
        *,
        responder: Responder | None = None,
    ) -> None:
        self._queue = list(responses or [])
        self._responder = responder
        self.prompts: list[tuple[str, str]] = []

    def complete(
        self,
        *,
        system: str,
        user: str,
        max_tokens: int = 2048,
        temperature: float = 0.0,
    ) -> LLMResponse:
        self.prompts.append((system, user))

        if self._queue:
            payload = self._queue.pop(0)
        elif self._responder is not None:
            payload = self._responder(system, user)
        else:
            raise LLMError("FakeClient exhausted: no scripted response left.")

        text = payload if isinstance(payload, str) else json.dumps(payload)
        return LLMResponse(
            text=text,
            usage=Usage(
                input_tokens=len(system) // 4 + len(user) // 4,
                output_tokens=len(text) // 4,
                calls=1,
            ),
            model="fake-model",
        )


@dataclass(frozen=True)
class ProviderSpec:
    """Everything the app needs to know about a provider, in one place.

    This is the single source of truth. Model defaults used to be duplicated
    between `config.resolved_model()` and a `DEFAULT_MODELS` dict here, which
    is how `provider="openai"` ended up silently keeping a Claude model name.
    Both now read this registry, so the two cannot disagree.

    `models[0]` is the default for that provider.
    """

    key: str
    label: str
    key_field: str
    """Name of the Settings field holding this provider's credential."""
    key_prefixes: tuple[str, ...]
    """Accepted key prefixes. EMPTY means 'make no assumption'.

    Only assert a format where the provider actually guarantees one. Google is
    migrating Gemini keys from `AIza...` to `AQ....`, so both are valid and a
    hardcoded check rejects legitimate keys. A credential format is the
    provider's to change, not ours to predict.
    """
    key_hint: str
    """Placeholder text for the UI. Illustrative only, never validated."""
    models: tuple[str, ...]
    install: str


PROVIDERS: dict[str, ProviderSpec] = {
    "anthropic": ProviderSpec(
        key="anthropic",
        label="Anthropic",
        key_field="anthropic_api_key",
        key_prefixes=("sk-ant-",),
        key_hint="sk-ant-...",
        models=(
            "claude-sonnet-4-5-20250929",
            "claude-opus-4-1-20250805",
            "claude-haiku-4-5-20251001",
        ),
        install="pip install anthropic",
    ),
    "openai": ProviderSpec(
        key="openai",
        label="OpenAI",
        key_field="openai_api_key",
        key_prefixes=("sk-",),
        key_hint="sk-...",
        models=("gpt-4.1", "gpt-4.1-mini", "gpt-4o"),
        install="pip install openai",
    ),
    "gemini": ProviderSpec(
        key="gemini",
        label="Google Gemini",
        key_field="gemini_api_key",
        # No prefix check: Google is migrating from `AIza...` to `AQ....` and
        # both are currently valid.
        key_prefixes=(),
        key_hint="AQ.... or AIza...",
        # Verified against Google's model docs, 2026-08-25. Gemini 2.0 Flash
        # and 2.0 Flash-Lite were shut down on 2026-06-01, and 2.5 Pro is no
        # longer offered to new users - all three return 404. Default is a GA
        # Flash model that does not require billing, so a reviewer with a free
        # key can run the project.
        models=(
            "gemini-3.5-flash",       # GA, billing not required - free-tier default
            "gemini-3.7-flash",       # newest/strongest Flash (paid)
            "gemini-3.5-flash-lite",  # GA, cheapest
            "gemini-3.1-flash-lite",  # GA, billing not required
            "gemini-3.1-pro-preview",  # flagship, no free tier
        ),
        install="pip install google-genai",
    ),
    "fake": ProviderSpec(
        key="fake",
        label="Fake (offline tests)",
        key_field="",
        key_prefixes=(),
        key_hint="",
        models=("fake-model",),
        install="",
    ),
}

#: Providers a person can pick in the UI. `fake` is test-only.
SELECTABLE_PROVIDERS: tuple[str, ...] = ("anthropic", "openai", "gemini")


def default_model(provider: str) -> str:
    """Default model for a provider. Raises for an unknown provider."""
    try:
        return PROVIDERS[provider].models[0]
    except KeyError as exc:
        raise LLMError(f"Unknown provider: {provider}") from exc


def build_client(settings: Any) -> LLMClient:
    """Instantiate the configured provider."""
    provider = settings.provider
    spec = PROVIDERS.get(provider)
    if spec is None:
        raise LLMError(f"Unknown provider: {provider}")

    model = settings.model or spec.models[0]

    if provider == "anthropic":
        return AnthropicClient(settings.anthropic_api_key, model)
    if provider == "openai":
        return OpenAIClient(
            settings.openai_api_key, model, base_url=settings.openai_base_url
        )
    if provider == "gemini":
        return GeminiClient(settings.gemini_api_key, model)
    if provider == "fake":
        return FakeClient()
    raise LLMError(f"Unknown provider: {provider}")
