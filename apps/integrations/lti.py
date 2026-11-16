"""QQplay as an LTI 1.3 tool: launched from inside an LMS.

Moodle, Canvas, Blackboard, Brightspace, Schoology, Sakai and Open edX all
open outside tools this way. The LMS has already signed its people in; a
launch tells this portal who is there, with what role, in which course, and
the portal shows them a live with its chat or a video with its comments.

The launch, as the specification has it (1EdTech LTI 1.3 Core and Security):

1. The LMS sends the browser to /lti/login/ ("third-party initiated login")
   saying who it is (iss) and whom it means to sign in (login_hint).
2. This portal sends the browser back to the LMS's authentication address
   with a state and a nonce of its own.
3. The LMS posts an id_token to /lti/launch/: a JWT it signed with its
   private key, checked here against the public keys it publishes.

A registered LMS is an LtiPlatform, under one integration: its people become
that integration's people (identities.py), a teacher there a moderator here,
the course their class. What a link shows is chosen by the teacher when the
activity is created ("deep linking"), or written in the link's address.

Third-party cookies: inside an LMS's frame most browsers keep none, so the
state cannot ride on one. It is signed instead, good for ten minutes, and its
nonce is taken once; a cookie is still set and, where the browser kept it,
must match.
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
import time
import urllib.request
from dataclasses import dataclass
from urllib.parse import parse_qs, urlencode, urlsplit

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from django.conf import settings
from django.core import signing
from django.core.cache import cache
from django.urls import reverse
from django.utils.translation import gettext as _

from apps.live.models import parse_groups

from . import net
from .models import EVERY_CLASS, LtiKey, LtiPlatform
from .tokens import Person

CLAIM = "https://purl.imsglobal.org/spec/lti/claim/"
DL_CLAIM = "https://purl.imsglobal.org/spec/lti-dl/claim/"
TOOL_CONFIGURATION = "https://purl.imsglobal.org/spec/lti-tool-configuration"
RESOURCE_LINK = "LtiResourceLinkRequest"
DEEP_LINKING = "LtiDeepLinkingRequest"
STATE_SALT = "qqplay.lti.state"
STATE_AGE = 600
COOKIE = "qqplay_lti_state"
#: Parts of a role's name that make its holder staff here: the course's
#: teachers and their assistants, and the LMS's administrators.
# Who teaches in the course the launch came from. An LMS also says what
# someone is elsewhere in the school ("institution" roles: Canvas gives
# Instructor there to whoever teaches any course): that does not make them a
# teacher of this one. Only the school's administrators count from there.
STAFF_ROLES = ("Instructor", "TeachingAssistant", "ContentDeveloper", "Administrator")
COURSE_ROLES = ("http://purl.imsglobal.org/vocab/lis/v2/membership", "urn:lti:role:ims/lis/")
SCHOOL_ROLES = ("http://purl.imsglobal.org/vocab/lis/v2/system/person#",
                "http://purl.imsglobal.org/vocab/lis/v2/institution/person#",
                "urn:lti:sysrole:ims/lis/", "urn:lti:instrole:ims/lis/")
LEEWAY = 60


class LtiError(Exception):
    """Why a launch or a registration was refused, for the page that says so."""


# ---------------------------------------------------------------- our keys


def tool_key() -> tuple[str, str]:
    """The key this tool signs with (kid, private PEM), made on first use."""
    key = LtiKey.objects.first()
    if key is None:
        private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pem = private.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        ).decode()
        key = LtiKey.objects.create(kid=secrets.token_hex(8), private_pem=pem)
    return key.kid, key.private_pem


def jwks() -> dict:
    """The public halves of the tool's keys, as LMSs fetch them."""
    tool_key()
    keys = []
    for key in LtiKey.objects.all():
        private = serialization.load_pem_private_key(key.private_pem.encode(), password=None)
        public = jwt.algorithms.RSAAlgorithm.to_jwk(private.public_key(), as_dict=True)
        keys.append({**public, "kid": key.kid, "alg": "RS256", "use": "sig"})
    return {"keys": keys}


