"""The people enrolled at a school that signs no tokens (see Enrolment).

They are who their e-mail says. The list is fed by hand and by spreadsheet in
the studio, by the school's server through the API, and by the purchases the
sales platforms announce (hooks.py). Being on it and active is what lets
someone sign in with a code by e-mail (login.py).
"""
from __future__ import annotations

import csv
import io

from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.live.models import parse_groups

from .models import Enrolment
from .tokens import Person

#: Rows one import takes.
IMPORT_LIMIT = 5000
TRUE_WORDS = {"1", "true", "yes", "y", "sim", "s", "x", "professor", "professora", "teacher", "moderator"}
HEADERS = {
    "email": {"email", "e-mail", "mail", "e_mail"},
    "name": {"name", "nome", "full_name", "nome completo", "aluno", "student"},
    "classes": {"classes", "class", "turma", "turmas", "groups", "group", "grupo", "grupos"},
    "teacher": {"teacher", "professor", "moderator", "moderador"},
    "id": {"id", "external_id", "matricula", "matrícula", "codigo", "código"},
}


def normalize_email(value) -> str:
    """The e-mail in the one form it is kept in, or '' if it is not one."""
    email = str(value or "").strip().lower()
    if not email or len(email) > 254:
        return ""
    try:
        validate_email(email)
    except ValidationError:
        return ""
    return email


def find(integration, email: str) -> Enrolment | None:
    email = normalize_email(email)
    return Enrolment.objects.filter(integration=integration, email=email).first() if email else None


def upsert(integration, email, *, name=None, classes=None, teacher=None, external_id=None, active=True,
           source="") -> tuple[Enrolment | None, bool]:
    """Add someone to the list or bring what is known of them up to date.
    Only what is given changes; (None, False) when the e-mail is not one."""
    email = normalize_email(email)
    if not email:
        return None, False
    fresh = {
        "name": str(name or "").strip()[:120],
        "groups": parse_groups(classes) if isinstance(classes, (list, str)) else [],
        "is_moderator": bool(teacher),
        "external_id": str(external_id or "").strip()[:200],
        "is_active": bool(active),
        "source": source[:40],
    }
    try:
        with transaction.atomic():
            enrolment, created = Enrolment.objects.get_or_create(integration=integration, email=email, defaults=fresh)
    except IntegrityError:  # two posts about the same person at once
        enrolment, created = Enrolment.objects.get(integration=integration, email=email), False
    if created:
        return enrolment, True
    changed = []
    updates = {"is_active": bool(active)}
    if name is not None and str(name).strip():
        updates["name"] = fresh["name"]
    if classes is not None:
        updates["groups"] = fresh["groups"]
    if teacher is not None:
        updates["is_moderator"] = fresh["is_moderator"]
    if external_id is not None and fresh["external_id"]:
        updates["external_id"] = fresh["external_id"]
    for field, value in updates.items():
        if getattr(enrolment, field) != value:
            setattr(enrolment, field, value)
            changed.append(field)
    if changed:
        enrolment.save(update_fields=changed + ["updated_at"])
        if "is_active" in changed and not enrolment.is_active:
            _end_sessions(enrolment)
    return enrolment, False


def add_classes(enrolment: Enrolment, classes) -> None:
    """Classes a purchase brings, on top of the ones they had."""
    merged = parse_groups(list(enrolment.groups or []) + parse_groups(classes))
    if merged != enrolment.groups:
        enrolment.groups = merged
        enrolment.save(update_fields=["groups", "updated_at"])


def revoke(integration, email) -> bool:
    """Their access ended: they stay on the list, off, and whatever session
    they had is over. True if they were on it and active."""
    enrolment = find(integration, email)
    if enrolment is None or not enrolment.is_active:
        return False
    enrolment.is_active = False
    enrolment.save(update_fields=["is_active", "updated_at"])
    _end_sessions(enrolment)
    return True


def remove(enrolment: Enrolment) -> None:
    _end_sessions(enrolment)
    enrolment.delete()


def _end_sessions(enrolment: Enrolment) -> None:
    if enrolment.identity_id:
        type(enrolment.identity).objects.filter(pk=enrolment.identity_id).update(sessions_since=timezone.now())


def person_for(enrolment: Enrolment) -> Person:
    """The enrolment as the sign-in knows people, so the same code that keeps
    a site's students up to date keeps these too."""
    return Person(
        integration=enrolment.integration,
        external_id=enrolment.external_id or f"email:{enrolment.email}",
        name=enrolment.name or enrolment.email.split("@")[0],
        email=enrolment.email,
        picture="",
        moderator=enrolment.is_moderator,
        groups=list(enrolment.groups or []),
    )


def import_rows(integration, text: str, source: str = "csv") -> dict:
    """A spreadsheet's rows: e-mail, name, classes, teacher, in that order or
    under a header row naming them (in English or Portuguese). Commas,
    semicolons or tabs. Classes within a cell are split by | or by commas.
    Returns {"added", "updated", "bad": [line numbers]}."""
    text = (text or "").lstrip("\ufeff")
    sample = text[:2000]
    delimiter = max(",;\t", key=sample.count) if sample else ","
    rows = [row for row in csv.reader(io.StringIO(text), delimiter=delimiter) if any(cell.strip() for cell in row)]
    result = {"added": 0, "updated": 0, "bad": []}
    if not rows:
        return result
    columns = {"email": 0, "name": 1, "classes": 2, "teacher": 3}
    first = [cell.strip().lower() for cell in rows[0]]
    if any(cell in HEADERS["email"] for cell in first):
        columns = {}
        for index, cell in enumerate(first):
            for field, names in HEADERS.items():
                if cell in names and field not in columns:
                    columns[field] = index
        rows = rows[1:]

    def cell(row, field):
        index = columns.get(field)
        return row[index].strip() if index is not None and index < len(row) else ""

    for number, row in enumerate(rows[:IMPORT_LIMIT], start=1):
        classes = cell(row, "classes").replace("|", ",")
        enrolment, created = upsert(
            integration, cell(row, "email"), name=cell(row, "name") or None,
            classes=classes if "classes" in columns else None,
            teacher=(cell(row, "teacher").lower() in TRUE_WORDS) if "teacher" in columns else None,
            external_id=cell(row, "id") or None, source=source,
        )
        if enrolment is None:
            result["bad"].append(number)
        else:
            result["added" if created else "updated"] += 1
    return result
