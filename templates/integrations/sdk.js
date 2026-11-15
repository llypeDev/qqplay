{% autoescape off %}/* QQplay on your site: lives and their chat, with your own sign-in.
 *
 *   <div data-qqplay-live="LIVE_ID" data-layout="both" data-token="TOKEN"></div>
 *   <div data-qqplay-video="VIDEO_ID" data-layout="both" data-token="TOKEN"></div>
 *   <script src="{{ origin }}/embed/sdk.js" async></script>
 *
 * data-qqplay-live   a live's id (the end of its address on QQplay)
 * data-qqplay-video  or a video of the library, with its comments
 * data-layout        both (the player with the chat or the comments, the
 *                    default), player, or chat (the comments, for a video)
 * data-token         the token your server signs for the person signed in
 * data-token-url     or an address on your site that answers {"token": "..."}
 *                    for the person signed in; the script asks it again when
 *                    a session runs out, so long classes need nothing else
 * data-school        your integration's key (sch_...). Only needed when the
 *                    token comes later (QQplay.setToken) or does not name the
 *                    integration in its header (kid)
 * data-email         for platforms that sign no tokens: the e-mail of the
 * data-name          person signed in, as the platform's page writes it
 *                    (a merge tag). Taken only when the integration trusts
 *                    it, for enrolled students; needs data-school
 * data-theme         dark or light (the person's system setting otherwise)
 * data-lang          the interface language: pt-br or en (QQplay's own
 *                    otherwise)
 * data-height        the chat's height in pixels, for data-layout="chat"
 * data-loading       lazy, to load the frame only when it is about to be seen
 *
 * Without a token the live shows and its chat can be read, not written to.
 * The token reaches the frame in a message to QQplay's own origin, never in
 * an address, so it is not left in histories or server logs.
 *
 * Elements added to the page later (a single-page site changing lessons) are
 * picked up by themselves, and the ones taken away are cleaned up.
 *
 * From your own script: QQplay.mount(element, {live, video, layout, token,
 * tokenUrl, school, email, name, theme, lang, height}), QQplay.setToken(element, token)
 * and QQplay.unmount(element). The element receives these events:
 *   qqplay:signed-in   {name}             the person may write now
 *   qqplay:signed-out                     their session is over
 *   qqplay:status      {status}           scheduled, live, interrupted, ended
 *   qqplay:error       {message, code}    code: token, origin, blocked, rate,
 *                                         network, or frame when the frame
 *                                         itself could not be shown
 */
