// End-to-end test of the school integration, with a school site of its own.
//
// A small Node server plays the school: on another origin than the portal, it
// signs its people in with the integration's secret (a token URL, as the
// studio page shows) and puts the live and a video of the library on its
// lesson pages with the script. A creator makes the integration, sets its
// look and checks a sample token in the studio; the school's server schedules
// a private live for one class through the API; a student and a teacher of
// that class talk in the chat, the teacher wears the Streaming badge, answers
// a question, pins a message, slows the chat down and mutes the student for a
// moment; a student of another class is kept out; a lesson drawn after the
// page loaded (a single-page site) is picked up, and an address that is not
// among the integration's sites gets an explanation; the two comment on a
// video and the teacher hides a comment; the school's server reads the
// attendance, blocks the
// student and erases their data. Text selectors follow the default
// interface language, Brazilian Portuguese, or avoid text altogether.
//
// Needs the portal running and an account that can create integrations and
// owns at least one ready video (the studio account of `manage.py seed_demo`):
//
//   BASE_URL=http://127.0.0.1:8000 E2E_HOST_EMAIL=studio@example.com \
//   E2E_PASSWORD=demo-pass-123 node tools/e2e/integration.mjs
//
// Optional: E2E_LIVE_HOOK_SECRET=<the portal's LIVE_HOOK_SECRET> puts the live
// on air with the ingest server's callback (no encoder needed), which also
// checks the attendance; E2E_SCHOOL_PORT (8765) for the school's server;
// E2E_SHOTS=<dir> for screenshots; E2E_INSECURE_TLS=1 against a local HTTPS
// stack whose certificate the browser does not trust (Caddy's own, for
// PORTAL_DOMAIN=localhost), with NODE_TLS_REJECT_UNAUTHORIZED=0 for the API
// calls. E2E_MAIL_DIR=<dir> where the portal writes its e-mails as files
// (EMAIL_BACKEND=django.core.mail.backends.filebased.EmailBackend with
// EMAIL_FILE_PATH=<dir> in development) adds the school with no server of its
// own: a student enrolled by e-mail opens the class link, signs in with the
// code from the e-mail, stays signed in after a reload, and loses the access
// when the sales platform posts a refund. E2E_LTI=1 (with the portal started
// with LTI_ALLOW_LOCAL_PLATFORMS=1, a development setting) adds an LMS: the
// same Node server plays one that launches the portal as an LTI 1.3 tool,
// with a key pair of its own; a student and a teacher open the activity from
// a course page, a student of another course is kept out, and the teacher
// picks the live in the content selection, which the LMS checks against the
// portal's public keys.
import { createHmac, createPublicKey, generateKeyPairSync, sign as rsaSign, verify as rsaVerify } from "node:crypto";
import { mkdirSync, readdirSync, readFileSync, statSync } from "node:fs";
import { createServer } from "node:http";
import { join } from "node:path";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const { chromium } = require("playwright");

const BASE = (process.env.BASE_URL || "http://127.0.0.1:8000").replace(/\/$/, "");
const HOST = process.env.E2E_HOST_EMAIL || "studio@example.com";
const PASSWORD = process.env.E2E_PASSWORD || "demo-pass-123";
const HOOK = process.env.E2E_LIVE_HOOK_SECRET || "";
const PORT = Number(process.env.E2E_SCHOOL_PORT || 8765);
const SCHOOL = `http://127.0.0.1:${PORT}`;
const SHOTS = process.env.E2E_SHOTS || "";
const INSECURE = process.env.E2E_INSECURE_TLS === "1";
const MAIL_DIR = process.env.E2E_MAIL_DIR || "";
const LTI = process.env.E2E_LTI === "1";
const stamp = Date.now().toString(36);
const WAIT = 30000; // a poll of a scheduled live comes every 10 s

let step = 0;
function ok(message) { console.log(`  ✓ ${String(++step).padStart(2, "0")} ${message}`); }
function fail(message) { throw new Error(message); }
function expect(condition, message) { if (!condition) fail(message); ok(message); }

async function shot(page, name) {
  if (!SHOTS) return;
  mkdirSync(SHOTS, { recursive: true });
  await page.screenshot({ path: join(SHOTS, `${name}.png`) });
}

