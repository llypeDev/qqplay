"""Signing in with a code sent by e-mail, for schools that sign no tokens.

The person types their e-mail in the frame (or on the class page); if it is
on the integration's list of enrolments, a six-digit code goes to it, good
for ten minutes and a few tries. The code proves the e-mail is theirs, which
no page they are on could do for them. The answer to "send me a code" is the
same whether the e-mail is enrolled or not, so the list cannot be read by
asking.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets

from django.conf import settings
from django.core.cache import cache
from django.core.mail import send_mail
from django.utils import translation
from django.utils.translation import gettext as _

from . import enrolments

CODE_MINUTES = 10
#: Wrong codes before a new one must be asked for.
TRIES = 5
#: Codes one e-mail may ask for while one is still good.
CODES_PER_WINDOW = 3


class LoginError(Exception):
    def __init__(self, message, code="code", status=400):
        super().__init__(message)
        self.code = code
        self.status = status


def _key(integration, email: str) -> str:
    return f"integrations:code:{integration.pk}:{hashlib.sha256(email.encode()).hexdigest()[:24]}"


def _digest(code: str) -> str:
    return hmac.new(settings.SECRET_KEY.encode(), code.encode(), hashlib.sha256).hexdigest()


def start(integration, email: str, language: str | None = None) -> bool:
    """Send a code if the e-mail is enrolled. True when one was sent; callers
    answer the visitor the same either way."""
    email = enrolments.normalize_email(email)
    if not email:
        raise LoginError(_("That does not look like an e-mail address."), "email")
    counter = _key(integration, email) + ":asked"
    cache.add(counter, 0, timeout=CODE_MINUTES * 60)
    try:
        asked = cache.incr(counter)
    except ValueError:
        asked = 1
    if asked > CODES_PER_WINDOW:
        raise LoginError(_("Too many codes asked for: wait a few minutes."), "rate", 429)
    enrolment = enrolments.find(integration, email)
    if enrolment is None or not enrolment.is_active:
        return False
    code = f"{secrets.randbelow(1_000_000):06d}"
    cache.set(_key(integration, email), {"code": _digest(code), "tries": 0}, timeout=CODE_MINUTES * 60)
    from .tasks import send_login_code

    send_login_code.delay(email, code, integration.name, language or settings.LANGUAGE_CODE)
    return True


def verify(integration, email: str, code: str):
    """The enrolment whose e-mail the code was sent to, or LoginError."""
    email = enrolments.normalize_email(email)
    code = "".join(ch for ch in str(code or "") if ch.isdigit())[:6]
    wrong = LoginError(_("That code is not right, or it is no longer good. Ask for a new one."))
    if not email or len(code) != 6:
        raise wrong
    key = _key(integration, email)
    entry = cache.get(key)
    if not entry:
        raise wrong
    if not hmac.compare_digest(entry["code"], _digest(code)):
        entry["tries"] += 1
        if entry["tries"] >= TRIES:
            cache.delete(key)
        else:
            cache.set(key, entry, timeout=CODE_MINUTES * 60)
        raise wrong
    cache.delete(key)
    enrolment = enrolments.find(integration, email)
    if enrolment is None or not enrolment.is_active:
        raise wrong
    return enrolment


def send(email: str, code: str, school: str, language: str) -> None:
    """The message with the code (called by the task)."""
    with translation.override(language):
        subject = _("%(code)s is your code for %(school)s") % {"code": code, "school": school}
        body = _(
            "Your code to sign in to %(school)s is:\n\n    %(code)s\n\n"
            "It is good for %(minutes)d minutes. If you did not ask for it, ignore this message."
        ) % {"code": code, "school": school, "minutes": CODE_MINUTES}
    send_mail(subject, body, settings.DEFAULT_FROM_EMAIL, [email])
