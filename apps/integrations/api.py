"""The API for a school's server: its lives, their attendance, its people.

Every request carries ``Authorization: Bearer <token>``: the same kind of
token the site makes for its students (tokens.py), with ``"scope": "api"``
instead of a person, valid for an hour at most. It acts for the integration's
owner, on the owner's lives and videos and on the integration's people only.

    GET    /api/integrations/v1/lives/                    the lives, newest first
    POST   /api/integrations/v1/lives/                    schedule one
    GET    /api/integrations/v1/lives/<id>/               one, with its stream key
    PATCH  /api/integrations/v1/lives/<id>/               change it
    DELETE /api/integrations/v1/lives/<id>/               delete it (not on air)
    POST   /api/integrations/v1/lives/<id>/end/           end it
    GET    /api/integrations/v1/lives/<id>/attendance/    who watched, how long
    GET    /api/integrations/v1/videos/                   the videos ready to show
    POST   /api/integrations/v1/students/<id>/block/      and .../unblock/
    DELETE /api/integrations/v1/students/<id>/            forget (?keep_messages=1)
"""
from __future__ import annotations

import json
from functools import wraps

from django.db.models import Q
from django.http import JsonResponse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.utils.translation import gettext as _
from django.views.decorators.csrf import csrf_exempt

from apps.live import attendance, services
from apps.live.models import Live, parse_groups
from apps.media.models import Media

from . import enrolments, identities, tokens
from .webhooks import live_data

VISIBILITIES = {choice for choice, _label in Media.Visibility.choices}


def _error(message: str, status: int = 400) -> JsonResponse:
    return JsonResponse({"detail": message}, status=status)


def server_api(view):
    """Authenticate the site's server; the view gets the integration."""

    @csrf_exempt
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        header = request.META.get("HTTP_AUTHORIZATION", "")
        token = header[7:].strip() if header[:7].lower() == "bearer " else ""
        try:
            integration = tokens.verify_api(token)
        except tokens.TokenError as error:
            return _error(str(error), 401)
        response = view(request, integration, *args, **kwargs)
        response["Cache-Control"] = "no-store"
        return response

    return wrapper


def _body(request) -> dict:
    try:
        data = json.loads(request.body or b"{}")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _live_out(live: Live, with_stream: bool = False) -> dict:
    data = live_data(live)
    data["description"] = live.description
    data["chat"] = live.chat_enabled
    data["record"] = live.record
    data["peak_viewers"] = live.peak_viewers
    if with_stream:
        data["stream"] = {"server": live.ingest_url, "key": live.encoder_key}
    return data


def _apply(live: Live, data: dict) -> str:
    """Copy the given fields onto the live; an error message, or ''."""
    if "title" in data:
        title = str(data.get("title") or "").strip()[:255]
        if not title:
            return _("The live needs a title.")
        live.title = title
    if "description" in data:
        live.description = str(data.get("description") or "")[:5000]
    if "visibility" in data:
        if not isinstance(data["visibility"], str) or data["visibility"] not in VISIBILITIES:
            return _("visibility is public, unlisted or private.")
        live.visibility = data["visibility"]
    if "scheduled_for" in data:
        value = data.get("scheduled_for")
        if value in (None, ""):
            live.scheduled_for = None
        else:
            try:
                parsed = parse_datetime(str(value))
            except ValueError:  # well formed, but no such day or hour
                parsed = None
            if parsed is None or timezone.is_naive(parsed):
                return _("scheduled_for is a date and time with its time zone, such as 2026-11-20T19:00:00-03:00.")
            live.scheduled_for = parsed
    if "classes" in data:
        classes = data.get("classes") or []
        groups = ", ".join(parse_groups(classes if isinstance(classes, (list, str)) else []))
        if len(groups) > Live._meta.get_field("groups").max_length:
            return _("Too many classes for one live.")
        live.groups = groups
    if "chat" in data:
        live.chat_enabled = bool(data["chat"])
    if "record" in data:
        live.record = bool(data["record"])
    return ""


@server_api
def lives(request, integration):
    if request.method == "GET":
        found = Live.objects.filter(owner_id=integration.owner_id).select_related("recording").order_by("-created_at")
        status = request.GET.get("status")
        if status:
            found = found.filter(status=status)
        return JsonResponse({"lives": [_live_out(live) for live in found[:100]]})
    if request.method != "POST":
        return _error(_("Not allowed."), 405)
    live = Live(owner_id=integration.owner_id, visibility=Media.Visibility.PRIVATE)
    data = _body(request)
    if "title" not in data:
        return _error(_("The live needs a title."))
    problem = _apply(live, data)
    if problem:
        return _error(problem)
    live.save()
    return JsonResponse(_live_out(live, with_stream=True), status=201)


def _owned(integration, uid: str):
    return Live.objects.filter(owner_id=integration.owner_id, uid=uid).select_related("recording").first()


@server_api
def live_detail(request, integration, uid: str):
    live = _owned(integration, uid)
    if live is None:
        return _error(_("No such live."), 404)
    if request.method == "GET":
        return JsonResponse(_live_out(live, with_stream=True))
    if request.method == "PATCH":
        problem = _apply(live, _body(request))
        if problem:
            return _error(problem)
        live.save()
        return JsonResponse(_live_out(live, with_stream=True))
    if request.method == "DELETE":
        if live.is_on_air:
            return _error(_("End the live before deleting it."), 409)
        live.delete()
        return JsonResponse({"deleted": uid})
    return _error(_("Not allowed."), 405)


