"""Household identity (addresses, phones, Apple ID, ops host) from config.

Real household contact details live only in the private, gitignored
`config.yaml` (shipped to Kavi by deploy.sh; staging ships
config-staging.yaml under the same name). Public clones fall back to
`config.example.yaml`, which carries fictional values. Setting
`HOMEOS_PUBLIC_EXAMPLES=1` (same switch as private_overlay.py) forces the
fictional example config. `KAVI_HOUSEHOLD_CONFIG=<path>` points at any
other config file (tests set it to config.example.yaml so they run on the
fictional values without touching the spec overlay).

Code that used to hard-code a household address or phone reads it from
here instead. Values come from the on-disk config file, NOT from the
config dict a caller passes around, so a partial test config can never
silently change who counts as household.

Config shape (see config.example.yaml):

    imessage:
      megha_phone: "+1..."          # reused as Megha's phone
      max_phone: "+1..."            # reused as Max's phone
    household:
      members:
        megha: {emails: [primary, alias...]}
        max:   {emails: [primary, alias...]}
      kavi_apple_id: "..."
    ops:
      kavi_ssh_host: "user@host"
      kavi_tailnet_addr: "host"

The first address in `emails` is the member's primary Microsoft mailbox.
A missing `household` block raises: an empty household would make the
outbound scanner treat every family handle as a stranger.
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

_RUNTIME_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = _RUNTIME_DIR / "config.yaml"
EXAMPLE_CONFIG_PATH = _RUNTIME_DIR / "config.example.yaml"

MEMBERS = ("megha", "max")

_LOCK = threading.Lock()
_CACHE: dict[str, Any] | None = None


class HouseholdConfigError(RuntimeError):
    pass


def _load() -> dict[str, Any]:
    global _CACHE
    with _LOCK:
        if _CACHE is None:
            path = CONFIG_PATH
            override = os.environ.get("KAVI_HOUSEHOLD_CONFIG")
            if override:
                path = Path(override)
            elif os.environ.get("HOMEOS_PUBLIC_EXAMPLES") == "1":
                path = EXAMPLE_CONFIG_PATH
            elif not path.exists():
                logger.warning(
                    "household: %s missing; using fictional %s", CONFIG_PATH, EXAMPLE_CONFIG_PATH,
                )
                path = EXAMPLE_CONFIG_PATH
            with open(path) as f:
                cfg = yaml.safe_load(f) or {}
            if not isinstance(cfg.get("household"), dict):
                raise HouseholdConfigError(f"household: no `household:` block in {path}")
            _CACHE = cfg
        return _CACHE


def reset_cache() -> None:
    """Drop the cached config (tests that change the config source)."""
    global _CACHE
    with _LOCK:
        _CACHE = None


def _member(name: str) -> dict[str, Any]:
    members = _load()["household"].get("members") or {}
    if name not in members:
        raise HouseholdConfigError(f"household: member {name!r} missing from config")
    return members[name] or {}


def member_emails(name: str) -> list[str]:
    """All of a member's email addresses, primary first."""
    return list(_member(name).get("emails") or [])


def primary_email(name: str) -> str:
    """The member's primary Microsoft mailbox (first entry in `emails`)."""
    emails = member_emails(name)
    if not emails:
        raise HouseholdConfigError(f"household: member {name!r} has no emails")
    return emails[0]


def member_phones(name: str) -> list[str]:
    """The member's iMessage phone(s), from `imessage.<name>_phone`."""
    phone = (_load().get("imessage") or {}).get(f"{name}_phone")
    return [phone] if phone else []


def primary_phone(name: str) -> str:
    phones = member_phones(name)
    if not phones:
        raise HouseholdConfigError(f"household: imessage.{name}_phone missing")
    return phones[0]


def member_handles(name: str) -> set[str]:
    """Phones + emails for one member, as written in config."""
    return set(member_phones(name)) | set(member_emails(name))


def kavi_apple_id() -> str:
    return _load()["household"].get("kavi_apple_id") or ""


def all_handles() -> set[str]:
    """Every household iMessage/email handle, plus Kavi's own Apple ID."""
    out: set[str] = set()
    for name in MEMBERS:
        out |= member_handles(name)
    kavi = kavi_apple_id()
    if kavi:
        out.add(kavi)
    return out


def member_for_handle(handle: str | None) -> str | None:
    """'megha' / 'max' for a known phone or email (case-insensitive), else None."""
    if not handle:
        return None
    h = handle.strip().lower()
    for name in MEMBERS:
        if h in {x.lower() for x in member_handles(name)}:
            return name
    return None


def kavi_ssh_host() -> str:
    """ssh target for Kavi's Mac (user@host), from `ops.kavi_ssh_host`."""
    return (_load().get("ops") or {}).get("kavi_ssh_host") or ""


def kavi_tailnet_addr() -> str:
    """Kavi's Mac on the private mesh, from `ops.kavi_tailnet_addr`."""
    return (_load().get("ops") or {}).get("kavi_tailnet_addr") or ""
