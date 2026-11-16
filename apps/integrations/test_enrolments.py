"""Schools that sign no tokens: the list of enrolments, signing in with a
code by e-mail, the platform's word for who is there, the purchases the
sales platforms post, the class link and the studio's handling of it all."""
import base64
import hashlib
import hmac
import json
import re
import time
from unittest import mock

from django.core import mail
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.urls import reverse

from apps.live.models import Live
from apps.media.models import Media

from . import enrolments, hooks, sessions, tokens
from .models import Enrolment, ExternalIdentity, Integration
from .tests import SITE, IntegrationTestCase, make_user


@override_settings(CELERY_TASK_ALWAYS_EAGER=True)
class Base(IntegrationTestCase):
    def setUp(self):
        super().setUp()
        Integration.objects.filter(pk=self.integration.pk).update(email_login=True)
        self.integration.refresh_from_db()
        self.ana, _created = enrolments.upsert(self.integration, "Ana@Escola.com", name="Ana Souza", classes="turma-a")

    def post(self, name, body, **headers):
        return self.client.post(reverse(f"integrations:{name}"), json.dumps(body), content_type="application/json",
                                headers=headers)

    def ask(self, email="ana@escola.com", **headers):
        return self.post("login_start", {"school": self.integration.key, "email": email}, **headers)

    def code(self):
        return re.search(r"\b(\d{6})\b", mail.outbox[-1].body).group(1)

    def enter(self, email="ana@escola.com", code=None):
        return self.post("login_verify", {"school": self.integration.key, "email": email, "code": code or self.code()})

    def session(self, email="ana@escola.com"):
        self.assertEqual(self.ask(email).status_code, 200)
        response = self.enter(email)
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()["session"]


class ListTests(Base):
    def test_emails_are_kept_in_one_form(self):
        self.assertEqual(self.ana.email, "ana@escola.com")
        again, created = enrolments.upsert(self.integration, " ANA@escola.com ", classes=["Turma-B"])
        self.assertFalse(created)
        self.assertEqual((again.pk, again.groups, again.name), (self.ana.pk, ["turma-b"], "Ana Souza"))
        self.assertEqual(enrolments.upsert(self.integration, "not an e-mail"), (None, False))

    def test_a_spreadsheet_in_portuguese_with_semicolons(self):
        text = "﻿Nome;E-mail;Turmas;Professor\nBia Lima;bia@escola.com;3a|3b;\nRui;rui@escola.com;3a;sim\n;sem-arroba;;\n"
        result = enrolments.import_rows(self.integration, text)
        self.assertEqual(result, {"added": 2, "updated": 0, "bad": [3]})
        bia = enrolments.find(self.integration, "bia@escola.com")
        self.assertEqual((bia.name, bia.groups, bia.is_moderator), ("Bia Lima", ["3a", "3b"], False))
        self.assertTrue(enrolments.find(self.integration, "rui@escola.com").is_moderator)

    def test_rows_without_a_header_and_people_already_there(self):
        result = enrolments.import_rows(self.integration, "ana@escola.com, Ana S. Lima, turma-z\nleo@escola.com, Leo\n")
        self.assertEqual(result, {"added": 1, "updated": 1, "bad": []})
        self.ana.refresh_from_db()
        self.assertEqual((self.ana.name, self.ana.groups), ("Ana S. Lima", ["turma-z"]))