def tool_urls(request) -> dict:
    return {
        "login": request.build_absolute_uri(reverse("integrations:lti_login")),
        "launch": request.build_absolute_uri(reverse("integrations:lti_launch")),
        "jwks": request.build_absolute_uri(reverse("integrations:lti_jwks")),
    }


# ------------------------------------------------------------- the LMS's side


def _fetch(method: str, url: str, body: bytes | None = None, headers: dict | None = None) -> tuple[int, bytes]:
    """Talk to an LMS's server, as carefully as to any address a site gives
    (net.py). In development a local LMS may be allowed (dev settings)."""
    local = urlsplit(url).hostname in ("127.0.0.1", "localhost")
    if local and getattr(settings, "LTI_ALLOW_LOCAL_PLATFORMS", False):
        request = urllib.request.Request(url, data=body, headers=headers or {}, method=method)
        try:
            with urllib.request.urlopen(request, timeout=8) as response:  # noqa: S310 - development only
                return response.status, response.read(512 * 1024)
        except urllib.error.HTTPError as error:
            return error.code, error.read(64 * 1024)
        except OSError as error:
            raise LtiError(str(error)) from error
    try:
        answer = net.request(method, url, body=body, headers=headers, max_bytes=512 * 1024)
    except net.NetError as error:
        raise LtiError(_("The LMS could not be reached: %(why)s") % {"why": error}) from error
    return answer.status, answer.body


def _json(method: str, url: str, body: bytes | None = None, headers: dict | None = None) -> dict:
    status, raw = _fetch(method, url, body, {"Accept": "application/json", **(headers or {})})
    if not 200 <= status < 300:
        # What it says about why is worth showing: it names what it refused.
        said = " ".join(raw[:400].decode("utf-8", "replace").split())
        if said and "<" not in said:
            raise LtiError(_("The LMS answered %(status)d: %(said)s") % {"status": status, "said": said[:160]})
        raise LtiError(_("The LMS answered %(status)d.") % {"status": status})
    try:
        data = json.loads(raw)
    except ValueError as error:
        raise LtiError(_("The LMS's answer could not be read.")) from error
    if not isinstance(data, dict):
        raise LtiError(_("The LMS's answer could not be read."))
    return data


def platform_key(platform: LtiPlatform, kid: str):
    """The LMS's public key a token says it was signed with. Its key set is
    kept for an hour and fetched again when a key is not in it (LMSs rotate
    their keys)."""
    cached = f"integrations:lti:jwks:{platform.pk}"
    keys = cache.get(cached) or {}
    if kid not in keys:
        found = _json("GET", platform.jwks_url).get("keys")
        keys = {str(key.get("kid")): key for key in found or [] if isinstance(key, dict)}
        cache.set(cached, keys, timeout=3600)
    if kid not in keys:
        raise LtiError(_("The LMS signed with a key it does not publish."))
    try:
        return jwt.PyJWK.from_dict(keys[kid]).key
    except jwt.PyJWTError as error:
        raise LtiError(_("The LMS's key could not be read.")) from error


def find_platform(issuer: str, client_id: str = "") -> LtiPlatform | None:
    """The registration a login is for. An LMS that does not say which of
    its tools it means (client_id) is only understood when it has one here:
    with several, guessing could hand the login to someone else's."""
    found = LtiPlatform.objects.select_related("integration").filter(
        issuer=issuer, is_active=True, integration__is_active=True
    )
    if client_id:
        return found.filter(client_id=client_id).first()
    matches = list(found[:2])
    return matches[0] if len(matches) == 1 else None


# ------------------------------------------------------------------ the launch


