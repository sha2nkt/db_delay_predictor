/* The way into an account, on one card: Google, Apple, a phone number, or
   an email address - the last of which leads to a password, and for a new
   address to a verification mail. Firebase does all the proving; this page
   only walks between the steps and, at the end, asks our own server for the
   one thing Firebase cannot hand out: a unique public username. A visitor
   who left half-way (mail not yet clicked, name not yet chosen) lands back
   on exactly the step they are missing. */
import * as fb from "/firebase.js?v=1";

const I18N = {
  de: {
    docTitle: "Anmelden – DelayBahn",
    tagline: "Ein Name für alle deine Geschichten",
    chooseHeading: "Anmelden oder registrieren",
    chooseLead: "Mit einem Konto kannst du Geschichten schreiben, kommentieren und abstimmen.",
    withGoogle: "Weiter mit Google",
    withApple: "Weiter mit Apple",
    withPhone: "Weiter mit Telefonnummer",
    or: "ODER",
    email: "E-Mail-Adresse",
    continue: "Weiter",
    chooseNote: "Deine Anmeldedaten verwaltet Firebase (Google) – auf unserem Server landen sie nicht. Öffentlich erscheint nur dein selbst gewählter Benutzername; Werbung gibt es keine.",
    passwordHeading: "Passwort",
    changeEmail: "Adresse ändern",
    password: "Passwort",
    loginBtn: "Anmelden",
    createBtn: "Neues Konto mit dieser Adresse erstellen",
    forgotBtn: "Passwort vergessen?",
    errPassword: "Falsches Passwort – oder noch kein Konto? Dann erstelle unten eins.",
    errExists: "Zu dieser Adresse gibt es schon ein Konto: Melde dich mit deinem Passwort an oder setze es zurück.",
    errOtherProvider: "Zu dieser Adresse gibt es schon ein Konto mit einer anderen Anmeldeart – probier es mit Google oder Apple.",
    errWeak: "Das Passwort braucht mindestens 8 Zeichen.",
    errEmail: "Das sieht nicht nach einer E-Mail-Adresse aus.",
    resetSent: "Wir haben dir eine E-Mail zum Zurücksetzen des Passworts geschickt (auch im Spam-Ordner nachsehen).",
    verifyHeading: "E-Mail-Adresse bestätigen",
    verifySent: "Wir haben eine E-Mail an {email} geschickt (auch im Spam-Ordner nachsehen). Klick auf den Link darin – danach geht es hier weiter.",
    verifiedBtn: "Ich habe bestätigt",
    errNotVerified: "Noch nicht bestätigt – klick zuerst auf den Link in der E-Mail.",
    resendBtn: "E-Mail erneut senden",
    resendWait: "Erneut senden in {s} s",
    resent: "Neue E-Mail ist unterwegs.",
    otherAccount: "Mit einem anderen Konto anmelden",
    phoneHeading: "Telefonnummer",
    phoneLabel: "Mobilnummer mit Ländervorwahl",
    sendCode: "Code senden",
    phoneNote: "Du bekommst einen 6-stelligen Code per SMS. Google prüft die Anfrage im Hintergrund per reCAPTCHA.",
    errPhone: "Bitte eine gültige Mobilnummer mit Ländervorwahl angeben, z. B. +49 151 23456789.",
    codeHeading: "Code eingeben",
    codeLabel: "6-stelliger Code aus der SMS",
    codeBtn: "Anmelden",
    codeSent: "Wir haben eine SMS an {phone} geschickt.",
    errCode: "Der Code ist falsch oder abgelaufen.",
    back: "Zurück",
    nameHeading: "Benutzername wählen",
    username: "Benutzername",
    nameNote: "Öffentlich erscheint nur dieser Name – an jeder Geschichte, jedem Kommentar. Nach der Wahl lässt er sich nicht mehr ändern.",
    nameBtn: "Los geht’s",
    nameRules: "2–25 Zeichen: Buchstaben, Zahlen, - und _",
    nameFree: "Guter Name! Er ist noch frei – er gehört ganz dir.",
    shuffleTitle: "Anderen Namen vorschlagen",
    errNameChars: "Nur Buchstaben, Zahlen, \"-\" und \"_\" sind erlaubt",
    errNameShort: "Der Benutzername braucht mindestens 2 Zeichen",
    errTaken: "Dieser Name ist schon vergeben.",
    errName: "Name: 2–25 Zeichen, nur Buchstaben, Zahlen, - und _.",
    errProvider: "Diese Anmeldeart ist noch nicht aktiviert.",
    errPopup: "Das Anmeldefenster wurde geschlossen, bevor die Anmeldung fertig war.",
    errRate: "Zu viele Versuche – bitte warte kurz und versuch es erneut.",
    errDown: "Die Anmeldung ist gerade nicht erreichbar – bitte später erneut versuchen.",
    errUnconfigured: "Die Anmeldung ist auf dieser Installation noch nicht eingerichtet.",
    errGeneric: "Hat nicht geklappt – bitte später erneut versuchen.",
    sending: "Einen Moment …",
    footerStories: "← Zu den Delay Geschichten",
    footerLegal: "Impressum & Datenschutz",
  },
  en: {
    docTitle: "Login – DelayBahn",
    tagline: "One name for all your stories",
    chooseHeading: "Log in or sign up",
    chooseLead: "With an account you can write stories, comment and vote.",
    withGoogle: "Continue with Google",
    withApple: "Continue with Apple",
    withPhone: "Continue with phone",
    or: "OR",
    email: "Email address",
    continue: "Continue",
    chooseNote: "Firebase (Google) keeps your login details – they never reach our server. Only the username you pick is ever shown publicly, and there is no marketing.",
    passwordHeading: "Password",
    changeEmail: "Change address",
    password: "Password",
    loginBtn: "Log in",
    createBtn: "Create a new account with this address",
    forgotBtn: "Forgot your password?",
    errPassword: "Wrong password – or no account yet? Then create one below.",
    errExists: "This address already has an account: log in with your password, or reset it.",
    errOtherProvider: "This address already has an account with a different sign-in method – try Google or Apple.",
    errWeak: "The password needs at least 8 characters.",
    errEmail: "That doesn't look like an email address.",
    resetSent: "We've sent you an email to reset your password (check the spam folder too).",
    verifyHeading: "Confirm your email address",
    verifySent: "We sent an email to {email} (check the spam folder too). Click the link in it – then carry on here.",
    verifiedBtn: "I've confirmed",
    errNotVerified: "Not confirmed yet – click the link in the email first.",
    resendBtn: "Resend email",
    resendWait: "Resend in {s} s",
    resent: "A new email is on its way.",
    otherAccount: "Log in with a different account",
    phoneHeading: "Phone number",
    phoneLabel: "Mobile number with country code",
    sendCode: "Send code",
    phoneNote: "You'll get a 6-digit code by SMS. Google checks the request in the background with reCAPTCHA.",
    errPhone: "Please enter a valid mobile number with country code, e.g. +49 151 23456789.",
    codeHeading: "Enter your code",
    codeLabel: "6-digit code from the SMS",
    codeBtn: "Log in",
    codeSent: "We sent an SMS to {phone}.",
    errCode: "That code is wrong or has expired.",
    back: "Back",
    nameHeading: "Pick a username",
    username: "Username",
    nameNote: "Only this name is ever shown – on every story and every comment. Once picked, it can't be changed.",
    nameBtn: "Let's go",
    nameRules: "2–25 characters: letters, digits, - and _",
    nameFree: "Great name! It's not taken so it's all yours.",
    shuffleTitle: "Suggest another name",
    errNameChars: "Username can only contain letters, numbers, \"-\", and \"_\"",
    errNameShort: "Username needs at least 2 characters",
    errTaken: "That name is already taken.",
    errName: "Name: 2–25 characters, only letters, digits, - and _.",
    errProvider: "This sign-in method isn't enabled yet.",
    errPopup: "The sign-in window was closed before the sign-in finished.",
    errRate: "Too many attempts – please wait a moment and try again.",
    errDown: "Sign-in is unavailable right now – please try again later.",
    errUnconfigured: "Sign-in hasn't been set up on this installation yet.",
    errGeneric: "That didn't work – please try again later.",
    sending: "One moment …",
    footerStories: "← Back to Delay Stories",
    footerLegal: "Legal notice & privacy",
  },
};

