"""Integrations: the frame's sign-in, the embed script and the studio pages."""
import hashlib
import json
import time
from urllib.parse import urlsplit

from django import forms
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.http import urlencode
from django.utils import timezone, translation
from django.utils.dateparse import parse_datetime
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy
from django.views.decorators.cache import cache_control
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from apps.core.embedding import missing as embed_missing
from apps.core.templatetags.akon import avatar
from apps.live.models import Live
from apps.rbac import permissions as policy

from . import diagnostics, enrolments, hooks, identities, login, lti, sessions, tokens, webhooks, wordpress
from .models import Enrolment, Integration, LtiPlatform, WebhookDelivery, new_hook_secret, normalize_origin

#: Sign-ins one address may attempt per minute.
#: Refused sign-ins from one address in a minute before it has to wait: what
#: stops someone trying tokens, without ever holding back a class.
REFUSED_BURST = 40
#: Sign-ins of one person in a minute: reloads and a few tabs, not a loop.
PERSON_BURST = 20
#: Sign-in codes asked for from one address in a minute (a class in one room).
CODES_PER_ADDRESS = 120
#: People listed in the studio at a time.
PEOPLE_PAGE = 50
#: The largest spreadsheet an import takes.
IMPORT_BYTES = 2 * 1024 * 1024


def _origin_of(request) -> str:
    return normalize_origin(request.build_absolute_uri("/"))


# ---------------------------------------------------------------- the frame


def client_address(request) -> str:
    """The visitor's address. Behind the proxies of a production setup the
    connection comes from the proxy, so the address is read from what they
    forward (settings.TRUSTED_PROXY_COUNT says how many there are)."""
    hops = settings.TRUSTED_PROXY_COUNT
    forwarded = [part.strip() for part in request.META.get("HTTP_X_FORWARDED_FOR", "").split(",") if part.strip()]
    if hops > 0 and forwarded:
        return forwarded[-hops] if len(forwarded) >= hops else forwarded[0]
    return request.META.get("REMOTE_ADDR", "")


def _one_more(key: str, limit: int) -> bool:
    """Count one more in this minute; True once past the limit."""
    cache.add(key, 0, timeout=60)
    try:
        used = cache.incr(key)
    except ValueError:
        used = 1
    return used > limit


def _slow_down():
    response = JsonResponse({"detail": _("Too many sign-ins: wait a minute."), "code": "rate"}, status=429)
    response["Retry-After"] = "5"
    return response


@csrf_exempt
@sessions.open_to_any_origin
@require_POST
def session(request):
    """Trade a site's token for a session of the embed frame.

    Body (JSON): {"token": "<the site's token>", "origin": "<the site's page
    origin>", "direct": true when the frame is not inside a page}

    A whole class arrives in the same minute, often from one address (the
    school's network), so good sign-ins are never counted against an
    address: only refused ones are, and each person's own.
    """
    address = client_address(request)
    refused = f"integrations:signin:refused:{address}"
    if (cache.get(refused) or 0) >= REFUSED_BURST:
        return _slow_down()

    try:
        data = json.loads(request.body or b"{}")
    except ValueError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    said = str(data.get("origin") or "")[:200]
    try:
        person = tokens.verify(str(data.get("token") or ""))
    except tokens.TokenError as error:
        _one_more(refused, REFUSED_BURST)
        if error.integration is not None:
            diagnostics.record(error.integration, "token", str(error), said)
        return JsonResponse({"detail": str(error), "code": "token"}, status=401)

    integration = person.integration
    who = hashlib.sha256(person.external_id.encode()).hexdigest()[:16]
    if _one_more(f"integrations:signin:person:{integration.pk}:{who}", PERSON_BURST):
        return _slow_down()

    allowed, detail, said = _site_allowed(integration, request, data)
    if not allowed:
        _one_more(refused, REFUSED_BURST)
        diagnostics.record(integration, "origin", detail, said)
        return JsonResponse({"detail": detail, "code": "origin"}, status=403)

    return _signed_in(integration, identities.sync(person), said, secret_version=person.secret_version)


