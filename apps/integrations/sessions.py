"""Sessions for people signed in through a site.

An embed lives in a frame on another site, where browsers keep QQplay's
cookies away (Safari and Firefox always, Chrome more and more). So the
frame trades the site's token for a session of its own and sends it with
every request in a header. No cookie is involved, which is also why the
embed API needs no CSRF token: a page elsewhere cannot make a browser add
that header.

The session is signed with the portal's SECRET_KEY and names the account,
the integration and the integration secret's version: turning the
integration off, replacing its secret or deactivating the account ends it.
"""
from __future__ import annotations

import contextlib
import time
from dataclasses import dataclass
from functools import wraps

from django.contrib.auth.models import AnonymousUser
from django.core import signing
from django.http import Http404, HttpResponse, JsonResponse
from django.utils import translation
from django.utils.cache import patch_cache_control
from django.views.decorators.csrf import csrf_exempt

from apps.core.embedding import language_from

from .models import ExternalIdentity, Integration

SALT = "qqplay.integrations.session"
#: A class is a few hours long; the site's script asks for a new token after.
LIFETIME = 12 * 3600
#: Someone who signed in with a code by e-mail is not asked for another one
#: at every class: their session lasts this long on that browser.
EMAIL_LIFETIME = 30 * 24 * 3600
HEADER = "HTTP_AUTHORIZATION"


@dataclass
class EmbedContext:
    """What a request from a site's frame may do, for the live views."""

    integration: Integration
    identity: ExternalIdentity | None

    def grants_live(self, live) -> bool:
        """The live is the integration owner's, and for the person's classes."""
        if not self.integration.grants_live(live):
            return False
        return self.identity is None or self.identity.in_groups(live.group_list)

    def grants_media(self, media) -> bool:
        """A video of the integration owner's; a live's recording only for
        the classes the live was for."""
        if not self.integration.grants_media(media):
            return False
        from apps.live.models import Live

        source = Live.objects.filter(recording=media).only("groups").first()
        return source is None or self.identity is None or self.identity.in_groups(source.group_list)

    def moderates(self, live) -> bool:
        return bool(self.identity and self.identity.is_moderator and self.grants_live(live))


def issue(identity: ExternalIdentity, secret_version: str = "", method: str = "token", lifetime: int = LIFETIME) -> str:
    """A session for the person. It remembers which secret signed the token
    it came from, so it ends when that secret stops being accepted, and when
    it was made, so blocking the person or turning the integration off ends
    it for good. One made without a token (``method`` "email" or "declared")
    has nothing to do with the secret and outlives its replacement."""
    integration = identity.integration
    return signing.dumps(
        {
            "u": str(identity.user_id),
            "i": str(integration.pk),
            "v": (secret_version or integration.secret_version) if method == "token" else "",
            "t": int(time.time()),
            "m": method,
            "l": lifetime,
        },
        salt=SALT,
    )


def load(token: str) -> ExternalIdentity | None:
    try:
        data = signing.loads(token, salt=SALT, max_age=EMAIL_LIFETIME)
    except signing.BadSignature:
        return None
    identity = (
        ExternalIdentity.objects.select_related("user", "integration")
        .filter(user_id=data.get("u"), integration_id=data.get("i"))
        .first()
    )
    if identity is None or identity.is_blocked or not identity.user.is_active:
        return None
    integration = identity.integration
    versions = {Integration.version_of(secret) for secret in integration.secrets_in_force()}
    by_token = (data.get("m") or "token") == "token"
    if not integration.is_active or (by_token and data.get("v") not in versions):
        return None
    made = data.get("t") or 0
    # Sessions from before they carried their own length last the usual one;
    # with no date of their own, the signature's is all there is.
    if made and time.time() > made + (data.get("l") or LIFETIME):
        return None
    if not made and (data.get("l") or LIFETIME) <= LIFETIME:
        try:
            signing.loads(token, salt=SALT, max_age=LIFETIME)
        except signing.BadSignature:
            return None
    for since in (integration.sessions_since, identity.sessions_since):
        if since is not None and made < since.timestamp():
            return None
    return identity


def confine(request, owner_id) -> None:
    """A site's session speaks only on its creator's lives and videos: on
    anyone else's it is a visitor, who reads what is public and no more.
    Otherwise an integration could make accounts at will to write on others'
    chats, out of reach of their mutes."""
    embed = getattr(request, "embed", None)
    if embed is not None and embed.integration.owner_id != owner_id:
        request.user = AnonymousUser()
        request.embed = None


#: The frame's API answers any origin. It can: nothing here goes by cookies,
#: only by the session in the Authorization header, so a page elsewhere has
#: no more power than a program holding the same session. It must: a frame
#: inside a sandboxed page has no origin of its own, and a school's own app
#: or page may talk to the API directly.
CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "Authorization, Content-Type, X-QQplay-Lang, X-CSRFToken",
    "Access-Control-Expose-Headers": "X-QQplay-Session, Retry-After",
    "Access-Control-Max-Age": "600",
}


def open_to_any_origin(view):
    """Answer the browser's preflight and mark the answers as readable from
    any origin (see CORS_HEADERS)."""

    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if request.method == "OPTIONS":
            response = HttpResponse(status=204)
        else:
            # ...and in the language the frame is shown in.
            language = language_from(request.META.get("HTTP_X_QQPLAY_LANG"))
            with translation.override(language) if language else contextlib.nullcontext():
                response = view(request, *args, **kwargs)
        for name, value in CORS_HEADERS.items():
            response[name] = value
        return response

    return wrapper


def _bearer(request) -> str:
    value = request.META.get(HEADER, "")
    return value[7:].strip() if value[:7].lower() == "bearer " else ""


def embed_api(view):
    """Run a live view for a site's frame: the person comes from the session
    header and nowhere else (cookies are ignored here), and the site's
    integration may open private lives and moderation to them."""

    @csrf_exempt
    @open_to_any_origin
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        token = _bearer(request)
        identity = load(token) if token else None
        if identity is not None:
            request.user = identity.user
            request.embed = EmbedContext(identity.integration, identity)
        else:
            request.user = AnonymousUser()
            request.embed = None
        try:
            response = view(request, *args, **kwargs)
        except Http404:
            # Answered here, so the note about a refused session goes with it:
            # a private live that stops being someone's looks like this.
            response = JsonResponse({"detail": "Not found."}, status=404)
        patch_cache_control(response, no_store=True)
        if token and identity is None:
            # The page knows to ask its site for a new token.
            response["X-QQplay-Session"] = "expired"
        return response

    return wrapper