@server_api
def live_end(request, integration, uid: str):
    if request.method != "POST":
        return _error(_("Not allowed."), 405)
    live = _owned(integration, uid)
    if live is None:
        return _error(_("No such live."), 404)
    services.end(live)
    live.refresh_from_db()
    return JsonResponse(_live_out(live))


@server_api
def live_attendance(request, integration, uid: str):
    live = _owned(integration, uid)
    if live is None:
        return _error(_("No such live."), 404)
    rows = [
        {
            "student": row["site_id"],
            "name": row["name"],
            "joined_at": row["first_seen"].isoformat(),
            "last_seen_at": row["last_seen"].isoformat(),
            "minutes": row["minutes"],
            "messages": row["messages"],
        }
        for row in attendance.report(live)
        if row["integration_id"] == str(integration.pk)
    ]
    return JsonResponse({"live": live.uid, "attendance": rows})


@server_api
def videos(request, integration):
    found = (
        Media.objects.filter(owner_id=integration.owner_id, state=Media.State.READY)
        .filter(Q(kind=Media.Kind.VIDEO) | Q(kind=Media.Kind.AUDIO))
        .order_by("-created_at")[:200]
    )
    return JsonResponse({"videos": [
        {
            "id": media.uid,
            "title": media.title,
            "kind": media.kind,
            "visibility": media.visibility,
            "duration": media.duration_seconds,
            "created_at": media.created_at.isoformat(),
        }
        for media in found
    ]})


@server_api
def student(request, integration, external_id: str, action: str = ""):
    identity = identities.find(integration, external_id)
    if identity is None:
        return _error(_("No such student."), 404)
    if request.method == "POST" and action in ("block", "unblock"):
        identities.block(identity, action == "block")
        return JsonResponse({"student": external_id, "blocked": action == "block"})
    if request.method == "DELETE" and not action:
        identities.erase(identity, keep_messages=request.GET.get("keep_messages") == "1")
        return JsonResponse({"student": external_id, "deleted": True})
    return _error(_("Not allowed."), 405)


def _enrolment_out(enrolment) -> dict:
    return {
        "email": enrolment.email,
        "name": enrolment.name,
        "id": enrolment.external_id,
        "classes": list(enrolment.groups or []),
        "teacher": enrolment.is_moderator,
        "active": enrolment.is_active,
        "source": enrolment.source,
        "updated_at": enrolment.updated_at.isoformat(),
    }


def _enrol(integration, item) -> tuple[dict | None, bool]:
    if not isinstance(item, dict):
        return None, False
    classes = item.get("classes")
    enrolment, created = enrolments.upsert(
        integration, item.get("email"),
        name=item.get("name") if isinstance(item.get("name"), str) else None,
        classes=classes if isinstance(classes, (list, str)) else None,
        teacher=item.get("teacher") if isinstance(item.get("teacher"), bool) else None,
        external_id=item.get("id") if isinstance(item.get("id"), (str, int)) else None,
        active=item.get("active") is not False,
        source="api",
    )
    return (_enrolment_out(enrolment) if enrolment else None), created


@server_api
def enrolment_list(request, integration):
    """The people enrolled, for schools whose students sign in by e-mail.

    GET  ?q=<part of an e-mail or name>&page=2     200 at a time
    POST one {"email", "name", "classes", "teacher", "id", "active"} or
         {"enrolments": [ ...up to 1000 of them ]}: each is added or
         brought up to date; "active": false turns the access off
    """
    if request.method == "GET":
        found = integration.enrolments.all()
        search = (request.GET.get("q") or "").strip()[:80]
        if search:
            found = found.filter(Q(email__icontains=search) | Q(name__icontains=search))
        try:
            page = max(1, int(request.GET.get("page", 1)))
        except (TypeError, ValueError):
            page = 1
        total = found.count()
        rows = found[(page - 1) * 200: page * 200]
        return JsonResponse({"total": total, "page": page, "enrolments": [_enrolment_out(row) for row in rows]})
    if request.method != "POST":
        return _error(_("Not allowed."), 405)
    data = _body(request)
    if isinstance(data.get("enrolments"), list):
        added = updated = 0
        bad = []
        for index, item in enumerate(data["enrolments"][:1000]):
            out, created = _enrol(integration, item)
            if out is None:
                bad.append(index)
            elif created:
                added += 1
            else:
                updated += 1
        return JsonResponse({"added": added, "updated": updated, "bad": bad})
    out, created = _enrol(integration, data)
    if out is None:
        return _error(_("That does not look like an e-mail address."))
    return JsonResponse(out, status=201 if created else 200)


@server_api
def enrolment_detail(request, integration, email: str):
    """GET one; DELETE takes them off the list (and ends their session)."""
    enrolment = enrolments.find(integration, email)
    if enrolment is None:
        return _error(_("No such enrolment."), 404)
    if request.method == "GET":
        return JsonResponse(_enrolment_out(enrolment))
    if request.method == "DELETE":
        enrolments.remove(enrolment)
        return JsonResponse({"deleted": enrolment.email})
    return _error(_("Not allowed."), 405)