def _signed_in(integration, identity, said="", *, secret_version="", method="token", lifetime=sessions.LIFETIME):
    """The answer every way of signing in ends with: the session, how long it
    lasts and who the person is, for the frame to show."""
    if identity.is_blocked or not identity.user.is_active:
        diagnostics.record(integration, "blocked", _("This account is blocked."), said)
        return JsonResponse({"detail": _("This account is blocked."), "code": "blocked"}, status=403)
    Integration.objects.filter(pk=integration.pk).update(last_used_at=timezone.now())
    return JsonResponse(
        {
            "session": sessions.issue(identity, secret_version, method, lifetime),
            "expires_in": lifetime,
            "user": {
                "name": identity.user.get_short_name(),
                "avatar": str(avatar(identity.user, "sm")),
                "moderator": identity.is_moderator,
            },
        }
    )


def _site_allowed(integration, request, data) -> tuple[bool, str, str]:
    """Whether the page a frame is on may sign people in: (yes, why not, the
    origin it said)."""
    said = str(data.get("origin") or "")[:200]
    origin = normalize_origin(said)
    if not integration.origin_list:
        return True, "", said
    direct = data.get("direct") is True and not origin
    if (direct and integration.allow_direct) or (
        origin and (origin == _origin_of(request) or integration.allows_origin(origin))
    ):
        return True, "", said
    if direct:
        detail = _("Opening this address by itself is turned off for the integration: its creator can allow it in the integration's settings.")
    elif not origin:
        detail = _("The page did not say which site it is on. Use the QQplay script, or do not set a no-referrer policy on the frame.")
    else:
        detail = _("This site is not allowed to use the integration: add %(origin)s to its sites.") % {"origin": origin}
    return False, detail, said or ("direct" if direct else "")


def _body(request) -> dict:
    try:
        data = json.loads(request.body or b"{}")
    except ValueError:
        data = {}
    return data if isinstance(data, dict) else {}


def _school(data):
    """The integration a frame names by its key, if it is on."""
    key = str(data.get("school") or "")[:40]
    return Integration.objects.filter(key=key, is_active=True).first() if key else None


def _not_here():
    return JsonResponse({"detail": _("This way of signing in is not available here."), "code": "off"}, status=404)


def _enrolled(integration, enrolment, said, method, lifetime):
    """Sign an enrolled person in: their account is made or brought up to
    date from the enrolment, and tied to it."""
    identity = identities.sync(enrolments.person_for(enrolment))
    if enrolment.identity_id != identity.pk:
        try:
            with transaction.atomic():
                Enrolment.objects.filter(pk=enrolment.pk).update(identity=identity)
        except IntegrityError:  # the account is another enrolment's already
            pass
    return _signed_in(integration, identity, said, method=method, lifetime=lifetime)


@csrf_exempt
@sessions.open_to_any_origin
@require_POST
def login_start(request):
    """Send a sign-in code to an e-mail, if it is enrolled (login.py).

    Body (JSON): {"school": "<the integration's key>", "email": "..."}
    The answer is the same whether the e-mail is on the list or not.
    """
    data = _body(request)
    integration = _school(data)
    if integration is None or not integration.email_login:
        return _not_here()
    if _one_more(f"integrations:code:address:{client_address(request)}", CODES_PER_ADDRESS):
        return _slow_down()
    try:
        login.start(integration, data.get("email"), translation.get_language())
    except login.LoginError as error:
        return JsonResponse({"detail": str(error), "code": error.code}, status=error.status)
    return JsonResponse({"sent": True, "minutes": login.CODE_MINUTES})


