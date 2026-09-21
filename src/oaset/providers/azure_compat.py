"""Azure OpenAI: deployment-style URL + api-version query + api-key auth.

Azure is OpenAI-wire-compatible but NOT base_url-compatible: the path embeds
the deployment (`/openai/deployments/<deployment>/chat/completions`), auth is
the `api-key` header, and every call carries `api-version`. The openai SDK
ships a client for exactly this, so this provider is the stock
OpenAICompatProvider with the client swapped at the existing injection point —
streaming, tools and reasoning handling stay identical.
"""

from __future__ import annotations

from typing import Any

from oaset.providers.openai_compat import OpenAICompatProvider

DEFAULT_API_VERSION = "2024-10-21"


class AzureCompatProvider(OpenAICompatProvider):
    def __init__(
        self,
        *,
        model_id: str,
        api_key: str,
        base_url: str,
        deployment: str,
        api_version: str = DEFAULT_API_VERSION,
        reasoning_key: str | None = None,
        policy: Any = None,
        timeout: float = 300.0,
        first_byte_timeout: float = 45.0,
        client: Any | None = None,
    ):
        if client is None:
            from openai import AsyncAzureOpenAI

            client = AsyncAzureOpenAI(
                azure_endpoint=base_url,
                api_key=api_key or "EMPTY",
                api_version=api_version,
                azure_deployment=deployment,
                timeout=timeout,
                max_retries=0,  # retries are owned by the agent loop
            )
        super().__init__(
            model_id=model_id,
            api_key=api_key or "EMPTY",
            base_url=base_url,
            reasoning_key=reasoning_key,
            timeout=timeout,
            client=client,
            policy=policy,
            first_byte_timeout=first_byte_timeout,
        )