class CodeTests(Base):
    def test_an_enrolled_person_signs_in_with_the_code_from_their_email(self):
        asked = self.ask()
        self.assertEqual(asked.json(), {"sent": True, "minutes": 10})
        self.assertEqual(mail.outbox[0].to, ["ana@escola.com"])
        self.assertIn("Escola Virtual", mail.outbox[0].subject)
        entered = self.enter()
        self.assertEqual(entered.status_code, 200)
        data = entered.json()
        self.assertEqual((data["user"]["name"], data["expires_in"]), ("Ana Souza", sessions.EMAIL_LIFETIME))
        identity = sessions.load(data["session"])
        self.assertEqual((identity.name, identity.groups, identity.is_moderator), ("Ana Souza", ["turma-a"], False))
        self.ana.refresh_from_db()
        self.assertEqual(self.ana.identity, identity)
        # They talk in the chat as themselves.
        sent = self.api("live_chat", session=data["session"], method="post", data={"body": "Oi"})
        self.assertEqual(sent.json()["message"]["author"], "Ana Souza")
        # The next time is the same person, not another account.
        self.session()
        self.assertEqual(ExternalIdentity.objects.count(), 1)

    def test_the_answer_does_not_say_who_is_enrolled(self):
        for email in ("ninguem@escola.com", "ana@escola.com"):
            self.assertEqual(self.ask(email).json(), {"sent": True, "minutes": 10})
        self.assertEqual(len(mail.outbox), 1)
        enrolments.revoke(self.integration, "ana@escola.com")
        cache.clear()
        self.ask()
        self.assertEqual(len(mail.outbox), 1)  # no access, no code

    def test_wrong_codes_run_out(self):
        self.ask()
        good = self.code()
        wrong = "000000" if good != "000000" else "111111"
        for _ in range(5):
            self.assertEqual(self.enter(code=wrong).status_code, 400)
        self.assertEqual(self.enter(code=good).status_code, 400)  # the code died with the tries

    def test_a_code_works_once(self):
        self.ask()
        good = self.code()
        self.assertEqual(self.enter(code=good).status_code, 200)
        self.assertEqual(self.enter(code=good).status_code, 400)
        self.assertEqual(self.enter(code="12").status_code, 400)

    def test_codes_are_not_handed_out_endlessly(self):
        codes = [self.ask().status_code for _ in range(4)]
        self.assertEqual(codes, [200, 200, 200, 429])

    def test_the_message_comes_in_the_frames_language(self):
        self.ask()
        self.assertIn("código", mail.outbox[-1].body)
        cache.clear()
        self.ask(**{"X-QQplay-Lang": "en"})
        self.assertIn("Your code to sign in", mail.outbox[-1].body)

    def test_only_where_it_is_turned_on(self):
        Integration.objects.filter(pk=self.integration.pk).update(email_login=False)
        self.assertEqual(self.ask().status_code, 404)
        self.assertEqual(self.post("login_verify", {"school": self.integration.key, "email": "ana@escola.com",
                                                    "code": "123456"}).status_code, 404)
        self.assertEqual(self.post("login_start", {"school": "sch_nope", "email": "a@b.co"}).status_code, 404)

    def test_teachers_and_classes_come_from_the_list(self):
        enrolments.upsert(self.integration, "rui@escola.com", name="Prof. Rui", classes="turma-a", teacher=True)
        Live.objects.filter(pk=self.live.pk).update(visibility=Media.Visibility.PRIVATE, groups="turma-a")
        teacher = self.session("rui@escola.com")
        self.assertTrue(self.api("live_state", session=teacher).json()["chat"]["can_moderate"])
        enrolments.upsert(self.integration, "zeca@escola.com", name="Zeca", classes="turma-z")
        self.assertEqual(self.api("live_state", session=self.session("zeca@escola.com")).status_code, 404)

    def test_the_session_ends_with_the_access_and_not_with_the_secret(self):
        session = self.session()
        self.integration.rotate_secret()  # about tokens: nothing to do with a code by e-mail
        self.assertIsNotNone(sessions.load(session))
        self.assertTrue(enrolments.revoke(self.integration, "ana@escola.com"))
        self.assertIsNone(sessions.load(session))

    def test_a_private_live_says_when_the_session_is_what_ended(self):
        Live.objects.filter(pk=self.live.pk).update(visibility=Media.Visibility.PRIVATE)
        session = self.session()
        self.assertEqual(self.api("live_state", session=session).status_code, 200)
        enrolments.revoke(self.integration, "ana@escola.com")
        gone = self.api("live_state", session=session)
        # Not found, as for anyone without access, and the frame is told its
        # session is over, so it asks to sign in again instead of guessing why.
        self.assertEqual((gone.status_code, gone["X-QQplay-Session"]), (404, "expired"))
        self.assertEqual(gone["Access-Control-Allow-Origin"], "*")

    def test_a_session_lasts_its_own_time(self):
        session = self.session()
        with mock.patch("time.time", return_value=time.time() + sessions.LIFETIME + 3600):
            self.assertIsNotNone(sessions.load(session))  # a day later: still theirs
        with mock.patch("time.time", return_value=time.time() + sessions.EMAIL_LIFETIME + 3600):
            self.assertIsNone(sessions.load(session))
        # A session from a site's token lasts the usual twelve hours.
        by_token = IntegrationTestCase.session(self, sub="77", name="Outra")
        with mock.patch("time.time", return_value=time.time() + sessions.LIFETIME + 3600):
            self.assertIsNone(sessions.load(by_token))


