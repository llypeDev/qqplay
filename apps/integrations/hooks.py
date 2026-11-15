"""Purchases announced by the sales platforms, turned into enrolments.

A school that sells on Hotmart, Kiwify and the like has no server of its
own, but the platform posts every purchase, refund and cancellation to an
address the seller gives it. That address is this module's:

    POST /api/integrations/hooks/<integration key>/<secret>/
         ?platform=hotmart      which platform is posting (optional: without
                                it the fields are looked for where the
                                platforms known here put them)
         ?classes=turma-a       classes the buyers of this product join; one
                                address per product does it

A purchase that gives access puts the buyer's e-mail on the integration's
list of enrolments; a refund, chargeback or cancellation turns it off there
(and ends their session). Anything else is answered and ignored: platforms
repeat what gets an error, and a webhook that keeps failing is often turned
off by them.

Who is posting is settled by the secret in the address, which only the
seller pasted into the platform. Where the platform also signs or tags its
posts, and its token is saved in the integration, that is checked too.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
from dataclasses import dataclass

from django.core.cache import cache
from django.http import HttpResponse, JsonResponse
from django.utils import timezone
from django.utils.crypto import constant_time_compare
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from apps.live.models import parse_groups

from . import enrolments
from .models import Integration

#: Words of an event or status that mean the access ended, and that it began.
#: The first are looked for first: "subscription_canceled" is not an "active".
REVOKE_WORDS = {
    "refunded", "refund", "chargeback", "chargedback", "canceled", "cancelled", "cancellation", "expired",
    "revoked", "removed", "inactive", "dispute", "reembolsado", "reembolsada", "reembolso", "estornado",
    "estornada", "cancelado", "cancelada", "cancelamento", "expirado", "expirada",
}
GRANT_WORDS = {
    "approved", "paid", "complete", "completed", "authorized", "active", "activated", "granted", "renewed",
    "aprovado", "aprovada", "pago", "paga", "completo", "completa", "liberado", "liberada", "ativo", "ativa",
}

#: Where each platform puts things, as its documentation shows them. A path
#: is the keys from the top of the post to the value, dots between them.
#: "grant" and "revoke" are the exact values of the status field; a platform
#: without them here is read by the words above.
PRESETS: dict[str, dict] = {
    # developers.hotmart.com/docs/pt-BR/2.0.0/webhook/purchase-webhook (Webhook 2.0.0)
    "hotmart": {
        "email": ["data.buyer.email", "data.subscriber.email"],
        "name": ["data.buyer.name", "data.subscriber.name"],
        "product": ["data.product.id", "data.product.name"],
        "status": ["event"],
        "grant": {"PURCHASE_APPROVED", "PURCHASE_COMPLETE"},
        "revoke": {"PURCHASE_REFUNDED", "PURCHASE_CHARGEBACK", "PURCHASE_CANCELED", "PURCHASE_EXPIRED",
                   "SUBSCRIPTION_CANCELLATION"},
        "check": lambda request, token, data: _same(request.headers.get("X-HOTMART-HOTTOK", ""), token),
        "header": "X-HOTMART-HOTTOK",
    },
    # docs.kiwify.com.br/api-reference/webhooks/receiving. The events and the
    # signature are documented; the keys inside Customer and Product are not
    # shown there (they are the ones its test deliveries carry): if a test
    # shows "no e-mail" in the studio, they have changed.
    "kiwify": {
        "email": ["Customer.email"],
        "name": ["Customer.full_name"],
        "product": ["Product.product_id", "Product.product_name"],
        "status": ["webhook_event_type", "order_status"],
        "grant": {"order_approved", "subscription_renewed", "paid"},
        "revoke": {"order_refunded", "chargeback", "subscription_canceled", "subscription_trial_canceled",
                   "refunded", "chargedback"},
        "check": lambda request, token, data: _same(
            request.GET.get("signature", ""), hmac.new(token.encode(), request.body, hashlib.sha1).hexdigest()
        ),
    },
    # developers.eduzz.com/reference/webhook (Developer Hub). A chargeback is
    # posted at each of its steps: only its "refunded" ends the access.
    "eduzz": {
        "email": ["data.buyer.email", "data.customer.email"],
        "name": ["data.buyer.name", "data.customer.name"],
        "product": ["data.items.0.productId", "data.items.0.name", "data.products.0.id", "data.products.0.name"],
        "status": ["event", "data.contract.status", "data.chargeback.status"],
        "grant": {"myeduzz.invoice_paid"},
        "revoke": {"myeduzz.invoice_refunded", "myeduzz.invoice_canceled", "myeduzz.invoice_expired", "canceled",
                   "refunded"},
        "check": lambda request, token, data: _hmac_matches(
            request.headers.get("x-signature", ""), hmac.new(token.encode(), request.body, hashlib.sha256).digest()
        ),
        "header": "x-signature",
    },
    # greenn.com.br/ajuda/vendo/webhook (no token or signature documented)
    "greenn": {
        "email": ["client.email"],
        "name": ["client.name"],
        "product": ["product.id", "product.name"],
        "status": ["currentStatus"],
        "grant": {"paid", "trialing"},
        "revoke": {"refunded", "chargedback", "canceled"},
    },
    # webhook.ticto.dev/docs/v2
    "ticto": {
        "email": ["customer.email"],
        "name": ["customer.name"],
        "product": ["item.product_id", "item.product_name"],
        "status": ["status"],
        "grant": {"authorized", "uncanceled"},
        "revoke": {"refunded", "chargeback", "subscription_canceled"},
        "check": lambda request, token, data: _same(str(data.get("token") or ""), token),
    },
    # ajuda.memberkit.com.br/webhooks (Standard Webhooks signatures)
    "memberkit": {
        "email": ["data.user.email"],
        "name": ["data.user.full_name"],
        "product": ["data.membership_level.name", "data.membership_level.id", "data.course_id"],
        "status": ["data.status"],
        "grant": {"active"},
        "revoke": {"inactive", "expired"},
        "check": lambda request, token, data: _standard_webhook(request, token),
        "header": "webhook-signature",
    },
    # cademi.dev/webhooks (payload version 3). The e-mail only comes when the
    # endpoint's "payload_detail" is "full".
    "cademi": {
        "email": ["expanded.user.email"],
        "name": ["expanded.user.name"],
        "product": [],
        "status": ["type", "data.status"],
        "grant": {"enrollment.created", "enrollment.restored", "active"},
        "revoke": {"enrollment.revoked", "revoked"},
        "check": lambda request, token, data: _cademi(request, token),
        "header": "Cademi-Signature",
    },
    # apidoc.monetizze.com.br/webhook (codes of postback_evento)
    "monetizze": {
        "email": ["comprador.email"],
        "name": ["comprador.nome"],
        "product": ["produto.codigo", "produto.nome"],
        "status": ["postback_evento", "tipoEvento.codigo"],
        "grant": {"2", "6", "101"},
        "revoke": {"3", "4", "103"},
        "check": lambda request, token, data: _same(str(data.get("chave_unica") or ""), token),
    },
}

#: With no platform named: the places the known platforms use, then common ones.
COMMON = {
    "email": ["buyer.email", "customer.email", "client.email", "user.email", "student.email", "member.email",
              "subscriber.email", "contact.email", "email"],
    "name": ["buyer.name", "customer.name", "customer.full_name", "client.name", "user.name", "user.full_name",
             "student.name", "member.name", "subscriber.name", "name", "full_name"],
    "product": ["product.id", "product.name", "product_id", "product_name"],
    "status": ["event", "status", "order_status", "type", "webhook_event_type", "purchase.status",
               "subscription.status", "current_status"],
}
KEEP = 20
TTL = 14 * 24 * 3600


@dataclass
class Purchase:
    email: str
    name: str
    product: str
    status: str
    action: str  # "grant", "revoke" or "ignore"


def _at(data, path: str):
    """The value at a dotted path (a number steps into a list)."""
    value = data
    for key in path.split("."):
        if isinstance(value, dict):
            value = value.get(key)
        elif isinstance(value, list) and key.isdigit():
            value = value[int(key)] if int(key) < len(value) else None
        else:
            value = None
        if value is None:
            break
    return None if value in ("", [], {}) else value


def _nest(form) -> dict:
    """A form's fields as an object: ``comprador[email]`` is comprador.email."""
    out: dict = {}
    for name in form:
        keys = [key for key in re.split(r"[\[\]]+", name) if key]
        node = out
        for key in keys[:-1]:
            child = node.get(key)
            if not isinstance(child, dict):
                child = node[key] = {}
            node = child
        if keys:
            node[keys[-1]] = form.get(name)
    return out


