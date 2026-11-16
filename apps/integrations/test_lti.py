"""QQplay launched from an LMS (LTI 1.3), against an LMS made up here: it
has a real key pair and signs real tokens, and its addresses are answered by
a stand-in for the network."""
import json
import re
import time
from unittest import mock
from urllib.parse import parse_qs, urlsplit

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from django.core.cache import cache
from django.urls import reverse

from apps.live.models import Live
from apps.media.models import Media

from . import lti, sessions
from .models import ExternalIdentity, Integration, LtiPlatform
from .tests import SITE, IntegrationTestCase, make_user

CLAIM = lti.CLAIM
ISSUER = "https://moodle.escola.example.com"
INSTRUCTOR = "http://purl.imsglobal.org/vocab/lis/v2/membership#Instructor"
LEARNER = "http://purl.imsglobal.org/vocab/lis/v2/membership#Learner"


class Lms:
    """The LMS's side: its key, the tokens it signs, what its server answers."""

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    kid = "lms-key-1"

    def jwks(self):
        public = jwt.algorithms.RSAAlgorithm.to_jwk(self.key.public_key(), as_dict=True)
        return {"keys": [{**public, "kid": self.kid, "alg": "RS256", "use": "sig"}]}

    def token(self, client_id, nonce, key=None, **claims):
        now = int(time.time())
        payload = {
            "iss": ISSUER, "aud": client_id, "sub": "user-7", "iat": now, "exp": now + 300, "nonce": nonce,
            "name": "Ana Souza", "email": "ana@escola.example.com",
            CLAIM + "message_type": lti.RESOURCE_LINK, CLAIM + "version": "1.3.0", CLAIM + "deployment_id": "dep-1",
            CLAIM + "roles": [LEARNER],
            CLAIM + "context": {"id": "c-55", "label": "FIS-101", "title": "Física"},
            CLAIM + "resource_link": {"id": "rl-1"},
            CLAIM + "target_link_uri": "http://testserver/lti/launch/",
        }
        payload.update(claims)
        payload = {name: value for name, value in payload.items() if value is not None}
        return jwt.encode(payload, key or self.key, algorithm="RS256", headers={"kid": self.kid})


class Base(IntegrationTestCase):
    def setUp(self):
        super().setUp()
        self.lms = Lms()
        self.platform = LtiPlatform.objects.create(
            integration=self.integration, name="Moodle", issuer=ISSUER, client_id="client-1",
            auth_login_url=f"{ISSUER}/mod/lti/auth.php", jwks_url=f"{ISSUER}/mod/lti/certs.php",
        )
        self.fetched = []
        patcher = mock.patch.object(lti, "_fetch", side_effect=self.network)
        patcher.start()
        self.addCleanup(patcher.stop)

    def network(self, method, url, body=None, headers=None):
        self.fetched.append((method, url, body, headers))
        if url == self.platform.jwks_url:
            return 200, json.dumps(self.lms.jwks()).encode()
        return 404, b"{}"

    def start(self, client=None, **params):
        """The LMS starts a login; returns what the portal sent the browser back with."""
        client = client or self.client
        query = {"iss": ISSUER, "login_hint": "7", "target_link_uri": "http://testserver/lti/launch/",
                 "client_id": "client-1", "lti_message_hint": "hint-9", **params}
        response = client.get(reverse("integrations:lti_login"), query)
        self.assertEqual(response.status_code, 302, response.content[:300])
        sent = {name: values[0] for name, values in parse_qs(urlsplit(response["Location"]).query).items()}
        return response, sent

    def launch(self, client=None, forget_cookie=False, **claims):
        client = client or self.client
        _response, sent = self.start(client)
        if forget_cookie:
            client.cookies.clear()
        token = self.lms.token("client-1", sent["nonce"], **claims)
        return client.post(reverse("integrations:lti_launch"), {"id_token": token, "state": sent["state"]})

    def handed(self, response):
        found = re.search(r'data-handoff="([^"]+)"', response.content.decode())
        self.assertIsNotNone(found, response.content.decode()[:400])
        return json.loads(found.group(1).replace("&quot;", '"').replace("&#x27;", "'").replace("&amp;", "&"))


