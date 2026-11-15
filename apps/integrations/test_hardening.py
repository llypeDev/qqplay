"""What the review of the integration found, kept from coming back: a whole
class signing in at once, sessions kept to their creator's content, bad
input, erasure, sites and apps, framing, language and the studio's
diagnostics."""
import hashlib
import hmac
import itertools
import json
import time
from datetime import timedelta
from unittest import mock

from django.conf import settings
from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone, translation

from apps.core.embedding import language_from
from apps.live.models import ChatMessage, Live, LiveAttendance
from apps.media.models import Comment, Media
from apps.users.models import User

from . import diagnostics, identities, net, sessions, tokens, webhooks
from .models import ExternalIdentity, Integration, WebhookDelivery, normalize_origin
from .tests import SITE, IntegrationTestCase, make_user
from .views import client_address


class Base(IntegrationTestCase):
    def post_session(self, body, **extra):
        return self.client.post(reverse("integrations:session"), json.dumps(body), content_type="application/json",
                                **extra)

    def headers(self, session):
        return {"Authorization": f"Bearer {session}"}


class ClassStartTests(Base):
    """Everyone arrives in the same minute, often from the same address."""

    def test_a_whole_class_gets_in_through_one_address(self):
        codes = [self.sign_in(sub=f"s{n}", name=f"Aluno {n}").status_code for n in range(100)]
        self.assertEqual(codes.count(200), 100)

    def test_refused_sign_ins_are_what_slows_an_address_down(self):
        bad = self.token(exp=int(time.time()) - 3600)
        codes = [self.post_session({"token": bad, "origin": SITE}).status_code for _ in range(45)]
        self.assertEqual((codes.count(401), codes.count(429)), (40, 5))
        slowed = self.sign_in()
        self.assertEqual((slowed.status_code, slowed["Retry-After"]), (429, "5"))

    @override_settings(TRUSTED_PROXY_COUNT=1)
    def test_behind_a_proxy_each_visitor_has_their_own_address(self):
        bad = self.token(exp=int(time.time()) - 3600)
        for _ in range(45):
            self.post_session({"token": bad, "origin": SITE}, HTTP_X_FORWARDED_FOR="203.0.113.9")
        self.assertEqual(self.post_session({"token": self.token(), "origin": SITE},
                                           HTTP_X_FORWARDED_FOR="203.0.113.9").status_code, 429)
        self.assertEqual(self.post_session({"token": self.token(), "origin": SITE},
                                           HTTP_X_FORWARDED_FOR="198.51.100.7").status_code, 200)

    def test_the_address_is_read_as_the_proxies_in_front_allow(self):
        request = mock.Mock(META={"REMOTE_ADDR": "172.18.0.9", "HTTP_X_FORWARDED_FOR": "203.0.113.9, 10.0.0.2"})
        with override_settings(TRUSTED_PROXY_COUNT=0):  # no proxy: the header is anyone's to write
            self.assertEqual(client_address(request), "172.18.0.9")
        with override_settings(TRUSTED_PROXY_COUNT=1):
            self.assertEqual(client_address(request), "10.0.0.2")
        with override_settings(TRUSTED_PROXY_COUNT=2):
            self.assertEqual(client_address(request), "203.0.113.9")


class ConfinementTests(Base):
    """A site's session speaks on its creator's content only."""

    def setUp(self):
        super().setUp()
        self.other = make_user("outro@example.com")
        self.student = self.session()

    def test_another_creators_live_is_read_only(self):
        theirs = Live.objects.create(owner=self.other, title="Deles", status=Live.Status.LIVE, publishing=True,
                                     visibility=Media.Visibility.PUBLIC)
        url = reverse("integrations:live_chat", args=[theirs.uid])
        self.assertEqual(self.client.post(url, {"body": "spam"}, headers=self.headers(self.student)).status_code, 403)
        state = self.client.get(reverse("integrations:live_state", args=[theirs.uid]), {"client": "c1"},
                                headers=self.headers(self.student))
        self.assertEqual(state.status_code, 200)
        self.assertFalse(state.json()["chat"]["can_post"])
        self.assertFalse(ChatMessage.objects.filter(live=theirs).exists())
        self.assertFalse(LiveAttendance.objects.filter(live=theirs).exists())
        # Their own creator's live is theirs to talk in.
        self.assertEqual(self.api("live_chat", session=self.student, method="post", data={"body": "Oi"}).status_code,
                         201)

    def test_another_creators_video_takes_no_comments(self):
        theirs = Media.objects.create(owner=self.other, title="V", state=Media.State.READY,
                                      visibility=Media.Visibility.PUBLIC, file="media/x/v.mp4")
        url = reverse("integrations:media_comments", args=[theirs.uid])
        sent = self.client.post(url, json.dumps({"body": "x"}), content_type="application/json",
                                headers=self.headers(self.student))
        self.assertEqual(sent.status_code, 403)
        self.assertFalse(Comment.objects.exists())