class DeclaredTests(Base):
    def setUp(self):
        super().setUp()
        Integration.objects.filter(pk=self.integration.pk).update(declared_login=True)

    def declare(self, email="ana@escola.com", origin=SITE, **extra):
        return self.post("login_declared", {"school": self.integration.key, "email": email, "origin": origin, **extra})

    def test_an_enrolled_student_is_taken_at_the_pages_word(self):
        response = self.declare()
        self.assertEqual(response.status_code, 200)
        self.assertEqual((response.json()["user"]["name"], response.json()["expires_in"]),
                         ("Ana Souza", sessions.LIFETIME))

    def test_but_not_from_elsewhere_nor_for_strangers_nor_teachers(self):
        self.assertEqual(self.declare(origin="https://outro.example.com").json()["code"], "origin")
        self.assertEqual(self.declare(email="ninguem@escola.com").json()["code"], "enrolment")
        enrolments.upsert(self.integration, "rui@escola.com", teacher=True)
        teacher = self.declare(email="rui@escola.com")
        self.assertEqual((teacher.status_code, teacher.json()["code"]), (403, "code"))
        Integration.objects.filter(pk=self.integration.pk).update(declared_login=False)
        self.assertEqual(self.declare().status_code, 404)

    def test_the_name_the_page_gives_fills_a_missing_one(self):
        enrolments.upsert(self.integration, "leo@escola.com")
        self.assertEqual(self.declare(email="leo@escola.com", name="Leo Park").json()["user"]["name"], "Leo Park")
        self.assertEqual(self.declare(name="Outro Nome").json()["user"]["name"], "Ana Souza")


class PurchaseTests(Base):
    def url(self, **query):
        base = reverse("integrations:purchase_hook", args=[self.integration.key, self.integration.hook_secret])
        return base + ("?" + "&".join(f"{k}={v}" for k, v in query.items()) if query else "")

    def send(self, body, **query):
        return self.client.post(self.url(**query), json.dumps(body), content_type="application/json")

    def test_the_address_is_the_secret(self):
        wrong = reverse("integrations:purchase_hook", args=[self.integration.key, "x" * 32])
        self.assertEqual(self.client.post(wrong, "{}", content_type="application/json").status_code, 404)
        other = Integration.objects.create(owner=self.owner, name="Outra")
        self.assertNotEqual(other.hook_secret, self.integration.hook_secret)
        self.assertEqual(self.client.get(self.url()).status_code, 405)

    def test_a_purchase_enrols_and_a_refund_ends_the_access(self):
        bought = self.send({"event": "PURCHASE_APPROVED",
                            "data": {"buyer": {"email": "Leo@Escola.com", "name": "Leo Park"}}}, classes="turma-b")
        self.assertEqual(bought.json(), {"result": "added"})
        leo = enrolments.find(self.integration, "leo@escola.com")
        self.assertEqual((leo.name, leo.groups, leo.is_active, leo.source), ("Leo Park", ["turma-b"], True, "webhook"))
        # Another product: they keep the first class and join the second.
        self.send({"event": "PURCHASE_APPROVED", "data": {"buyer": {"email": "leo@escola.com"}}}, classes="turma-c")
        leo.refresh_from_db()
        self.assertEqual(leo.groups, ["turma-b", "turma-c"])
        session = self.session("leo@escola.com")
        refunded = self.send({"event": "PURCHASE_REFUNDED", "data": {"buyer": {"email": "leo@escola.com"}}})
        self.assertEqual(refunded.json(), {"result": "removed"})
        self.assertIsNone(sessions.load(session))
        self.assertFalse(enrolments.find(self.integration, "leo@escola.com").is_active)

    def test_what_is_neither_is_answered_and_left_alone(self):
        for body in ({"order_status": "waiting_payment", "Customer": {"email": "x@escola.com"}},
                     {"event": "PURCHASE_APPROVED"}, {}, ["not", "an", "object"]):
            response = self.send(body)
            self.assertEqual(response.status_code, 200)
            self.assertIn(response.json()["result"], ("ignored", "no e-mail"))
        self.assertIsNone(enrolments.find(self.integration, "x@escola.com"))

    def test_form_posts_are_read_too(self):
        response = self.client.post(self.url(), {"email": "form@escola.com", "name": "Form", "status": "paid"})
        self.assertEqual(response.json(), {"result": "added"})

    def test_the_words_of_a_status(self):
        for status, action in (("PURCHASE_APPROVED", "grant"), ("paid", "grant"), ("subscription_canceled", "revoke"),
                               ("chargedback", "revoke"), ("Compra Aprovada", "grant"), ("unpaid", "ignore"),
                               ("waiting_payment", "ignore"), ("", "ignore")):
            self.assertEqual(hooks.classify(status), action, status)

    def test_the_studio_shows_what_arrived(self):
        self.send({"event": "PURCHASE_APPROVED", "data": {"buyer": {"email": "leo@escola.com"}}})
        self.client.force_login(self.owner)
        page = self.client.get(reverse("integrations:detail", args=[self.integration.pk])).content.decode()
        self.assertIn("PURCHASE_APPROVED", page)
        self.assertIn(self.integration.hook_secret, page)


