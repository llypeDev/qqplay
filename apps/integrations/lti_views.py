"""The addresses an LMS talks to (see lti.py for what is said).

    /lti/login/             the LMS starts a sign-in here
    /lti/launch/            ...and posts the launch here
    /lti/jwks/              this tool's public keys
    /lti/deeplink/          the teacher's choice, answered back to the LMS
    /lti/register/<token>/  an LMS registering the tool by itself

Every page here shows inside the LMS's frame, and the posts come from another
site by design: what makes them trustworthy is the LMS's signature on the
launch and the signed values this portal hands out, not a CSRF cookie that a
frame on another site would not be sent.
"""
from __future__ import annotations

import json

from django.core import signing
from django.http import JsonResponse, QueryDict
from django.shortcuts import redirect, render
from django.utils import timezone, translation
from django.utils.translation import gettext as _
from django.views.decorators.cache import cache_control
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from apps.core import views as core_views
from apps.core.embedding import allow_framing, language_from
from apps.core.templatetags.akon import avatar
from apps.live import views as live_views
from apps.live.models import Live
from apps.media.models import Media

from . import identities, lti, sessions
from .models import LtiPlatform


def _problem(request, message: str, status: int = 400):
    response = render(request, "integrations/lti_problem.html", {"message": message}, status=status)
    return allow_framing(response)


@csrf_exempt
def login(request):
    """Third-party initiated login: the LMS says who it is and whom it is
    signing in; the browser goes back to it with a state and a nonce."""
    params = request.POST if request.method == "POST" else request.GET
    issuer = params.get("iss") or ""
    if not issuer or not params.get("login_hint"):
        return _problem(request, _("Open this activity from your course in the LMS."))
    platform = lti.find_platform(issuer, params.get("client_id") or "")
    if platform is None:
        return _problem(request, _("This LMS is not registered here. Whoever looks after it registers the tool in the integration's page."))
    url, mark = lti.login_redirect(request, platform, params)
    response = redirect(url)
    secure = request.is_secure()
    # Kept where the browser allows a cookie in a frame; the launch does
    # without it otherwise.
    response.set_cookie(
        lti.COOKIE, mark, max_age=lti.STATE_AGE, httponly=True, secure=secure, samesite="None" if secure else "Lax",
    )
    return response


@csrf_exempt
def launch(request):
    if request.method != "POST":
        return _problem(request, _("Open this activity from your course in the LMS."))
    try:
        launched = lti.validate(
            request.POST.get("id_token") or "", request.POST.get("state") or "", request.COOKIES.get(lti.COOKIE, "")
        )
    except lti.LtiError as error:
        return _problem(request, str(error))
    platform = launched.platform
    LtiPlatform.objects.filter(pk=platform.pk).update(last_launch_at=timezone.now())
    presentation = launched.claim("launch_presentation")
    locale = str(presentation.get("locale") or "") if isinstance(presentation, dict) else ""

    if launched.message_type == lti.DEEP_LINKING:
        if not launched.is_staff:
            return _problem(request, _("Only teachers choose what an activity shows."), 403)
        return _picker(request, launched)

    identity = identities.sync(lti.person(launched))
    if identity.is_blocked or not identity.user.is_active:
        return _problem(request, _("This account is blocked."), 403)
    what = lti.content(launched)
    if not what["live"] and not what["video"]:
        if launched.is_staff:
            return _problem(request, _(
                "No live or video was chosen for this activity. Add it again and pick one when the LMS offers "
                "to select content, or write ?live=ID at the end of the tool's address."
            ))
        return _problem(request, _("Your teacher has not chosen what this activity shows yet."))
    handoff = {
        "session": sessions.issue(identity, method="lti"),
        "user": {
            "name": identity.user.get_short_name(),
            "avatar": str(avatar(identity.user, "sm")),
            "moderator": identity.is_moderator,
        },
    }
    return _show(request, platform.integration, what, handoff, locale)