class LoginTests(Base):
    def test_the_browser_goes_back_to_the_lms_to_be_signed_in(self):
        response, sent = self.start()
        self.assertTrue(response["Location"].startswith(self.platform.auth_login_url + "?"))
        self.assertEqual(
            {name: sent[name] for name in ("scope", "response_type", "response_mode", "prompt", "client_id",
                                           "redirect_uri", "login_hint", "lti_message_hint")},
            {"scope": "openid", "response_type": "id_token", "response_mode": "form_post", "prompt": "none",
             "client_id": "client-1", "redirect_uri": "http://testserver/lti/launch/", "login_hint": "7",
             "lti_message_hint": "hint-9"},
        )
        self.assertTrue(sent["state"] and sent["nonce"])
        self.assertIn(lti.COOKIE, response.cookies)

    def test_a_post_starts_it_too_and_strangers_are_told(self):
        posted = self.client.post(reverse("integrations:lti_login"), {"iss": ISSUER, "login_hint": "7",
                                                                      "client_id": "client-1"})
        self.assertEqual(posted.status_code, 302)
        unknown = self.client.get(reverse("integrations:lti_login"), {"iss": "https://outro.example.com",
                                                                      "login_hint": "7"})
        self.assertEqual(unknown.status_code, 400)
        self.assertNotIn("X-Frame-Options", unknown)  # it shows inside the LMS
        self.assertEqual(self.client.get(reverse("integrations:lti_login")).status_code, 400)

    def test_an_lms_with_two_tools_here_must_say_which(self):
        other = Integration.objects.create(owner=make_user("x@example.com"), name="Outra")
        LtiPlatform.objects.create(integration=other, issuer=ISSUER, client_id="client-2",
                                   auth_login_url="https://evil.example.com/auth", jwks_url="https://evil.example.com/k")
        unnamed = self.client.get(reverse("integrations:lti_login"), {"iss": ISSUER, "login_hint": "7"})
        self.assertEqual(unnamed.status_code, 400)
        _response, sent = self.start()
        self.assertEqual(sent["client_id"], "client-1")


