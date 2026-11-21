"""Configuration loader: council.yaml + .env -> ModelEntry objects.

Replaces the old hardcoded COUNCIL_MODELS / CHAIRMAN_MODEL / OpenRouter config.
API keys come only from the environment (key_env in the YAML), never from the
YAML itself. A provider in use whose key_env is unset fails fast at startup
(except Ollama, which needs no key).
"""

import os
from dataclasses import dataclass, field

import yaml
from dotenv import load_dotenv

from .providers import ModelEntry

DEFAULT_CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "council.yaml"
)


@dataclass
class CouncilConfig:
    providers: dict = field(default_factory=dict)
    councils: dict[str, list[ModelEntry]] = field(default_factory=dict)
    chairmen: dict[str, ModelEntry] = field(default_factory=dict)


def _entry(raw: dict, providers: dict) -> ModelEntry:
    provider = raw["provider"]
    pconf = dict(providers.get(provider, {}))
    key_env = pconf.pop("key_env", None)
    if key_env:
        key = os.getenv(key_env, "")
        if not key:
            raise ValueError(
                f"missing env var {key_env} required by provider {provider!r}"
            )
    else:
        key = pconf.pop("key", "")
    base_url = raw.get("base_url") or pconf.pop("base_url", "")
    timeout = float(raw.get("timeout", pconf.pop("timeout", 120.0)))
    max_tokens = int(raw.get("max_tokens", pconf.pop("max_tokens", 4096)))
    if not base_url and provider != "anthropic":
        raise ValueError(
            f"no base_url for provider {provider!r} (set it in council.yaml)"
        )
    return ModelEntry(
        provider=provider,
        model=raw["model"],
        base_url=base_url,
        key=key,
        timeout=timeout,
        max_tokens=max_tokens,
    )


def load_config(path: str | None = None) -> CouncilConfig:
    load_dotenv()
    path = path or os.environ.get("COUNCIL_CONFIG", DEFAULT_CONFIG_PATH)
    with open(path) as f:
        raw = yaml.safe_load(f)
    providers = raw.get("providers", {})
    councils = {
        name: [_entry(e, providers) for e in entries]
        for name, entries in raw.get("councils", {}).items()
    }
    chairmen = {
        name: _entry(e, providers) for name, e in raw.get("chairmen", {}).items()
    }
    if not councils:
        raise ValueError(f"{path}: no councils defined")
    return CouncilConfig(providers=providers, councils=councils, chairmen=chairmen)
