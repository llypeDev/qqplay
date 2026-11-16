"""oEmbed: what another site is told about a video or a live."""
import json
from xml.etree import ElementTree

from django.test import TestCase

from apps.live.models import Live
from apps.media.models import Media
from apps.users.models import User

from .embedding import iframe_html
from .tests import make_media

HOST = "http://testserver"


class OEmbedTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(email="ana@example.com", password="x" * 20, display_name="Ana Lima")
        self.video = make_media(self.owner, title="Frações", width=1920, height=1080)
        self.live = Live.objects.create(owner=self.owner, title="Aula ao vivo")

    def ask(self, url, **more):
        return self.client.get("/oembed/", {"url": url, **more})

    def test_a_video_is_described_with_its_player(self):
        response = self.ask(f"{HOST}/view/{self.video.uid}/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/json")
        self.assertEqual(response["Access-Control-Allow-Origin"], "*")
        data = response.json()
        self.assertEqual((data["version"], data["type"], data["title"]), ("1.0", "video", "Frações"))
        self.assertEqual((data["width"], data["height"]), (640, 360))
        self.assertIn(f'src="{HOST}/embed/{self.video.uid}/"', data["html"])
        self.assertIn('width="640" height="360"', data["html"])
        self.assertEqual(data["author_name"], "Ana Lima")
        self.assertEqual(data["author_url"], f"{HOST}/channel/{self.owner.pk}/")
        self.assertNotIn("thumbnail_url", data)  # no poster: nothing made up
        # The frame's own address describes the same video.
        self.assertEqual(self.ask(f"{HOST}/embed/{self.video.uid}/").json()["html"], data["html"])

    def test_the_size_fits_what_the_site_has(self):
        wide = self.ask(f"{HOST}/view/{self.video.uid}/", maxwidth="1200", maxheight="2000").json()
        self.assertEqual((wide["width"], wide["height"]), (1200, 675))
        low = self.ask(f"{HOST}/view/{self.video.uid}/", maxwidth="1200", maxheight="270").json()
        self.assertEqual((low["width"], low["height"]), (480, 270))
        portrait = make_media(self.owner, title="Em pé", width=1080, height=1920)
        tall = self.ask(f"{HOST}/view/{portrait.uid}/", maxwidth="360").json()
        self.assertEqual((tall["width"], tall["height"]), (360, 640))
        odd = self.ask(f"{HOST}/view/{self.video.uid}/", maxwidth="abc", maxheight="-5").json()
        self.assertEqual((odd["width"], odd["height"]), (640, 360))

    def test_a_live_is_described_too(self):
        data = self.ask(f"{HOST}/live/{self.live.uid}/").json()
        self.assertEqual((data["type"], data["title"]), ("video", "Aula ao vivo"))
        self.assertIn(f'src="{HOST}/embed/live/{self.live.uid}/"', data["html"])

    def test_audio_is_a_rich_frame(self):
        audio = make_media(self.owner, title="Podcast", kind=Media.Kind.AUDIO)
        self.assertEqual(self.ask(f"{HOST}/view/{audio.uid}/").json()["type"], "rich")

    def test_xml_for_who_asks(self):
        response = self.ask(f"{HOST}/view/{self.video.uid}/", format="xml")
        self.assertEqual(response["Content-Type"], "text/xml; charset=utf-8")
        root = ElementTree.fromstring(response.content)
        self.assertEqual(root.tag, "oembed")
        self.assertEqual(root.findtext("title"), "Frações")
        self.assertIn("<iframe", root.findtext("html"))
        self.assertEqual(self.ask(f"{HOST}/view/{self.video.uid}/", format="yaml").status_code, 501)

    def test_what_is_private_looks_like_what_does_not_exist(self):
        private = make_media(self.owner, title="Só meu", visibility=Media.Visibility.PRIVATE)
        closed = Live.objects.create(owner=self.owner, title="Fechada", visibility=Media.Visibility.PRIVATE)
        draft = make_media(self.owner, title="Em preparo", state=Media.State.PROCESSING)
        for address in (f"{HOST}/view/{private.uid}/", f"{HOST}/live/{closed.uid}/", f"{HOST}/view/{draft.uid}/",
                        f"{HOST}/view/nothing-here/", f"{HOST}/studio/", "https://elsewhere.example.com/view/x/",
                        f"javascript:{HOST}/view/{self.video.uid}/", ""):
            self.assertEqual(self.ask(address).status_code, 404, address)
        # ...even to its owner: the answer is for the site that asks.
        self.client.force_login(self.owner)
        self.assertEqual(self.ask(f"{HOST}/view/{private.uid}/").status_code, 404)

    def test_an_unlisted_video_is_described_to_who_has_the_link(self):
        unlisted = make_media(self.owner, title="Com o link", visibility=Media.Visibility.UNLISTED)
        self.assertEqual(self.ask(f"{HOST}/view/{unlisted.uid}/").status_code, 200)

    def test_only_get(self):
        self.assertEqual(self.client.post("/oembed/", {"url": f"{HOST}/view/{self.video.uid}/"}).status_code, 405)


class DiscoveryTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(email="ana@example.com", password="x" * 20)

    def test_the_pages_say_where_to_ask(self):
        video = make_media(self.owner, title="Frações")
        page = self.client.get(f"/view/{video.uid}/")
        self.assertContains(page, 'type="application/json+oembed"')
        self.assertContains(page, 'type="text/xml+oembed"')
        self.assertContains(page, f"/oembed/?url=http%3A%2F%2Ftestserver%2Fview%2F{video.uid}%2F&amp;format=json")
        live = Live.objects.create(owner=self.owner, title="Aula")
        self.assertContains(self.client.get(f"/live/{live.uid}/"), 'type="application/json+oembed"')

    def test_private_pages_do_not(self):
        video = make_media(self.owner, title="Só meu", visibility=Media.Visibility.PRIVATE)
        self.client.force_login(self.owner)
        page = self.client.get(f"/view/{video.uid}/")
        self.assertEqual(page.status_code, 200)
        self.assertNotContains(page, "+oembed")


class EmbedCodeTests(TestCase):
    """The code a visitor copies into their own site."""

    def test_a_title_cannot_add_attributes_to_the_frame(self):
        html = iframe_html("https://qqplay.example/embed/abc/", 'x" onload="alert(1)')
        self.assertNotIn('onload="alert', html)
        self.assertIn("title=\"x&quot; onload=&quot;alert(1)\"", html)

    def test_the_watch_page_offers_code_that_is_safe_to_paste(self):
        owner = User.objects.create_user(email="mal@example.com", password="x" * 20)
        video = make_media(owner, title='"><script>alert(1)</script>')
        page = self.client.get(f"/view/{video.uid}/")
        snippet = page.context["embed_snippet"]
        self.assertNotIn("<script>", snippet)
        self.assertEqual(snippet.count("<"), 2)  # the frame's opening and closing tags, nothing else
        data = self.client.get("/oembed/", {"url": f"http://testserver/view/{video.uid}/"}).json()
        self.assertNotIn("<script>", data["html"])
        self.assertEqual(json.loads(json.dumps(data))["title"], '"><script>alert(1)</script>')