def login_redirect(request, platform: LtiPlatform, params) -> tuple[str, str]:
    """Where the browser goes to be signed in by the LMS, and the value a
    cookie keeps to recognise it on the way back."""
    nonce = secrets.token_urlsafe(24)
    state = signing.dumps({"p": platform.pk, "n": nonce}, salt=STATE_SALT, compress=True)
    query = {
        "scope": "openid",
        "response_type": "id_token",
        "response_mode": "form_post",
        "prompt": "none",
        "client_id": platform.client_id,
        "redirect_uri": tool_urls(request)["launch"],
        "login_hint": params.get("login_hint", ""),
        "state": state,
        "nonce": nonce,
    }
    if params.get("lti_message_hint"):
        query["lti_message_hint"] = params["lti_message_hint"]
    separator = "&" if "?" in platform.auth_login_url else "?"
    return platform.auth_login_url + separator + urlencode(query), _mark(state)


def _mark(state: str) -> str:
    return hashlib.sha256(state.encode()).hexdigest()[:32]


@dataclass
class Launch:
    platform: LtiPlatform
    claims: dict

    def claim(self, name: str, default=None):
        return self.claims.get(CLAIM + name, default)

    @property
    def message_type(self) -> str:
        return self.claim("message_type", "")

    @property
    def is_staff(self) -> bool:
        roles = self.claim("roles")
        for role in roles if isinstance(roles, list) else []:
            if not isinstance(role, str):
                continue
            if role.startswith(SCHOOL_ROLES):
                if role.endswith(("#Administrator", "/Administrator")):
                    return True
                continue
            course = next((prefix for prefix in COURSE_ROLES if role.startswith(prefix)), None)
            if course is None and ("/" in role or ":" in role):
                continue  # some other vocabulary
            # "…membership#Instructor", "…membership/Instructor#TeachingAssistant",
            # or the short names ("Instructor") some platforms still send.
            names = re.split(r"[#/]", role[len(course or ""):])
            if any(name in names for name in STAFF_ROLES):
                return True
        return False

    @property
    def deployment_id(self) -> str:
        return str(self.claim("deployment_id") or "")

    @property
    def deep_linking(self) -> dict:
        settings_claim = self.claims.get(DL_CLAIM + "deep_linking_settings")
        return settings_claim if isinstance(settings_claim, dict) else {}


def validate(id_token: str, state: str, cookie: str = "") -> Launch:
    """The launch an LMS posted, checked, or LtiError."""
    if not id_token or not state:
        raise LtiError(_("This is not a launch from an LMS."))
    try:
        sent = signing.loads(state, salt=STATE_SALT, max_age=STATE_AGE)
    except signing.BadSignature as error:
        raise LtiError(_("The launch took too long or did not start here. Open the activity again.")) from error
    # Where the browser kept the cookie it must be this launch's; inside a
    # frame it usually kept none, and the signed state stands alone.
    if cookie and cookie != _mark(state):
        raise LtiError(_("The launch does not match the one this browser started. Open the activity again."))
    platform = LtiPlatform.objects.select_related("integration").filter(
        pk=sent.get("p"), is_active=True, integration__is_active=True
    ).first()
    if platform is None:
        raise LtiError(_("This LMS is no longer registered here."))
    try:
        kid = jwt.get_unverified_header(id_token).get("kid") or ""
        claims = jwt.decode(
            id_token, platform_key(platform, str(kid)), algorithms=["RS256"], audience=platform.client_id,
            issuer=platform.issuer, leeway=LEEWAY, options={"require": ["exp", "iat", "nonce", "sub"]},
        )
    except jwt.PyJWTError as error:
        raise LtiError(_("The LMS's launch could not be verified: %(why)s") % {"why": error}) from error
    # More than one audience: the token must say it is for this tool (azp).
    if isinstance(claims.get("aud"), list) and len(claims["aud"]) > 1 and claims.get("azp") != platform.client_id:
        raise LtiError(_("The launch was made for another tool."))
    if not secrets.compare_digest(str(claims.get("nonce")), str(sent.get("n"))):
        raise LtiError(_("The launch does not match the one that was started. Open the activity again."))
    if not cache.add(f"integrations:lti:nonce:{claims['nonce']}", 1, timeout=3600):
        raise LtiError(_("This launch was used already. Open the activity again from your course."))
    launch = Launch(platform, claims)
    if launch.claim("version") != "1.3.0":
        raise LtiError(_("Only LTI 1.3 launches are understood here."))
    if launch.message_type not in (RESOURCE_LINK, DEEP_LINKING):
        raise LtiError(_("This kind of launch is not supported: %(kind)s") % {"kind": launch.message_type or "?"})
    if platform.deployment_ids and launch.deployment_id not in [str(item) for item in platform.deployment_ids]:
        raise LtiError(_("This deployment of the tool is not registered here."))
    return launch


