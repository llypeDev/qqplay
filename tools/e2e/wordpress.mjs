// End-to-end test of the WordPress plugin (wordpress/qqplay), against a real
// WordPress with the plugin active.
//
// A WordPress for the test, on the same machine as the portal:
//
//   docker run -d --name wp-db --network host -e MARIADB_ROOT_PASSWORD=root \
//     -e MARIADB_DATABASE=wp -e MARIADB_USER=wp -e MARIADB_PASSWORD=wp mariadb:11
//   docker run -d --name wp --network host -e WORDPRESS_DB_HOST=127.0.0.1 \
//     -e WORDPRESS_DB_USER=wp -e WORDPRESS_DB_PASSWORD=wp -e WORDPRESS_DB_NAME=wp wordpress
//
// then, with wp-cli (the wordpress:cli image, with --volumes-from wp and the
// same variables): `wp core install --url=http://127.0.0.1 ...`, `wp plugin
// install tutor lifterlms --activate`, the plugin downloaded from the portal
// (/plugins/wordpress/qqplay.zip) installed and activated, and
// `wp eval-file tools/e2e/wordpress-seed.php` for the people and the courses.
//
//   BASE_URL=http://127.0.0.1:8080 WP_URL=http://127.0.0.1 \
//   E2E_WP_ADMIN=admin E2E_WP_ADMIN_PASSWORD=... node tools/e2e/wordpress.mjs
//
// (WordPress only asks an address on port 80, 443 or 8080 for an embed, so
// the portal runs on 8080 here. The lives' ingest server uses that port in
// development: stop it for this test, which needs no stream.)
//
// The test makes its own integration and lives on the portal, writes the
// integration's key and secret in the plugin's settings (use a WordPress
// that is only for tests), makes its own pages, and removes all of it at the
// end. Optional: E2E_HOST_EMAIL / E2E_PASSWORD (the portal's creator; the
// seed_demo accounts work), E2E_WP_PASSWORD (the seeded people's password),
// E2E_SHOTS=<dir> for screenshots.
import { createHmac } from "node:crypto";
import { mkdirSync } from "node:fs";
import { join } from "node:path";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const { chromium } = require("playwright");

const BASE = (process.env.BASE_URL || "http://127.0.0.1:8080").replace(/\/$/, "");
const WP = (process.env.WP_URL || "http://127.0.0.1").replace(/\/$/, "");
const HOST = process.env.E2E_HOST_EMAIL || "studio@example.com";
const PASSWORD = process.env.E2E_PASSWORD || "demo-pass-123";
const WP_ADMIN = process.env.E2E_WP_ADMIN || "admin";
const WP_ADMIN_PASSWORD = process.env.E2E_WP_ADMIN_PASSWORD || "";
const WP_PASSWORD = process.env.E2E_WP_PASSWORD || "Aluno-pass-123!";
const SHOTS = process.env.E2E_SHOTS || "";
const stamp = Date.now().toString(36);
const CLASS = "matematica-1";

let step = 0;
function ok(message) { console.log(`  ✓ ${String(++step).padStart(2, "0")} ${message}`); }
function fail(message) { throw new Error(message); }
function expect(condition, message, detail = "") { if (!condition) fail(detail ? `${message} — ${detail}` : message); ok(message); }

async function shot(page, name) {
  if (!SHOTS) return;
  mkdirSync(SHOTS, { recursive: true });
  await page.screenshot({ path: join(SHOTS, `${name}.png`), fullPage: true });
}

const b64 = (value) => Buffer.from(JSON.stringify(value)).toString("base64url");
function sign(claims, key, secret) {
  const header = b64({ alg: "HS256", typ: "JWT", kid: key });
  const body = b64(claims);
  return `${header}.${body}.${createHmac("sha256", secret).update(`${header}.${body}`).digest("base64url")}`;
}
const claimsOf = (token) => JSON.parse(Buffer.from(token.split(".")[1], "base64url").toString("utf8"));

