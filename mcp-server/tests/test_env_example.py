"""`.env.example` is the only place a deployer can discover what is configurable."""

from __future__ import annotations

import re
from pathlib import Path

from dotenv import dotenv_values

_ROOT = Path(__file__).resolve().parent.parent
_EXAMPLE = _ROOT / ".env.example"


def _settings_fields() -> set[str]:
    src = (_ROOT / "app" / "config.py").read_text(encoding="utf-8")
    fields = set(re.findall(r"^\s{4}([a-z_][a-z0-9_]*)\s*:", src, re.M))
    return fields - {"model_config"}


def test_every_setting_is_documented():
    documented = {k.lower() for k in dotenv_values(_EXAMPLE)}
    missing = sorted(_settings_fields() - documented)
    assert not missing, (
        f"settings only discoverable by reading config.py: {missing} — "
        "add them to .env.example"
    )


def test_no_key_is_documented_that_config_does_not_read():
    extra = sorted(k for k in dotenv_values(_EXAMPLE)
                   if k.lower() not in _settings_fields())
    assert not extra, f".env.example documents keys config.py ignores: {extra}"


def test_no_value_swallowed_an_inline_comment():
    swallowed = {k: v for k, v in dotenv_values(_EXAMPLE).items()
                 if v and v.lstrip().startswith("#")}
    assert not swallowed, (
        f"these parse as the comment text, not as empty: {sorted(swallowed)} — "
        "put the comment on its own line ABOVE the key"
    )


def test_the_credentials_are_blank_and_the_file_carries_no_real_secret():
    values = dotenv_values(_EXAMPLE)
    for key in ("RABBITMQ_PASSWORD",):
        assert not values.get(key), f"{key} must ship blank, got {values.get(key)!r}"
    body = _EXAMPLE.read_text(encoding="utf-8")
    # No long hex runs.
    assert not re.search(r"[A-Fa-f0-9]{32,}", body), \
        "a real-looking secret is in .env.example"
