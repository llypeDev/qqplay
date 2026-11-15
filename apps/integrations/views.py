"""Integrations: the frame's sign-in, the embed script and the studio pages."""
import hashlib
import json
import time

from django import forms
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.core.exceptions import PermissionDenied
from django.db.models import Q
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy
from django.views.decorators.cache import cache_control
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from apps.core.templatetags.akon import avatar
from apps.live.models import Live
from apps.rbac import permissions as policy

from . import diagnostics, identities, sessions, tokens, webhooks
from .models import Integration, WebhookDelivery, normalize_origin

#: Sign-ins one address may attempt per minute.
#: Refused sign-ins from one address in a minute before it has to wait: what
#: stops someone trying tokens, without ever holding back a class.
REFUSED_BURST = 40
#: Sign-ins of one person in a minute: reloads and a few tabs, not a loop.
PERSON_BURST = 20
#: People listed in the studio at a time.
PEOPLE_PAGE = 50


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

    origin = normalize_origin(said)
    if integration.origin_list:
        direct = data.get("direct") is True and not origin
        allowed = (
            (direct and integration.allow_direct)
            or (origin and (origin == _origin_of(request) or integration.allows_origin(origin)))
        )
        if not allowed:
            if direct:
                detail = _("Opening this address by itself is turned off for the integration: its creator can allow it in the integration's settings.")
            elif not origin:
                detail = _("The page did not say which site it is on. Use the QQplay script, or do not set a no-referrer policy on the frame.")
            else:
                detail = _("This site is not allowed to use the integration: add %(origin)s to its sites.") % {"origin": origin}
            _one_more(refused, REFUSED_BURST)
            diagnostics.record(integration, "origin", detail, said or ("direct" if direct else ""))
            return JsonResponse({"detail": detail, "code": "origin"}, status=403)

    identity = identities.sync(person)
    if identity.is_blocked or not identity.user.is_active:
        diagnostics.record(integration, "blocked", _("This account is blocked."), said)
        return JsonResponse({"detail": _("This account is blocked."), "code": "blocked"}, status=403)
    Integration.objects.filter(pk=integration.pk).update(last_used_at=timezone.now())
    return JsonResponse(
        {
            "session": sessions.issue(identity, person.secret_version),
            "expires_in": sessions.LIFETIME,
            "user": {
                "name": identity.user.get_short_name(),
                "avatar": str(avatar(identity.user, "sm")),
                "moderator": identity.is_moderator,
            },
        }
    )


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