class PlatformTests(Base):
    """Each platform's post, in the shape its documentation shows."""

    def url(self, **query):
        base = reverse("integrations:purchase_hook", args=[self.integration.key, self.integration.hook_secret])
        return base + ("?" + "&".join(f"{k}={v}" for k, v in query.items()) if query else "")

    def result(self, body, headers=None, **query):
        response = self.client.post(self.url(**query), json.dumps(body), content_type="application/json",
                                    headers=headers or {})
        return response.json().get("result", response.status_code)

    def active(self, email):
        found = enrolments.find(self.integration, email)
        return found is not None and found.is_active

    def test_hotmart(self):
        buyer = {"data": {"buyer": {"email": "h@escola.com", "name": "Hugo"}, "product": {"id": 7, "name": "Curso"}}}
        self.assertEqual(self.result({"event": "PURCHASE_APPROVED", **buyer}, platform="hotmart"), "added")
        self.assertEqual(enrolments.find(self.integration, "h@escola.com").name, "Hugo")
        self.assertEqual(self.result({"event": "PURCHASE_DELAYED", **buyer}, platform="hotmart"), "ignored")
        self.assertEqual(self.result({"event": "PURCHASE_CHARGEBACK", **buyer}, platform="hotmart"), "removed")
        self.assertEqual(self.result({"event": "PURCHASE_COMPLETE", **buyer}, platform="hotmart"), "kept")
        cancelled = {"event": "SUBSCRIPTION_CANCELLATION", "data": {"subscriber": {"email": "h@escola.com"}}}
        self.assertEqual(self.result(cancelled, platform="hotmart"), "removed")
        # With its token saved, the header must carry it; the header also says who is posting.
        Integration.objects.filter(pk=self.integration.pk).update(hook_token="hottok-123")
        self.assertEqual(self.result({"event": "PURCHASE_APPROVED", **buyer}, platform="hotmart"), 401)
        self.assertEqual(self.result({"event": "PURCHASE_APPROVED", **buyer}, {"X-HOTMART-HOTTOK": "wrong"}), 401)
        self.assertEqual(self.result({"event": "PURCHASE_APPROVED", **buyer}, {"X-HOTMART-HOTTOK": "hottok-123"}),
                         "kept")
        self.assertTrue(self.active("h@escola.com"))

    def test_kiwify_and_its_signature(self):
        body = json.dumps({"webhook_event_type": "order_approved", "order_status": "paid",
                           "Customer": {"email": "k@escola.com", "full_name": "Kia"}})
        Integration.objects.filter(pk=self.integration.pk).update(hook_token="kiwi-token")
        good = hmac.new(b"kiwi-token", body.encode(), hashlib.sha1).hexdigest()
        refused = self.client.post(self.url(platform="kiwify", signature="0" * 40), body, content_type="application/json")
        self.assertEqual(refused.status_code, 401)
        accepted = self.client.post(self.url(platform="kiwify", signature=good), body, content_type="application/json")
        self.assertEqual(accepted.json(), {"result": "added"})
        Integration.objects.filter(pk=self.integration.pk).update(hook_token="")
        self.assertEqual(self.result({"webhook_event_type": "subscription_late", "Customer": {"email": "k@escola.com"}},
                                     platform="kiwify"), "ignored")
        self.assertEqual(self.result({"webhook_event_type": "chargeback", "order_status": "chargedback",
                                      "Customer": {"email": "k@escola.com"}}, platform="kiwify"), "removed")

    def test_eduzz(self):
        invoice = {"data": {"buyer": {"email": "e@escola.com", "name": "Edu"}, "status": "paid",
                            "items": [{"productId": "p1", "name": "Curso"}]}}
        self.assertEqual(self.result({"event": "myeduzz.invoice_paid", **invoice}, platform="eduzz"), "added")
        # A chargeback is posted at each step: only the refund ends the access.
        review = {"event": "myeduzz.invoice_chargeback",
                  "data": {**invoice["data"], "chargeback": {"status": "underReview"}}}
        self.assertEqual(self.result(review, platform="eduzz"), "ignored")
        review["data"]["chargeback"]["status"] = "refunded"
        self.assertEqual(self.result(review, platform="eduzz"), "removed")
        self.assertEqual(self.result({"event": "ping"}, platform="eduzz"), "no e-mail")

    def test_greenn_ticto_and_cademi(self):
        self.assertEqual(self.result({"type": "sale", "event": "saleUpdated", "currentStatus": "paid",
                                      "client": {"email": "g@escola.com", "name": "Gil"}}, platform="greenn"), "added")
        self.assertEqual(self.result({"currentStatus": "waiting_payment", "client": {"email": "g@escola.com"}},
                                     platform="greenn"), "ignored")
        self.assertEqual(self.result({"currentStatus": "chargedback", "client": {"email": "g@escola.com"}},
                                     platform="greenn"), "removed")
        Integration.objects.filter(pk=self.integration.pk).update(hook_token="ticto-token")
        ticto = {"status": "authorized", "customer": {"email": "t@escola.com", "name": "Tom"}}
        self.assertEqual(self.result({**ticto, "token": "other"}, platform="ticto"), 401)
        self.assertEqual(self.result({**ticto, "token": "ticto-token"}, platform="ticto"), "added")
        Integration.objects.filter(pk=self.integration.pk).update(hook_token="")
        cademi = {"type": "enrollment.created", "data": {"user_id": 1, "status": "active"},
                  "expanded": {"user": {"email": "c@escola.com", "name": "Caio"}}}
        self.assertEqual(self.result(cademi, platform="cademi"), "added")
        cademi["type"], cademi["data"]["status"] = "enrollment.updated", "suspended"
        self.assertEqual(self.result(cademi, platform="cademi"), "ignored")
        cademi["type"], cademi["data"]["status"] = "enrollment.revoked", "revoked"
        self.assertEqual(self.result(cademi, platform="cademi"), "removed")
        # Without the people's data in the post (payload_detail other than "full"), nothing can be done.
        self.assertEqual(self.result({"type": "enrollment.created", "data": {"user_id": 2}}, platform="cademi"),
                         "no e-mail")

    def test_memberkit_signs_as_standard_webhooks(self):
        secret = base64.b64encode(b"a-memberkit-secret").decode()
        Integration.objects.filter(pk=self.integration.pk).update(hook_token=f"whsec_{secret}")
        body = json.dumps({"type": "enrollment.created",
                           "data": {"status": "active", "user": {"email": "m@escola.com", "full_name": "Mel"}}})
        signature = base64.b64encode(
            hmac.new(b"a-memberkit-secret", b"msg_1.1700000000." + body.encode(), hashlib.sha256).digest()
        ).decode()
        headers = {"webhook-id": "msg_1", "webhook-timestamp": "1700000000", "webhook-signature": f"v1,{signature}"}
        sent = self.client.post(self.url(), body, content_type="application/json", headers=headers)
        self.assertEqual(sent.json(), {"result": "added"})  # known by its header, no ?platform= needed
        forged = self.client.post(self.url(), body, content_type="application/json",
                                  headers={**headers, "webhook-signature": "v1,AAAA"})
        self.assertEqual(forged.status_code, 401)

    def test_monetizze_posts_a_form(self):
        Integration.objects.filter(pk=self.integration.pk).update(hook_token="chave-1")
        form = {"chave_unica": "chave-1", "postback_evento": "2", "comprador[email]": "mo@escola.com",
                "comprador[nome]": "Moa", "produto[codigo]": "99"}
        self.assertEqual(self.client.post(self.url(platform="monetizze"), form).json(), {"result": "added"})
        self.assertEqual(enrolments.find(self.integration, "mo@escola.com").name, "Moa")
        self.assertEqual(self.client.post(self.url(platform="monetizze"), {**form, "postback_evento": "9"}).json(),
                         {"result": "ignored"})
        self.assertEqual(self.client.post(self.url(platform="monetizze"), {**form, "postback_evento": "4"}).json(),
                         {"result": "removed"})
        self.assertEqual(self.client.post(self.url(platform="monetizze"), {**form, "chave_unica": "x"}).status_code,
                         401)

    def test_a_shape_not_known_here_is_still_read(self):
        # Fields named as a platform not listed might name them.
        unknown = {"event": "SALE_APPROVED", "sale": {"client_email": "u@escola.com"}}
        self.assertEqual(self.result(unknown), "added")
        self.assertEqual(self.result({"event": "SALE_REFUNDED", "payload": [{"buyer": {"E-mail": "u@escola.com"}}]}),
                         "removed")