class LaunchTests(Base):
    def test_a_student_arrives_signed_in_as_themselves(self):
        response = self.launch(**{CLAIM + "custom": {"qqplay_live": self.live.uid}})
        self.assertEqual(response.status_code, 200)
        handed = self.handed(response)
        self.assertEqual((handed["user"]["name"], handed["user"]["moderator"]), ("Ana Souza", False))
        identity = sessions.load(handed["session"])
        self.assertEqual((identity.name, identity.groups, identity.is_moderator), ("Ana Souza", ["fis-101", "c-55"], False))
        self.assertEqual(identity.integration, self.integration)
        self.assertTrue(identity.external_id.startswith(f"lti:{self.platform.pk}:"))
        # The frame is the school's, chat and all, and they can write in it.
        self.assertContains(response, f'data-school="{self.integration.key}"')
        sent = self.api("live_chat", session=handed["session"], method="post", data={"body": "Oi"})
        self.assertEqual(sent.json()["message"]["author"], "Ana Souza")
        # The LMS signed the launch: its frame is not held to the list of sites.
        self.assertNotIn("Content-Security-Policy", response)
        self.assertNotIn("X-Frame-Options", response)
        self.platform.refresh_from_db()
        self.assertIsNotNone(self.platform.last_launch_at)
        # The same person the next time.
        self.launch(**{CLAIM + "custom": {"qqplay_live": self.live.uid}})
        self.assertEqual(ExternalIdentity.objects.count(), 1)

    def test_a_teacher_moderates_and_the_course_is_the_class(self):
        Live.objects.filter(pk=self.live.pk).update(visibility=Media.Visibility.PRIVATE, groups="FIS-101")
        teacher = self.handed(self.launch(**{CLAIM + "roles": [INSTRUCTOR], "sub": "prof-1", "name": "Prof. Rui",
                                            CLAIM + "custom": {"qqplay_live": self.live.uid}}))
        self.assertTrue(teacher["user"]["moderator"])
        self.assertTrue(self.api("live_state", session=teacher["session"]).json()["chat"]["can_moderate"])
        elsewhere = self.handed(self.launch(**{"sub": "outro-9", CLAIM + "context": {"id": "c-9", "label": "QUI-200"},
                                              CLAIM + "custom": {"qqplay_live": self.live.uid}}))
        self.assertEqual(self.api("live_state", session=elsewhere["session"]).status_code, 404)

    def test_no_cookie_is_needed_but_a_wrong_one_is_refused(self):
        custom = {CLAIM + "custom": {"qqplay_live": self.live.uid}}
        self.assertEqual(self.launch(forget_cookie=True, **custom).status_code, 200)
        _response, sent = self.start()
        self.client.cookies[lti.COOKIE] = "someone-elses"
        token = self.lms.token("client-1", sent["nonce"], **custom)
        refused = self.client.post(reverse("integrations:lti_launch"), {"id_token": token, "state": sent["state"]})
        self.assertEqual(refused.status_code, 400)

    def test_a_launch_works_once(self):
        _response, sent = self.start()
        token = self.lms.token("client-1", sent["nonce"], **{CLAIM + "custom": {"qqplay_live": self.live.uid}})
        body = {"id_token": token, "state": sent["state"]}
        self.assertEqual(self.client.post(reverse("integrations:lti_launch"), body).status_code, 200)
        self.assertEqual(self.client.post(reverse("integrations:lti_launch"), body).status_code, 400)

    def test_what_is_refused(self):
        url = reverse("integrations:lti_launch")
        _response, sent = self.start()

        def post(token, state=None):
            cache.clear()
            return self.client.post(url, {"id_token": token, "state": state or sent["state"]}).status_code

        good = self.lms.token("client-1", sent["nonce"])
        stranger = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.assertEqual(post(self.lms.token("client-1", sent["nonce"], key=stranger)), 400)  # not the LMS's key
        self.assertEqual(post(self.lms.token("another-tool", sent["nonce"])), 400)  # for another tool
        self.assertEqual(post(self.lms.token("client-1", "another-nonce")), 400)
        self.assertEqual(post(self.lms.token("client-1", sent["nonce"], iss="https://evil.example.com")), 400)
        self.assertEqual(post(self.lms.token("client-1", sent["nonce"], exp=int(time.time()) - 3600)), 400)
        self.assertEqual(post(self.lms.token("client-1", sent["nonce"], **{CLAIM + "version": "1.1"})), 400)
        self.assertEqual(post(self.lms.token("client-1", sent["nonce"], **{CLAIM + "message_type": "Other"})), 400)
        self.assertEqual(post(good, state="forged"), 400)
        self.assertEqual(post("not-a-token"), 400)
        self.assertEqual(self.client.get(url).status_code, 400)
        # A state from long ago.
        with mock.patch("time.time", return_value=time.time() + 3600):
            self.assertEqual(self.client.post(url, {"id_token": good, "state": sent["state"]}).status_code, 400)
        # A deployment that is not the registered one.
        LtiPlatform.objects.filter(pk=self.platform.pk).update(deployment_ids=["dep-9"])
        self.assertEqual(post(good), 400)
        LtiPlatform.objects.filter(pk=self.platform.pk).update(deployment_ids=["dep-1"], is_active=False)
        self.assertEqual(post(good), 400)

    def test_what_the_link_shows(self):
        media = Media.objects.create(owner=self.owner, title="Revisão", state=Media.State.READY,
                                     visibility=Media.Visibility.PRIVATE, file="media/x/r.mp4")
        video = self.launch(**{CLAIM + "custom": {"qqplay_video": media.uid, "qqplay_layout": "chat"}})
        self.assertContains(video, "data-media-embed")
        self.assertContains(video, "embed-comments")
        by_address = self.launch(**{CLAIM + "target_link_uri": f"http://testserver/lti/launch/?live={self.live.uid}&layout=player"})
        self.assertContains(by_address, "data-handoff")
        nothing_teacher = self.launch(**{CLAIM + "roles": [INSTRUCTOR]})
        self.assertContains(nothing_teacher, "?live=ID", status_code=400)
        nothing_student = self.launch()
        self.assertEqual(nothing_student.status_code, 400)
        self.assertNotContains(nothing_student, "?live=ID", status_code=400)

    def test_names_and_languages(self):
        handed = self.handed(self.launch(**{"name": None, "given_name": "Bia", "family_name": "Lima", "sub": "b-1",
                                           CLAIM + "custom": {"qqplay_live": self.live.uid},
                                           CLAIM + "launch_presentation": {"locale": "en-US"}}))
        self.assertEqual(handed["user"]["name"], "Bia Lima")
        english = self.launch(**{"sub": "b-2", CLAIM + "custom": {"qqplay_live": self.live.uid},
                                 CLAIM + "launch_presentation": {"locale": "en"}})
        self.assertContains(english, "Live chat")
        nameless = self.handed(self.launch(**{"name": None, "email": None, "sub": "b-3",
                                             CLAIM + "custom": {"qqplay_live": self.live.uid}}))
        self.assertTrue(nameless["user"]["name"])

    def test_the_lms_keys_are_fetched_once_and_again_when_they_change(self):
        custom = {CLAIM + "custom": {"qqplay_live": self.live.uid}}
        self.launch(**custom)
        self.launch(**custom)
        self.assertEqual(len([call for call in self.fetched if call[1] == self.platform.jwks_url]), 1)
        self.lms.kid = "lms-key-2"  # the LMS rotated its keys
        self.assertEqual(self.launch(**custom).status_code, 200)
        self.assertEqual(len([call for call in self.fetched if call[1] == self.platform.jwks_url]), 2)


