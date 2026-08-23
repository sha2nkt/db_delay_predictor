"use strict";
/* Login / create account for the stories board, passwordless: both forms end
   with "check your inbox" - the session only starts when the emailed magic
   link is clicked (see verify.html). Logging in with an address that has no
   account says so, rather than promising a mail that would never arrive.

   One page, three views (login, create, code) and exactly one visible at a
   time: showing both forms at once turns arriving here into a decision to
   make rather than a field to fill, and returning visitors - the majority -
   only ever want the first one. The other is one click away in the card's
   footer, and lives at #create so Back works and it can be linked directly. */
(function () {

const I18N = {
  de: {
    docTitle: "Anmelden – DelayBahn",
    createDocTitle: "Konto erstellen – DelayBahn",
    tagline: "Ein Name für alle deine Geschichten",
    loginHeading: "Anmelden",
    createHeading: "Konto erstellen",
    username: "Benutzername",
    email: "E-Mail-Adresse",
    loginNote: "Kein Passwort nötig: Du bekommst einen Anmelde-Link per E-Mail.",
    loginBtn: "Link senden",
    createBtn: "Konto erstellen",
    createNote: "Deine E-Mail-Adresse dient nur der Anmeldung per Link – sie bleibt privat, und Werbung gibt es keine. Öffentlich erscheint nur dein Benutzername. Nach der Bestätigung lässt er sich nicht mehr ändern. Gibt es zu dieser Adresse schon ein Konto, melden wir dich einfach damit an. Unbestätigte Konten werden nach 7 Tagen gelöscht.",
    nameRules: "2–25 Zeichen: Buchstaben, Zahlen, - und _",
    nameFree: "Guter Name! Er ist noch frei – er gehört ganz dir.",
    shuffleTitle: "Anderen Namen vorschlagen",
    errNameChars: "Nur Buchstaben, Zahlen, \"-\" und \"_\" sind erlaubt",
    errNameShort: "Der Benutzername braucht mindestens 2 Zeichen",
    footerStories: "← Zu den Bahnhofs-Geschichten",
    footerLegal: "Impressum & Datenschutz",
    sending: "Einen Moment …",
    sentLogin: "Schau in dein Postfach (auch im Spam-Ordner): Dein Anmelde-Link ist unterwegs.",
    sentRegister: "Schau in dein Postfach (auch im Spam-Ordner): Deine Bestätigungs-E-Mail ist unterwegs.",
    codeHeading: "Code eingeben",
    codeLabel: "6-stelliger Code aus der E-Mail",
    codeBtn: "Anmelden",
    codeSent: "Wir haben eine E-Mail an {email} geschickt. Klick auf den Link darin – oder gib hier den Code ein, wenn du die E-Mail auf einem anderen Gerät liest.",
    errCode: "Der Code ist falsch, abgelaufen oder wurde zu oft versucht. Fordere eine neue E-Mail an.",
    resendBtn: "E-Mail erneut senden",
    resendWait: "Erneut senden in {s} s",
    resent: "Neue E-Mail ist unterwegs – sie enthält einen neuen Code.",
    errNoAccount: "Zu dieser Adresse gibt es noch kein Konto – erstelle unten eins.",
    newHere: "Neu bei DelayBahn?",
    haveAccount: "Schon ein Konto?",
    errTaken: "Dieser Name ist schon vergeben.",
    errRate: "Zu viele Versuche – bitte warte kurz und versuch es erneut.",
    errName: "Name: 2–25 Zeichen, nur Buchstaben, Zahlen, - und _.",
    errGeneric: "Hat nicht geklappt – bitte später erneut versuchen.",
  },
  en: {
    docTitle: "Login – DelayBahn",
    createDocTitle: "Create account – DelayBahn",
    tagline: "One name for all your stories",
    loginHeading: "Login",
    createHeading: "Create account",
    username: "Username",
    email: "Email address",
    loginNote: "No password needed: you'll get a login link by email.",
    loginBtn: "Send link",
    createBtn: "Create account",
    createNote: "We only use your email address to log you in via a link – it stays private, and there is no marketing. Only your username is ever shown publicly. Once confirmed, it can't be changed. If this address already has an account, we'll simply log you into it. Unconfirmed accounts are deleted after 7 days.",
    nameRules: "2–25 characters: letters, digits, - and _",
    nameFree: "Great name! It's not taken so it's all yours.",
    shuffleTitle: "Suggest another name",
    errNameChars: "Username can only contain letters, numbers, \"-\", and \"_\"",
    errNameShort: "Username needs at least 2 characters",
    footerStories: "← Back to the station stories",
    footerLegal: "Legal notice & privacy",
    sending: "One moment …",
    sentLogin: "Check your inbox (and the spam folder): your login link is on its way.",
    sentRegister: "Check your inbox (and the spam folder): your confirmation email is on its way.",
    codeHeading: "Enter your code",
    codeLabel: "6-digit code from the email",
    codeBtn: "Log in",
    codeSent: "We sent an email to {email}. Click the link in it – or enter the code here if you're reading the email on another device.",
    errCode: "That code is wrong, expired, or was tried too many times. Request a new email.",
    resendBtn: "Resend email",
    resendWait: "Resend in {s} s",
    resent: "A new email is on its way – it carries a new code.",
    errNoAccount: "There's no account for this address yet – create one below.",
    newHere: "New to DelayBahn?",
    haveAccount: "Already have an account?",
    errTaken: "That name is already taken.",
    errRate: "Too many attempts – please wait a moment and try again.",
    errName: "Name: 2–25 characters, only letters, digits, - and _.",
    errGeneric: "That didn't work – please try again later.",
  },
};

let lang = "de";
try { if (localStorage.getItem("lang") === "en") lang = "en"; } catch (e) {}
const t = (key) => (I18N[lang][key] != null ? I18N[lang][key] : I18N.de[key]);
// no-op when the Umami script is blocked or unavailable; never the address
const track = (name, data) => window.umami?.track(name, data);

// exactly one of these is visible; the header and the tab title follow it
const VIEWS = {
  login:  { card: "login-card",    title: "loginHeading",  doc: "docTitle" },
  create: { card: "register-card", title: "createHeading", doc: "createDocTitle" },
  code:   { card: "code-card",     title: "codeHeading",   doc: "docTitle" },
};
let view = "login";

function showView(next) {
  view = next;
  Object.entries(VIEWS).forEach(([name, spec]) => {
    document.getElementById(spec.card).classList.toggle("hidden", name !== next);
  });
  applyStatic();  // the header and tab title are part of the view
}

/* Carry a typed address across the switch. Only into an empty field: someone
   who filled both and switched back meant what they typed on the other side. */
function carryEmail(fromId, toId) {
  const from = document.getElementById(fromId);
  const to = document.getElementById(toId);
  if (from.value.trim() && !to.value.trim()) to.value = from.value.trim();
}

// #create rather than a bare toggle: Back returns to the login form, and the
// create step can be linked to from elsewhere
const viewFromHash = () => (location.hash === "#create" ? "create" : "login");

document.getElementById("to-create").addEventListener("click", () => {
  carryEmail("login-email", "register-email");
  location.hash = "create";
});
document.getElementById("to-login").addEventListener("click", () => {
  carryEmail("register-email", "login-email");
  location.hash = "";
});
window.addEventListener("hashchange", () => {
  // the code step is past both forms; a stray hash must not drop out of it
  if (view !== "code") showView(viewFromHash());
});

function applyStatic() {
  document.documentElement.lang = lang;
  document.title = t(VIEWS[view].doc);
  document.getElementById("header-title").textContent = t(VIEWS[view].title);
  document.querySelectorAll("[data-i18n]").forEach((node) => {
    const text = I18N[lang][node.dataset.i18n];
    if (text != null) node.textContent = text;
  });
  document.querySelectorAll("[data-i18n-title]").forEach((node) => {
    const text = I18N[lang][node.dataset.i18nTitle];
    if (text != null) node.title = text;
  });
  document.querySelectorAll(".lang-btn").forEach((b) => {
    b.classList.toggle("active", b.dataset.lang === lang);
  });
  // glyph-only button: its accessible name has nowhere to live but here
  shuffleBtn.title = t("shuffleTitle");
  shuffleBtn.setAttribute("aria-label", t("shuffleTitle"));
  renderName();
  // the two lines built at runtime rather than from data-i18n
  if (pendingEmail) {
    document.getElementById("code-sent").textContent =
      t("codeSent").replace("{email}", pendingEmail);
  }
  renderResend();
}

document.querySelectorAll(".lang-btn").forEach((btn) => {
  btn.addEventListener("click", () => {
    if (btn.dataset.lang === lang) return;
    lang = btn.dataset.lang;
    try { localStorage.setItem("lang", lang); } catch (e) {}
    const wasUntouched = isVouched();
    applyStatic();
    if (wasUntouched) suggestName();
  });
});

// the address the pending code belongs to; consume-code needs it, since a
// code alone would be guessable against every account at once
let pendingEmail = null;

/* Resend, for the mail that never arrived. The link doubles as its own
   countdown - one control, two states - because a second always-live button
   next to "log in" would only invite clicking it before the mail lands.

   The server enforces the same wait per account (auth.RESEND_COOLDOWN_SECONDS,
   handed back as resend_after) and answers 202 whether or not it actually
   sent, so a resend that beat the cooldown would silently send nothing while
   we claimed otherwise. The countdown starts when that 202 lands - always
   after the server stamped the previous mail - so it can only ever be late. */
const RESEND_FALLBACK_SECONDS = 60;
const resendBtn = document.getElementById("resend-btn");
let resendUntil = 0;   // epoch ms; in the past means "offer it"
let resendTimer = null;

function renderResend() {
  const left = Math.ceil((resendUntil - Date.now()) / 1000);
  resendBtn.disabled = left > 0;
  resendBtn.textContent = left > 0
    ? t("resendWait").replace("{s}", left) : t("resendBtn");
  if (left <= 0 && resendTimer !== null) {
    clearInterval(resendTimer);
    resendTimer = null;
  }
}

// recomputed from a deadline rather than counted down, so a backgrounded tab
// (where the interval is throttled to once a minute) still comes back right
function startResendCooldown(seconds) {
  resendUntil = Date.now() + seconds * 1000;
  if (resendTimer === null) resendTimer = setInterval(renderResend, 1000);
  renderResend();
}

async function cooldownFrom(resp) {
  try {
    const seconds = Number((await resp.json()).resend_after);
    if (Number.isFinite(seconds) && seconds >= 0) return seconds;
  } catch (e) {}
  return RESEND_FALLBACK_SECONDS;  // an older server, or no body at all
}

function showCodeStep(email, cooldown) {
  pendingEmail = email;
  document.getElementById("code-sent").textContent =
    t("codeSent").replace("{email}", email);
  showView("code");
  startResendCooldown(cooldown);
  document.getElementById("code-input").focus();
}

resendBtn.addEventListener("click", async () => {
  if (!pendingEmail) return;
  const status = document.getElementById("code-status");
  const input = document.getElementById("code-input");
  resendBtn.disabled = true;
  status.classList.remove("sent");
  status.textContent = t("sending");
  try {
    // request-link, not register: the account exists by now either way, and
    // this is the one endpoint that never fails on a name that is taken
    const resp = await fetch("/api/auth/request-link", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email: pendingEmail, lang }),
    });
    if (!resp.ok) {
      status.textContent = resp.status === 404 ? t("errNoAccount")
        : resp.status === 429 ? t("errRate") : t("errGeneric");
      renderResend();  // nothing was spent; offer it again straight away
      return;
    }
    status.classList.add("sent");
    status.textContent = t("resent");
    track("login-resend");
    // the new mail voided the code the old one carried
    input.value = "";
    input.focus();
    startResendCooldown(await cooldownFrom(resp));
  } catch (e) {
    status.textContent = t("errGeneric");
    renderResend();
  }
});