@csrf_exempt
@sessions.open_to_any_origin
@require_POST
def login_verify(request):
    """Trade the code for a session.

    Body (JSON): {"school": "...", "email": "...", "code": "123456"}
    """
    data = _body(request)
    integration = _school(data)
    if integration is None or not integration.email_login:
        return _not_here()
    refused = f"integrations:signin:refused:{client_address(request)}"
    if (cache.get(refused) or 0) >= REFUSED_BURST:
        return _slow_down()
    try:
        enrolment = login.verify(integration, data.get("email"), data.get("code"))
    except login.LoginError as error:
        _one_more(refused, REFUSED_BURST)
        return JsonResponse({"detail": str(error), "code": error.code}, status=error.status)
    return _enrolled(integration, enrolment, "", "email", sessions.EMAIL_LIFETIME)


@csrf_exempt
@sessions.open_to_any_origin
@require_POST
def login_declared(request):
    """Sign in whoever the platform's page says is there, without proof: the
    e-mail a members area writes into the embed code. Taken only when the
    integration allows it, from a listed site, for an enrolled student; a
    teacher is asked for a code, since the page's word is anyone's to edit.

    Body (JSON): {"school": "...", "email": "...", "name": "...", "origin": "..."}
    """
    data = _body(request)
    integration = _school(data)
    if integration is None or not integration.declared_login:
        return _not_here()
    refused = f"integrations:signin:refused:{client_address(request)}"
    if (cache.get(refused) or 0) >= REFUSED_BURST:
        return _slow_down()
    allowed, detail, said = _site_allowed(integration, request, data)
    if not allowed:
        _one_more(refused, REFUSED_BURST)
        diagnostics.record(integration, "origin", detail, said)
        return JsonResponse({"detail": detail, "code": "origin"}, status=403)
    enrolment = enrolments.find(integration, data.get("email"))
    if enrolment is None or not enrolment.is_active:
        _one_more(refused, REFUSED_BURST)
        detail = _("This e-mail is not enrolled here.")
        diagnostics.record(integration, "enrolment", detail, said)
        return JsonResponse({"detail": detail, "code": "enrolment"}, status=403)
    if enrolment.is_moderator:
        return JsonResponse({"detail": _("Teachers sign in with a code sent to their e-mail."), "code": "code"}, status=403)
    name = str(data.get("name") or "").strip()[:120]
    if name and not enrolment.name:
        enrolment.name = name
        enrolment.save(update_fields=["name", "updated_at"])
    return _enrolled(integration, enrolment, said, "declared", sessions.LIFETIME)


@cache_control(max_age=3600, public=True)
def sdk(request):
    """The script a site adds to its pages (see templates/integrations/sdk.js)."""
    return render(
        request,
        "integrations/sdk.js",
        {
            "origin": _origin_of(request),
            # What the script says in the site's own page when a frame cannot show.
            "text": {
                "blocked": _("This content could not be shown here."),
                "blocked_hint": _(
                    "If you look after this site: add its address to the sites of your QQplay integration."
                ),
                "offline": _("This content is taking too long to load. Check your connection and reload the page."),
                "sandbox_hint": _(
                    "If you look after this site: this code is inside a sandboxed frame, where people cannot be "
                    "signed in. Put it in the page itself."
                ),
            },
        },
        content_type="application/javascript; charset=utf-8",
    )


# ----------------------------------------------------------------- studio


class IntegrationForm(forms.ModelForm):
    class Meta:
        model = Integration
        fields = ["name", "origins", "allow_direct"]
        widgets = {
            "name": forms.TextInput(attrs={"maxlength": 120, "autocomplete": "off"}),
            "origins": forms.Textarea(attrs={"rows": 3, "spellcheck": "false"}),
        }
        labels = {"name": gettext_lazy("Name"), "origins": gettext_lazy("Sites")}


class LookForm(forms.ModelForm):
    """How the frames look on the site, and where its news go."""

    class Meta:
        model = Integration
        fields = ["accent_color", "chat_title", "staff_label", "hide_branding", "webhook_url"]


def _can_integrate(user) -> bool:
    return policy.can_upload(user) and not hasattr(user, "external_identity")


