/* Signing in to a QQplay frame without a token from the site.

   For schools that run no server of their own (a members area, a course
   platform), the frame signs people in itself:

   - with a code sent to their e-mail, if it is on the integration's list of
     enrolments. The session is kept in this browser, so the code is asked
     for once, not at every class;
   - or as whoever the platform's page says is there (the e-mail a members
     area writes into the embed code), when the integration trusts that.

   The live page (live.js) and the video page (embed-comments.js) start this
   with QQplayLogin.boot and are handed the session through onSession. */
(function () {
  "use strict";

  function gettextSafe(text) {
    return typeof window.gettext === "function" ? window.gettext(text) : text;
  }

  function boot(options) {
    var root = options.root;
    var school = root.getAttribute("data-school") || "";
    var startUrl = root.getAttribute("data-login-start-url") || "";
    var verifyUrl = root.getAttribute("data-login-verify-url") || "";
    var declaredUrl = root.getAttribute("data-login-declared-url") || "";
    var lang = root.getAttribute("data-lang") || "";
    var forms = Array.prototype.slice.call(root.querySelectorAll("[data-email-login]"));
    var key = "qqplay:session:" + school;
    var email = "";
    var busy = false;
    var signedIn = false;
    var _ = gettextSafe;

    function stored() {
      try {
        var data = JSON.parse(window.localStorage.getItem(key) || "null");
        if (data && data.session && data.until > Date.now()) return data;
      } catch (e) { /* storage blocked, or something else in it */ }
      return null;
    }

    function remember(payload) {
      try {
        window.localStorage.setItem(key, JSON.stringify({
          session: payload.session,
          user: payload.user,
          until: Date.now() + (payload.expires_in || 0) * 1000 - 60000,
        }));
      } catch (e) { /* storage blocked: the code is asked for again next time */ }
    }

    function forget() {
      try { window.localStorage.removeItem(key); } catch (e) { /* nothing kept */ }
    }

    function post(url, body) {
      var headers = { "Content-Type": "application/json", Accept: "application/json" };
      if (lang) headers["X-QQplay-Lang"] = lang;
      return fetch(url, { method: "POST", credentials: "omit", headers: headers, body: JSON.stringify(body) })
        .then(function (response) {
          return response.json().catch(function () { return {}; }).then(function (payload) {
            if (!response.ok) {
              var error = new Error(payload.detail || _("Something went wrong. Try again."));
              error.code = payload.code || "";
              throw error;
            }
            return payload;
          });
        });
    }

    function each(selector, fn) {
      forms.forEach(function (form) {
        Array.prototype.forEach.call(form.querySelectorAll(selector), fn);
      });
    }

    function show(step, note, bad) {
      if (!startUrl || signedIn) return;
      // One form at a time: the one on the closed door of a private live or
      // video while the door is there, the one by the chat otherwise.
      var here = forms.filter(function (form) { return root.contains(form); });
      var doors = here.filter(function (form) { return !!form.closest("[data-awaiting-screen]"); });
      here.forEach(function (form) {
        form.hidden = doors.length > 0 && doors.indexOf(form) < 0;
        form.setAttribute("data-step", step);
      });
      each("[data-login-step]", function (node) { node.hidden = node.getAttribute("data-login-step") !== step; });
      each("[data-login-note]", function (node) {
        node.textContent = note || "";
        node.hidden = !note;
        node.classList.toggle("is-bad", !!bad);
      });
      each('input[name="email"]', function (input) { if (email && !input.value) input.value = email; });
      // The closed door of a private live or video says what to do now.
      Array.prototype.forEach.call(root.querySelectorAll("[data-awaiting-title]"), function (node) {
        node.textContent = _("Sign in to watch");
      });
      Array.prototype.forEach.call(root.querySelectorAll("[data-school-note], [data-comments-signin]"), function (node) {
        node.hidden = true;
      });
    }

    function hide() {
      forms.forEach(function (form) { form.hidden = true; });
    }

    function setBusy(value) {
      busy = value;
      forms.forEach(function (form) { form.classList.toggle("is-busy", value); });
    }

    function done(payload, keep) {
      signedIn = true;
      if (keep) remember(payload);
      hide();
      options.onSession(payload.session, payload.user);
    }

    function askCode(form) {
      var input = form.querySelector('input[name="email"]');
      email = (input && input.value || "").trim();
      if (!email || busy) return;
      setBusy(true);
      post(startUrl, { school: school, email: email })
        .then(function () {
          show("code", _("If this e-mail is enrolled, a code is on its way to it. Type it here."));
          each('input[name="code"]', function (code) { code.value = ""; });
          var first = form.querySelector('input[name="code"]');
          if (first) first.focus();
        })
        .catch(function (error) { show("email", error.message, true); })
        .then(function () { setBusy(false); });
    }

    function checkCode(form) {
      var input = form.querySelector('input[name="code"]');
      var code = (input && input.value || "").replace(/\D/g, "");
      if (code.length !== 6 || busy) {
        if (!busy) show("code", _("The code has six digits."), true);
        return;
      }
      setBusy(true);
      post(verifyUrl, { school: school, email: email, code: code })
        .then(function (payload) { done(payload, true); })
        .catch(function (error) { show("code", error.message, true); })
        .then(function () { setBusy(false); });
    }

    forms.forEach(function (form) {
      form.addEventListener("submit", function (event) {
        event.preventDefault();
        if (form.getAttribute("data-step") === "code") checkCode(form);
        else askCode(form);
      });
      Array.prototype.forEach.call(form.querySelectorAll("[data-login-back]"), function (button) {
        button.addEventListener("click", function () { show("email"); });
      });
    });

    var api = {
      /* Whether someone can sign in here by e-mail (the page then waits for
         them instead of sending them away). */
      byEmail: !!startUrl,
      hide: hide,
      /* The frame's session was refused: forget it and ask again. */
      expired: function () {
        signedIn = false;
        forget();
        show("email", _("Your session is over. Sign in again."));
      },
      /* Someone signed in another way (a token from the site). */
      signedIn: function () {
        signedIn = true;
        hide();
      },
      /* The platform's page says who is there. */
      declare: function (given, name, origin, direct) {
        given = String(given || "").trim();
        if (!declaredUrl || signedIn || busy || !given || given.indexOf("@") < 0) return;
        email = given;
        setBusy(true);
        post(declaredUrl, { school: school, email: given, name: String(name || ""), origin: origin || "", direct: !!direct })
          .then(function (payload) { done(payload, false); })
          .catch(function (error) {
            // A teacher, or someone the page's word is not enough for: by code.
            if (startUrl) show("email", error.code === "code" ? error.message : "", false);
            else if (options.onProblem) options.onProblem(error);
          })
          .then(function () { setBusy(false); });
      },
    };

    var saved = stored();
    if (saved) {
      signedIn = true;
      hide();
      window.setTimeout(function () { options.onSession(saved.session, saved.user); }, 0);
    } else if (startUrl) {
      // A site that signs its people in with tokens gets a moment to do it
      // before the form shows.
      window.setTimeout(function () {
        if (!signedIn && !busy && !(options.busy && options.busy())) show("email");
      }, options.delay || 0);
    }
    return api;
  }

  window.QQplayLogin = { boot: boot };
})();