class BadInputTests(Base):
    def setUp(self):
        super().setUp()
        self.media = Media.objects.create(owner=self.owner, title="V", state=Media.State.READY,
                                          visibility=Media.Visibility.PUBLIC, file="media/x/v.mp4")
        self.url = reverse("integrations:media_comments", args=[self.media.uid])
        self.student = self.session()

    def comment(self, raw):
        cache.clear()  # past the pause between one person's comments
        return self.client.post(self.url, raw, content_type="application/json", headers=self.headers(self.student))

    def test_a_comment_at_no_moment_at_all(self):
        sent = self.comment('{"body": "x", "timestamp": NaN}')
        self.assertEqual(sent.status_code, 201)
        self.assertIsNone(json.loads(sent.content)["comment"]["timestamp"])  # and the answer is real JSON
        self.assertIsNone(Comment.objects.get().timestamp)
        self.assertEqual(self.comment('{"body": "y", "timestamp": Infinity}').status_code, 201)
        self.assertEqual(self.comment('["not", "an", "object"]').status_code, 400)
        self.assertEqual(self.comment('{"body": "z", "parent": "not-an-id"}').status_code, 400)

    def test_tokens_that_never_expire_are_refused(self):
        for value in (float("nan"), float("inf"), True, "tomorrow"):
            with self.assertRaises(tokens.TokenError):
                tokens.verify(self.token(exp=value))

    def test_the_server_api_answers_400_not_500(self):
        now = int(time.time())
        api_token = tokens.sign({"scope": "api", "exp": now + 300}, self.integration)

        def post(data):
            return self.client.post(reverse("integrations:api_lives"), json.dumps(data),
                                    content_type="application/json", headers={"Authorization": f"Bearer {api_token}"})

        self.assertEqual(post({"title": "X", "visibility": ["public"]}).status_code, 400)
        self.assertEqual(post({"title": "X", "scheduled_for": "2026-02-30T10:00:00-03:00"}).status_code, 400)
        self.assertEqual(post({"title": "X", "classes": [f"turma-{n:03d}-{'x' * 40}" for n in range(40)]}).status_code,
                         400)
        self.assertEqual(post({"title": "X", "classes": {"a": 1}}).status_code, 201)

    def test_student_ids_with_slashes_reach_the_api(self):
        self.session(sub="2026/0042", name="Bia")
        api_token = tokens.sign({"scope": "api", "exp": int(time.time()) + 300}, self.integration)
        headers = {"Authorization": f"Bearer {api_token}"}
        block = self.client.post(reverse("integrations:api_student_block", args=["2026/0042"]), headers=headers)
        self.assertEqual(block.json(), {"student": "2026/0042", "blocked": True})
        self.assertEqual(self.client.post(reverse("integrations:api_student_unblock", args=["2026/0042"]),
                                          headers=headers).status_code, 200)
        self.assertEqual(self.client.delete(reverse("integrations:api_student", args=["2026/0042"]),
                                            headers=headers).status_code, 200)
        self.assertIsNone(identities.find(self.integration, "2026/0042"))