def _mine(request, pk) -> Integration:
    integration = get_object_or_404(Integration, pk=pk)
    if integration.owner_id != request.user.id and not policy.can_configure_portal(request.user):
        raise Http404("integration not found")
    return integration


@login_required
def studio_list(request):
    if not _can_integrate(request.user):
        raise PermissionDenied
    form = IntegrationForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        integration = form.save(commit=False)
        integration.owner = request.user
        integration.save()
        messages.success(request, _("Integration created. Follow the steps below to put your lives on your site."))
        return redirect("integrations:detail", pk=integration.pk)
    integrations = Integration.objects.filter(owner=request.user).order_by("-created_at")
    return render(request, "integrations/list.html", {"integrations": integrations, "form": form})


@login_required
def studio_detail(request, pk):
    integration = _mine(request, pk)
    form = IntegrationForm(request.POST or None, instance=integration)
    if request.method == "POST":
        if "is_active" in request.POST:
            integration.is_active = request.POST.get("is_active") == "1"
            if not integration.is_active:
                # Turned off: the sessions open now do not come back later.
                integration.sessions_since = timezone.now()
            integration.save(update_fields=["is_active", "sessions_since"])
            messages.success(
                request, _("Integration turned on.") if integration.is_active else _("Integration turned off.")
            )
            return redirect("integrations:detail", pk=integration.pk)
        if form.is_valid():
            form.save()
            messages.success(request, _("Integration saved."))
            return redirect("integrations:detail", pk=integration.pk)

    lives = list(
        Live.objects.filter(owner=integration.owner).order_by("-created_at").only("uid", "title", "status", "visibility")[:50]
    )
    chosen = request.GET.get("live") or (lives[0].uid if lives else "")
    layout = request.GET.get("layout", "both")
    layout = layout if layout in ("both", "player", "chat") else "both"
    theme = request.GET.get("theme", "")
    theme = theme if theme in ("dark", "light") else ""
    # The people, the latest first; a search reaches the ones further back.
    found = integration.identities.select_related("user").order_by("-last_seen_at")
    search = (request.GET.get("q") or "").strip()[:80]
    if search:
        found = found.filter(Q(name__icontains=search) | Q(external_id__icontains=search) | Q(email__icontains=search))
    try:
        shown = min(500, max(PEOPLE_PAGE, int(request.GET.get("people", PEOPLE_PAGE))))
    except (TypeError, ValueError):
        shown = PEOPLE_PAGE
    people_found = found.count()
    people = found[:shown]

    # The enrolments, for schools whose students sign in by themselves.
    enrolled = integration.enrolments.all()
    enrolment_search = (request.GET.get("eq") or "").strip()[:80]
    if enrolment_search:
        enrolled = enrolled.filter(Q(email__icontains=enrolment_search) | Q(name__icontains=enrolment_search))
    try:
        enrolled_shown = min(500, max(PEOPLE_PAGE, int(request.GET.get("enrolled", PEOPLE_PAGE))))
    except (TypeError, ValueError):
        enrolled_shown = PEOPLE_PAGE
    enrolled_found = enrolled.count()
    return render(
        request,
        "integrations/detail.html",
        {
            "integration": integration,
            "form": form,
            "lives": lives,
            "chosen": chosen,
            "layout": layout,
            "theme": theme,
            "people": people,
            "people_count": integration.identities.count(),
            "people_found": people_found,
            "people_more": shown + PEOPLE_PAGE if people_found > shown else 0,
            "search": search,
            "portal_origin": _origin_of(request),
            "wordpress_version": wordpress.version(),
            "lms": list(integration.lti_platforms.all()),
            "lti_urls": lti.tool_urls(request),
            "lti_register_url": request.build_absolute_uri(
                reverse("integrations:lti_register", args=[lti.registration_token(integration)])
            ),
            "enrolments": enrolled[:enrolled_shown],
            "enrolments_count": integration.enrolments.count(),
            "enrolments_active": integration.enrolments.filter(is_active=True).count(),
            "enrolments_found": enrolled_found,
            "enrolments_more": enrolled_shown + PEOPLE_PAGE if enrolled_found > enrolled_shown else 0,
            "enrolment_search": enrolment_search,
            "hook_url": request.build_absolute_uri(
                reverse("integrations:purchase_hook", args=[integration.key, integration.hook_secret])
            ),
            "platforms": sorted(hooks.PRESETS),
            "purchases": [{**item, "at": parse_datetime(item["at"])} for item in hooks.recent(integration)[:8]],
            "room_url": request.build_absolute_uri(reverse("integrations:room", args=[integration.key, chosen]))
            if chosen else "",
            "refusals": [
                {**item, "at": parse_datetime(item["at"])} for item in diagnostics.recent(integration)[:8]
            ],
            "checked": request.session.pop(f"integration-check-{integration.pk}", None),
            "look": LookForm(instance=integration),
            "deliveries": integration.deliveries.all()[:10],
            "grace_until": integration.previous_secret_until
            if integration.previous_secret and integration.previous_secret_until
            and integration.previous_secret_until > timezone.now() else None,
            "origin": _origin_of(request),
            "sdk_url": request.build_absolute_uri(reverse("integrations:sdk")),
            "api_url": request.build_absolute_uri(reverse("integrations:api_lives")).removesuffix("lives/"),
        },
    )