class RoleTests(Base):
    """Who the chat treats as a teacher of the course the launch came from."""

    V2 = "http://purl.imsglobal.org/vocab/lis/v2/"

    def staff(self, *roles) -> bool:
        return lti.Launch(platform=None, claims={CLAIM + "roles": list(roles)}).is_staff

    def test_teachers_of_the_course(self):
        for role in (INSTRUCTOR, self.V2 + "membership/Instructor#TeachingAssistant",
                     self.V2 + "membership#ContentDeveloper", self.V2 + "membership#Administrator",
                     "Instructor", "urn:lti:role:ims/lis/Instructor"):
            self.assertTrue(self.staff(role), role)

    def test_students_and_teachers_of_other_courses(self):
        # Canvas says "Instructor" of the institution for whoever teaches any
        # course: in this one they are a student.
        self.assertFalse(self.staff(LEARNER, self.V2 + "institution/person#Instructor"))
        self.assertFalse(self.staff(LEARNER, self.V2 + "institution/person#Student",
                                    self.V2 + "membership#Mentor"))
        self.assertFalse(self.staff("Learner"))
        self.assertFalse(self.staff("https://example.com/roles/Instructor"))
        self.assertFalse(self.staff())
        self.assertFalse(lti.Launch(platform=None, claims={CLAIM + "roles": "Instructor"}).is_staff)

    def test_the_schools_administrators(self):
        self.assertTrue(self.staff(self.V2 + "system/person#Administrator"))
        self.assertTrue(self.staff(LEARNER, self.V2 + "institution/person#Administrator"))
        self.assertTrue(self.staff("urn:lti:instrole:ims/lis/Administrator"))