class PeopleTests(Base):
    def test_an_identity_deleted_alone_comes_back_on_the_next_sign_in(self):
        self.session()
        user = ExternalIdentity.objects.get().user
        ExternalIdentity.objects.all().delete()  # as the Admin allows
        self.assertEqual(self.sign_in().status_code, 200)
        self.assertEqual(ExternalIdentity.objects.get().user, user)

    def test_erasing_someone_reaches_the_chats_already_open(self):
        student = self.session()
        self.api("live_chat", session=student, method="post", data={"body": "Meu nome completo"})
        before = self.api("live_state").json()["chat"]  # the shared copy of the chat is in the cache now
        self.assertEqual(before["messages"][0]["author"], "Ana Lima")
        identities.erase(ExternalIdentity.objects.get(), keep_messages=True)
        after = self.api("live_state").json()["chat"]
        with translation.override(settings.LANGUAGE_CODE):
            self.assertEqual(after["messages"][0]["author"], translation.gettext("Removed person"))
        self.assertGreater(after["version"], before["version"])

    def test_erasing_everything_takes_their_messages_out_at_once(self):
        student = self.session()
        self.api("live_chat", session=student, method="post", data={"body": "Apague isto"})
        self.assertEqual(len(self.api("live_state").json()["chat"]["messages"]), 1)
        identities.erase(ExternalIdentity.objects.get(), keep_messages=False)
        self.assertEqual(self.api("live_state").json()["chat"]["messages"], [])

    def test_other_peoples_replies_are_not_erased_with_them(self):
        self.session()
        student = ExternalIdentity.objects.get()
        media = Media.objects.create(owner=self.owner, title="V", state=Media.State.READY)
        question = Comment.objects.create(media=media, author=student.user, body="Dúvida")
        answer = Comment.objects.create(media=media, author=self.owner, parent=question, body="Resposta do professor")
        identities.erase(student, keep_messages=False)
        answer.refresh_from_db()
        self.assertIsNone(answer.parent)
        self.assertEqual(list(media.comments.values_list("body", flat=True)), ["Resposta do professor"])

    def test_deleting_the_integration_forgets_its_people(self):
        student = self.session()
        message = self.api("live_chat", session=student, method="post", data={"body": "Oi"}).json()["message"]
        user = ExternalIdentity.objects.get().user
        self.client.force_login(self.owner)
        self.client.post(reverse("integrations:delete", args=[self.integration.pk]))
        self.assertFalse(Integration.objects.exists())
        user.refresh_from_db()
        self.assertFalse(user.is_active)
        self.assertNotEqual(user.display_name, "Ana Lima")
        self.assertTrue(user.email.startswith("removed-"))
        self.assertEqual(ChatMessage.objects.get(pk=message["id"]).author, user)  # what they wrote stays

    def test_the_studio_finds_people_beyond_the_first_page(self):
        for n in range(3):
            self.session(sub=f"m{n}", name=f"Mariana {n}")
        self.session(sub="z9", name="Zeca")
        self.client.force_login(self.owner)
        page = self.client.get(reverse("integrations:detail", args=[self.integration.pk]), {"q": "zec"})
        self.assertEqual([person.name for person in page.context["people"]], ["Zeca"])
        by_id = self.client.get(reverse("integrations:detail", args=[self.integration.pk]), {"q": "m1"})
        self.assertEqual([person.name for person in by_id.context["people"]], ["Mariana 1"])


