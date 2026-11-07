"""Settings and loading of the provided data files (pricing, seed keys, gateway config)."""
import json
import os
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


@dataclass
class Settings:
    data_dir: Path = ROOT / "data"
    db_path: str = os.environ.get("PRISM_DB", str(ROOT / "prism.sqlite3"))
    admin_token: str = os.environ.get("PRISM_ADMIN_TOKEN", "prism-admin-demo")
    # Every upstream call has a timeout. A slow provider is abandoned after this long.
    upstream_timeout_s: float = float(os.environ.get("PRISM_UPSTREAM_TIMEOUT", "2.5"))
    upstream_connect_timeout_s: float = 1.0
    cache_scan_limit: int = 500  # newest N cache entries compared per (key, model)


@dataclass
class ProviderConfig:
    name: str
    base_url: str
    api_key: str  # gateway secret: never returned to callers or written to logs


@dataclass
class AliasConfig:
    primary: str
    fallbacks: list = field(default_factory=list)


@dataclass
class RetryConfig:
    max_attempts: int = 3
    initial_backoff_ms: int = 200
    backoff_multiplier: float = 2.0


@dataclass
class GatewayConfig:
    providers: list
    aliases: dict            # alias -> AliasConfig (fast, smart)
    auto_tiers: dict         # {"simple": "fast", "complex": "smart"}
    retry: RetryConfig


def load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_pricing(data_dir):
    """model -> (input_per_1m, output_per_1m) as Decimals, so cost maths is exact."""
    raw = load_json(Path(data_dir) / "model_pricing.json")
    return {m: (Decimal(str(p["input_per_1m"])), Decimal(str(p["output_per_1m"])))
            for m, p in raw.items() if not m.startswith("_")}


def load_seed_keys(data_dir):
    return load_json(Path(data_dir) / "seed_keys.json")["tenants"]


def load_gateway_config(data_dir):
    raw = load_json(Path(data_dir) / "gateway_config.sample.json")
    # Deployment overrides (used by Docker): PRISM_PROVIDER_<NAME>_URL / _API_KEY replace the JSON values,
    # e.g. PRISM_PROVIDER_ALPHA_URL=http://alpha:9001/v1 so containers reach each other by service name.
    providers = []
    for p in raw["providers"]:
        env = "PRISM_PROVIDER_" + p["name"].upper()
        providers.append(ProviderConfig(p["name"], os.environ.get(env + "_URL", p["base_url"]),
                                        os.environ.get(env + "_API_KEY", p["api_key"])))
    aliases, auto = {}, {"simple": "fast", "complex": "smart"}
    for name, spec in raw["model_aliases"].items():
        if "route_by_difficulty" in spec:
            auto = spec["route_by_difficulty"]
        else:
            aliases[name] = AliasConfig(spec["primary"], list(spec.get("fallbacks", [])))
    r = raw.get("retry", {})
    retry = RetryConfig(r.get("max_attempts", 3), r.get("initial_backoff_ms", 200),
                        r.get("backoff_multiplier", 2))
    return GatewayConfig(providers, aliases, auto, retry)


def provider_for_model(model, providers):
    """The mock providers serve '<provider>-small' / '<provider>-large'."""
    for p in providers:
        if model.startswith(p.name + "-"):
            return p.name
    return None