class ApiTests(Base):
    def call(self, method, name, *args, data=None, query=""):
        token = tokens.sign({"scope": "api", "exp": int(time.time()) + 300}, self.integration)
        return getattr(self.client, method)(
            reverse(f"integrations:{name}", args=args) + query, json.dumps(data) if data is not None else "",
            content_type="application/json", headers={"Authorization": f"Bearer {token}"})

    def test_the_schools_server_keeps_the_list(self):
        one = self.call("post", "api_enrolments", data={"email": "leo@escola.com", "name": "Leo", "classes": ["3a"],
                                                        "teacher": True, "id": "L-7"})
        self.assertEqual(one.status_code, 201)
        self.assertEqual((one.json()["classes"], one.json()["teacher"], one.json()["id"]), (["3a"], True, "L-7"))
        many = self.call("post", "api_enrolments", data={"enrolments": [
            {"email": "leo@escola.com", "active": False}, {"email": "bia@escola.com"}, {"email": "nope"}, "junk"]})
        self.assertEqual(many.json(), {"added": 1, "updated": 1, "bad": [2, 3]})
        self.assertFalse(enrolments.find(self.integration, "leo@escola.com").is_active)
        listed = self.call("get", "api_enrolments", query="?q=escola.com").json()
        self.assertEqual((listed["total"], [row["email"] for row in listed["enrolments"]]),
                         (3, ["ana@escola.com", "bia@escola.com", "leo@escola.com"]))
        self.assertEqual(self.call("post", "api_enrolments", data={"email": "nope"}).status_code, 400)
        self.assertEqual(self.call("delete", "api_enrolment", "bia@escola.com").status_code, 200)
        self.assertEqual(self.call("get", "api_enrolment", "bia@escola.com").status_code, 404)

    def test_someone_with_an_id_at_the_site_is_one_person_either_way(self):
        self.call("post", "api_enrolments", data={"email": "ana@escola.com", "id": "42"})
        by_email = sessions.load(self.session())
        by_token = sessions.load(IntegrationTestCase.session(self))  # the site's token for sub "42"
        self.assertEqual(by_email.pk, by_token.pk)


