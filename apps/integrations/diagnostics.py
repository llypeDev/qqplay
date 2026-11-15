"""Why sign-ins through a site fail, for whoever is setting the site up.

The last refusals of each integration are kept for two weeks (in the cache:
they are a help while integrating, not a record), and the studio can read a
token the way the sign-in would, saying what is wrong with it. Neither ever
shows a secret, and a token's content is only shown to the integration's
owner, who made it.
"""
from __future__ import annotations

import base64
import json

from django.core.cache import cache
from django.utils import timezone

from . import tokens

KEEP = 30
TTL = 14 * 24 * 3600


def _key(integration) -> str:
    return f"integrations:refusals:{integration.pk}"


def record(integration, code: str, detail: str, origin: str = "") -> None:
    """Remember a refused sign-in: the reason and the site it came from."""
    items = cache.get(_key(integration)) or []
    items.insert(0, {
        "at": timezone.now().isoformat(),
        "code": code,
        "detail": str(detail)[:200],
        "origin": str(origin)[:120],
    })
    cache.set(_key(integration), items[:KEEP], timeout=TTL)


def recent(integration) -> list[dict]:
    return cache.get(_key(integration)) or []


def _part(value: str):
    try:
        data = json.loads(base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)))
    except (ValueError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def explain(token: str, integration) -> dict:
    """What the sign-in would make of this token: accepted or why not, and
    what it says, so a wrong claim is seen at a glance."""
    token = (token or "").strip()
    parts = token.split(".")
    result = {"ok": False, "error": "", "header": None, "claims": None, "person": None, "kind": "person"}
    if len(parts) == 3:
        result["header"] = _part(parts[0])
        result["claims"] = _part(parts[1])
    claims = result["claims"] or {}
    result["kind"] = "api" if claims.get("scope") == "api" else "person"
    try:
        if result["kind"] == "api":
            found = tokens.verify_api(token)
        else:
            person = tokens.verify(token)
            found = person.integration
            result["person"] = {
                "id": person.external_id, "name": person.name, "moderator": person.moderator,
                "groups": person.groups, "picture": bool(person.picture),
            }
    except tokens.TokenError as error:
        result["error"] = str(error)
        return result
    if found.pk != integration.pk:
        result["person"] = None
        result["error"] = "other"
        return result
    result["ok"] = True
    return result