function wire(formId, statusId, path, body, sentKey, errorFor) {
  const form = document.getElementById(formId);
  const status = document.getElementById(statusId);
  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const send = form.querySelector('[type="submit"]');
    send.disabled = true;
    status.textContent = t("sending");
    const sent = body();
    try {
      const resp = await fetch(path, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(sent),
      });
      if (!resp.ok) {
        status.textContent = errorFor(resp.status);
        send.disabled = false;
        return;
      }
      // stay disabled: the next step is the link or the code, not this form
      status.classList.add("sent");
      status.textContent = t(sentKey);
      track(sentKey === "sentRegister" ? "register" : "login-request");
      showCodeStep(sent.email, await cooldownFrom(resp));
    } catch (e) {
      status.textContent = t("errGeneric");
      send.disabled = false;
    }
  });
}

document.getElementById("code-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const input = document.getElementById("code-input");
  const status = document.getElementById("code-status");
  const send = ev.target.querySelector('[type="submit"]');
  send.disabled = true;
  status.classList.remove("sent");  // a resend may have left this line green
  status.textContent = t("sending");
  try {
    const resp = await fetch("/api/auth/consume-code", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email: pendingEmail, code: input.value.trim() }),
    });
    if (!resp.ok) {
      status.textContent = resp.status === 429 ? t("errRate") : t("errCode");
      send.disabled = false;
      input.select();
      return;
    }
    track("login-code");
    location.assign("/stories");
  } catch (e) {
    status.textContent = t("errGeneric");
    send.disabled = false;
  }
});