let lang = "de";
try { if (localStorage.getItem("lang") === "en") lang = "en"; } catch (e) {}
const t = (key) => (I18N[lang][key] != null ? I18N[lang][key] : I18N.de[key]);
// no-op when the Umami script is blocked or unavailable; never an address
const track = (name, data) => window.umami?.track(name, data);
// the stories page lives at one URL per language
const storiesPath = () => (lang === "en" ? "/stories" : "/geschichten");
const $ = (id) => document.getElementById(id);

// exactly one of these is visible; the header and the tab title follow it
const VIEWS = {
  choose:   { card: "choose-card",   title: "chooseHeading",   focus: null },
  password: { card: "password-card", title: "passwordHeading", focus: "password-input" },
  verify:   { card: "verify-card",   title: "verifyHeading",   focus: null },
  phone:    { card: "phone-card",    title: "phoneHeading",    focus: "phone-input" },
  code:     { card: "code-card",     title: "codeHeading",     focus: "code-input" },
  name:     { card: "name-card",     title: "nameHeading",     focus: "register-name" },
};
let view = "choose";

function showView(next) {
  view = next;
  Object.entries(VIEWS).forEach(([name, spec]) => {
    $(spec.card).classList.toggle("hidden", name !== next);
  });
  applyStatic();  // the header and tab title are part of the view
  const focus = VIEWS[next].focus;
  if (focus) $(focus).focus();
}