def _find_email(data, depth: int = 0) -> str:
    """Any e-mail in a post of a shape not known here: under a key called
    "email" (or ending in it), the buyer's before anyone else's."""
    if not isinstance(data, dict) or depth > 4:
        return ""
    for key, value in data.items():
        if isinstance(value, str) and re.search(r"(^|[_.-])e-?mail$", str(key), re.I):
            found = enrolments.normalize_email(value)
            if found:
                return found
    children = sorted(
        (item for item in data.items() if isinstance(item[1], (dict, list))),
        key=lambda item: 0 if re.search(r"buyer|customer|client|comprador|user|student|subscriber|member", str(item[0]), re.I) else 1,
    )
    for _key, value in children:
        for child in (value if isinstance(value, list) else [value])[:5]:
            found = _find_email(child, depth + 1)
            if found:
                return found
    return ""


def _first(data, paths) -> str:
    for path in paths:
        value = _at(data, path)
        if isinstance(value, (str, int, float)) and not isinstance(value, bool):
            return str(value).strip()
    return ""


def _words(value: str) -> set[str]:
    return {word for word in re.split(r"[^a-zà-ú]+", value.lower()) if word}


def classify(status: str, preset: dict | None = None) -> str:
    """What a status or event means for someone's access."""
    if preset:
        if status in preset.get("revoke", ()):
            return "revoke"
        if status in preset.get("grant", ()):
            return "grant"
        if preset.get("grant") or preset.get("revoke"):
            return "ignore"
    words = _words(status)
    if words & REVOKE_WORDS:
        return "revoke"
    if words & GRANT_WORDS:
        return "grant"
    return "ignore"