class DeepLinkingTests(Base):
    def picker(self, **claims):
        return self.launch(**{
            CLAIM + "message_type": lti.DEEP_LINKING, CLAIM + "roles": [INSTRUCTOR],
            lti.DL_CLAIM + "deep_linking_settings": {
                "deep_link_return_url": f"{ISSUER}/mod/lti/contentitem_return.php", "data": "opaque-123",
                "accept_types": ["ltiResourceLink"],
            }, **claims,
        })

    def test_the_teacher_picks_and_the_lms_gets_a_signed_answer(self):
        media = Media.objects.create(owner=self.owner, title="Revisão", state=Media.State.READY)
        Live.objects.create(owner=make_user("x@example.com"), title="De outro criador")
        page = self.picker()
        html = page.content.decode()
        self.assertIn(self.live.title, html)
        self.assertIn("Revisão", html)
        self.assertNotIn("De outro criador", html)
        token = re.search(r'name="token" value="([^"]+)"', html).group(1)
        answer = self.client.post(reverse("integrations:lti_deeplink"),
                                  {"token": token, "item": f"live:{self.live.uid}", "layout": "both"})
        self.assertEqual(answer.status_code, 200)
        form = answer.content.decode()
        self.assertIn(f'action="{ISSUER}/mod/lti/contentitem_return.php"', form)
        signed = re.search(r'name="JWT" value="([^"]+)"', form).group(1)
        keys = self.client.get(reverse("integrations:lti_jwks")).json()["keys"]
        key = jwt.PyJWK.from_dict(next(k for k in keys if k["kid"] == jwt.get_unverified_header(signed)["kid"])).key
        claims = jwt.decode(signed, key, algorithms=["RS256"], audience=ISSUER, issuer="client-1")
        item = claims[lti.DL_CLAIM + "content_items"][0]
        self.assertEqual((item["type"], item["title"], item["custom"]), (
            "ltiResourceLink", self.live.title, {"qqplay_live": self.live.uid, "qqplay_layout": "both"}))
        self.assertIn(f"live={self.live.uid}", item["url"])
        self.assertEqual((claims[CLAIM + "message_type"], claims[CLAIM + "deployment_id"],
                          claims[lti.DL_CLAIM + "data"]), ("LtiDeepLinkingResponse", "dep-1", "opaque-123"))
        # A video too.
        video = self.client.post(reverse("integrations:lti_deeplink"), {"token": token, "item": f"video:{media.uid}"})
        self.assertEqual(video.status_code, 200)

    def test_only_teachers_and_only_their_schools_content(self):
        self.assertEqual(self.picker(**{CLAIM + "roles": [LEARNER]}).status_code, 403)
        theirs = Live.objects.create(owner=make_user("x@example.com"), title="De outro")
        token = re.search(r'name="token" value="([^"]+)"', self.picker().content.decode()).group(1)
        url = reverse("integrations:lti_deeplink")
        self.assertEqual(self.client.post(url, {"token": token, "item": f"live:{theirs.uid}"}).status_code, 400)
        self.assertEqual(self.client.post(url, {"token": token, "item": "nothing"}).status_code, 400)
        self.assertEqual(self.client.post(url, {"token": "forged", "item": f"live:{self.live.uid}"}).status_code, 400)

    def test_the_tools_keys_are_published_and_stay(self):
        first = self.client.get(reverse("integrations:lti_jwks")).json()
        self.assertEqual(first, self.client.get(reverse("integrations:lti_jwks")).json())
        self.assertEqual((first["keys"][0]["kty"], first["keys"][0]["alg"]), ("RSA", "RS256"))
        self.assertNotIn("d", first["keys"][0])  # the public half only


