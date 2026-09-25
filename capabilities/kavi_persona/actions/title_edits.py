"""kavi-persona title editors.

Phase 4 (2026-06-02): physical move out of `kavi_runtime/handlers.py`.

Pure functions that transform an MS To Do task title preserving the
[?] (low-confidence) / owner (MJ/MM) / [Tag] structure. Used by the
correction handler and the action layer to make targeted edits.
"""

from __future__ import annotations

OWNER_ABBREV = {"megha": "MJ", "max": "MM"}


def _strip_kavi_prefix(title: str) -> str:
    """Strip the [?] / owner / [Tag] prefix from a Kavi-formatted title,
    returning just the human-readable body. Used for ack-message readability."""
    if title.startswith("[?] "):
        title = title[4:]
    if len(title) >= 3 and title[2] == " " and title[:2] in OWNER_ABBREV.values():
        title = title[3:]
    if title.startswith("["):
        end = title.find("] ")
        if end != -1:
            title = title[end + 2:]
    return title


def _swap_owner_in_title(title: str, new_owner: str) -> str:
    """Replace the owner abbreviation (MJ/MM) in the title prefix.
    Preserves [?] and tag."""
    new_abbrev = OWNER_ABBREV.get(new_owner, "??")
    prefix = ""
    if title.startswith("[?] "):
        prefix = "[?] "
        title = title[4:]
    if len(title) >= 3 and title[2] == " " and title[:2] in OWNER_ABBREV.values():
        title = new_abbrev + title[2:]
    else:
        title = f"{new_abbrev} {title}"
    return prefix + title


def _replace_body_in_title(title: str, new_body: str) -> str:
    """Replace the human-readable body of the title; preserve [?] / owner / [Tag]."""
    prefix_parts = []
    rest = title
    if rest.startswith("[?] "):
        prefix_parts.append("[?]")
        rest = rest[4:]
    if len(rest) >= 3 and rest[2] == " " and rest[:2] in OWNER_ABBREV.values():
        prefix_parts.append(rest[:2])
        rest = rest[3:]
    if rest.startswith("["):
        end = rest.find("] ")
        if end != -1:
            prefix_parts.append(rest[:end + 1])
            rest = rest[end + 2:]
    prefix_parts.append(new_body)
    return " ".join(prefix_parts)


def _replace_tag_in_title(title: str, new_tag: str) -> str:
    """Replace just the [Tag] portion. Preserves [?] / owner / body."""
    prefix_parts = []
    rest = title
    if rest.startswith("[?] "):
        prefix_parts.append("[?]")
        rest = rest[4:]
    if len(rest) >= 3 and rest[2] == " " and rest[:2] in OWNER_ABBREV.values():
        prefix_parts.append(rest[:2])
        rest = rest[3:]
    if rest.startswith("["):
        end = rest.find("] ")
        if end != -1:
            rest = rest[end + 2:]
    prefix_parts.append(f"[{new_tag}]")
    prefix_parts.append(rest)
    return " ".join(prefix_parts)


def _adjust_confidence_marker(title: str, new_confidence: str) -> str:
    """Add or strip the [?] prefix based on new confidence.
    high/medium = no marker; low = [?]."""
    has_marker = title.startswith("[?] ")
    body = title[4:] if has_marker else title
    if new_confidence == "low":
        return f"[?] {body}"
    return body


__all__ = [
    "OWNER_ABBREV",
    "_strip_kavi_prefix",
    "_swap_owner_in_title",
    "_replace_body_in_title",
    "_replace_tag_in_title",
    "_adjust_confidence_marker",
]
