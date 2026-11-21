"""Configuration loader: council.yaml + .env -> ModelEntry objects.

Replaces the old hardcoded COUNCIL_MODELS / CHAIRMAN_MODEL / OpenRouter config.
API keys come only from the environment (key_env in the YAML), never from the
YAML itself. Keys are checked for the council and chairman a run actually uses
(check_keys), so a missing key for one provider doesn't block councils that
don't use it. Ollama needs no key.

`implementers` lists the models that write the code under review; they may not
sit on a review council, not even as chairman (enforced by the CLI).
"""

import os
from dataclasses import dataclass, field

import yaml
from dotenv import load_dotenv

from .providers import ModelEntry

def _default_config_path() -> str:
    # Package-relative first: works for editable installs and `python -m` from
    # the repo root. If the package was installed as a wheel (site-packages),
    # fall back to ./council.yaml in the cwd.
    here = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "council.yaml")
    if os.path.exists(here):
        return here
    return os.path.abspath("council.yaml")


DEFAULT_CONFIG_PATH = _default_config_path()

ANTHROPIC_BASE_URL = "https://api.anthropic.com"

MAX_SEATS = 26  # stage-2 labels are Response A..Z


@dataclass
class CouncilConfig:
    providers: dict = field(default_factory=dict)
    councils: dict[str, list[ModelEntry]] = field(default_factory=dict)
    chairmen: dict[str, ModelEntry] = field(default_factory=dict)
    implementers: set[str] = field(default_factory=set)  # entry names, "provider/model"


def _entry(raw: dict, providers: dict, where: str) -> ModelEntry:
    if not isinstance(raw, dict) or "provider" not in raw or "model" not in raw:
        raise ValueError(f"{where}: each entry needs provider and model, got {raw!r}")
    provider = raw["provider"]
    pconf = dict(providers.get(provider, {}))
    key_env = pconf.pop("key_env", "") or ""
    key = os.getenv(key_env, "") if key_env else ""
    base_url = raw.get("base_url") or pconf.pop("base_url", "")
    if not base_url and provider == "anthropic":
        base_url = ANTHROPIC_BASE_URL
    try:
        timeout = float(raw.get("timeout", pconf.pop("timeout", 120.0)))
        max_tokens = int(raw.get("max_tokens", pconf.pop("max_tokens", 4096)))
    except (TypeError, ValueError) as e:
        raise ValueError(f"{where}: timeout and max_tokens must be numbers ({e})") from e
    if not base_url:
        raise ValueError(
            f"no base_url for provider {provider!r} (set it in council.yaml)"
        )
    return ModelEntry(
        provider=provider,
        model=raw["model"],
        base_url=base_url,
        key=key,
        key_env=key_env,
        timeout=timeout,
        timeout_pinned="timeout" in raw,
        max_tokens=max_tokens,
    )


def check_keys(entries: list[ModelEntry]) -> None:
    """Fail fast if any of these entries needs an env var that is unset."""
    missing = sorted({e.key_env for e in entries if e.key_env and not e.key})
    if missing:
        users = sorted({e.provider for e in entries if e.key_env in missing})
        raise ValueError(
            f"missing env var(s) {', '.join(missing)} required by provider(s) {', '.join(users)}"
        )


def load_config(path: str | None = None) -> CouncilConfig:
    load_dotenv()
    path = path or os.environ.get("COUNCIL_CONFIG", DEFAULT_CONFIG_PATH)
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"council config not found at {path!r} — pass --config, set "
            f"COUNCIL_CONFIG, or run from the repo root (council.yaml)"
        )
    with open(path) as f:
        try:
            raw = yaml.safe_load(f)
        except yaml.YAMLError as e:
            raise ValueError(f"{path}: not valid YAML: {e}") from e
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: expected a mapping with providers/councils/chairmen")
    for key, kind in (("providers", dict), ("councils", dict), ("chairmen", dict), ("implementers", list)):
        if raw.get(key) is not None and not isinstance(raw[key], kind):
            raise ValueError(f"{path}: {key} must be a {'mapping' if kind is dict else 'list'}")
    providers = raw.get("providers") or {}
    if not all(isinstance(p, dict) for p in providers.values()):
        raise ValueError(f"{path}: each provider must be a mapping (base_url, key_env, ...)")
    councils = {}
    for name, entries in (raw.get("councils") or {}).items():
        if not isinstance(entries, list) or not entries:
            raise ValueError(f"{path}: council {name!r} must be a non-empty list of seats")
        if len(entries) > MAX_SEATS:
            raise ValueError(f"{path}: council {name!r} has {len(entries)} seats; at most {MAX_SEATS} "
                             f"(responses are labelled A-Z)")
        seats = [_entry(e, providers, f"{path}: councils.{name}") for e in entries]
        names = [s.name for s in seats]
        dupes = sorted({n for n in names if names.count(n) > 1})
        if dupes:
            # results are keyed by provider/model, so a repeated seat would
            # silently collapse into one
            raise ValueError(
                f"{path}: council {name!r} lists {', '.join(dupes)} more than once; seats must be distinct"
            )
        councils[name] = seats
    chairmen = {
        name: _entry(e, providers, f"{path}: chairmen.{name}")
        for name, e in (raw.get("chairmen") or {}).items()
    }
    implementers = set()
    for e in raw.get("implementers") or []:
        if not isinstance(e, dict) or "provider" not in e or "model" not in e:
            raise ValueError(f"{path}: each implementers entry needs provider and model, got {e!r}")
        implementers.add(f"{e['provider']}/{e['model']}")
    if not councils:
        raise ValueError(f"{path}: no councils defined")
    return CouncilConfig(
        providers=providers, councils=councils, chairmen=chairmen, implementers=implementers
    )