#: Hours the old secret may keep working after a new one is made.
GRACE_HOURS = (0, 1, 24)


@login_required
@require_POST
def rotate(request, pk):
    integration = _mine(request, pk)
    try:
        grace = int(request.POST.get("grace", 24))
    except (TypeError, ValueError):
        grace = 24
    grace = grace if grace in GRACE_HOURS else 24
    integration.rotate_secret(grace)
    if grace:
        messages.success(
            request,
            _("New secret created. The old one keeps working for %(hours)d h: update your site's server before then.")
            % {"hours": grace},
        )
    else:
        messages.success(request, _("New secret created. Update it on your site's server: the old one no longer works."))
    return redirect("integrations:detail", pk=integration.pk)


@login_required
@require_POST
def look(request, pk):
    integration = _mine(request, pk)
    form = LookForm(request.POST, instance=integration)
    if form.is_valid():
        form.save()
        messages.success(request, _("Integration saved."))
    else:
        for errors in form.errors.values():
            messages.error(request, errors[0])
    return redirect("integrations:detail", pk=integration.pk)


@login_required
@require_POST
def person(request, pk, person_id):
    """Block, unblock or forget one person of the site."""
    integration = _mine(request, pk)
    identity = get_object_or_404(integration.identities.select_related("user"), pk=person_id)
    action = request.POST.get("action")
    name = identity.name
    if action == "block":
        identities.block(identity, True)
        messages.success(request, _("%(name)s is blocked.") % {"name": name})
    elif action == "unblock":
        identities.block(identity, False)
        messages.success(request, _("%(name)s is unblocked.") % {"name": name})
    elif action == "erase":
        identities.erase(identity, keep_messages=request.POST.get("keep_messages") == "1")
        messages.success(request, _("%(name)s's data was deleted.") % {"name": name})
    return redirect(f"{reverse('integrations:detail', args=[integration.pk])}#people")


@login_required
@require_POST
def delete(request, pk):
    integration = _mine(request, pk)
    # Their names and pictures go with it; what they wrote stays, unsigned.
    identities.erase_all(integration)
    integration.delete()
    messages.success(request, _("Integration deleted."))
    return redirect("integrations:list")


