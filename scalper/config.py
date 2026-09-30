"""Configuration loading: YAML parameters + .env credentials."""
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Union

import yaml
from dotenv import load_dotenv

PAPER_BASE_URL = "https://paper-api.alpaca.markets"


@dataclass
class Credentials:
    api_key: str
    secret_key: str
    base_url: str = PAPER_BASE_URL


@dataclass
class Config:
    raw: dict = field(default_factory=dict)

    def __getattr__(self, item):
        # allow cfg.strategy["ema_fast"] style access via cfg.strategy
        try:
            return self.raw[item]
        except KeyError as e:
            raise AttributeError(item) from e


def tradable_symbols(raw: dict) -> list[str]:
    """Configured symbols minus `reserved_symbols`.

    Reserved symbols belong to another strategy sharing the account (the
    baseline pipeline's basket). The scalper never streams, trades, adopts or
    cancels orders for them.
    """
    reserved = {s.upper() for s in raw.get("reserved_symbols") or []}
    return [s for s in raw["symbols"] if s.upper() not in reserved]


def load_credentials() -> Credentials:
    load_dotenv()
    key = os.getenv("ALPACA_API_KEY")
    secret = os.getenv("ALPACA_SECRET_KEY")
    if not key or not secret:
        raise RuntimeError(
            "Missing ALPACA_API_KEY / ALPACA_SECRET_KEY. "
            "Copy .env.example to .env and fill in your paper trading keys."
        )
    return Credentials(api_key=key, secret_key=secret)


def load_config(path: Union[str, Path] = "config.yaml") -> Config:
    with open(path) as f:
        return Config(raw=yaml.safe_load(f))
