"""The QQplay account behind a person of a site.

Accounts are never matched by email address: a site could otherwise name any
address it likes, an administrator's included, and speak as that account.
Each person of each site gets an account of their own, with an address no
mailbox answers and no password.
"""
from __future__ import annotations

import hashlib

from django.db import IntegrityError, transaction
from django.db.models import F
from django.utils import timezone

from apps.rbac.roles import Role
from apps.users.models import User

from .models import ExternalIdentity
from .tokens import Person

#: The domain of the accounts made for people of a site (RFC 2606: it never
#: resolves, so nothing is ever sent there).
ACCOUNT_DOMAIN = "integrations.qqplay.invalid"


def account_email(integration, external_id: str) -> str:
    digest = hashlib.sha256(f"{integration.pk}:{external_id}".encode()).hexdigest()[:20]
    return f"site-{digest}@{ACCOUNT_DOMAIN}"


def sync(person: Person) -> ExternalIdentity:
    """The person's identity, created on their first visit and kept up to
    date with what the site says about them on every later one."""
    integration = person.integration
    identity = (
        ExternalIdentity.objects.select_related("user")
        .filter(integration=integration, external_id=person.external_id)
        .first()
    )
    if identity is None:
        email = account_email(integration, person.external_id)
        try:
            with transaction.atomic():
                # Their account may be there already without its identity (the
                # identity alone was deleted in the Admin): they get it back.
                user = User.objects.filter(email=email, external_identity__isnull=True).first()
                if user is None:
                    user = User(email=email, display_name=person.name, role=Role.MEMBER)
                    user.set_unusable_password()
                    user.save()
                identity = ExternalIdentity.objects.create(
                    integration=integration, external_id=person.external_id, user=user,
                    name=person.name, email=person.email, is_moderator=person.moderator, groups=person.groups,
                )
        except IntegrityError:  # the same person's first two requests at once
            identity = ExternalIdentity.objects.select_related("user").get(
                integration=integration, external_id=person.external_id
            )

    changed = []
    for field, value in (
        ("name", person.name), ("email", person.email), ("is_moderator", person.moderator), ("groups", person.groups),
    ):
        if getattr(identity, field) != value:
            setattr(identity, field, value)
            changed.append(field)
    picture_changed = identity.picture_url != person.picture
    if picture_changed:
        identity.picture_url = person.picture
        changed.append("picture_url")
    identity.last_seen_at = timezone.now()
    identity.save(update_fields=changed + ["last_seen_at"])

    user = identity.user
    if user.display_name != person.name:
        user.display_name = person.name
        user.save(update_fields=["display_name"])

    if picture_changed:
        from .tasks import fetch_picture  # the task module imports this one

        transaction.on_commit(lambda: fetch_picture.delay(identity.pk))
    return identity


def find(integration, external_id: str) -> ExternalIdentity | None:
    return (
        ExternalIdentity.objects.select_related("user")
        .filter(integration=integration, external_id=str(external_id))
        .first()
    )


def block(identity: ExternalIdentity, blocked: bool = True) -> None:
    """A blocked person's sessions end at once and they cannot sign in again
    through the site, whatever tokens it makes for them."""
    identity.is_blocked = blocked
    if blocked:
        # The sessions they had do not come back with an unblock.
        identity.sessions_since = timezone.now()
    identity.save(update_fields=["is_blocked", "sessions_since"])


def _people_changed(user) -> None:
    """Pages showing what this person wrote must drop or rename it now: the
    chats they took part in get a new version, which also retires the shared
    copy of each chat kept in the cache."""
    from apps.live.models import ChatMessage, Live

    lives = list(ChatMessage.objects.filter(author=user).values_list("live_id", flat=True).distinct())
    if lives:
        Live.objects.filter(pk__in=lives).update(chat_version=F("chat_version") + 1)


def erase(identity: ExternalIdentity, keep_messages: bool = False) -> None:
    """Forget a person (the right of deletion of the LGPD/GDPR): their name,
    address and picture go. With ``keep_messages`` their messages stay in the
    chats under "Removed person"; otherwise they go too, with their reactions
    and attendance. A later sign-in from the site makes a new, empty account.
    """
    user = identity.user
    with transaction.atomic():
        identity.delete()
        _forget(user, keep_messages)


def erase_all(integration) -> int:
    """Forget everyone who came through an integration that is going away,
    keeping what they wrote under "Removed person". Without this their
    accounts would stay behind with names and pictures, and no way left to
    reach them."""
    count = 0
    for identity in list(integration.identities.select_related("user")):
        erase(identity, keep_messages=True)
        count += 1
    return count


def _forget(user, keep_messages: bool) -> None:
    from django.conf import settings
    from django.utils import translation

    from apps.media.models import Comment
    from apps.users import avatars

    _people_changed(user)
    if not keep_messages:
        # Other people's replies to their comments are not theirs to take
        # along: they stay, as comments of their own.
        Comment.objects.filter(parent__author=user).exclude(author=user).update(parent=None)
        if user.avatar_photo:
            user.avatar_photo.delete(save=False)
        user.delete()
        return
    if user.avatar_photo:
        avatars.remove_photo(user)
    # In the portal's language, whoever asked for it.
    with translation.override(settings.LANGUAGE_CODE):
        user.display_name = translation.gettext("Removed person")
    user.email = f"removed-{hashlib.sha256(str(user.pk).encode()).hexdigest()[:20]}@{ACCOUNT_DOMAIN}"
    user.avatar = ""
    user.is_active = False
    user.save(update_fields=["display_name", "email", "avatar", "is_active"])