@login_required
def preview(request, pk):
    """The integration as a site would show it, signed in as a test student.

    The token is made here with the integration's secret, the way the site's
    server would make it; the page then loads the embed script like any site.
    """
    integration = _mine(request, pk)
    live = get_object_or_404(Live, uid=request.GET.get("live", ""), owner=integration.owner)
    layout = request.GET.get("layout", "both")
    layout = layout if layout in ("both", "player", "chat") else "both"
    theme = request.GET.get("theme", "")
    theme = theme if theme in ("dark", "light") else ""
    name = (request.GET.get("name") or "").strip()[:60] or _("Test student")
    moderator = request.GET.get("moderator") == "1"
    now = int(time.time())
    token = tokens.sign(
        {
            "sub": f"preview-{request.user.pk}-{'teacher' if moderator else 'student'}",
            "name": name,
            "moderator": moderator,
            "iat": now,
            "exp": now + 600,
        },
        integration,
    )
    return render(
        request,
        "integrations/preview.html",
        {
            "integration": integration,
            "live": live,
            "layout": layout,
            "theme": theme,
            "name": name,
            "moderator": moderator,
            "token": token,
            "sdk_url": reverse("integrations:sdk"),
        },
    )


@login_required
@require_POST
def webhook_test(request, pk):
    """Save the webhook address (the form posts it) and send a test."""
    integration = _mine(request, pk)
    form = LookForm(request.POST, instance=integration)
    if not form.is_valid():
        for errors in form.errors.values():
            messages.error(request, errors[0])
        return redirect(f"{reverse('integrations:detail', args=[integration.pk])}#webhooks")
    form.save()
    if not integration.webhook_url:
        messages.error(request, _("Fill in the webhook address first."))
    else:
        webhooks.send_test(integration)
        messages.success(request, _("Test sent. Its result shows in the list in a few seconds."))
    return redirect(f"{reverse('integrations:detail', args=[integration.pk])}#webhooks")


@login_required
@require_POST
def check_token(request, pk):
    """Read a token as the sign-in would and say what is wrong with it."""
    integration = _mine(request, pk)
    token = (request.POST.get("token") or "").strip()[:4000]
    if token:
        result = diagnostics.explain(token, integration)
        claims = result.pop("claims") or {}
        result.pop("header", None)
        result["claims_text"] = json.dumps(claims, indent=2, ensure_ascii=False)[:1500] if claims else ""
        request.session[f"integration-check-{integration.pk}"] = result
    return redirect(f"{reverse('integrations:detail', args=[integration.pk])}#diagnostics")


@login_required
@require_POST
def webhook_resend(request, pk, delivery_id):
    """Send a delivery that failed once more, signed with today's secret."""
    from .tasks import deliver_webhook

    integration = _mine(request, pk)
    delivery = get_object_or_404(WebhookDelivery, pk=delivery_id, integration=integration)
    if delivery.ok:
        messages.info(request, _("That one was delivered already."))
    else:
        delivery.url = integration.webhook_url or delivery.url
        delivery.save(update_fields=["url"])
        deliver_webhook.delay(delivery.pk)
        messages.success(request, _("Sent again. Its result shows in the list in a few seconds."))
    return redirect(f"{reverse('integrations:detail', args=[integration.pk])}#webhooks")


def room(request, key: str, uid: str, kind: str = "live"):
    """The class by itself, for platforms that take a link and nothing else:
    the live with its chat (or the video with its comments) filling the page,
    where the school's people sign in with a code by e-mail."""
    integration = Integration.objects.filter(key=key, is_active=True).first()
    if integration is None:
        return embed_missing(request, kind)
    target = reverse("live:embed", args=[uid]) if kind == "live" else reverse("core:embed", args=[uid])
    query = {"layout": "both", "school": integration.key}
    for name in ("lang", "theme"):
        if request.GET.get(name):
            query[name] = request.GET[name][:12]
    return redirect(f"{target}?{urlencode(query)}")