@override_settings(CELERY_TASK_ALWAYS_EAGER=True)
class WebhookTests(Base):
    def setUp(self):
        super().setUp()
        Integration.objects.filter(pk=self.integration.pk).update(webhook_url="https://escola.example.com/hook")
        self.integration.refresh_from_db()
        self.sent = []

    def answer(self, status=200):
        def fake(method, url, body=None, headers=None, **kwargs):
            self.sent.append({"body": body, "headers": headers})
            return net.Answer(status, b"ok")
        return fake

    def sig(self, secret, body):
        return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()

    def test_during_the_grace_a_server_on_the_old_secret_can_still_check(self):
        old = self.integration.secret
        self.integration.rotate_secret(grace_hours=24)
        live = Live.objects.create(owner=self.owner, title="L")
        with mock.patch.object(net, "request", side_effect=self.answer()), self.captureOnCommitCallbacks(execute=True):
            webhooks.notify(live, "live.started")
        headers, body = self.sent[0]["headers"], self.sent[0]["body"]
        self.assertEqual(headers["X-QQplay-Signature"], self.sig(self.integration.secret, body))
        self.assertEqual(headers["X-QQplay-Signature-Previous"], self.sig(old, body))
        # Once the grace is over, only the current one.
        Integration.objects.filter(pk=self.integration.pk).update(previous_secret_until=timezone.now())
        with mock.patch.object(net, "request", side_effect=self.answer()), self.captureOnCommitCallbacks(execute=True):
            webhooks.notify(live, "live.ended")
        self.assertNotIn("X-QQplay-Signature-Previous", self.sent[1]["headers"])

    def test_a_failed_delivery_can_be_sent_again_from_the_studio(self):
        live = Live.objects.create(owner=self.owner, title="L")
        with mock.patch.object(net, "request", side_effect=self.answer(500)), \
                self.captureOnCommitCallbacks(execute=True):
            webhooks.notify(live, "live.started")
        delivery = WebhookDelivery.objects.get()
        self.assertFalse(delivery.ok)
        self.client.force_login(self.owner)
        with mock.patch.object(net, "request", side_effect=self.answer(200)):
            response = self.client.post(reverse("integrations:webhook_resend",
                                                args=[self.integration.pk, delivery.pk]))
        self.assertEqual(response.status_code, 302)
        delivery.refresh_from_db()
        self.assertTrue(delivery.ok)
        # Not someone else's.
        self.client.force_login(make_user("nosy@example.com"))
        self.assertEqual(self.client.post(reverse("integrations:webhook_resend",
                                                  args=[self.integration.pk, delivery.pk])).status_code, 404)

    def test_the_database_keeps_one_delivery_per_event(self):
        live = Live.objects.create(owner=self.owner, title="L")
        fields = {"integration": self.integration, "event": "live.started", "subject": live.uid,
                  "url": self.integration.webhook_url}
        WebhookDelivery.objects.create(**fields)
        with self.assertRaises(IntegrityError), transaction.atomic():
            WebhookDelivery.objects.create(**fields)
        with mock.patch.object(net, "request", side_effect=self.answer()), self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(webhooks.notify(live, "live.started"), [])
        # Tests of the address are not events: as many as wanted.
        WebhookDelivery.objects.create(integration=self.integration, event="test", url="https://x.example.com/")
        WebhookDelivery.objects.create(integration=self.integration, event="test", url="https://x.example.com/")


class SitesTests(Base):
    def test_a_wildcard_takes_the_subdomains_at_sign_in_too(self):
        Integration.objects.filter(pk=self.integration.pk).update(origins="https://*.escola.com.br")
        self.assertEqual(self.sign_in(origin="https://aulas.escola.com.br").status_code, 200)
        self.assertEqual(self.sign_in(origin="https://a.b.escola.com.br").status_code, 200)
        for refused in ("https://escola.com.br", "https://escola.com.br.evil.example", "http://aulas.escola.com.br",
                        "https://outraescola.com.br"):
            self.assertEqual(self.sign_in(origin=refused).status_code, 403, refused)

    def test_an_app_is_a_site_like_another(self):
        self.assertEqual(normalize_origin("capacitor://localhost"), "capacitor://localhost")
        self.assertEqual(normalize_origin("ionic://localhost"), "ionic://localhost")
        for bad in ("file:///android_asset/x.html", "javascript://localhost", "data://x"):
            self.assertEqual(normalize_origin(bad), "", bad)
        Integration.objects.filter(pk=self.integration.pk).update(origins=f"{SITE}\ncapacitor://localhost")
        self.integration.refresh_from_db()
        self.assertEqual(self.sign_in(origin="capacitor://localhost").status_code, 200)
        self.assertEqual(self.integration.frame_ancestors(), f"frame-ancestors 'self' {SITE} capacitor://localhost")

    def test_opening_the_address_by_itself_is_a_choice(self):
        body = {"token": self.token(), "origin": "", "direct": True}
        refused = self.post_session(body)
        self.assertEqual((refused.status_code, refused.json()["code"]), (403, "origin"))
        Integration.objects.filter(pk=self.integration.pk).update(allow_direct=True)
        self.assertEqual(self.post_session(body).status_code, 200)
        # In a frame of a page that does not say where it is: still refused.
        cache.clear()
        self.assertEqual(self.post_session({"token": self.token(), "origin": ""}).status_code, 403)
        self.assertEqual(self.post_session({"token": self.token(), "origin": "null"}).status_code, 403)
        self.assertEqual(self.post_session({"token": self.token(), "origin": "https://evil.example",
                                            "direct": True}).status_code, 403)

    def test_the_studio_saves_the_choice(self):
        self.client.force_login(self.owner)
        self.client.post(reverse("integrations:detail", args=[self.integration.pk]),
                         {"name": "Escola", "origins": SITE, "allow_direct": "1"})
        self.integration.refresh_from_db()
        self.assertTrue(self.integration.allow_direct)

    def test_no_sites_listed_means_anywhere(self):
        Integration.objects.filter(pk=self.integration.pk).update(origins="")
        self.assertEqual(self.sign_in(origin="null").status_code, 200)
        page = self.client.get(reverse("live:embed", args=[self.live.uid]), {"school": self.integration.key})
        self.assertNotIn("Content-Security-Policy", page)