/* Suggested handles. The server picks the name - it is the only side that can
   see whether one is free, and letting the client ask about a name it names
   would turn this into a "does user X exist" oracle. */
const nameInput = document.getElementById("register-name");
const nameRow = nameInput.parentNode;
const nameOk = document.getElementById("name-ok");
const nameBad = document.getElementById("name-bad");
const nameHint = document.getElementById("name-hint");
const shuffleBtn = document.getElementById("name-shuffle");

// the last name the server vouched for; the tick and the green line belong to
// it, so both go the moment the field holds anything else
let suggested = null;
const isVouched = () => suggested !== null && nameInput.value === suggested;

// The same rule as the input's pattern and RegisterIn's, said a third time
// because neither of those can name WHICH half was broken - and "letters,
// numbers, - and _" is the useful half to say.
const NAME_CHARS = /^[A-Za-z0-9_-]*$/;

function nameProblem() {
  const value = nameInput.value;
  if (!value) return null;  // an empty field is not yet a mistake
  if (!NAME_CHARS.test(value)) return "errNameChars";
  // charset first: "a!" is about the "!", not about being one character long
  if (value.length < 2) return "errNameShort";
  return null;
}

// one line under the field, carrying whichever of the two states applies
function renderName() {
  const problem = nameProblem();
  const vouched = problem === null && isVouched();
  nameOk.classList.toggle("hidden", !vouched);
  nameBad.classList.toggle("hidden", problem === null);
  nameRow.classList.toggle("invalid", problem !== null);
  nameHint.classList.toggle("error", problem !== null);
  nameHint.textContent = problem ? t(problem) : vouched ? t("nameFree") : "";
  nameInput.setAttribute("aria-invalid", problem !== null);
  // the native bubble would otherwise say this in the browser's language and
  // its own words; submission stays blocked either way
  nameInput.setCustomValidity(problem ? t(problem) : "");
}

