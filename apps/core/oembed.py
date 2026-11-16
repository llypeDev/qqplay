"""oEmbed (https://oembed.com): how another site learns to show one of ours.

A site that is given the address of a video or a live (WordPress, when the
address is pasted into a post; Notion and others through services like
Iframely) asks here what to put in its page, and gets the player's frame:

    GET /oembed/?url=<address of a video or a live>&format=json
                 &maxwidth=800&maxheight=600

Only what anyone with the link may open is described. A private video or
live answers 404, the same as one that does not exist, like its own page.
oEmbed carries no identity: the frame is the public player. People sign in
through an integration (apps.integrations) instead.
"""
from __future__ import annotations

from urllib.parse import urlencode, urlsplit
from xml.sax.saxutils import escape as xml_escape

from django.conf import settings
from django.http import HttpResponse, JsonResponse
from django.urls import Resolver404, resolve, reverse
from django.views.decorators.http import require_GET

from apps.live.models import Live
from apps.media import access
from apps.media.models import Media

from .embedding import iframe_html

#: The pages and frames of a video and of a live, by the names of their routes.
MEDIA_ROUTES = ("core:media", "core:embed")
LIVE_ROUTES = ("live:watch", "live:embed")
DEFAULT_WIDTH = 640
WIDEST = 1920
#: How long a site may keep the answer, in seconds.
CACHE_AGE = 3600


def _problem(status: int, text: str) -> HttpResponse:
    response = HttpResponse(text, status=status, content_type="text/plain; charset=utf-8")
    response["Access-Control-Allow-Origin"] = "*"
    return response


def _limit(value) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _size(aspect: float, max_width: int, max_height: int) -> tuple[int, int]:
    """The frame's size: as wide as asked (640 otherwise), in the picture's
    own proportions, and inside both limits."""
    width = min(max_width or DEFAULT_WIDTH, WIDEST)
    height = round(width / aspect)
    if max_height and height > max_height:
        height = max_height
        width = round(height * aspect)
    return max(width, 1), max(height, 1)


def _found(request, url: str):
    """The video or the live an address of this portal is about: ("media",
    Media), ("live", Live) or None."""
    parts = urlsplit(url.strip())
    if parts.scheme not in ("http", "https") or parts.netloc.lower() != request.get_host().lower():
        return None
    try:
        match = resolve(parts.path)
    except Resolver404:
        return None
    uid = match.kwargs.get("uid", "")
    if match.view_name in MEDIA_ROUTES:
        media = Media.objects.select_related("owner").filter(uid=uid).first()
        # Anyone, signed in or not: what the link alone opens.
        if media is not None and access.can_view_media(None, media):
            return "media", media
    elif match.view_name in LIVE_ROUTES:
        live = Live.objects.select_related("owner").filter(uid=uid).first()
        if live is not None and access.can_view_live(None, live):
            return "live", live
    return None


def describe(request, kind: str, item, max_width: int = 0, max_height: int = 0) -> dict:
    absolute = request.build_absolute_uri
    if kind == "live":
        aspect = 16 / 9
        frame = absolute(reverse("live:embed", args=[item.uid]))
    else:
        aspect = item.width / item.height if item.width and item.height else 16 / 9
        # Very tall or very wide pictures still get a frame one can use.
        aspect = min(max(aspect, 9 / 16), 21 / 9)
        frame = absolute(reverse("core:embed", args=[item.uid]))
    width, height = _size(aspect, max_width, max_height)
    data = {
        "version": "1.0",
        "type": "video" if kind == "live" or item.kind == Media.Kind.VIDEO else "rich",
        "title": item.title,
        "author_name": item.owner.get_short_name(),
        "author_url": absolute(reverse("core:channel", args=[item.owner_id])),
        "provider_name": settings.PORTAL_NAME,
        "provider_url": absolute("/"),
        "cache_age": CACHE_AGE,
        "html": iframe_html(frame, item.title, width, height),
        "width": width,
        "height": height,
    }
    poster = getattr(item, "poster", None) if kind == "media" else None
    if poster:
        try:
            data.update(thumbnail_url=absolute(poster.url), thumbnail_width=poster.width,
                        thumbnail_height=poster.height)
        except (OSError, ValueError, TypeError):
            pass  # the picture is not where it should be: an answer without it
    return data


def _xml(data: dict) -> str:
    fields = "".join(f"<{name}>{xml_escape(str(value))}</{name}>" for name, value in data.items())
    return f'<?xml version="1.0" encoding="utf-8" standalone="yes"?><oembed>{fields}</oembed>'


@require_GET
def oembed(request):
    answer_as = (request.GET.get("format") or "json").lower()
    if answer_as not in ("json", "xml"):
        return _problem(501, "format is json or xml")
    found = _found(request, request.GET.get("url") or "")
    if found is None:
        return _problem(404, "No video or live open to everyone at that address.")
    data = describe(request, *found, _limit(request.GET.get("maxwidth")), _limit(request.GET.get("maxheight")))
    if answer_as == "xml":
        response = HttpResponse(_xml(data), content_type="text/xml; charset=utf-8")
    else:
        response = JsonResponse(data)
    response["Access-Control-Allow-Origin"] = "*"
    response["Cache-Control"] = f"public, max-age={CACHE_AGE}"
    return response


def discovery(request, kind: str, item) -> list[dict]:
    """The <link> tags a page carries so that sites find this by themselves."""
    if kind == "live":
        page = reverse("live:watch", args=[item.uid])
        open_to_all = access.can_view_live(None, item)
    else:
        page = reverse("core:media", args=[item.uid])
        open_to_all = access.can_view_media(None, item)
    if not open_to_all:
        return []
    base = request.build_absolute_uri(reverse("core:oembed"))
    address = request.build_absolute_uri(page)
    return [
        {"type": "application/json+oembed", "href": f"{base}?{urlencode({'url': address, 'format': 'json'})}"},
        {"type": "text/xml+oembed", "href": f"{base}?{urlencode({'url': address, 'format': 'xml'})}"},
    ]