class SessionEndTests(Base):
    def test_a_session_from_the_old_secret_ends_with_the_grace(self):
        old_token = self.token()
        self.integration.rotate_secret(grace_hours=24)
        session = self.post_session({"token": old_token, "origin": SITE}).json()["session"]
        self.assertIsNotNone(sessions.load(session))
        Integration.objects.filter(pk=self.integration.pk).update(previous_secret_until=timezone.now())
        self.assertIsNone(sessions.load(session))
        # One made with the new secret goes on.
        self.assertIsNotNone(sessions.load(self.session()))

    def test_unblocking_does_not_bring_old_sessions_back(self):
        session = self.session()
        identity = ExternalIdentity.objects.get()
        with mock.patch("apps.integrations.identities.timezone.now", return_value=timezone.now() + timedelta(seconds=5)):
            identities.block(identity, True)
        identities.block(identity, False)
        self.assertIsNone(sessions.load(session))

    def test_turning_it_off_and_on_does_not_either(self):
        session = self.session()
        self.client.force_login(self.owner)
        url = reverse("integrations:detail", args=[self.integration.pk])
        with mock.patch("apps.integrations.views.timezone.now", return_value=timezone.now() + timedelta(seconds=5)):
            self.client.post(url, {"is_active": "0"})
        self.client.post(url, {"is_active": "1"})
        self.assertTrue(Integration.objects.get().is_active)
        self.assertIsNone(sessions.load(session))


class FramingTests(Base):
    def test_no_made_up_x_frame_options(self):
        for url, params in (
            (reverse("live:embed", args=[self.live.uid]), {}),
            (reverse("live:embed", args=[self.live.uid]), {"school": self.integration.key}),
        ):
            response = self.client.get(url, params)
            self.assertNotIn("X-Frame-Options", response)
            self.assertEqual(response["Referrer-Policy"], "same-origin")

    def test_what_is_not_there_still_shows_in_the_frame(self):
        for url in (reverse("live:embed", args=["nope00000000"]), reverse("core:embed", args=["nope00000000"])):
            response = self.client.get(url)
            self.assertEqual(response.status_code, 404)
            self.assertNotIn("X-Frame-Options", response)
            self.assertContains(response, "qqplay:loaded", status_code=404)
        private = Media.objects.create(owner=self.owner, title="Segredo de Estado", state=Media.State.READY,
                                       visibility=Media.Visibility.PRIVATE, file="media/x/s.mp4")
        response = self.client.get(reverse("core:embed", args=[private.uid]))
        self.assertEqual(response.status_code, 404)
        self.assertNotContains(response, "Segredo de Estado", status_code=404)

    def test_a_private_lives_name_waits_for_the_sign_in(self):
        Live.objects.filter(pk=self.live.pk).update(visibility=Media.Visibility.PRIVATE, title="Prova surpresa")
        waiting = self.client.get(reverse("live:embed", args=[self.live.uid]), {"school": self.integration.key})
        self.assertNotContains(waiting, "Prova surpresa")
        closed = self.client.get(reverse("live:embed", args=[self.live.uid]))
        self.assertNotContains(closed, "Prova surpresa", status_code=404)
        private = Media.objects.create(owner=self.owner, title="Gabarito", state=Media.State.READY,
                                       visibility=Media.Visibility.PRIVATE, file="media/x/g.mp4")
        video = self.client.get(reverse("core:embed", args=[private.uid]), {"school": self.integration.key})
        self.assertNotContains(video, "Gabarito")

    def test_the_script(self):
        body = self.client.get(reverse("integrations:sdk")).content.decode()
        for needed in ("MutationObserver", "data-school", "qqplay:status", 'code: "frame"', "retryToken"):
            self.assertIn(needed, body)
        # A live loads at once; lazily only when the site asks for it.
        self.assertIn('if (this.lazy) frame.setAttribute("loading", "lazy");', body)