def read(data: dict, platform: str = "") -> Purchase:
    """What a post says: who, what they bought, and what happened."""
    preset = PRESETS.get(platform)
    paths = {field: list(preset.get(field, [])) if preset else [] for field in COMMON}
    if not preset:
        for known in PRESETS.values():
            for field in COMMON:
                paths[field] += [path for path in known.get(field, []) if path not in paths[field]]
        for field in COMMON:
            paths[field] += [path for path in COMMON[field] if path not in paths[field]]
    statuses = []
    for path in paths["status"]:
        value = _at(data, path)
        if isinstance(value, (str, int)) and not isinstance(value, bool) and str(value).strip():
            statuses.append(str(value).strip())
    action, status = "ignore", (statuses[0] if statuses else "")
    for candidate in statuses:
        found = classify(candidate, preset)
        if found == "revoke" or (found == "grant" and action == "ignore"):
            action, status = found, candidate
        if found == "revoke":
            break
    email = enrolments.normalize_email(_first(data, paths["email"]))
    if not email and not preset:
        email = _find_email(data)
    return Purchase(
        email=email,
        name=_first(data, paths["name"])[:120],
        product=_first(data, paths["product"])[:120],
        status=status[:80],
        action=action,
    )


def detect(request) -> str:
    """The platform a post comes from, by the header it signs with."""
    for name, preset in PRESETS.items():
        if preset.get("header") and request.headers.get(preset["header"]):
            return name
    return ""