class StudioTests(Base):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.owner)
        self.url = reverse("integrations:enrolments", args=[self.integration.pk])
        self.detail = reverse("integrations:detail", args=[self.integration.pk])

    def test_the_ways_of_signing_in_are_choices(self):
        self.client.post(self.url, {"action": "options", "declared_login": "1", "hook_token": " abc "})
        self.integration.refresh_from_db()
        self.assertEqual((self.integration.email_login, self.integration.declared_login, self.integration.hook_token),
                         (False, True, "abc"))

    def test_adding_importing_and_taking_off(self):
        self.client.post(self.url, {"action": "add", "email": "leo@escola.com", "name": "Leo", "classes": "3a, 3b",
                                    "teacher": "1"})
        leo = enrolments.find(self.integration, "leo@escola.com")
        self.assertEqual((leo.groups, leo.is_moderator, leo.source), (["3a", "3b"], True, "manual"))
        upload = SimpleUploadedFile("alunos.csv", "email,nome\nbia@escola.com,Bia\n".encode(), content_type="text/csv")
        self.client.post(self.url, {"action": "import", "file": upload})
        self.assertEqual(enrolments.find(self.integration, "bia@escola.com").name, "Bia")
        self.client.post(self.url, {"action": "off", "id": leo.pk})
        self.assertFalse(Enrolment.objects.get(pk=leo.pk).is_active)
        self.client.post(self.url, {"action": "on", "id": leo.pk})
        self.assertTrue(Enrolment.objects.get(pk=leo.pk).is_active)
        self.client.post(self.url, {"action": "remove", "id": leo.pk})
        self.assertFalse(Enrolment.objects.filter(pk=leo.pk).exists())

    def test_the_page_has_the_list_the_link_and_the_address(self):
        page = self.client.get(self.detail)
        html = page.content.decode()
        self.assertIn("ana@escola.com", html)
        self.assertIn(reverse("integrations:room", args=[self.integration.key, self.live.uid]), html)
        self.assertIn(f'data-school="{self.integration.key}"', html)
        self.assertEqual([row.email for row in self.client.get(self.detail, {"eq": "ana"}).context["enrolments"]],
                         ["ana@escola.com"])
        self.assertEqual(list(self.client.get(self.detail, {"eq": "zzz"}).context["enrolments"]), [])

    def test_a_new_address_replaces_the_old(self):
        old = self.integration.hook_secret
        self.client.post(self.url, {"action": "new_address"})
        self.integration.refresh_from_db()
        self.assertNotEqual(self.integration.hook_secret, old)

    def test_only_the_owner(self):
        self.client.force_login(make_user("nosy@example.com"))
        self.assertEqual(self.client.post(self.url, {"action": "add", "email": "x@y.co"}).status_code, 404)
        self.assertIsNone(enrolments.find(self.integration, "x@y.co"))