class AnyOriginTests(Base):
    """The frame's API from a page with no origin of its own (a sandboxed
    frame) or from a school's own page or app."""

    def test_the_preflight_and_the_answers(self):
        for url in (reverse("integrations:session"), reverse("integrations:live_state", args=[self.live.uid]),
                    reverse("integrations:live_chat", args=[self.live.uid])):
            preflight = self.client.options(url, headers={"Origin": "null",
                                                          "Access-Control-Request-Headers": "authorization"})
            self.assertEqual(preflight.status_code, 204, url)
            self.assertEqual(preflight["Access-Control-Allow-Origin"], "*")
            self.assertIn("Authorization", preflight["Access-Control-Allow-Headers"])
        signed = self.sign_in()
        self.assertEqual(signed["Access-Control-Allow-Origin"], "*")
        state = self.api("live_state", session=signed.json()["session"])
        self.assertEqual(state["Access-Control-Allow-Origin"], "*")
        self.assertIn("X-QQplay-Session", state["Access-Control-Expose-Headers"])

    def test_cookies_still_mean_nothing_there(self):
        self.client.force_login(self.owner)
        state = self.client.get(reverse("integrations:live_state", args=[self.live.uid]),
                                headers={"Origin": "https://evil.example"})
        self.assertFalse(state.json()["chat"]["can_post"])
        self.assertNotIn("control", state.json())
        # The portal's own pages are not opened up.
        self.assertNotIn("Access-Control-Allow-Origin", self.client.get(reverse("live:state", args=[self.live.uid])))


class LanguageTests(Base):
    def test_the_codes_a_frame_may_ask_for(self):
        self.assertEqual(language_from("en"), "en")
        self.assertEqual(language_from("EN-us"), "en")
        self.assertEqual(language_from("pt"), "pt-br")
        self.assertEqual(language_from("pt_BR"), "pt-br")
        for nothing in ("", None, "xx", "<script>"):
            self.assertIsNone(language_from(nothing))

    def test_the_frame_and_its_texts_come_in_that_language(self):
        url = reverse("live:embed", args=[self.live.uid])
        self.assertContains(self.client.get(url, {"layout": "both"}), "Chat ao vivo")
        english = self.client.get(url, {"layout": "both", "lang": "en"})
        self.assertContains(english, "Live chat")
        self.assertContains(english, 'data-lang="en"')
        self.assertContains(english, "/jsi18n/?lang=en")
        self.assertNotIn("Entre para reagir.", self.client.get("/jsi18n/", {"lang": "en"}).content.decode())
        self.assertIn("Entre para reagir.", self.client.get("/jsi18n/").content.decode())

    def test_the_frames_requests_are_answered_in_it(self):
        session = self.session()
        url = reverse("integrations:live_chat", args=[self.live.uid])
        portuguese = self.client.post(url, {"body": ""}, headers=self.headers(session))
        self.assertEqual(portuguese.json()["detail"], "Escreva uma mensagem primeiro.")
        english = self.client.post(url, {"body": ""}, headers={**self.headers(session), "X-QQplay-Lang": "en"})
        self.assertEqual(english.json()["detail"], "Write a message first.")