def _show(request, integration, what, handoff, locale: str = ""):
    """The live or the video, as its frame on a school's site, with the
    person the LMS signed in handed to it."""
    layout = what["layout"]
    if what["live"]:
        layout = "chat" if layout == "comments" else layout
    else:
        layout = "comments" if layout == "chat" else layout
    query = QueryDict(mutable=True)
    query.update({"layout": layout, "school": integration.key})
    if locale:
        query["lang"] = locale
    request.GET = query
    request.method = "GET"
    request.lti_handoff = handoff
    if what["live"]:
        return live_views.embed(request, what["live"])
    return core_views.embed(request, what["video"])


def _picker(request, launched):
    """Deep linking: the teacher picks the live or the video the activity
    being made will show."""
    owner_id = launched.platform.integration.owner_id
    lives = Live.objects.filter(owner_id=owner_id).order_by("-created_at")[:60]
    videos = (
        Media.objects.filter(owner_id=owner_id, state=Media.State.READY, kind__in=[Media.Kind.VIDEO, Media.Kind.AUDIO])
        .order_by("-created_at")[:60]
    )
    presentation = launched.claim("launch_presentation")
    language = language_from(presentation.get("locale") if isinstance(presentation, dict) else "")
    with translation.override(language or translation.get_language()):
        response = render(request, "integrations/lti_pick.html", {
            "lives": lives, "videos": videos, "token": lti.deep_link_token(launched),
            "school": launched.platform.integration.name,
        })
    return allow_framing(response)


@csrf_exempt
@require_POST
def deeplink(request):
    """The teacher's choice, sent back to the LMS as a signed message its
    own page posts there."""
    kind, _sep, uid = (request.POST.get("item") or "").partition(":")
    layout = request.POST.get("layout") or "both"
    layout = layout if layout in ("both", "player") else "both"
    token = request.POST.get("token") or ""
    try:
        # The token names the LMS; the item must be its integration's owner's.
        sent = signing.loads(token, salt=lti.STATE_SALT + ".deep", max_age=3600)
        platform = LtiPlatform.objects.select_related("integration").get(pk=sent.get("p"), is_active=True)
    except (signing.BadSignature, LtiPlatform.DoesNotExist):
        return _problem(request, _("Choosing took too long. Start again from your course."))
    owner_id = platform.integration.owner_id
    if kind == "live":
        chosen = Live.objects.filter(owner_id=owner_id, uid=uid).first()
    elif kind == "video":
        chosen = Media.objects.filter(owner_id=owner_id, uid=uid, state=Media.State.READY).first()
    else:
        chosen = None
    if chosen is None:
        return _problem(request, _("Pick a live or a video."))
    try:
        back, signed = lti.deep_link_response(token, request, kind, chosen.uid, chosen.title, layout)
    except lti.LtiError as error:
        return _problem(request, str(error))
    return allow_framing(render(request, "integrations/lti_post.html", {"action": back, "fields": {"JWT": signed}}))


@require_GET
@cache_control(max_age=300, public=True)
def jwks(request):
    response = JsonResponse(lti.jwks())
    response["Access-Control-Allow-Origin"] = "*"
    return response


@require_GET
def register(request, token: str):
    """LTI Dynamic Registration: the LMS's administrator pasted this address
    in the LMS, which opens it with where its configuration is."""
    integration = lti.integration_for(token)
    if integration is None:
        return _problem(request, _("This registration address is no longer good. Copy a new one from the integration's page."))
    configuration = request.GET.get("openid_configuration") or ""
    if not configuration:
        return _problem(request, _("Paste this address in your LMS where it asks for a tool's registration address (dynamic registration): the LMS opens it by itself."))
    try:
        platform = lti.register(integration, request, configuration, request.GET.get("registration_token") or "")
    except lti.LtiError as error:
        return _problem(request, str(error))
    return allow_framing(render(request, "integrations/lti_registered.html", {"platform": platform}))


def handoff_json(request) -> str:
    """For the frames' templates: the session a launch hands to the page."""
    handoff = getattr(request, "lti_handoff", None)
    return json.dumps(handoff) if handoff else ""