class RegistrationTests(Base):
    CONFIG = "https://canvas.escola.example.com/api/lti/security/openid-configuration"

    def network(self, method, url, body=None, headers=None):
        self.fetched.append((method, url, body, headers))
        if url == self.CONFIG:
            return 200, json.dumps(self.configuration).encode()
        if url == self.configuration["registration_endpoint"] and method == "POST":
            return 200, json.dumps({"client_id": "new-client-9",
                                    lti.TOOL_CONFIGURATION: {"deployment_id": "dep-77"}}).encode()
        return 404, b"{}"

    def setUp(self):
        super().setUp()
        self.configuration = {
            "issuer": "https://canvas.escola.example.com",
            "authorization_endpoint": "https://canvas.escola.example.com/api/lti/authorize_redirect",
            "token_endpoint": "https://canvas.escola.example.com/login/oauth2/token",
            "jwks_uri": "https://canvas.escola.example.com/api/lti/security/jwks",
            "registration_endpoint": "https://canvas.escola.example.com/api/lti/registrations",
            "https://purl.imsglobal.org/spec/lti-platform-configuration": {"product_family_code": "canvas"},
        }
        self.url = reverse("integrations:lti_register", args=[lti.registration_token(self.integration)])

    def test_an_lms_registers_the_tool_by_itself(self):
        page = self.client.get(self.url, {"openid_configuration": self.CONFIG, "registration_token": "reg-tok"})
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "org.imsglobal.lti.close")
        platform = LtiPlatform.objects.get(client_id="new-client-9")
        self.assertEqual((platform.integration, platform.issuer, platform.name, platform.deployment_ids),
                         (self.integration, "https://canvas.escola.example.com", "canvas", ["dep-77"]))
        self.assertEqual(platform.auth_login_url, self.configuration["authorization_endpoint"])
        method, url, body, headers = self.fetched[-1]
        sent = json.loads(body)
        self.assertEqual((method, headers["Authorization"]), ("POST", "Bearer reg-tok"))
        self.assertEqual((sent["redirect_uris"], sent["initiate_login_uri"], sent["jwks_uri"]),
                         (["http://testserver/lti/launch/"], "http://testserver/lti/login/",
                          "http://testserver/lti/jwks/"))
        self.assertEqual(sent[lti.TOOL_CONFIGURATION]["messages"][0]["type"], "LtiDeepLinkingRequest")

    def test_the_domain_carries_the_port_the_launch_has(self):
        # Moodle compares the tool's domain with the launch address, port and
        # all, and refuses the registration when they differ.
        self.client.get(self.url, {"openid_configuration": self.CONFIG}, SERVER_PORT="8443")
        tool = json.loads(self.fetched[-1][2])[lti.TOOL_CONFIGURATION]
        self.assertEqual(tool["domain"], "testserver:8443")
        self.assertEqual(tool["target_link_uri"], "http://testserver:8443/lti/launch/")

    def test_the_reason_an_lms_gives_is_shown(self):
        def refusing(method, url, body=None, headers=None):
            if method == "POST":
                return 400, b"domain_targetlinkuri_mismatch"
            return self.network(method, url, body, headers)

        with mock.patch.object(lti, "_fetch", refusing):
            page = self.client.get(self.url, {"openid_configuration": self.CONFIG})
        self.assertContains(page, "domain_targetlinkuri_mismatch", status_code=400)
        # A page of HTML is not a reason worth showing.
        with mock.patch.object(lti, "_fetch", lambda *a, **k: (502, b"<html><body>Bad gateway</body></html>")):
            page = self.client.get(self.url, {"openid_configuration": self.CONFIG})
        self.assertNotContains(page, "Bad gateway", status_code=400)

    def test_what_is_refused(self):
        self.assertEqual(self.client.get(self.url).status_code, 400)  # opened by hand
        bad = reverse("integrations:lti_register", args=["forged"])
        self.assertEqual(self.client.get(bad, {"openid_configuration": self.CONFIG}).status_code, 400)
        # A configuration that sends the registration to another site.
        self.configuration["registration_endpoint"] = "https://evil.example.com/register"
        self.assertEqual(self.client.get(self.url, {"openid_configuration": self.CONFIG}).status_code, 400)
        self.assertFalse(LtiPlatform.objects.filter(client_id="new-client-9").exists())

    def test_a_registration_cannot_be_taken_over(self):
        other = Integration.objects.create(owner=make_user("x@example.com"), name="Outra")
        LtiPlatform.objects.create(integration=other, issuer="https://canvas.escola.example.com",
                                   client_id="new-client-9", auth_login_url="https://a/", jwks_url="https://b/")
        page = self.client.get(self.url, {"openid_configuration": self.CONFIG})
        self.assertEqual(page.status_code, 400)
        self.assertEqual(LtiPlatform.objects.get(client_id="new-client-9").integration, other)


class StudioTests(Base):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.owner)
        self.url = reverse("integrations:lms", args=[self.integration.pk])
        self.fields = {"name": "Canvas", "issuer": "https://canvas.instructure.com", "client_id": "10000000000001",
                       "deployment_id": "1:abc", "auth_login_url": "https://sso.canvaslms.com/api/lti/authorize_redirect",
                       "jwks_url": "https://sso.canvaslms.com/api/lti/security/jwks", "auth_token_url": ""}

    def test_registering_by_hand_and_taking_off(self):
        self.client.post(self.url, self.fields)
        platform = LtiPlatform.objects.get(client_id="10000000000001")
        self.assertEqual((platform.integration, platform.deployment_ids), (self.integration, ["1:abc"]))
        self.client.post(self.url, self.fields)  # twice: refused, not doubled
        self.assertEqual(LtiPlatform.objects.filter(client_id="10000000000001").count(), 1)
        self.client.post(self.url, {"action": "remove", "id": platform.pk})
        self.assertFalse(LtiPlatform.objects.filter(pk=platform.pk).exists())

    def test_the_page_gives_what_the_lms_asks_for(self):
        html = self.client.get(reverse("integrations:detail", args=[self.integration.pk])).content.decode()
        for needed in ("/lti/login/", "/lti/launch/", "/lti/jwks/", "/lti/register/", ISSUER):
            self.assertIn(needed, html)

    def test_only_the_owner(self):
        self.client.force_login(make_user("nosy@example.com"))
        self.assertEqual(self.client.post(self.url, self.fields).status_code, 404)
        self.assertFalse(LtiPlatform.objects.filter(client_id="10000000000001").exists())
