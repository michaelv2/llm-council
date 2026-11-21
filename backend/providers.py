"""Multi-provider transport layer for the LLM Council.

Two wire formats cover all supported platforms:
- OpenAI-compatible chat completions (Ollama, Mistral, Fireworks):
  POST {base_url}/chat/completions with Bearer auth.
- Anthropic messages API: POST https://api.anthropic.com/v1/messages with
  x-api-key + anthropic-version headers and a mandatory max_tokens.

All transports return the same normalized shape:
{"content": str, "reasoning_details": ... | None}, or None on failure.

Gotchas handled here:
- Mistral's API returns message.content as a LIST of typed parts for reasoning
  models ([{"type": "thinking", ...}, {"type": "text", ...}]), not a string —
  see _message_content.
- Diagnostics go to stderr so `council --json` stdout stays machine-readable.
- Every call is timed into ProviderRegistry.calls (model, start offset,
  elapsed, ok) so slow seats are visible in the CLI output.
"""

import asyncio
import sys
import time
from dataclasses import dataclass
from typing import Any, Optional

import httpx

# Statuses worth one retry (rate limits / transient server errors). 520-524
# are Cloudflare's origin errors, seen live from api.mistral.ai.
RETRYABLE_STATUS = {429, 500, 502, 503, 504, 520, 521, 522, 523, 524}


def _message_content(message: dict) -> tuple[str, Any]:
    """Normalize a chat-completion message to (text, reasoning_details).

    Handles both content shapes seen in the wild:
    - plain string (OpenAI, Ollama, most OpenAI-compatible servers)
    - list of typed parts (Mistral reasoning models):
      [{"type": "thinking", "thinking": [{"type": "text", "text": ...}]},
       {"type": "text", "text": ...}]
    """
    content = message.get("content")
    if content is None:
        return "", message.get("reasoning_details")
    if isinstance(content, str):
        return content, message.get("reasoning_details")
    if isinstance(content, list):
        texts: list[str] = []
        reasoning: list[str] = []
        for part in content:
            if not isinstance(part, dict):
                continue
            ptype = part.get("type")
            if ptype == "text":
                texts.append(part.get("text", ""))
            elif ptype == "thinking":
                for block in part.get("thinking", []):
                    if isinstance(block, dict) and block.get("type") == "text":
                        reasoning.append(block.get("text", ""))
        return "".join(texts), (reasoning or None)
    return str(content), message.get("reasoning_details")


@dataclass
class ModelEntry:
    provider: str            # "ollama" | "mistral" | "fireworks" | "anthropic"
    model: str               # provider-specific model id
    base_url: str = ""
    key: str = ""
    key_env: str = ""        # env var the key came from (for check_keys)
    timeout: float = 120.0
    max_tokens: int = 4096   # required by the Anthropic API

    @property
    def name(self) -> str:
        return f"{self.provider}/{self.model}"


class ProviderRegistry:
    """Holds ModelEntry objects and dispatches queries to the right transport."""

    def __init__(self, entries: list[ModelEntry]):
        self._entries = {e.name: e for e in entries}
        self._t0 = time.monotonic()
        self.calls: list[dict] = []

    def get(self, name: str) -> ModelEntry:
        return self._entries[name]

    async def query(
        self,
        entry: ModelEntry,
        messages: list[dict],
        timeout: Optional[float] = None,
    ) -> Optional[dict]:
        timeout = timeout if timeout is not None else entry.timeout
        start = time.monotonic()
        result = await self._query_with_retry(entry, messages, timeout)
        self.calls.append({
            "model": entry.name,
            "start_s": round(start - self._t0, 1),
            "elapsed_s": round(time.monotonic() - start, 1),
            "ok": result is not None,
        })
        return result

    async def _query_with_retry(
        self, entry: ModelEntry, messages: list[dict], timeout: float
    ) -> Optional[dict]:
        for attempt in (1, 2):
            try:
                if entry.provider == "anthropic":
                    return await self._query_anthropic(entry, messages, timeout)
                return await self._query_openai_compatible(entry, messages, timeout)
            except httpx.HTTPStatusError as e:
                status = e.response.status_code
                if attempt == 1 and status in RETRYABLE_STATUS:
                    await asyncio.sleep(2.0)
                    continue
                print(f"[council] {entry.name} failed (HTTP {status}): {e}", file=sys.stderr)
                return None
            except Exception as e:
                # include the type: TimeoutError etc. have an empty str(e)
                print(f"[council] {entry.name} failed: {type(e).__name__}: {e}", file=sys.stderr)
                return None
        return None

    async def query_parallel(
        self,
        entries: list[ModelEntry],
        messages: list[dict],
        timeout: Optional[float] = None,
    ) -> dict[str, Optional[dict]]:
        """Query all entries in parallel; dict maps entry name -> response or None."""
        responses = await asyncio.gather(
            *[self.query(e, messages, timeout) for e in entries]
        )
        return {e.name: r for e, r in zip(entries, responses)}

    async def _query_openai_compatible(
        self, entry: ModelEntry, messages: list[dict], timeout: float
    ) -> dict:
        headers = {"Content-Type": "application/json"}
        if entry.key:  # keyless (Ollama): send no credential rather than a fake one
            headers["Authorization"] = f"Bearer {entry.key}"
        payload = {"model": entry.model, "messages": messages}
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                f"{entry.base_url.rstrip('/')}/chat/completions",
                headers=headers,
                json=payload,
            )
            response.raise_for_status()
            message = response.json()["choices"][0]["message"]
            content, reasoning = _message_content(message)
            return {"content": content, "reasoning_details": reasoning}

    async def _query_anthropic(
        self, entry: ModelEntry, messages: list[dict], timeout: float
    ) -> dict:
        # The Anthropic API takes the system prompt as a parameter, not a message.
        system = "\n\n".join(
            m["content"] for m in messages if m.get("role") == "system"
        )
        api_messages = [
            {"role": m["role"], "content": m["content"]}
            for m in messages
            if m.get("role") != "system"
        ]
        headers = {
            "x-api-key": entry.key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }
        payload: dict[str, Any] = {
            "model": entry.model,
            "messages": api_messages,
            "max_tokens": entry.max_tokens,
        }
        if system:
            payload["system"] = system
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                f"{(entry.base_url or 'https://api.anthropic.com').rstrip('/')}/v1/messages",
                headers=headers,
                json=payload,
            )
            response.raise_for_status()
            data = response.json()
            text = "".join(
                block.get("text", "")
                for block in data.get("content", [])
                if block.get("type") == "text"
            )
            return {"content": text, "reasoning_details": None}