class DiagnosticsTests(Base):
    def setUp(self):
        super().setUp()
        self.detail = reverse("integrations:detail", args=[self.integration.pk])

    def test_refused_sign_ins_are_kept_for_the_owner(self):
        self.post_session({"token": self.token(exp=int(time.time()) - 3600), "origin": SITE})
        self.sign_in(origin="https://outro.example.com")
        found = diagnostics.recent(self.integration)
        self.assertEqual([item["code"] for item in found], ["origin", "token"])
        self.assertEqual(found[0]["origin"], "https://outro.example.com")
        self.client.force_login(self.owner)
        page = self.client.get(self.detail).content.decode()
        self.assertIn("https://outro.example.com", page)
        self.assertNotIn(self.integration.secret[:12], page.replace(f'value="{self.integration.secret}"', ""))

    def test_checking_a_token_says_what_is_wrong_with_it(self):
        self.client.force_login(self.owner)
        check = reverse("integrations:check_token", args=[self.integration.pk])

        def result(token):
            self.assertEqual(self.client.post(check, {"token": token}).status_code, 302)
            return self.client.get(self.detail).context["checked"]

        good = result(self.token(moderator=True, groups=["3a"]))
        self.assertTrue(good["ok"])
        self.assertEqual((good["person"]["name"], good["person"]["moderator"], good["person"]["groups"]),
                         ("Ana Lima", True, ["3a"]))
        self.assertIn('"sub": "42"', good["claims_text"])
        self.assertFalse(ExternalIdentity.objects.exists())  # nobody was signed in
        expired = result(self.token(exp=int(time.time()) - 3600))
        self.assertFalse(expired["ok"])
        self.assertTrue(expired["error"])
        other = Integration.objects.create(owner=self.owner, name="Outra")
        self.assertEqual(result(self.token(integration=other))["error"], "other")
        api = result(tokens.sign({"scope": "api", "exp": int(time.time()) + 300}, self.integration))
        self.assertEqual((api["ok"], api["kind"]), (True, "api"))
        self.assertFalse(result("garbage")["ok"])
        # Shown once.
        self.assertIsNone(self.client.get(self.detail).context["checked"])

    def test_only_the_owner_checks(self):
        self.client.force_login(make_user("nosy@example.com"))
        self.assertEqual(self.client.post(reverse("integrations:check_token", args=[self.integration.pk]),
                                          {"token": "x"}).status_code, 404)


class _Response:
    status = 200

    def __init__(self, chunks):
        self.chunks = chunks

    def getheader(self, name):
        return None

    def read1(self, size):
        return next(self.chunks, b"")


class _Connection:
    chunks = iter(())
    path = ""

    def __init__(self, *args, **kwargs):
        pass

    def request(self, method, path, body=None, headers=None):
        type(self).path = path

    def getresponse(self):
        return _Response(type(self).chunks)

    def close(self):
        pass


class NetTests(Base):
    def run_request(self, url, chunks, clock=None, **kwargs):
        _Connection.chunks = iter(chunks)
        patches = [
            mock.patch.object(net, "public_address", return_value="93.184.216.34"),
            mock.patch.object(net, "_PinnedHTTPSConnection", _Connection),
        ]
        if clock is not None:
            patches.append(mock.patch.object(net.time, "monotonic", side_effect=clock))
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        return net.request("GET", url, **kwargs)

    def test_an_answer_is_read_piece_by_piece(self):
        answer = self.run_request("https://cdn.example.com/a.jpg", [b"abc", b"def"])
        self.assertEqual((answer.status, answer.body), (200, b"abcdef"))

    def test_a_trickle_is_cut_off(self):
        with self.assertRaisesMessage(net.NetError, "too long"):
            self.run_request("https://cdn.example.com/a.jpg", itertools.repeat(b"x"), clock=itertools.count(0, 10))

    def test_too_much_is_refused(self):
        with self.assertRaisesMessage(net.NetError, "too large"):
            self.run_request("https://cdn.example.com/a.jpg", itertools.repeat(b"x" * 1024), max_bytes=4096)

    def test_odd_addresses_are_errors_of_the_address(self):
        with self.assertRaises(net.NetError):
            self.run_request("https://cdn.example.com:porta/a.jpg", [])
        self.run_request("https://cdn.example.com/fotos/joão.jpg?v=é", [b"ok"])
        self.assertEqual(_Connection.path, "/fotos/jo%C3%A3o.jpg?v=%C3%A9")