def signed_right(integration, request, platform: str, data: dict) -> bool:
    """The platform's own proof, where it gives one and its token is saved.
    With no token saved, the secret in the address is the proof."""
    token = integration.hook_token
    check = (PRESETS.get(platform) or {}).get("check")
    if not token or check is None:
        return True
    try:
        return bool(check(request, token, data))
    except (ValueError, TypeError):
        return False


def recent(integration) -> list[dict]:
    return cache.get(f"integrations:purchases:{integration.pk}") or []


def _log(integration, platform: str, purchase: Purchase, done: str) -> None:
    key = f"integrations:purchases:{integration.pk}"
    items = cache.get(key) or []
    items.insert(0, {
        "at": timezone.now().isoformat(), "platform": platform or "auto", "email": purchase.email,
        "status": purchase.status, "done": done,
    })
    cache.set(key, items[:KEEP], timeout=TTL)


@csrf_exempt
@require_POST
def receive(request, key: str, secret: str):
    integration = Integration.objects.filter(key=key, is_active=True).first()
    if integration is None or not constant_time_compare(secret, integration.hook_secret):
        return HttpResponse(status=404)
    platform = (request.GET.get("platform") or "").strip().lower()[:30] or detect(request)
    data = None
    if request.content_type == "application/json" or request.body[:1] in (b"{", b"["):
        try:
            data = json.loads(request.body or b"{}")
        except ValueError:
            data = None
    if not isinstance(data, dict):
        data = _nest(request.POST)
    if not signed_right(integration, request, platform, data):
        return JsonResponse({"detail": "The platform's token does not match."}, status=401)
    purchase = read(data, platform)

    if not purchase.email:
        done = "no e-mail"
    elif purchase.action == "grant":
        enrolment, created = enrolments.upsert(
            integration, purchase.email, name=purchase.name or None, source=platform or "webhook",
        )
        classes = parse_groups(request.GET.get("classes") or "")
        if classes:
            enrolments.add_classes(enrolment, classes)
        done = "added" if created else "kept"
    elif purchase.action == "revoke":
        done = "removed" if enrolments.revoke(integration, purchase.email) else "not there"
    else:
        done = "ignored"
    _log(integration, platform, purchase, done)
    return JsonResponse({"result": done})


# ----------------------------------------------------- platforms' own proofs


def _same(given: str, expected: str) -> bool:
    return bool(given) and constant_time_compare(given, expected)


def _hmac_matches(given: str, digest: bytes) -> bool:
    """A signature that may be written in hex or in base64."""
    given = given.strip()
    return _same(given.lower(), digest.hex()) or _same(given, base64.b64encode(digest).decode())


def _standard_webhook(request, secret: str) -> bool:
    """standardwebhooks.com: HMAC-SHA256 of "<id>.<timestamp>.<body>" with the
    base64 secret (after "whsec_"), in base64, among "v1,<signature>" items."""
    key = base64.b64decode(secret.removeprefix("whsec_"))
    signed = b".".join([
        request.headers.get("webhook-id", "").encode(), request.headers.get("webhook-timestamp", "").encode(),
        request.body,
    ])
    expected = base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode()
    return any(
        _same(item.partition(",")[2], expected) for item in request.headers.get("webhook-signature", "").split()
    )


def _cademi(request, secret: str) -> bool:
    """Cademi-Signature: t=<unix>,v1=<hex HMAC-SHA256 of "<t>.<body>">, five
    minutes of tolerance."""
    parts = dict(item.split("=", 1) for item in request.headers.get("Cademi-Signature", "").split(",") if "=" in item)
    moment = int(parts.get("t", "0"))
    if abs(timezone.now().timestamp() - moment) > 300:
        return False
    signed = f"{moment}.".encode() + request.body
    return _same(parts.get("v1", ""), hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest())