function watchErrors(page, bucket) {
  page.on("pageerror", (error) => bucket.push(`pageerror ${page.url()}: ${error.message}`));
  page.on("console", (message) => {
    if (message.type() !== "error") return;
    const where = (message.location() && message.location().url) || "";
    // With no ingest server, the stream's playlist never comes; the player
    // retries on purpose. Refusals checked below show as failed loads too.
    if (/\/hls\//.test(where) || /Failed to load resource/.test(message.text())) return;
    bucket.push(`console ${page.url()}: ${message.text()} ${where}`);
  });
}

// ------------------------------------------------------------ the school

const b64 = (value) => Buffer.from(JSON.stringify(value)).toString("base64url");

/* The token the school's server makes, as on the studio page. */
function sign(claims, key, secret) {
  const header = b64({ alg: "HS256", typ: "JWT", kid: key });
  const body = b64(claims);
  const signature = createHmac("sha256", secret).update(`${header}.${body}`).digest("base64url");
  return `${header}.${body}.${signature}`;
}

const PEOPLE = {
  ana: { id: `ana-${stamp}`, name: "Ana Souza", classes: ["turma-a"] },
  rui: { id: `rui-${stamp}`, name: "Prof. Rui", classes: ["turma-a"], teacher: true },
  bia: { id: `bia-${stamp}`, name: "Bia Lima", classes: ["turma-z"] },
};

// ------------------------------------------------------------------ the LMS

const LMS = { ...generateKeyPairSync("rsa", { modulusLength: 2048 }), kid: "e2e-lms-key", client: "e2e-client", added: "" };
const LMS_ISSUER = `${SCHOOL}/lms`;
const LTI_CLAIM = "https://purl.imsglobal.org/spec/lti/claim/";
const LMS_PEOPLE = {
  ana: { sub: "lms-ana", name: "Ana do Moodle", roles: ["http://purl.imsglobal.org/vocab/lis/v2/membership#Learner"], course: "turma-a" },
  rui: { sub: "lms-rui", name: "Prof. Rui do Moodle", roles: ["http://purl.imsglobal.org/vocab/lis/v2/membership#Instructor"], course: "turma-a" },
  bia: { sub: "lms-bia", name: "Bia do Moodle", roles: ["http://purl.imsglobal.org/vocab/lis/v2/membership#Learner"], course: "turma-z" },
};

function lmsToken(claims) {
  const head = b64({ alg: "RS256", typ: "JWT", kid: LMS.kid });
  const body = b64(claims);
  return `${head}.${body}.${rsaSign("RSA-SHA256", Buffer.from(`${head}.${body}`), LMS.privateKey).toString("base64url")}`;
}

/* What the LMS answers at its own addresses; null when the address is not its. */
async function lms(url, state) {
  const html = (body) => ({ status: 200, type: "text/html; charset=utf-8", body: `<!doctype html><meta charset="utf-8"><body style="margin:0;font:15px system-ui">${body}</body>` });
  if (url.pathname === "/lms/jwks") {
    return { status: 200, type: "application/json", body: JSON.stringify({ keys: [{ ...LMS.publicKey.export({ format: "jwk" }), kid: LMS.kid, alg: "RS256", use: "sig" }] }) };
  }
  if (url.pathname === "/lms/course" || url.pathname === "/lms/add") {
    // A course page: the activity opens in a frame, starting at the tool's login.
    const hint = JSON.stringify({ who: url.searchParams.get("who"), live: url.pathname === "/lms/course" ? state.live : "", pick: url.pathname === "/lms/add" });
    const login = `${BASE}/lti/login/?` + new URLSearchParams({ iss: LMS_ISSUER, login_hint: url.searchParams.get("who"), client_id: LMS.client,
      target_link_uri: `${BASE}/lti/launch/`, lti_message_hint: hint });
    return html(`<h1 style="margin:12px 20px;font-size:18px">Moodle de teste · Física</h1><iframe name="tool" src="${login}" style="display:block;width:100%;height:640px;border:0" allow="autoplay; fullscreen"></iframe>`);
  }
  if (url.pathname === "/lms/auth") {
    // The LMS's authentication address: it signs the launch and posts it to the tool.
    const hint = JSON.parse(url.searchParams.get("lti_message_hint") || "{}");
    const person = LMS_PEOPLE[hint.who];
    if (!person || url.searchParams.get("client_id") !== LMS.client) return { status: 400, type: "text/plain", body: "bad request" };
    const now = Math.floor(Date.now() / 1000);
    const claims = {
      iss: LMS_ISSUER, aud: LMS.client, sub: person.sub, name: person.name, iat: now, exp: now + 120, nonce: url.searchParams.get("nonce"),
      [LTI_CLAIM + "version"]: "1.3.0", [LTI_CLAIM + "deployment_id"]: "1", [LTI_CLAIM + "roles"]: person.roles,
      [LTI_CLAIM + "context"]: { id: `course-${person.course}`, label: person.course, title: "Física" },
      [LTI_CLAIM + "resource_link"]: { id: "activity-1" }, [LTI_CLAIM + "target_link_uri"]: `${BASE}/lti/launch/`,
      [LTI_CLAIM + "launch_presentation"]: { locale: "pt-BR", document_target: "iframe" },
    };
    if (hint.pick) {
      claims[LTI_CLAIM + "message_type"] = "LtiDeepLinkingRequest";
      claims["https://purl.imsglobal.org/spec/lti-dl/claim/deep_linking_settings"] = { deep_link_return_url: `${SCHOOL}/lms/return`, data: "from-the-lms", accept_types: ["ltiResourceLink"] };
    } else {
      claims[LTI_CLAIM + "message_type"] = "LtiResourceLinkRequest";
      claims[LTI_CLAIM + "custom"] = { qqplay_live: hint.live };
    }
    return html(`<form id="f" method="post" action="${url.searchParams.get("redirect_uri")}"><input type="hidden" name="id_token" value="${lmsToken(claims)}"><input type="hidden" name="state" value="${url.searchParams.get("state")}"></form><script>document.getElementById("f").submit()</script>`);
  }
  return null;
}

/* The tool's answer to a content selection, checked as an LMS checks it:
   against the tool's published keys. Returns what was added. */
async function lmsAccept(form) {
  const signed = form.get("JWT") || "";
  const [head, body, signature] = signed.split(".");
  const { keys } = await (await fetch(`${BASE}/lti/jwks/`)).json();
  const jwk = keys.find((key) => key.kid === JSON.parse(Buffer.from(head, "base64url")).kid);
  if (!jwk || !rsaVerify("RSA-SHA256", Buffer.from(`${head}.${body}`), createPublicKey({ key: jwk, format: "jwk" }), Buffer.from(signature, "base64url"))) return "BAD SIGNATURE";
  const claims = JSON.parse(Buffer.from(body, "base64url"));
  const item = (claims["https://purl.imsglobal.org/spec/lti-dl/claim/content_items"] || [])[0] || {};
  if (claims.iss !== LMS.client || claims.aud !== LMS_ISSUER || claims["https://purl.imsglobal.org/spec/lti-dl/claim/data"] !== "from-the-lms") return "BAD CLAIMS";
  return `ADDED ${item.type} ${(item.custom || {}).qqplay_live}`;
}

function school(state) {
  const page = (title, widget) => `<!doctype html>
<html lang="pt-BR"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>${title} · Escola Virtual</title>
<style>
  body { margin: 0; font: 15px/1.5 system-ui, sans-serif; background: #f4f1ea; color: #2b2a26; }
  header { display: flex; align-items: center; gap: 12px; padding: 14px 28px; background: #1f3a2e; color: #fff; }
  header b { font-size: 17px; }
  main { max-width: 1180px; margin: 0 auto; padding: 22px 28px; }
  h1 { margin: 0 0 14px; font-size: 22px; }
</style></head>
<body><header><b>Escola Virtual</b><span>${title}</span></header>
<main><h1>${title}</h1>${widget}</main>
<script src="${BASE}/embed/sdk.js" async></script></body></html>`;

  return createServer(async (request, response) => {
    const url = new URL(request.url, SCHOOL);
    const who = PEOPLE[url.searchParams.get("who")] ? url.searchParams.get("who") : "";
    const send = (status, type, body) => {
      response.writeHead(status, { "Content-Type": type, "Cache-Control": "no-store" });
      response.end(body);
    };
    if (url.pathname === "/lms/return" && request.method === "POST") {
      const chunks = [];
      for await (const chunk of request) chunks.push(chunk);
      LMS.added = await lmsAccept(new URLSearchParams(Buffer.concat(chunks).toString()));
      return send(200, "text/html; charset=utf-8", `<!doctype html><p id="added">${LMS.added}</p>`);
    }
    const fromLms = url.pathname.startsWith("/lms/") ? await lms(url, state) : null;
    if (fromLms) return send(fromLms.status, fromLms.type, fromLms.body);
    if (url.pathname === "/token" && who) {
      const person = PEOPLE[who];
      const now = Math.floor(Date.now() / 1000);
      const token = sign({ sub: person.id, name: person.name, moderator: Boolean(person.teacher),
        groups: person.classes, iat: now, exp: now + 600 }, state.key, state.secret);
      return send(200, "application/json", JSON.stringify({ token }));
    }
    if (url.pathname === "/aula" && who) {
      return send(200, "text/html; charset=utf-8", page("Aula ao vivo",
        `<div data-qqplay-live="${state.live}" data-layout="both" data-token-url="/token?who=${who}"></div>`));
    }
    if (url.pathname === "/spa" && who) {
      // A site that draws the lesson after the page has loaded (React, Vue…).
      const widget = `<div data-qqplay-live="${state.live}" data-layout="both" data-token-url="/token?who=${who}"></div>`;
      return send(200, "text/html; charset=utf-8", page("Aula ao vivo",
        `<div id="app"></div><script>setTimeout(function () { document.getElementById("app").innerHTML = ${JSON.stringify(widget)}; }, 1200);</script>`));
    }
    if (url.pathname === "/video" && who) {
      return send(200, "text/html; charset=utf-8", page("Revisão em vídeo",
        `<div data-qqplay-video="${state.video}" data-layout="both" data-token-url="/token?who=${who}"></div>`));
    }
    return send(404, "text/plain", "not found");
  });
}

// --------------------------------------------------------------- the API

function apiToken(state) {
  const now = Math.floor(Date.now() / 1000);
  return sign({ scope: "api", iat: now, exp: now + 300 }, state.key, state.secret);
}

async function api(state, method, path, body, token) {
  const response = await fetch(`${BASE}/api/integrations/v1/${path}`, {
    method,
    headers: {
      Authorization: `Bearer ${token === undefined ? apiToken(state) : token}`,
      "Content-Type": "application/json",
    },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = await response.json().catch(() => ({}));
  return { status: response.status, data };
}

async function hook(event, state) {
  const [name, query] = state.streamKey.split("?");
  const key = new URLSearchParams(query).get("key");
  const response = await fetch(`${BASE}/live/hooks/${HOOK}/${event}/`, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({ name, key, addr: "127.0.0.1" }).toString(),
  });
  return response.status;
}

/* The sign-in code in the newest e-mail written after a moment. */
async function codeFromMail(since) {
  for (let attempt = 0; attempt < 40; attempt++) {
    const files = readdirSync(MAIL_DIR).map((name) => join(MAIL_DIR, name))
      .filter((path) => statSync(path).mtimeMs >= since).sort((a, b) => statSync(b).mtimeMs - statSync(a).mtimeMs);
    for (const path of files) {
      const found = readFileSync(path, "utf8").match(/^\s{4}(\d{6})\s*$/m);
      if (found) return found[1];
    }
    await new Promise((resolve) => setTimeout(resolve, 250));
  }
  fail("no e-mail with a code arrived");
}

// ---------------------------------------------------------------- helpers

async function signIn(page, email) {
  await page.goto(`${BASE}/accounts/login/`);
  await page.fill('input[name="login"]', email);
  await page.fill('input[name="password"]', PASSWORD);
  await Promise.all([page.waitForNavigation(), page.click('.auth-card button[type="submit"]')]);
}

/* Answers "yes" in the portal's confirm dialog (app.js). */
async function confirmIn(page) {
  await page.locator('.confirm-dialog[open] button[value="ok"]').click();
}

/* A person of the school on a lesson page, and the QQplay frame in it. */
async function lesson(browser, errors, who, path = "aula") {
  const context = await browser.newContext({ viewport: { width: 1280, height: 860 }, ignoreHTTPSErrors: INSECURE });
  const page = await context.newPage();
  watchErrors(page, errors);
  await page.goto(`${SCHOOL}/${path}?who=${who}`);
  const frame = page.frameLocator("iframe");
  return { context, page, frame };
}

async function signedIn(frame, name) {
  await frame.locator("[data-chat-who] strong").filter({ hasText: name }).waitFor({ timeout: WAIT });
}

async function send(frame, text, question = false) {
  if (question) await frame.locator("[data-chat-ask]").click();
  await frame.locator("[data-chat-form] textarea").fill(text);
  await frame.locator('[data-chat-form] button[type="submit"]').click();
}

const message = (frame, text) => frame.locator(".chat-list .chat-msg").filter({ hasText: text }).last();

/* A moderator's tool on a message: hover it, then click. */
async function tool(frame, text, selector) {
  const item = message(frame, text);
  await item.hover();
  await item.locator(selector).click();
}

// ------------------------------------------------------------------ run

async function main() {
  const errors = [];
  const state = { key: "", secret: "", live: "", video: "", streamKey: "" };
  const server = school(state);
  await new Promise((resolve) => server.listen(PORT, "127.0.0.1", resolve));
  const browser = await chromium.launch();
  let studio;
  let onAir = false;
  try {
    // ------------------------------------------------ the creator's studio
    studio = await browser.newPage({ viewport: { width: 1280, height: 900 }, ignoreHTTPSErrors: INSECURE });
    watchErrors(studio, errors);
    await signIn(studio, HOST);
    await studio.goto(`${BASE}/studio/integrations/`);
    await studio.fill('input[name="name"]', `Escola Virtual E2E ${stamp}`);
    await studio.fill('textarea[name="origins"]', SCHOOL);
    await Promise.all([studio.waitForNavigation(), studio.click('form.form-grid button[type="submit"]')]);
    state.key = await studio.inputValue("#integration-key");
    state.secret = await studio.inputValue("#integration-secret");
    expect(state.key.startsWith("sch_") && state.secret.length > 20, "the studio creates the integration, with its key and secret");
    const detailUrl = studio.url();

    const look = studio.locator("form", { has: studio.locator('input[name="chat_title"]:not([type="hidden"])') });
    await look.locator('input[name="accent_color"][data-color-text]').fill("#7c3aed");
    await look.locator('input[name="chat_title"]').fill("Sala de aula");
    await Promise.all([studio.waitForNavigation(), look.locator('button[type="submit"]').click()]);
    expect(await studio.locator('input[name="chat_title"]:not([type="hidden"])').inputValue() === "Sala de aula",
      "the look of the frames is saved (colour and the chat's title)");

    const now = Math.floor(Date.now() / 1000);
    const sample = sign({ sub: "t-1", name: "Aluna de teste", groups: ["turma-a"], exp: now + 600 }, state.key, state.secret);
    await studio.locator('#diagnostics textarea[name="token"]').fill(sample);
    await Promise.all([studio.waitForNavigation(), studio.locator('#diagnostics button[type="submit"]').click()]);
    expect(await studio.locator("#diagnostics .check-result.is-ok").filter({ hasText: "Aluna de teste" }).count() === 1,
      "the studio reads a token as the sign-in would and says it is valid");
    await studio.locator('#diagnostics textarea[name="token"]').fill(sample.slice(0, -4) + "AAAA");
    await Promise.all([studio.waitForNavigation(), studio.locator('#diagnostics button[type="submit"]').click()]);
    expect(await studio.locator("#diagnostics .check-result.is-bad").count() === 1, "...and says what is wrong with a bad one");

    // ------------------------------------------------ the school's server
    expect((await api(state, "GET", "lives/", undefined, "")).status === 401, "the API refuses a request without a token");
    const personToken = sign({ sub: "x", name: "X", exp: Math.floor(Date.now() / 1000) + 60 }, state.key, state.secret);
    expect((await api(state, "GET", "lives/", undefined, personToken)).status === 401, "...and a person's token");
    const created = await api(state, "POST", "lives/", {
      title: `Revisão para a prova ${stamp}`, classes: ["turma-a"], chat: true,
      scheduled_for: new Date(Date.now() + 3600 * 1000).toISOString(),
    });
    expect(created.status === 201 && created.data.visibility === "private" && created.data.classes[0] === "turma-a",
      "the school's server schedules a private live for one class");
    state.live = created.data.id;
    state.streamKey = created.data.stream.key;
    const patched = await api(state, "PATCH", `lives/${state.live}/`, { description: "Traga suas dúvidas." });
    expect(patched.status === 200 && patched.data.description === "Traga suas dúvidas.", "...and changes it");
    const videos = await api(state, "GET", "videos/");
    const video = (videos.data.videos || []).find((item) => item.kind === "video");
    expect(Boolean(video), "the API lists the creator's videos");
    state.video = video.id;

    if (HOOK) {
      onAir = (await hook("publish", state)) === 200;
      expect(onAir, "the encoder connects (the ingest server's callback): the live is on air");
    }

    // ------------------------------------------------ a student and a teacher
    const ana = await lesson(browser, errors, "ana");
    await signedIn(ana.frame, "Ana Souza");
    expect(true, "the school signs the student in inside the frame, with no QQplay account");
    expect((await ana.frame.locator(".live-chat h2").textContent()).includes("Sala de aula"), "the chat carries the school's title");
    await send(ana.frame, "Bom dia, turma!");
    await message(ana.frame, "Bom dia, turma!").waitFor();
    await ana.page.waitForTimeout(2100); // one message every 2 s per person
    await send(ana.frame, "Vai cair na prova?", true);
    await ana.frame.locator(".chat-msg.is-question").filter({ hasText: "Vai cair na prova?" }).waitFor();
    await ana.frame.locator("[data-questions-count]:not([hidden])").filter({ hasText: /^1$/ }).waitFor({ timeout: WAIT });
    expect(true, "the student talks and asks a question, listed apart for the host");
    await shot(ana.page, "integration-student");

    const rui = await lesson(browser, errors, "rui");
    await signedIn(rui.frame, "Prof. Rui");
    await message(rui.frame, "Bom dia, turma!").waitFor({ timeout: WAIT });
    await send(rui.frame, "Bem-vindos! Já começamos.");
    const badge = message(rui.frame, "Bem-vindos!").locator(".chat-host");
    await badge.waitFor();
    expect(await badge.textContent() === "Streaming", "the teacher wears the Streaming badge");
    await message(ana.frame, "Bem-vindos!").locator(".chat-host").waitFor({ timeout: WAIT });
    expect(true, "...and the student sees it");

    await rui.frame.locator('[data-chat-tab="questions"]').click();
    await rui.frame.locator("[data-question]").filter({ hasText: "Vai cair na prova?" }).locator("[data-chat-answer]").click();
    await ana.frame.locator(".chat-msg.is-answered").filter({ hasText: "Vai cair na prova?" }).waitFor({ timeout: WAIT });
    expect(true, "the teacher answers the question, and the student sees it answered");
    await rui.frame.locator('[data-chat-tab="chat"]').click();

    await tool(rui.frame, "Bom dia, turma!", '[data-chat-pin][data-pin-on="1"]');
    await ana.frame.locator("[data-chat-pinned]:not([hidden])").filter({ hasText: "Bom dia, turma!" }).waitFor({ timeout: WAIT });
    expect(true, "the teacher pins a message over everyone's chat");

    await rui.frame.locator("[data-chat-mod] summary").click();
    await rui.frame.locator("[data-slow-mode]").selectOption("10");
    await ana.frame.locator("[data-chat-slow]:not([hidden])").waitFor({ timeout: WAIT });
    expect(true, "the teacher turns the slow mode on, and the student is told");
    await rui.frame.locator("[data-slow-mode]").selectOption("0");
    await ana.frame.locator("[data-chat-slow]").waitFor({ state: "hidden", timeout: WAIT });

    await tool(rui.frame, "Bom dia, turma!", "[data-chat-mute]");
    await ana.frame.locator("[data-chat-muted]:not([hidden])").waitFor({ timeout: WAIT });
    expect(await ana.frame.locator("[data-chat-form]").isHidden(), "the teacher mutes the student, whose box goes away");
    await rui.frame.locator("[data-muted-list] [data-unmute]").click();
    await ana.frame.locator("[data-chat-form]").waitFor({ state: "visible", timeout: WAIT });
    expect(true, "...and lets them talk again");
    await shot(rui.page, "integration-teacher");

    // ------------------------------------------------ another class
    const bia = await lesson(browser, errors, "bia");
    const title = bia.frame.locator("[data-awaiting-title]");
    await title.waitFor();
    const before = await title.textContent();
    await bia.frame.locator("[data-awaiting-title]").filter({ hasNotText: before }).waitFor({ timeout: WAIT });
    expect(await bia.frame.locator("[data-chat-form]").isHidden() && await bia.frame.locator(".chat-msg").count() === 0,
      "a student of another class is kept out of the live");
    await shot(bia.page, "integration-other-class");
    await bia.context.close();

    // ------------------------------------------------ single-page sites, sites not listed
    const spa = await lesson(browser, errors, "rui", "spa");
    await signedIn(spa.frame, "Prof. Rui");
    expect(true, "a lesson drawn after the page loaded is picked up by itself (single-page sites)");
    await spa.context.close();

    // The same lesson on an address that is not among the integration's
    // sites: the browser refuses the frame (and says so in the console), and
    // the script explains instead of leaving a blank box.
    const elsewhere = await browser.newContext({ viewport: { width: 1280, height: 860 }, ignoreHTTPSErrors: INSECURE });
    const stranger = await elsewhere.newPage();
    await stranger.goto(`http://localhost:${PORT}/aula?who=rui`);
    await stranger.locator("[data-qqplay-problem]").waitFor({ timeout: WAIT });
    expect(await stranger.locator("iframe").count() === 0, "a site that is not listed gets an explanation, not a blank frame");
    await shot(stranger, "integration-not-listed");
    await elsewhere.close();

    // ------------------------------------------------ attendance
    if (onAir) {
      const report = await api(state, "GET", `lives/${state.live}/attendance/`);
      const students = (report.data.attendance || []).map((row) => row.student).sort();
      expect(students.join() === [PEOPLE.ana.id, PEOPLE.rui.id].sort().join(),
        "the attendance lists the student and the teacher, with their ids at the school");
    }

    // ------------------------------------------------ a video and its comments
    const anaVideo = await lesson(browser, errors, "ana", "video");
    const anaForm = anaVideo.frame.locator("[data-comment-form]");
    await anaForm.waitFor({ state: "visible", timeout: WAIT });
    await anaForm.locator("textarea").fill("Ótima revisão, obrigada!");
    await anaForm.locator('button[type="submit"]').click();
    await anaVideo.frame.locator("[data-comment].is-mine").filter({ hasText: "Ótima revisão" }).waitFor();
    expect(true, "the student comments on a video of the library");

    const ruiVideo = await lesson(browser, errors, "rui", "video");
    const comment = ruiVideo.frame.locator("[data-comment]").filter({ hasText: "Ótima revisão" }).first();
    await comment.waitFor({ timeout: WAIT });
    await comment.locator("[data-reply]").first().click();
    const ruiForm = ruiVideo.frame.locator("[data-comment-form]");
    await ruiForm.locator("textarea").fill("Bons estudos!");
    await ruiForm.locator('button[type="submit"]').click();
    const reply = ruiVideo.frame.locator(".comment-replies [data-comment]").filter({ hasText: "Bons estudos!" });
    await reply.waitFor();
    expect(await reply.locator(".chat-host").textContent() === "Streaming", "the teacher replies, with the badge");
    await shot(ruiVideo.page, "integration-video");
    await comment.locator("[data-hide-comment]").first().click();
    await ruiVideo.frame.locator("[data-comment]").filter({ hasText: "Ótima revisão" }).waitFor({ state: "detached", timeout: WAIT });
    await anaVideo.page.reload();
    await anaVideo.frame.locator("[data-comment-form]").waitFor({ state: "visible", timeout: WAIT });
    expect(await anaVideo.frame.locator("[data-comment]").filter({ hasText: "Ótima revisão" }).count() === 0,
      "the teacher hides the comment, for everyone");
    await anaVideo.context.close();
    await ruiVideo.context.close();

    // ------------------------------------------------ inside an LMS (LTI 1.3)
    if (LTI) {
      await studio.goto(detailUrl);
      await studio.locator("#lms details summary").click();
      const lmsForm = studio.locator('#lms form', { has: studio.locator('input[name="issuer"]') });
      await lmsForm.locator('input[name="name"]').fill("Moodle de teste");
      await lmsForm.locator('input[name="issuer"]').fill(LMS_ISSUER);
      await lmsForm.locator('input[name="client_id"]').fill(LMS.client);
      await lmsForm.locator('input[name="auth_login_url"]').fill(`${SCHOOL}/lms/auth`);
      await lmsForm.locator('input[name="jwks_url"]').fill(`${SCHOOL}/lms/jwks`);
      await Promise.all([studio.waitForNavigation(), lmsForm.locator('button[type="submit"]').click()]);
      expect(await studio.locator("#lms .enrol-list li").filter({ hasText: LMS_ISSUER }).count() === 1, "the studio registers an LMS for the integration");

      const course = async (who, path = "course") => {
        const context = await browser.newContext({ viewport: { width: 1280, height: 860 }, ignoreHTTPSErrors: INSECURE });
        const page = await context.newPage();
        watchErrors(page, errors);
        await page.goto(`${SCHOOL}/lms/${path}?who=${who}`);
        return { context, page, tool: page.frameLocator('iframe[name="tool"]') };
      };

      const anaLms = await course("ana");
      await signedIn(anaLms.tool, "Ana do Moodle");
      expect(true, "a student opens the activity in the LMS and is signed in by it: no token, no code");
      await anaLms.tool.locator("[data-chat-form] textarea").fill("Vim pelo Moodle!");
      await anaLms.tool.locator('[data-chat-form] button[type="submit"]').click();
      await anaLms.tool.locator(".chat-list .chat-msg").filter({ hasText: "Vim pelo Moodle!" }).waitFor();
      expect(true, "...and talks in the chat of the private live of their course");
      await shot(anaLms.page, "integration-lms");

      const ruiLms = await course("rui");
      await signedIn(ruiLms.tool, "Prof. Rui do Moodle");
      await ruiLms.tool.locator("[data-chat-mod]:not([hidden])").waitFor({ timeout: WAIT });
      expect(true, "a teacher in the LMS is a moderator here");

      const biaLms = await course("bia");
      const closed = biaLms.tool.locator("[data-awaiting-title]");
      await closed.waitFor({ timeout: WAIT });
      await biaLms.tool.locator("[data-awaiting-text]").filter({ hasNotText: /^$/ }).first().waitFor({ timeout: WAIT });
      await biaLms.page.waitForTimeout(6000);
      expect(await biaLms.tool.locator(".chat-msg").count() === 0 && await biaLms.tool.locator("[data-chat-form]").isHidden(),
        "a student of another course is kept out of the class's live");
      await biaLms.context.close();

      const adding = await course("rui", "add");
      await adding.tool.locator(`input[name="item"][value="live:${state.live}"]`).check({ timeout: WAIT });
      await adding.tool.locator('button[type="submit"]').click();
      await adding.tool.locator("#added").waitFor({ timeout: WAIT });
      expect((await adding.tool.locator("#added").textContent()) === `ADDED ltiResourceLink ${state.live}`,
        "the teacher picks the live in the content selection, and the LMS accepts the tool's signed answer");
      await adding.context.close();
      await anaLms.context.close();
      await ruiLms.context.close();
    }

    // ------------------------------------------------ a school with no server of its own
    if (MAIL_DIR) {
      const bia = `bia-${stamp}@escola.example.com`;
      await studio.goto(detailUrl);
      const options = studio.locator('#enrolments form', { has: studio.locator('input[name="email_login"]') });
      await options.locator('input[name="email_login"]').check();
      await Promise.all([studio.waitForNavigation(), options.locator('button[type="submit"]').click()]);
      const enrol = studio.locator("#enrolments form.enrol-form");
      await enrol.locator('input[name="email"]').fill(bia);
      await enrol.locator('input[name="name"]').fill("Bia Castro");
      await enrol.locator('input[name="classes"]').fill("turma-a");
      await Promise.all([studio.waitForNavigation(), enrol.locator('button[type="submit"]').click()]);
      expect(await studio.locator("#enrolments .enrol-list li").filter({ hasText: bia }).count() === 1,
        "the studio turns the sign-in by e-mail on and enrols a student");
      const hookUrl = await studio.inputValue("#hook-url");

      // The class link, opened as a page: no school site around it.
      const alone = await browser.newContext({ viewport: { width: 1280, height: 860 }, ignoreHTTPSErrors: INSECURE });
      const room = await alone.newPage();
      watchErrors(room, errors);
      await room.goto(`${BASE}/sala/${state.key}/${state.live}/`);
      const door = room.locator("[data-email-login]:visible").first();
      await door.waitFor({ timeout: WAIT });
      expect(await room.locator(".chat-msg").count() === 0, "the class link shows a private live's closed door and asks for the e-mail");
      const asked = Date.now() - 1000;
      await door.locator('input[name="email"]').fill(bia);
      await door.locator('[data-login-step="email"] button[type="submit"]').click();
      await door.locator('input[name="code"]').waitFor({ state: "visible", timeout: WAIT });
      await door.locator('input[name="code"]').fill(await codeFromMail(asked));
      await door.locator('[data-login-step="code"] button[type="submit"]').click();
      await room.locator("[data-chat-who] strong").filter({ hasText: "Bia Castro" }).waitFor({ timeout: WAIT });
      expect(true, "the code from the e-mail signs the student in, by name");
      await room.locator("[data-chat-form] textarea").fill("Entrei pelo link!");
      await room.locator('[data-chat-form] button[type="submit"]').click();
      await room.locator(".chat-list .chat-msg").filter({ hasText: "Entrei pelo link!" }).waitFor();
      expect(true, "...and they talk in the chat");
      await shot(room, "integration-class-link");

      await room.reload();
      await room.locator("[data-chat-who] strong").filter({ hasText: "Bia Castro" }).waitFor({ timeout: WAIT });
      expect(await room.locator("[data-email-login]:visible").count() === 0, "a reload keeps them signed in: no new code");

      // The sales platform posts a refund: the access ends.
      const refund = await fetch(hookUrl, { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ event: "PURCHASE_REFUNDED", data: { buyer: { email: bia } } }) });
      expect(refund.status === 200 && (await refund.json()).result === "removed", "a refund posted by the sales platform ends the access");
      await room.locator("[data-email-login]:visible").first().waitFor({ timeout: WAIT });
      expect(true, "...and the student is back at the door");

      const bought = await fetch(`${hookUrl}?classes=turma-a`, { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ event: "PURCHASE_APPROVED", data: { buyer: { email: `leo-${stamp}@escola.example.com`, name: "Leo Park" } } }) });
      const listed = await api(state, "GET", `enrolments/?q=leo-${stamp}`);
      expect((await bought.json()).result === "added" && listed.data.total === 1 && listed.data.enrolments[0].classes[0] === "turma-a",
        "an approved purchase enrols the buyer in the product's class");
      await alone.close();
    }

    // ------------------------------------------------ blocking and erasing
    const blocked = await api(state, "POST", `students/${PEOPLE.ana.id}/block/`);
    expect(blocked.status === 200 && blocked.data.blocked, "the school's server blocks the student");
    const refused = ana.page.waitForResponse((response) => response.url().includes("/embed/api/session/"));
    await ana.page.reload();
    expect((await refused).status() === 403, "...who can no longer sign in through the school");
    await ana.frame.locator("[data-chat-form]").waitFor({ state: "hidden" });
    const erased = await api(state, "DELETE", `students/${PEOPLE.ana.id}/`);
    expect(erased.status === 200 && (await api(state, "POST", `students/${PEOPLE.ana.id}/block/`)).status === 404,
      "the school's server erases the student's data");
    await ana.context.close();
    await rui.context.close();

    // ------------------------------------------------ the end
    if (onAir) await hook("publish-done", state);
    const ended = await api(state, "POST", `lives/${state.live}/end/`);
    expect(ended.status === 200 && ended.data.status === "ended", "the school's server ends the live");
    expect((await api(state, "DELETE", `lives/${state.live}/`)).status === 200, "...and deletes it");
    state.live = "";

    await studio.goto(detailUrl);
    await studio.locator('form[action$="/delete/"] button[type="submit"]').click();
    await confirmIn(studio);
    await studio.waitForURL(`${BASE}/studio/integrations/`);
    expect(true, "the creator deletes the integration");
    state.key = "";

    if (errors.length) fail(`errors in the pages:\n${errors.join("\n")}`);
    ok("no errors in any page");
  } finally {
    if (state.live && state.key) {
      if (onAir) await hook("publish-done", state).catch(() => {});
      await api(state, "POST", `lives/${state.live}/end/`).catch(() => {});
      await api(state, "DELETE", `lives/${state.live}/`).catch(() => {});
    }
    await browser.close();
    server.close();
  }
}

console.log(`QQplay ⇄ school integration at ${BASE}, the school at ${SCHOOL}`);
main().then(
  () => console.log(`\n${step} checks passed.`),
  (error) => {
    console.error(`\n✗ ${error.message}`);
    process.exit(1);
  },
);