(function () {
  "use strict";

  var ORIGIN = "{{ origin|escapejs }}";
  if (window.QQplay && window.QQplay.origin === ORIGIN) {
    window.QQplay.mountAll();
    return;
  }

  var CHAT_WIDTH = 340;
  var STACK_BELOW = 720;
  var STACKED_CHAT = 480;
  /* How long a frame that has loaded may take to say so, before we conclude
     the browser refused to show it. */
  var READY_WAIT = 5000;
  /* How long the frame may take to load at all. */
  var LOAD_WAIT = 25000;
  var RENEWALS_A_MINUTE = 4;
  var SELECTOR = "[data-qqplay-live], [data-qqplay-video]";
  var TEXT = {
    blocked: "{{ text.blocked|escapejs }}",
    blockedHint: "{{ text.blocked_hint|escapejs }}",
    offline: "{{ text.offline|escapejs }}",
    sandboxHint: "{{ text.sandbox_hint|escapejs }}",
  };
  var widgets = [];

  function part(token, index) {
    try {
      var value = String(token || "").split(".")[index].replace(/-/g, "+").replace(/_/g, "/");
      while (value.length % 4) value += "=";
      var data = JSON.parse(atob(value));
      return data && typeof data === "object" ? data : {};
    } catch (e) {
      return {};
    }
  }

  function keyOf(token) {
    // The integration's key travels in the token's header (kid), or as its
    // issuer (iss).
    var kid = part(token, 0).kid;
    if (typeof kid === "string" && kid) return kid;
    var iss = part(token, 1).iss;
    return typeof iss === "string" ? iss : "";
  }

  function emit(widget, name, detail) {
    widget.el.dispatchEvent(new CustomEvent(name, { bubbles: true, detail: detail || {} }));
  }

  function Widget(el, options) {
    this.el = el;
    this.live = String(options.live || "");
    this.video = String(options.video || "");
    var layout = options.layout === "comments" ? "chat" : options.layout;
    this.layout = ["both", "player", "chat"].indexOf(layout) >= 0 ? layout : "both";
    this.theme = options.theme === "dark" || options.theme === "light" ? options.theme : "";
    this.lang = /^[a-zA-Z-]{2,12}$/.test(options.lang || "") ? String(options.lang).toLowerCase() : "";
    this.height = Number(options.height) || 600;
    this.school = String(options.school || "");
    this.email = String(options.email || "");
    this.name = String(options.name || "");
    this.lazy = options.loading === "lazy";
    this.token = options.token || "";
    this.tokenUrl = options.tokenUrl || "";
    this.frame = null;
    this.frameKey = "";
    this.ready = false;
    this.alive = true;
    this.renewals = [];
    var self = this;
    if (this.token || !this.tokenUrl) {
      this.build();
    } else {
      this.fetchToken().then(function (token) {
        if (!self.alive) return;
        self.build();
        // The site's address for tokens did not answer: try it again shortly.
        if (!token) self.retryToken(1);
      });
    }
  }

  Widget.prototype.key = function () {
    return this.school || keyOf(this.token);
  };

  Widget.prototype.fetchToken = function () {
    var self = this;
    if (!this.tokenUrl) return Promise.resolve("");
    return fetch(this.tokenUrl, { credentials: "include", headers: { Accept: "application/json" } })
      .then(function (response) { return response.ok ? response.json() : {}; })
      .then(function (data) {
        self.token = (data && data.token) || "";
        return self.token;
      })
      .catch(function () { return ""; });
  };

  Widget.prototype.retryToken = function (attempt) {
    var self = this;
    if (attempt > 3) return;
    window.setTimeout(function () {
      if (!self.alive || self.token) return;
      self.fetchToken().then(function (token) {
        if (token) self.setToken(token);
        else self.retryToken(attempt + 1);
      });
    }, attempt * 4000);
  };

  Widget.prototype.build = function () {
    var params = ["layout=" + (this.video && this.layout === "chat" ? "comments" : this.layout), "sdk=1"];
    var key = this.key();
    if (key) params.push("school=" + encodeURIComponent(key));
    if (this.theme) params.push("theme=" + this.theme);
    if (this.lang) params.push("lang=" + encodeURIComponent(this.lang));
    var frame = document.createElement("iframe");
    frame.src = this.video
      ? ORIGIN + "/embed/" + encodeURIComponent(this.video) + "/?" + params.join("&")
      : ORIGIN + "/embed/live/" + encodeURIComponent(this.live) + "/?" + params.join("&");
    frame.title = this.video ? "QQplay video" : this.layout === "chat" ? "QQplay chat" : "QQplay live";
    frame.allow = "autoplay; fullscreen; picture-in-picture";
    frame.setAttribute("allowfullscreen", "");
    // A live is what the page is for: it loads at once unless asked not to.
    if (this.lazy) frame.setAttribute("loading", "lazy");
    frame.style.cssText = "display:block;width:100%;border:0;border-radius:12px;background:#05070c;";
    this.el.textContent = "";
    this.el.appendChild(frame);
    this.frame = frame;
    this.frameKey = key;
    this.ready = false;
    this.loaded = false;
    this.resize();
    var self = this;
    if (!this.observer) {
      if (typeof ResizeObserver === "function") {
        this.observer = new ResizeObserver(function () { self.resize(); });
        this.observer.observe(this.el);
      } else {
        this.onResize = function () { self.resize(); };
        window.addEventListener("resize", this.onResize);
      }
    }
    this.watch(frame);
  };

  /* A frame the browser refuses to show (this site is not among the
     integration's sites, or a policy of the page forbids frames) loads an
     error page that never speaks. Say so, instead of leaving a blank box. */
  Widget.prototype.watch = function (frame) {
    var self = this;
    window.clearTimeout(this.watchdog);
    function fail(text, hint) {
      if (!self.alive || self.frame !== frame || self.loaded) return;
      self.problem(text, hint);
    }
    frame.addEventListener("load", function () {
      window.clearTimeout(self.watchdog);
      self.watchdog = window.setTimeout(function () {
        // A page with no origin of its own is a sandboxed one.
        fail(TEXT.blocked, window.location.origin === "null" ? TEXT.sandboxHint : TEXT.blockedHint);
      }, READY_WAIT);
    });
    if (!this.lazy) this.watchdog = window.setTimeout(function () { fail(TEXT.offline, ""); }, LOAD_WAIT);
  };

  Widget.prototype.problem = function (text, hint) {
    var box = document.createElement("div");
    box.setAttribute("role", "alert");
    box.setAttribute("data-qqplay-problem", "");
    box.style.cssText = "box-sizing:border-box;display:flex;flex-direction:column;justify-content:center;gap:6px;" +
      "min-height:180px;padding:24px;border-radius:12px;background:#0b0f17;color:#e8eef6;text-align:center;" +
      "font:15px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;";
    var title = document.createElement("strong");
    title.textContent = text;
    box.appendChild(title);
    if (hint) {
      var more = document.createElement("span");
      more.style.cssText = "color:#8794a8;font-size:13px;";
      more.textContent = window.location.origin === "null" ? hint : hint + " (" + window.location.origin + ")";
      box.appendChild(more);
    }
    if (this.frame) this.frame.remove();
    this.frame = null;
    this.el.textContent = "";
    this.el.appendChild(box);
    if (window.console && console.warn) {
      console.warn("[QQplay] " + text + " " + hint + " Page origin: " + window.location.origin);
    }
    emit(this, "qqplay:error", { message: text, code: "frame" });
  };

  Widget.prototype.resize = function () {
    if (!this.frame) return;
    var width = this.el.clientWidth || this.frame.clientWidth || 640;
    var height;
    if (this.layout === "player") height = width * 9 / 16;
    else if (this.layout === "chat") height = this.height;
    else if (width >= STACK_BELOW) height = Math.max(360, (width - CHAT_WIDTH) * 9 / 16);
    else height = width * 9 / 16 + STACKED_CHAT;
    this.frame.style.height = Math.round(height) + "px";
  };

  Widget.prototype.send = function () {
    if (!this.frame || !this.frame.contentWindow || !this.ready) return;
    if (this.token) {
      this.frame.contentWindow.postMessage({ type: "qqplay:token", token: this.token }, ORIGIN);
    } else if (this.email) {
      // No token: the page's word for who is there.
      this.frame.contentWindow.postMessage({ type: "qqplay:identity", email: this.email, name: this.name }, ORIGIN);
    }
  };

  Widget.prototype.setToken = function (token) {
    this.token = token || "";
    // The frame was made before the integration was known: make it again,
    // this time for the school (its private lives, its sites).
    if (this.frame && this.key() && this.key() !== this.frameKey) this.build();
    else this.send();
  };

  Widget.prototype.renew = function () {
    var self = this;
    var now = Date.now();
    this.renewals = this.renewals.filter(function (at) { return now - at < 60000; });
    if (!this.tokenUrl || this.renewals.length >= RENEWALS_A_MINUTE) {
      emit(this, "qqplay:signed-out");
      return;
    }
    this.renewals.push(now);
    this.fetchToken().then(function (token) {
      if (token) self.setToken(token);
      else emit(self, "qqplay:signed-out");
    });
  };

  Widget.prototype.destroy = function () {
    this.alive = false;
    window.clearTimeout(this.watchdog);
    if (this.observer) this.observer.disconnect();
    if (this.onResize) window.removeEventListener("resize", this.onResize);
    if (this.frame) this.frame.remove();
    this.frame = null;
  };

  window.addEventListener("message", function (event) {
    if (event.origin !== ORIGIN || !event.data || typeof event.data.type !== "string") return;
    var widget = null;
    for (var i = 0; i < widgets.length; i++) {
      if (widgets[i].frame && widgets[i].frame.contentWindow === event.source) widget = widgets[i];
    }
    if (!widget) return;
    var data = event.data;
    // Any word from the frame means the browser is showing it.
    widget.loaded = true;
    window.clearTimeout(widget.watchdog);
    if (data.type === "qqplay:ready") {
      widget.ready = true;
      widget.send();
    } else if (data.type === "qqplay:token-needed") {
      widget.renew();
    } else if (data.type === "qqplay:signed-in") {
      emit(widget, "qqplay:signed-in", { name: data.name || "" });
    } else if (data.type === "qqplay:status") {
      emit(widget, "qqplay:status", { status: data.status || "" });
    } else if (data.type === "qqplay:error") {
      emit(widget, "qqplay:error", { message: data.message || "", code: data.code || "" });
    }
  });

  function fromAttributes(el) {
    return {
      live: el.getAttribute("data-qqplay-live"),
      video: el.getAttribute("data-qqplay-video"),
      layout: el.getAttribute("data-layout"),
      theme: el.getAttribute("data-theme"),
      lang: el.getAttribute("data-lang"),
      height: el.getAttribute("data-height"),
      school: el.getAttribute("data-school"),
      email: el.getAttribute("data-email"),
      name: el.getAttribute("data-name"),
      loading: el.getAttribute("data-loading"),
      token: el.getAttribute("data-token"),
      tokenUrl: el.getAttribute("data-token-url"),
    };
  }

  function find(el) {
    for (var i = 0; i < widgets.length; i++) if (widgets[i].el === el) return widgets[i];
    return null;
  }

  var api = {
    origin: ORIGIN,
    version: 2,
    mount: function (el, options) {
      if (!el || find(el)) return find(el);
      var widget = new Widget(el, options || fromAttributes(el));
      widgets.push(widget);
      return widget;
    },
    mountAll: function () {
      var nodes = document.querySelectorAll(SELECTOR);
      for (var i = 0; i < nodes.length; i++) api.mount(nodes[i]);
    },
    setToken: function (el, token) {
      var widget = find(el);
      if (widget) widget.setToken(token);
    },
    unmount: function (el) {
      var widget = find(el);
      if (!widget) return;
      widget.destroy();
      widgets.splice(widgets.indexOf(widget), 1);
    },
  };
  window.QQplay = api;

  /* Sites that change page without loading one (React, Vue, Next…) add the
     lesson's element after this script ran and take it away later. */
  function sweep() {
    for (var i = widgets.length - 1; i >= 0; i--) {
      if (!document.documentElement.contains(widgets[i].el)) api.unmount(widgets[i].el);
    }
    api.mountAll();
  }

  function start() {
    api.mountAll();
    if (typeof MutationObserver !== "function" || !document.documentElement) return;
    var pending = 0;
    new MutationObserver(function () {
      if (pending) return;
      pending = window.setTimeout(function () {
        pending = 0;
        sweep();
      }, 50);
    }).observe(document.documentElement, { childList: true, subtree: true });
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start);
  else start();
})();
{% endautoescape %}