class PageTests(Base):
    def test_the_class_link_opens_the_frame_as_a_page(self):
        response = self.client.get(reverse("integrations:room", args=[self.integration.key, self.live.uid]))
        target = reverse("live:embed", args=[self.live.uid]) + f"?layout=both&school={self.integration.key}"
        self.assertRedirects(response, target, fetch_redirect_response=False)
        video = self.client.get(reverse("integrations:room_video", args=[self.integration.key, "abc"]), {"lang": "en"})
        self.assertIn("/embed/abc/?layout=both", video["Location"])
        self.assertIn("lang=en", video["Location"])
        self.assertEqual(self.client.get(reverse("integrations:room", args=["sch_nope", "x"])).status_code, 404)

    def test_the_frame_offers_the_email_form_only_where_it_is_on(self):
        url = reverse("live:embed", args=[self.live.uid])
        on = self.client.get(url, {"layout": "both", "school": self.integration.key}).content.decode()
        self.assertIn("data-email-login", on)
        self.assertIn(reverse("integrations:login_start"), on)
        self.assertIn("embed-login.js", on)
        self.assertNotIn("data-login-declared-url", on)
        Integration.objects.filter(pk=self.integration.pk).update(email_login=False)
        off = self.client.get(url, {"layout": "both", "school": self.integration.key}).content.decode()
        self.assertNotIn("data-email-login", off)
        self.assertNotIn("embed-login.js", off)
        media = Media.objects.create(owner=self.owner, title="V", state=Media.State.READY,
                                     visibility=Media.Visibility.PRIVATE, file="media/x/v.mp4")
        Integration.objects.filter(pk=self.integration.pk).update(email_login=True)
        video = self.client.get(reverse("core:embed", args=[media.uid]),
                                {"layout": "both", "school": self.integration.key}).content.decode()
        self.assertIn("data-email-login", video)
        self.assertIn("embed-login.js", video)
