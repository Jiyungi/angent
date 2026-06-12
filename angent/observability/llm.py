"""TrueFoundry gateway client factory with the Langfuse OpenAI drop-in.

Best practice (Langfuse skill → references/instrumentation.md): **prefer the
framework integration over manual instrumentation**. For the OpenAI SDK that
means importing Langfuse's drop-in replacement — ``from langfuse.openai import
OpenAI`` — which automatically records every call as a *generation* observation
with the model name, token usage, cost, latency and API errors, linked to the
currently active trace/span. This is strictly better than hand-rolling spans
around each call.

All of Angent's LLM reasoning + email prose goes through the TrueFoundry
gateway via the OpenAI SDK with a custom ``base_url``. :func:`build_openai_client`
returns the Langfuse-wrapped client when Langfuse is installed AND configured
(``LANGFUSE_PUBLIC_KEY`` + ``LANGFUSE_SECRET_KEY`` present), and otherwise falls
back to the plain OpenAI client — so the loop runs identically whether or not
tracing is enabled (Requirement 13.3 / 18.9).

Import order note (a common Langfuse mistake): we import the wrapped client
lazily, inside the factory, which runs after ``load_dotenv()`` in
:mod:`angent.config`, so credentials are present when Langfuse initializes.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

logger = logging.getLogger("angent.observability.llm")


def langfuse_configured() -> bool:
    """True when Langfuse credentials are present in the environment.

    Mirrors the SDK's own check: a public + secret key must be set. Used so the
    factory only attempts the wrapped client when tracing can actually work.
    """
    return bool(
        os.environ.get("LANGFUSE_PUBLIC_KEY") and os.environ.get("LANGFUSE_SECRET_KEY")
    )


def build_openai_client(base_url: str, api_key: str) -> Any:
    """Return an OpenAI client for the TrueFoundry gateway, traced if possible.

    Returns the Langfuse drop-in (`langfuse.openai.OpenAI`) when Langfuse is
    installed and configured — giving automatic generation tracing with model,
    tokens, cost and errors — otherwise the plain `openai.OpenAI`. Raises
    ``ImportError`` only if the base OpenAI SDK itself is missing (a declared
    dependency), which callers already handle.
    """
    if langfuse_configured():
        try:
            from langfuse.openai import OpenAI as LangfuseOpenAI  # type: ignore

            logger.debug("Using Langfuse-wrapped OpenAI client (auto-traced generations).")
            return LangfuseOpenAI(base_url=base_url, api_key=api_key)
        except Exception as exc:  # noqa: BLE001 - any failure -> fall back to plain SDK
            logger.warning(
                "Langfuse OpenAI drop-in unavailable (%s); using untraced OpenAI client.",
                exc,
            )

    from openai import OpenAI  # plain SDK fallback

    return OpenAI(base_url=base_url, api_key=api_key)