function dropSuggestion() {
  suggested = null;
  renderName();
}

async function suggestName() {
  shuffleBtn.disabled = true;
  try {
    const resp = await fetch("/api/auth/suggest-name?lang=" + lang);
    if (!resp.ok) return;  // a name we cannot offer is not worth an error line
    suggested = (await resp.json()).name;
    nameInput.value = suggested;
    renderName();
  } catch (e) {
    // offline or throttled: the field still works, it is just empty
  } finally {
    shuffleBtn.disabled = false;
  }
}

shuffleBtn.addEventListener("click", suggestName);
nameInput.addEventListener("input", renderName);
// a value restored by the browser is the visitor's own choice; leave it
if (!nameInput.value) suggestName();

wire("login-form", "login-status", "/api/auth/request-link",
  () => ({ email: document.getElementById("login-email").value.trim(), lang }),
  "sentLogin",
  (code) => code === 404 ? t("errNoAccount") : code === 429 ? t("errRate")
    : t("errGeneric"));

wire("register-form", "register-status", "/api/auth/register",
  () => ({
    name: document.getElementById("register-name").value.trim(),
    email: document.getElementById("register-email").value.trim(),
    lang,
  }),
  "sentRegister",
  (code) => {
    if (code === 409) dropSuggestion();  // taken meanwhile; the tick lied
    return code === 409 ? t("errTaken") : code === 422 ? t("errName")
      : code === 429 ? t("errRate") : t("errGeneric");
  });

showView(viewFromHash());  // applies every string as a side effect

})();