// one status line per card; `ok` turns it from a refusal into a confirmation
function say(id, key, ok = false) {
  const node = $(id);
  node.classList.toggle("sent", ok);
  node.textContent = key ? t(key) : "";
}

// what a visitor should hear about a Firebase error, by its code
function errorKey(e) {
  switch (e && e.code) {
    case "auth/invalid-credential":
    case "auth/wrong-password":
    case "auth/user-not-found":
      return "errPassword";
    case "auth/email-already-in-use":
      return "errExists";
    case "auth/account-exists-with-different-credential":
      return "errOtherProvider";
    case "auth/weak-password":
    case "auth/password-does-not-meet-requirements":
      return "errWeak";
    case "auth/invalid-email":
    case "auth/missing-email":
      return "errEmail";
    case "auth/invalid-phone-number":
    case "auth/missing-phone-number":
      return "errPhone";
    case "auth/invalid-verification-code":
    case "auth/code-expired":
      return "errCode";
    case "auth/too-many-requests":
      return "errRate";
    case "auth/operation-not-allowed":
      return "errProvider";
    case "auth/popup-closed-by-user":
    case "auth/cancelled-popup-request":
    case "auth/user-cancelled":
      return "errPopup";
    case "auth/network-request-failed":
      return "errDown";
    default:
      return "errGeneric";
  }
}

