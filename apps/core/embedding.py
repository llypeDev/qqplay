"""What every page made to live inside another site's frame shares."""
from __future__ import annotations

import contextlib

from django.conf import settings
from django.shortcuts import render
from django.utils import translation
from django.utils.html import escape
from django.utils.translation import gettext as _
from django.views.i18n import JavaScriptCatalog


def language_from(value) -> str | None:
    """The interface language a frame asked for (?lang=en, pt, pt-BR…), or
    None. Inside another site the language cookie is not sent, so a frame
    says its language itself; without it the portal's own is used."""
    value = str(value or "").strip().lower().replace("_", "-")[:12]
    if not value:
        return None
    codes = [code.lower() for code, _name in settings.LANGUAGES]
    if value in codes:
        return value
    base = value.split("-")[0]
    for code in codes:
        if code.split("-")[0] == base:
            return code
    return None


def iframe_html(src: str, title: str, width: int = 640, height: int = 360) -> str:
    """The code someone pastes into their own site to show a player. The
    title is whatever a creator typed: it is escaped, so it cannot close the
    attribute and add others to a page that is not ours."""
    return (
        f'<iframe src="{escape(src)}" width="{int(width)}" height="{int(height)}" frameborder="0" '
        f'allow="autoplay; fullscreen; picture-in-picture" allowfullscreen '
        f'title="{escape(title)}"></iframe>'
    )


def allow_framing(response, integration=None):
    """Let the page show inside other sites: everywhere, or only on the sites
    an integration lists. The X-Frame-Options header has no value for
    "anywhere", so it is left out rather than sent with a made-up one that a
    proxy adding its own could turn into a refusal."""
    response.xframe_options_exempt = True
    policy = integration.frame_ancestors() if integration is not None else ""
    if policy:
        response["Content-Security-Policy"] = policy
    # The frame's address may carry a token: it goes nowhere else.
    response["Referrer-Policy"] = "same-origin"
    return response


def missing(request, what: str = "video"):
    """The answer of a frame whose content is not there (or not for this
    visitor): a page that can be framed and says so, where the site's 404
    page could not be shown in a frame at all."""
    language = language_from(request.GET.get("lang"))
    with translation.override(language) if language else contextlib.nullcontext():
        context = {
            "title": _("This live is not available") if what == "live" else _("This video is not available"),
            "text": _("It may have been removed, or it is not open to you."),
        }
        response = render(request, "embed/missing.html", context, status=404)
    return allow_framing(response)


class FrameJavaScriptCatalog(JavaScriptCatalog):
    """The scripts' texts, in the language a frame asked for (?lang=)."""

    def get(self, request, *args, **kwargs):
        language = language_from(request.GET.get("lang"))
        if language is None:
            return super().get(request, *args, **kwargs)
        with translation.override(language):
            return super().get(request, *args, **kwargs)