def person(launch: Launch) -> Person:
    """Who the LMS says is there, as the sign-in knows people."""
    claims = launch.claims
    name = str(claims.get("name") or "").strip() or " ".join(
        part for part in (str(claims.get("given_name") or "").strip(), str(claims.get("family_name") or "").strip())
        if part
    )
    email = str(claims.get("email") or "").strip()[:254]
    if not name:
        name = email.split("@")[0] if "@" in email else (_("Teacher") if launch.is_staff else _("Student"))
    picture = str(claims.get("picture") or "").strip()[:1000]
    context = launch.claim("context") if isinstance(launch.claim("context"), dict) else {}
    # The course is the class: by its short name, and by its id.
    # (A course named "*" is a course named "*", not every class.)
    classes = [name for name in parse_groups([context.get("label") or "", context.get("id") or ""])
               if name != EVERY_CLASS]
    return Person(
        integration=launch.platform.integration,
        external_id=f"lti:{launch.platform.pk}:{claims['sub']}"[:200],
        name=name[:120],
        email=email if "@" in email else "",
        picture=picture if urlsplit(picture).scheme == "https" else "",
        moderator=launch.is_staff,
        groups=classes,
    )


def content(launch: Launch) -> dict:
    """What the link shows: chosen when the activity was made (custom
    parameters), or written in the link's address (?live= or ?video=)."""
    custom = launch.claim("custom") if isinstance(launch.claim("custom"), dict) else {}
    query = parse_qs(urlsplit(str(launch.claim("target_link_uri") or "")).query)

    def pick(name: str) -> str:
        value = custom.get(f"qqplay_{name}") or (query.get(name) or [""])[0]
        return str(value)[:40]

    layout = pick("layout")
    return {
        "live": pick("live"),
        "video": pick("video"),
        "layout": layout if layout in ("both", "player", "chat", "comments") else "both",
    }


# --------------------------------------------------------------- deep linking


def deep_link_token(launch: Launch) -> str:
    """What the content picker needs of a deep-linking launch, to answer the
    LMS after the teacher chose: signed, for the few minutes choosing takes."""
    settings_claim = launch.deep_linking
    return signing.dumps(
        {
            "p": launch.platform.pk,
            "d": launch.deployment_id,
            "r": str(settings_claim.get("deep_link_return_url") or ""),
            "x": settings_claim.get("data"),
        },
        salt=STATE_SALT + ".deep", compress=True,
    )


def deep_link_response(token: str, request, kind: str, uid: str, title: str, layout: str) -> tuple[str, str]:
    """(the LMS's return address, the signed answer to post there)."""
    try:
        sent = signing.loads(token, salt=STATE_SALT + ".deep", max_age=3600)
    except signing.BadSignature as error:
        raise LtiError(_("Choosing took too long. Start again from your course.")) from error
    platform = LtiPlatform.objects.filter(pk=sent.get("p"), is_active=True).first()
    if platform is None or not sent.get("r"):
        raise LtiError(_("This LMS is no longer registered here."))
    launch_url = tool_urls(request)["launch"]
    item = {
        "type": "ltiResourceLink",
        "title": title[:200],
        "url": f"{launch_url}?{urlencode({kind: uid, 'layout': layout})}",
        "custom": {f"qqplay_{kind}": uid, "qqplay_layout": layout},
    }
    now = int(time.time())
    payload = {
        "iss": platform.client_id,
        "aud": platform.issuer,
        "iat": now,
        "exp": now + 300,
        "nonce": secrets.token_urlsafe(16),
        CLAIM + "message_type": "LtiDeepLinkingResponse",
        CLAIM + "version": "1.3.0",
        CLAIM + "deployment_id": sent.get("d") or "",
        DL_CLAIM + "content_items": [item],
    }
    if sent.get("x") is not None:
        payload[DL_CLAIM + "data"] = sent["x"]
    kid, pem = tool_key()
    return sent["r"], jwt.encode(payload, pem, algorithm="RS256", headers={"kid": kid})