function applyStatic() {
  document.documentElement.lang = lang;
  document.title = t("docTitle");
  $("header-title").textContent = t(VIEWS[view].title);
  document.querySelectorAll("[data-i18n]").forEach((node) => {
    const text = I18N[lang][node.dataset.i18n];
    if (text != null) node.textContent = text;
  });
  document.querySelectorAll("[data-i18n-placeholder]").forEach((node) => {
    const text = I18N[lang][node.dataset.i18nPlaceholder];
    if (text != null) node.placeholder = text;
  });
  document.querySelectorAll("[data-i18n-title]").forEach((node) => {
    const text = I18N[lang][node.dataset.i18nTitle];
    if (text != null) node.title = text;
  });
  document.querySelectorAll(".lang-btn").forEach((b) => {
    b.classList.toggle("active", b.dataset.lang === lang);
  });
  $("footer-stories").href = storiesPath();
  // glyph-only button: its accessible name has nowhere to live but here
  shuffleBtn.title = t("shuffleTitle");
  shuffleBtn.setAttribute("aria-label", t("shuffleTitle"));
  renderName();
  // the lines built at runtime rather than from data-i18n
  if (pendingUser) {
    $("verify-sent").textContent = t("verifySent").replace("{email}", pendingUser.email || "");
  }
  if (pendingPhone) {
    $("code-sent").textContent = t("codeSent").replace("{phone}", pendingPhone);
  }
  renderResend();
  if (fb.auth) fb.auth.languageCode = lang;  // Firebase's own mails and popups
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

/* -- routing: which step this account still needs ------------------------- */

let pendingUser = null;   // the account whose mail is not yet clicked
let pendingPhone = null;  // the number the SMS went to

/* Send the visitor to the step they are missing, or on to the board when
   nothing is. `refresh` re-reads the account and forces a fresh token, which
   is what makes a verification click or a claimed name visible. */
async function route(user, refresh) {
  if (!user) {
    showView("choose");
    return;
  }
  const who = await fb.identity(user, refresh);
  if (!who.verified) {
    pendingUser = user;
    showView("verify");
    return;
  }
  if (!who.name) {
    showView("name");
    if (!nameInput.value) suggestName();
    return;
  }
  fb.remember(true);
  location.replace(storiesPath());
}

async function signOutHere() {
  try { await fb.signOut(fb.auth); } catch (e) { /* the state reload sorts it out */ }
  fb.remember(false);
  pendingUser = null;
  pendingPhone = null;
  showView("choose");
}

document.querySelectorAll(".other-account").forEach((b) => b.addEventListener("click", signOutHere));
document.querySelectorAll(".to-choose").forEach((b) => b.addEventListener("click", () => showView("choose")));

/* -- Google and Apple ------------------------------------------------------- */

async function withProvider(kind, btn) {
  let provider;
  if (kind === "google") {
    provider = new fb.GoogleAuthProvider();
    provider.setCustomParameters({ prompt: "select_account" });
  } else {
    provider = new fb.OAuthProvider("apple.com");
    provider.addScope("email");
    provider.addScope("name");
    provider.setCustomParameters({ locale: lang === "de" ? "de_DE" : "en_US" });
  }
  btn.disabled = true;
  say("choose-status", null);
  try {
    const cred = await fb.signInWithPopup(fb.auth, provider);
    track("login-" + kind);
    await route(cred.user, true);
  } catch (e) {
    if (e && e.code === "auth/popup-blocked") {
      // a browser that refuses popups gets the whole-page round trip instead
      await fb.signInWithRedirect(fb.auth, provider);
      return;
    }
    say("choose-status", errorKey(e));
  } finally {
    btn.disabled = false;
  }
}

$("sso-google").addEventListener("click", (ev) => withProvider("google", ev.currentTarget));
$("sso-apple").addEventListener("click", (ev) => withProvider("apple", ev.currentTarget));
$("sso-phone").addEventListener("click", () => {
  say("phone-status", null);
  showView("phone");
});

/* -- email + password ------------------------------------------------------- */

let email = "";

$("email-form").addEventListener("submit", (ev) => {
  ev.preventDefault();
  email = $("email-input").value.trim();
  $("password-email").textContent = email;
  $("password-username").value = email;
  $("password-input").value = "";
  say("password-status", null);
  showView("password");
});

$("change-email").addEventListener("click", () => {
  showView("choose");
  $("email-input").focus();
});

// the "continue" URL every Firebase mail points back to: this page, which
// then routes to whatever step is still missing
const backHere = () => ({ url: location.origin + "/login" });

async function withForm(formId, statusId, action) {
  const form = $(formId);
  const send = form.querySelector('[type="submit"]');
  send.disabled = true;
  say(statusId, "sending");
  try {
    await action();
    say(statusId, null);
  } catch (e) {
    say(statusId, errorKey(e));
  } finally {
    send.disabled = false;
  }
}

$("password-form").addEventListener("submit", (ev) => {
  ev.preventDefault();
  withForm("password-form", "password-status", async () => {
    const cred = await fb.signInWithEmailAndPassword(fb.auth, email, $("password-input").value);
    track("login-password");
    await route(cred.user, true);
  });
});

$("create-btn").addEventListener("click", () => {
  const password = $("password-input").value;
  if (password.length < 8) {
    say("password-status", "errWeak");
    $("password-input").focus();
    return;
  }
  withForm("password-form", "password-status", async () => {
    const cred = await fb.createUserWithEmailAndPassword(fb.auth, email, password);
    track("register");
    await sendVerification(cred.user);
    await route(cred.user, false);
  });
});

$("forgot-btn").addEventListener("click", () => {
  withForm("password-form", "password-status", async () => {
    await fb.sendPasswordResetEmail(fb.auth, email, backHere());
    say("password-status", "resetSent", true);
    throw { code: "shown" };  // keep the line; withForm would blank it
  }).catch(() => {});
});

/* -- the verification mail --------------------------------------------------
   Resend doubles as its own countdown - one control, two states - because a
   second always-live button would only invite clicking it before the mail
   lands. Recomputed from a deadline rather than counted down, so a
   backgrounded tab (where intervals are throttled) still comes back right. */
const RESEND_SECONDS = 60;
const resendBtn = $("resend-btn");
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

async function sendVerification(user) {
  await fb.sendEmailVerification(user, backHere());
  resendUntil = Date.now() + RESEND_SECONDS * 1000;
  if (resendTimer === null) resendTimer = setInterval(renderResend, 1000);
  renderResend();
}

$("verify-form").addEventListener("submit", (ev) => {
  ev.preventDefault();
  withForm("verify-form", "verify-status", async () => {
    await pendingUser.reload();
    if (!pendingUser.emailVerified) {
      say("verify-status", "errNotVerified");
      throw { code: "shown" };
    }
    await route(pendingUser, true);
  }).catch(() => {});
});

resendBtn.addEventListener("click", async () => {
  if (!pendingUser) return;
  resendBtn.disabled = true;
  say("verify-status", "sending");
  try {
    await sendVerification(pendingUser);
    say("verify-status", "resent", true);
    track("login-resend");
  } catch (e) {
    say("verify-status", errorKey(e));
    renderResend();
  }
});

/* -- phone number ------------------------------------------------------------
   The invisible reCAPTCHA hangs off the send button; a failed attempt has to
   drop it, because a solved challenge is single use. */
let recaptcha = null;
let confirmation = null;

function verifier() {
  if (recaptcha === null) {
    recaptcha = new fb.RecaptchaVerifier(fb.auth, "phone-send", { size: "invisible" });
  }
  return recaptcha;
}

function dropVerifier() {
  if (recaptcha !== null) {
    try { recaptcha.clear(); } catch (e) {}
    recaptcha = null;
  }
}

// digits and a leading plus; a domestic number gets Germany's code
function normalizePhone(raw) {
  let digits = raw.replace(/[^\d+]/g, "");
  if (digits.startsWith("00")) digits = "+" + digits.slice(2);
  if (digits.startsWith("0")) digits = "+49" + digits.slice(1);
  return digits;
}

$("phone-form").addEventListener("submit", (ev) => {
  ev.preventDefault();
  withForm("phone-form", "phone-status", async () => {
    const number = normalizePhone($("phone-input").value);
    try {
      confirmation = await fb.signInWithPhoneNumber(fb.auth, number, verifier());
    } catch (e) {
      dropVerifier();
      throw e;
    }
    pendingPhone = number;
    $("code-input").value = "";
    say("code-status", null);
    showView("code");
  });
});

$("code-form").addEventListener("submit", (ev) => {
  ev.preventDefault();
  withForm("code-form", "code-status", async () => {
    const cred = await confirmation.confirm($("code-input").value.trim());
    track("login-phone");
    await route(cred.user, true);
  }).then(() => { if (view === "code") $("code-input").select(); });
});

/* -- the username -----------------------------------------------------------
   The server picks the suggestion - it is the only side that can see whether
   one is free, and letting the client ask about a name it names would turn
   this into a "does user X exist" oracle. */
const nameInput = $("register-name");
const nameRow = nameInput.parentNode;
const nameOk = $("name-ok");
const nameBad = $("name-bad");
const nameHint = $("name-hint");
const shuffleBtn = $("name-shuffle");

// the last name the server vouched for; the tick and the green line belong to
// it, so both go the moment the field holds anything else
let suggested = null;
const isVouched = () => suggested !== null && nameInput.value === suggested;

// The same rule as the input's pattern and HandleIn's, said a third time
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

$("name-form").addEventListener("submit", (ev) => {
  ev.preventDefault();
  withForm("name-form", "name-status", async () => {
    const user = fb.auth.currentUser;
    const resp = await fetch("/api/auth/handle", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Authorization": "Bearer " + await user.getIdToken(),
      },
      body: JSON.stringify({ name: nameInput.value.trim() }),
    });
    if (resp.status === 201) {
      track("handle");
      await route(user, true);
      return;
    }
    let detail = "";
    try { detail = (await resp.json()).detail; } catch (e) {}
    if (resp.status === 409 && detail === "taken") {
      dropSuggestion();  // taken meanwhile; the tick lied
      say("name-status", "errTaken");
    } else if (resp.status === 409 || resp.status === 403) {
      // already named, or not yet verified: the token is behind the account
      await route(user, true);
      return;
    } else {
      say("name-status", resp.status === 422 ? "errName" : resp.status === 429 ? "errRate"
        : resp.status === 503 ? "errDown" : "errGeneric");
    }
    throw { code: "shown" };
  }).catch(() => {});
});

/* -- start ------------------------------------------------------------------ */

async function init() {
  if (!fb.auth) {
    document.querySelectorAll(".sso-btn, #email-form button").forEach((b) => { b.disabled = true; });
    showView("choose");
    say("choose-status", "errUnconfigured");
    return;
  }
  ["google", "apple", "phone"].forEach((kind) => {
    $("sso-" + kind).classList.toggle("hidden", !fb.providers[kind]);
  });
  $("auth-or").classList.toggle("hidden", !Object.values(fb.providers).some(Boolean));
  showView("choose");
  try {
    await fb.getRedirectResult(fb.auth);  // the popup-blocked fallback landing
  } catch (e) {
    say("choose-status", errorKey(e));
  }
  await fb.auth.authStateReady();
  try {
    await route(fb.auth.currentUser, true);
  } catch (e) {
    say("choose-status", errorKey(e));
  }
}

init();