async function api(state, method, path, body) {
  const now = Math.floor(Date.now() / 1000);
  const response = await fetch(`${BASE}/api/integrations/v1/${path}`, {
    method,
    headers: {
      Authorization: `Bearer ${sign({ scope: "api", iat: now, exp: now + 300 }, state.key, state.secret)}`,
      "Content-Type": "application/json",
    },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  return { status: response.status, data: await response.json().catch(() => ({})) };
}

async function wpSignIn(page, user, password) {
  await page.goto(`${WP}/wp-login.php`);
  await page.fill("#user_login", user);
  await page.fill("#user_pass", password);
  await page.click("#wp-submit");
  await page.waitForLoadState("networkidle");
  if (page.url().includes("wp-login.php")) fail(`could not sign in to WordPress as ${user}`);
}

/* WordPress's own API, as the administrator signed in on this page. */
async function wpRest(page, method, route, body) {
  return page.evaluate(async ({ method, route, body }) => {
    const nonce = await (await fetch("/wp-admin/admin-ajax.php?action=rest-nonce", { credentials: "include" })).text();
    const response = await fetch(`/index.php?rest_route=${route}`, {
      method,
      credentials: "include",
      headers: { "X-WP-Nonce": nonce, "Content-Type": "application/json" },
      body: body ? JSON.stringify(body) : undefined,
    });
    return { status: response.status, data: await response.json().catch(() => ({})) };
  }, { method, route, body });
}

/* The token the plugin gives the person signed in on this page. */
async function tokenOf(page) {
  return page.evaluate(async () => {
    const response = await fetch("/wp-admin/admin-ajax.php?action=qqplay_token", { credentials: "include" });
    return {
      cache: response.headers.get("cache-control") || "",
      cors: response.headers.get("access-control-allow-origin"),
      type: response.headers.get("content-type") || "",
      token: (await response.json()).token,
    };
  });
}

async function frameOf(page, part, ms = 20000) {
  const until = Date.now() + ms;
  while (Date.now() < until) {
    const found = page.frames().find((frame) => frame.url().startsWith(BASE) && frame.url().includes(part));
    if (found) return found;
    await page.waitForTimeout(250);
  }
  return fail(`no QQplay frame with ${part} in ${page.url()}`);
}
const textOf = async (frame) => (await frame.locator("body").innerText()).replace(/\s+/g, " ");

async function person(browser, errors, user) {
  const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  const page = await context.newPage();
  page.on("pageerror", (error) => errors.push(`${user} ${page.url()}: ${error.message}`));
  if (user) await wpSignIn(page, user, WP_PASSWORD);
  return page;
}

async function main() {
  if (!WP_ADMIN_PASSWORD) fail("E2E_WP_ADMIN_PASSWORD: the password of the WordPress administrator");
  const errors = [];
  const state = { key: "", secret: "", live: "", open: "", video: "", pages: [] };
  const browser = await chromium.launch();
  let studio;
  let admin;
  let detailUrl = "";
  try {
    // ------------------------------------------------------- on the portal
    studio = await browser.newPage({ viewport: { width: 1280, height: 900 } });
    await studio.goto(`${BASE}/accounts/login/`);
    await studio.fill('input[name="login"]', HOST);
    await studio.fill('input[name="password"]', PASSWORD);
    await Promise.all([studio.waitForNavigation(), studio.click('.auth-card button[type="submit"]')]);
    await studio.goto(`${BASE}/studio/integrations/`);
    await studio.fill('input[name="name"]', `WordPress E2E ${stamp}`);
    await studio.fill('textarea[name="origins"]', WP);
    await Promise.all([studio.waitForNavigation(), studio.click('form.form-grid button[type="submit"]')]);
    detailUrl = studio.url();
    state.key = await studio.inputValue("#integration-key");
    state.secret = await studio.inputValue("#integration-secret");
    expect(state.key.startsWith("sch_") && state.secret.length > 20, "the studio creates the integration for the WordPress site");
    const download = await fetch(`${BASE}${await studio.locator("#wordpress a[download]").getAttribute("href")}`);
    expect(download.status === 200 && download.headers.get("content-type") === "application/zip", "...and offers the plugin to download");

    let made = await api(state, "POST", "lives/", { title: `Aula da turma ${stamp}`, classes: [CLASS] });
    state.live = made.data.id;
    made = await api(state, "POST", "lives/", { title: `Aula aberta ${stamp}`, visibility: "public" });
    state.open = made.data.id;
    const videos = await api(state, "GET", "videos/");
    state.video = ((videos.data.videos || []).find((video) => video.kind === "video" && video.visibility === "public") || {}).id || "";
    expect(state.live && state.open && state.video, "a private live for one course, a public one and a video are there");

    // --------------------------------------------------- the administrator
    admin = await browser.newPage({ viewport: { width: 1280, height: 1100 } });
    admin.on("pageerror", (error) => errors.push(`admin ${admin.url()}: ${error.message}`));
    await wpSignIn(admin, WP_ADMIN, WP_ADMIN_PASSWORD);
    await admin.goto(`${WP}/wp-admin/options-general.php?page=qqplay`);
    expect(await admin.locator("#qqplay_portal").count() === 1, "the plugin's settings page opens");
    await admin.fill("#qqplay_portal", `${BASE}/studio/`);
    await admin.fill("#qqplay_key", state.key);
    await admin.fill("#qqplay_secret", state.secret);
    await admin.click("#submit");
    await admin.waitForLoadState("networkidle");
    const settings = await admin.content();
    expect(await admin.inputValue("#qqplay_portal") === BASE, "the portal's address is kept down to its origin");
    expect(await admin.inputValue("#qqplay_secret") === "" && !settings.includes(state.secret), "the secret is not written back into the page");
    expect(settings.includes(`WordPress E2E ${stamp}`) && (await admin.locator(".notice-success.inline").count()) === 1, "the portal says the plugin is connected");
    expect((await admin.locator(".notice-warning.inline").count()) === 0, "...with no warning about the list of sites");
    expect(settings.includes(`[qqplay_live id=&quot;${state.live}&quot;]`) && settings.includes(`[qqplay_video id=&quot;${state.video}&quot;]`), "the lives and the videos come with their shortcodes");
    expect(settings.includes(`value="${CLASS}"`), "the courses are listed as classes", "run tools/e2e/wordpress-seed.php with Tutor LMS active");
    await shot(admin, "wordpress-settings");
    expect(claimsOf((await tokenOf(admin)).token).moderator === true, "an administrator moderates");

    const pages = {
      live: `[qqplay_live id="${state.live}"]`,
      video: `[qqplay_video id="${state.video}"]`,
      openLink: `Assista:\n\n${BASE}/live/${state.open}/\n\nBoa aula!`,
      videoLink: `${BASE}/view/${state.video}/`,
    };
    const links = {};
    for (const [name, content] of Object.entries(pages)) {
      const page = await wpRest(admin, "POST", "/wp/v2/pages", { title: `QQplay E2E ${name} ${stamp}`, status: "publish", content });
      if (page.status !== 201) fail(`could not create the page ${name}: ${page.status}`);
      state.pages.push(page.data.id);
      links[name] = page.data.link;
    }
    const embed = await wpRest(admin, "GET", `/oembed/1.0/proxy&url=${encodeURIComponent(`${BASE}/view/${state.video}/`)}`);
    expect(embed.status === 200 && embed.data.type === "video" && /<iframe /.test(embed.data.html || ""), "the editor's preview of a pasted address comes from the portal's oEmbed", JSON.stringify(embed).slice(0, 200));

    // ----------------------------------------------------------- a student
    const ana = await person(browser, errors, "ana");
    const mine = await tokenOf(ana);
    const claims = claimsOf(mine.token);
    expect(claims.name === "Ana Souza" && /^ana[.@]/.test(claims.email || "") && /^\d+$/.test(claims.sub), "the token names the WordPress user", JSON.stringify(claims));
    expect((claims.groups || []).includes(CLASS) && claims.moderator === undefined, "...with their courses as classes, and no moderation", JSON.stringify(claims));
    expect(claims.exp - claims.iat === 600, "...valid for ten minutes");
    expect(/no-store/.test(mine.cache) && mine.cors === null && /application\/json/.test(mine.type), "...never kept by a cache, never offered to other sites");
    await ana.goto(links.live);
    const html = await ana.content();
    expect(!html.includes(mine.token.split(".")[0]) && /data-token-url="[^"]*admin-ajax\.php\?action=qqplay_token"/.test(html), "the token is not written into the page");
    let frame = await frameOf(ana, `/embed/live/${state.live}`);
    const box = frame.locator('textarea[name="body"]').first();
    await box.waitFor({ state: "visible", timeout: 20000 });
    expect(/Ana Souza/.test(await textOf(frame)), "an enrolled student sees the private live, signed in");
    const hello = `Oi do WordPress ${stamp}!`;
    await box.fill(hello);
    await box.press("Enter");
    await frame.getByText(hello).waitFor({ timeout: 10000 });
    expect(!/Moderation|Moderação/.test(await textOf(frame)), "...and writes in the chat, without moderation tools");
    await shot(ana, "wordpress-student");

    await ana.goto(links.video);
    frame = await frameOf(ana, `/embed/${state.video}`);
    const form = frame.locator("[data-comment-form]");
    await form.waitFor({ state: "visible", timeout: 20000 });
    const comment = `Comentário pelo WordPress ${stamp}`;
    await form.locator('textarea[name="body"]').fill(comment);
    await form.locator('button[type="submit"]').click();
    await frame.locator("[data-comment-list]").getByText(comment).waitFor({ timeout: 10000 });
    expect(/Ana Souza/.test(await frame.locator("[data-comment-list]").innerText()), "the student comments on a video");
    await ana.goto(links.videoLink);
    frame = await frameOf(ana, `/embed/${state.video}`);
    expect(frame.url().includes("layout=player"), "a pasted video address becomes the player alone");

    // -------------------------------------------- a student of no course
    const beto = await person(browser, errors, "beto");
    await beto.goto(links.live);
    frame = await frameOf(beto, `/embed/live/${state.live}`);
    await beto.waitForTimeout(5000);
    expect(!(await textOf(frame)).includes(hello) && !(await frame.locator('textarea[name="body"]').first().isVisible().catch(() => false)), "a student who is not in the course is kept out");

    // ----------------------------------------------------------- a teacher
    const rui = await person(browser, errors, "rui");
    expect(JSON.stringify(claimsOf((await tokenOf(rui)).token).groups) === '["*"]', "an editor is in every class");
    await rui.goto(links.live);
    frame = await frameOf(rui, `/embed/live/${state.live}`);
    await frame.getByText(hello).waitFor({ timeout: 20000 });
    expect(/Rui Lima/.test(await textOf(frame)) && /Moderation|Moderação/.test(await textOf(frame)), "...and moderates the chat of the course's live");
    await shot(rui, "wordpress-teacher");

    // ----------------------------------------------------------- a visitor
    const visitor = await person(browser, errors, "");
    let asked = 0;
    visitor.on("request", (request) => { if (request.url().includes("action=qqplay_token")) asked += 1; });
    await visitor.goto(links.live);
    frame = await frameOf(visitor, `/embed/live/${state.live}`);
    await visitor.waitForTimeout(14000);
    expect(!(await textOf(frame)).includes(hello), "a visitor does not see the private live");
    expect(asked === 1, "...and the site is asked for a token once, not again and again", `asked ${asked} times`);
    await visitor.goto(links.openLink);
    frame = await frameOf(visitor, `/embed/live/${state.open}`);
    const around = (await visitor.locator("body").innerText()).replace(/\s+/g, " ");
    expect(frame.url().includes("layout=both") && /Assista:/.test(around) && /Boa aula!/.test(around) && !around.includes(`${BASE}/live/`), "a pasted live address becomes the live with its chat");

    if (errors.length) fail(`errors in the pages:\n${errors.join("\n")}`);
    ok("no JavaScript errors in any page");
  } finally {
    // What the test made, taken away: the pages, the lives, the integration.
    for (const id of state.pages) {
      if (admin) await wpRest(admin, "DELETE", `/wp/v2/pages/${id}&force=true`).catch(() => {});
    }
    for (const live of [state.live, state.open]) {
      if (live && state.key) await api(state, "DELETE", `lives/${live}/`).catch(() => {});
    }
    if (studio && detailUrl) {
      await studio.goto(detailUrl).catch(() => {});
      await studio.locator('form[action$="/delete/"] button[type="submit"]').click().catch(() => {});
      await studio.locator('.confirm-dialog[open] button[value="ok"]').click().catch(() => {});
      await studio.waitForURL(`${BASE}/studio/integrations/`).catch(() => {});
    }
    await browser.close();
  }
  console.log(`\n${step} checks passed.`);
}

main().catch((error) => {
  console.error(`\n  ✗ ${error.message}`);
  process.exit(1);
});