@login_required
@require_POST
def enrolments_view(request, pk):
    """The studio's handling of the list of enrolments: how people sign in,
    adding one, importing a spreadsheet, taking someone off."""
    integration = _mine(request, pk)
    action = request.POST.get("action")
    back = f"{reverse('integrations:detail', args=[integration.pk])}#enrolments"
    if action == "options":
        integration.email_login = request.POST.get("email_login") == "1"
        integration.declared_login = request.POST.get("declared_login") == "1"
        integration.hook_token = (request.POST.get("hook_token") or "").strip()[:200]
        integration.save(update_fields=["email_login", "declared_login", "hook_token"])
        messages.success(request, _("Saved."))
    elif action == "add":
        enrolment, created = enrolments.upsert(
            integration, request.POST.get("email"), name=request.POST.get("name") or None,
            classes=request.POST.get("classes") or "", teacher=request.POST.get("teacher") == "1", source="manual",
        )
        if enrolment is None:
            messages.error(request, _("That does not look like an e-mail address."))
        else:
            messages.success(request, _("%(email)s is enrolled.") % {"email": enrolment.email})
    elif action == "import":
        text = request.POST.get("rows") or ""
        upload = request.FILES.get("file")
        if upload is not None:
            if upload.size > IMPORT_BYTES:
                messages.error(request, _("That file is too large: at most 2 MB."))
                return redirect(back)
            text = upload.read().decode("utf-8-sig", "replace")
        result = enrolments.import_rows(integration, text)
        messages.success(
            request, _("%(added)d added, %(updated)d brought up to date.")
            % {"added": result["added"], "updated": result["updated"]},
        )
        if result["bad"]:
            messages.warning(
                request, _("Lines with no valid e-mail were skipped: %(lines)s")
                % {"lines": ", ".join(str(line) for line in result["bad"][:20])},
            )
    elif action in ("remove", "off", "on"):
        enrolment = get_object_or_404(Enrolment, pk=request.POST.get("id"), integration=integration)
        if action == "remove":
            enrolments.remove(enrolment)
            messages.success(request, _("%(email)s was taken off the list.") % {"email": enrolment.email})
        elif action == "off":
            enrolments.revoke(integration, enrolment.email)
        else:
            enrolments.upsert(integration, enrolment.email, active=True)
    elif action == "new_address":
        integration.hook_secret = new_hook_secret()
        integration.save(update_fields=["hook_secret"])
        messages.success(request, _("New address made: paste it in the platform again."))
    return redirect(back)


class LtiPlatformForm(forms.ModelForm):
    """An LMS registered by hand, with what its administrator copied from it."""

    deployment_id = forms.CharField(max_length=200, required=False)

    class Meta:
        model = LtiPlatform
        fields = ["name", "issuer", "client_id", "auth_login_url", "auth_token_url", "jwks_url"]

    def clean(self):
        data = super().clean()
        for field in ("auth_login_url", "jwks_url", "auth_token_url"):
            value = (data.get(field) or "").strip()
            if value and urlsplit(value).scheme not in ("https", "http"):
                self.add_error(field, _("This must be an address starting with https://."))
        taken = LtiPlatform.objects.filter(issuer=data.get("issuer"), client_id=data.get("client_id")).first()
        if taken is not None:
            self.add_error("client_id", _("This LMS and tool id are registered already."))
        return data


@login_required
@require_POST
def lms_view(request, pk):
    """The LMSs that launch this integration (LTI 1.3): registering one by
    hand, taking one away."""
    integration = _mine(request, pk)
    back = f"{reverse('integrations:detail', args=[integration.pk])}#lms"
    if request.POST.get("action") == "remove":
        LtiPlatform.objects.filter(pk=request.POST.get("id"), integration=integration).delete()
        messages.success(request, _("The LMS was taken off. Launches from it stop working."))
        return redirect(back)
    form = LtiPlatformForm(request.POST)
    if not form.is_valid():
        for errors in form.errors.values():
            messages.error(request, errors[0])
        return redirect(back)
    platform = form.save(commit=False)
    platform.integration = integration
    deployment = form.cleaned_data.get("deployment_id", "").strip()
    platform.deployment_ids = [deployment] if deployment else []
    platform.save()
    messages.success(request, _("%(name)s is registered: add the tool to a course there.") % {"name": platform})
    return redirect(back)