# --------------------------------------------------------------- registration


def registration_token(integration) -> str:
    """What makes an address an LMS's administrator can paste to register
    the tool by itself, under this integration. Good for a week."""
    return signing.dumps(str(integration.pk), salt=STATE_SALT + ".register")


def integration_for(token: str):
    from .models import Integration

    try:
        pk = signing.loads(token, salt=STATE_SALT + ".register", max_age=7 * 24 * 3600)
    except signing.BadSignature:
        return None
    return Integration.objects.filter(pk=pk, is_active=True).first()


def register(integration, request, configuration_url: str, token: str = "") -> LtiPlatform:
    """LTI Dynamic Registration: read the LMS's configuration, tell it about
    this tool, and keep what it answers."""
    configuration = _json("GET", configuration_url)
    issuer = str(configuration.get("issuer") or "")
    endpoint = str(configuration.get("registration_endpoint") or "")
    needed = ("authorization_endpoint", "jwks_uri")
    if not issuer or not endpoint or any(not configuration.get(name) for name in needed):
        raise LtiError(_("The LMS's configuration is missing what a registration needs."))
    # An LMS registers tools at its own address, not at someone else's.
    if urlsplit(endpoint).hostname != urlsplit(configuration_url).hostname:
        raise LtiError(_("The LMS's configuration points to another site."))
    urls = tool_urls(request)
    # With its port, when it is not the usual one: Moodle compares this with
    # the address of the launch, port and all.
    host = urlsplit(urls["launch"]).netloc
    name = f"{settings.PORTAL_NAME} · {integration.name}"[:100]
    registration = {
        "application_type": "web",
        "response_types": ["id_token"],
        "grant_types": ["implicit", "client_credentials"],
        "initiate_login_uri": urls["login"],
        "redirect_uris": [urls["launch"]],
        "client_name": name,
        "jwks_uri": urls["jwks"],
        "token_endpoint_auth_method": "private_key_jwt",
        "scope": "",
        TOOL_CONFIGURATION: {
            "domain": host,
            "target_link_uri": urls["launch"],
            "description": "Lives with chat and videos with comments.",
            "claims": ["iss", "sub", "name", "given_name", "family_name", "email", "picture"],
            "messages": [{"type": DEEP_LINKING, "target_link_uri": urls["launch"], "label": name}],
        },
    }
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    answer = _json("POST", endpoint, json.dumps(registration).encode(), headers)
    client_id = str(answer.get("client_id") or "")
    if not client_id:
        raise LtiError(_("The LMS did not give the tool an id."))
    tool = answer.get(TOOL_CONFIGURATION) if isinstance(answer.get(TOOL_CONFIGURATION), dict) else {}
    deployment = str(tool.get("deployment_id") or "")
    family = configuration.get("https://purl.imsglobal.org/spec/lti-platform-configuration")
    family = family.get("product_family_code") if isinstance(family, dict) else ""
    taken = LtiPlatform.objects.filter(issuer=issuer[:500], client_id=client_id[:200]).first()
    if taken is not None and taken.integration_id != integration.pk:
        raise LtiError(_("This LMS and tool id are registered under another integration."))
    platform, _created = LtiPlatform.objects.update_or_create(
        issuer=issuer[:500], client_id=client_id[:200],
        defaults={
            "integration": integration,
            "name": str(family or urlsplit(issuer).hostname or "")[:120],
            "deployment_ids": [deployment] if deployment else [],
            "auth_login_url": str(configuration["authorization_endpoint"])[:500],
            "auth_token_url": str(configuration.get("token_endpoint") or "")[:500],
            "jwks_url": str(configuration["jwks_uri"])[:500],
            "is_active": True,
        },
    )
    return platform
