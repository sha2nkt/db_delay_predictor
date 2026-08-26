"""Transactional email over SMTP - currently only the six-digit login codes
for the stories board. Configured via SMTP_HOST/PORT/USER/PASS and SMTP_FROM;
the defaults fit Brevo, so a deploy only needs the credentials. Without
SMTP_USER/SMTP_PASS sending is a logged no-op, same contract as the ntfy
pushes, so dev setups run without an email account.

Firebase has no email one-time code of its own - its codes are SMS only and
its email flows are all clickable links - so the code is minted here, mailed
from here, and redeemed against Firestore (see auth.issue_email_code). Only
the sending lives on this server; the pending login itself never does.

Every mail goes out as multipart/alternative: an HTML part with a large,
selectable code, and a plain-text part carrying the same digits for clients
that refuse HTML. Both are rendered from one set of strings below, so the two
halves cannot drift apart.

Blocking (smtplib) - run it off the event loop.

Two alarms live here as well, because a silent mail outage is the one failure
nobody notices - the site stays up, users just never get their code. A send
failure pages ntfy (once per MAIL_ALERT_COOLDOWN, with the relay's reply), and
with BREVO_API_KEY set a background poll watches the account's remaining daily
credits and pages before the free plan's cap is hit.
"""

import asyncio
import logging
import os
import smtplib
import time
from datetime import datetime, timezone
from email.message import EmailMessage
from html import escape

import httpx

from app.auth import CODE_TTL_MINUTES
from app.config import env_int

log = logging.getLogger(__name__)

# "welcome" is the first code an address ever gets, "login" every later one.
# One mechanism, two wordings - a welcome mail talking about "your login
# request" would read like phishing.
#
# The code leads the subject line, because that is the part a phone shows in
# the notification and the inbox list: most people never have to open the mail
# at all, and iOS/Android offer a code found there for autofill.
_SUBJECT = {
    ("welcome", "de"): "{code} – dein DelayBahn-Bestätigungscode",
    ("welcome", "en"): "{code} – your DelayBahn confirmation code",
    ("login", "de"): "{code} – dein DelayBahn-Anmeldecode",
    ("login", "en"): "{code} – your DelayBahn login code",
}

_INTRO = {
    ("welcome", "de"): "willkommen bei Delay Geschichten! Gib diesen Code auf der "
                       "Anmeldeseite ein, um dein Konto zu erstellen:",
    ("welcome", "en"): "welcome to Delay Stories! Enter this code on the sign-in "
                       "page to create your account:",
    ("login", "de"): "gib diesen Code auf der Anmeldeseite ein, um dich anzumelden:",
    ("login", "en"): "enter this code on the sign-in page to log in:",
}

_IGNORE = {
    ("welcome", "de"): "Du hast kein Konto erstellt? Dann ignoriere diese E-Mail "
                       "einfach – ohne den Code passiert nichts.",
    ("welcome", "en"): "Didn't create an account? Just ignore this email – nothing "
                       "happens without the code.",
    ("login", "de"): "Falls du das nicht angefordert hast, kannst du diese E-Mail "
                     "ignorieren – ohne den Code passiert nichts.",
    ("login", "en"): "If you didn't request this, you can ignore this email – "
                     "nothing happens without the code.",
}

_T = {
    "de": {
        "greeting": "Hallo,",
        "code_lead": "Dein Code:",
        "tap_to_copy": "Zum Kopieren antippen",
        "validity_one": "Der Code ist eine Minute gültig und funktioniert nur einmal.",
        "validity_many": "Der Code ist {minutes} Minuten gültig und funktioniert "
                         "nur einmal.",
        "never_asked": "Wir fragen dich niemals per E-Mail oder Telefon nach diesem "
                       "Code. Gib ihn nur auf delaybahn.com ein.",
    },
    "en": {
        "greeting": "Hi,",
        "code_lead": "Your code:",
        "tap_to_copy": "Tap to copy",
        "validity_one": "The code is valid for one minute and works only once.",
        "validity_many": "The code is valid for {minutes} minutes and works only once.",
        "never_asked": "We will never ask you for this code by email or phone. Only "
                       "ever enter it on delaybahn.com.",
    },
}

# DelayBahn palette, matching static/style.css
_RED, _TEXT, _MUTED, _BG, _BORDER = "#fd1c17", "#282d37", "#646973", "#f0f3f5", "#d7dce1"
_FONT = "-apple-system,'Segoe UI',Helvetica,Arial,sans-serif"
_MONO = "'SFMono-Regular',Consolas,'Liberation Mono',Menlo,monospace"


def _parts(code: str, lang: str, kind: str) -> dict:
    t = _T[lang]
    validity = (
        t["validity_one"] if CODE_TTL_MINUTES == 1
        else t["validity_many"].format(minutes=CODE_TTL_MINUTES)
    )
    return {
        "greeting": t["greeting"],
        "intro": _INTRO[(kind, lang)],
        "code_lead": t["code_lead"],
        "tap_to_copy": t["tap_to_copy"],
        "validity": validity,
        "never_asked": t["never_asked"],
        "ignore": _IGNORE[(kind, lang)],
        "code": code,
    }


def _text_body(p: dict) -> str:
    return (
        f"{p['greeting']}\n\n"
        f"{p['intro']}\n\n"
        f"    {p['code']}\n\n"
        f"{p['validity']}\n\n"
        f"{p['never_asked']}\n\n"
        f"{p['ignore']}\n"
    )


def _html_body(p: dict, lang: str) -> str:
    """Table layout and inline styles only - the shared subset every mail
    client renders. text-indent offsets the trailing letter-space so the code
    stays optically centred; letter-spacing is visual, so copying the code
    still yields bare digits."""
    e = {k: escape(v, quote=True) for k, v in p.items()}
    return f"""<!DOCTYPE html>
<html lang="{lang}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light">
<meta name="supported-color-schemes" content="light">
<title>{e['code_lead']}</title>
</head>
<body style="margin:0;padding:0;background:{_BG};">
<!-- preheader: the line the inbox shows next to the subject. Repeating the
     code there means the list view alone is often enough. The zero-height
     span after it stops the client padding the preview with body text. -->
<div style="display:none;max-height:0;overflow:hidden;opacity:0;">{e['code']} · {e['validity']}</div>
<div style="display:none;max-height:0;overflow:hidden;">&#8199;&#65279;&#8199;&#65279;&#8199;&#65279;&#8199;&#65279;&#8199;&#65279;&#8199;&#65279;&#8199;&#65279;&#8199;&#65279;</div>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"
       style="background:{_BG};padding:24px 12px;">
<tr><td align="center">
<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="480"
       style="width:100%;max-width:480px;background:#ffffff;border-radius:10px;
              padding:32px 28px;font-family:{_FONT};color:{_TEXT};">
<tr><td style="font-size:16px;line-height:1.5;padding-bottom:14px;">{e['greeting']}</td></tr>
<tr><td style="font-size:16px;line-height:1.5;padding-bottom:26px;">{e['intro']}</td></tr>
<tr><td style="font-size:15px;line-height:1.5;color:{_MUTED};padding-bottom:10px;">{e['code_lead']}</td></tr>
<tr><td align="center" style="padding-bottom:8px;">
  <!-- Selectable text, never an image: the whole point is copying it. The
       letter-spacing is visual only, so a copy still yields bare digits, and
       user-select:all makes one click (or one long-press on a phone) take
       the whole code rather than a digit at a time. -->
  <div style="font-family:{_MONO};font-size:38px;font-weight:700;letter-spacing:10px;
              text-indent:10px;color:{_TEXT};background:{_BG};border:1px solid {_BORDER};
              border-radius:8px;padding:20px 12px;-webkit-user-select:all;user-select:all;">{e['code']}</div>
</td></tr>
<tr><td align="center" style="font-size:12px;line-height:1.5;color:{_MUTED};padding-bottom:22px;">{e['tap_to_copy']}</td></tr>
<tr><td style="font-size:13px;line-height:1.5;color:{_MUTED};padding-bottom:18px;">{e['validity']}</td></tr>
<tr><td style="font-size:13px;line-height:1.5;color:{_TEXT};background:{_BG};
               border-radius:6px;padding:12px 14px;">{e['never_asked']}</td></tr>
<tr><td style="font-size:13px;line-height:1.5;color:{_MUTED};border-top:1px solid {_BORDER};
               padding-top:18px;margin-top:18px;">{e['ignore']}</td></tr>
</table>
</td></tr>
</table>
</body>
</html>
"""


_warned_unconfigured = False

# Send failures: counted for /health, paged at most once per cooldown so a
# quota-out day is one push, not one per visitor.
MAIL_ALERT_COOLDOWN = env_int("MAIL_ALERT_COOLDOWN", 3600)
_failures = 0
_failures_at_alert = 0
_last_fail_alert: float | None = None


def _alert(text: str, priority: str = "default") -> None:
    """Blocking ntfy push; never raises. NTFY_TOPIC unset = no-op, same as the
    other notifiers."""
    topic = os.environ.get("NTFY_TOPIC")
    if not topic:
        return
    base = os.environ.get("NTFY_URL", "https://ntfy.sh").rstrip("/")
    try:
        httpx.post(
            f"{base}/{topic}",
            content=text.encode("utf-8"),
            headers={"Title": "DelayBahn mail alert", "Tags": "email,warning",
                     "Priority": priority},
            timeout=5,
        ).raise_for_status()
    except httpx.HTTPError as exc:
        log.warning("ntfy alert failed: %s", exc)


def _note_failure(exc: Exception) -> None:
    global _failures, _failures_at_alert, _last_fail_alert
    _failures += 1
    # the recipient is personal data and stays out of both the log and the push
    log.warning("login-code email failed: %s", exc)
    now = time.monotonic()
    if _last_fail_alert is not None and now - _last_fail_alert < MAIL_ALERT_COOLDOWN:
        return
    since = _failures - _failures_at_alert
    _last_fail_alert, _failures_at_alert = now, _failures
    # the relay's reply names the cause (out of credits, bad login, ...)
    _alert(
        f"Login-code email failed ({since} failure(s) since the last alert). "
        f"Users see 'try again later' until this clears. Relay said: {exc}",
        priority="high",
    )


def send_login_code(email: str, code: str, lang: str, kind: str) -> bool:
    """kind is "welcome" or "login". Never raises; False when the relay
    refused or was unreachable, so the caller can tell the user instead of
    letting them wait for a mail that will not come. An unconfigured dev setup
    counts as sent - there is nothing to retry."""
    global _warned_unconfigured
    user = os.environ.get("SMTP_USER")
    password = os.environ.get("SMTP_PASS")
    if not user or not password:
        if not _warned_unconfigured:
            _warned_unconfigured = True
            log.warning("SMTP_USER/SMTP_PASS unset: login-code emails are not sent")
        return True

    if lang not in ("de", "en"):
        lang = "de"
    parts = _parts(code, lang, kind)

    msg = EmailMessage()
    msg["From"] = os.environ.get("SMTP_FROM", "DelayBahn <kontakt@delaybahn.com>")
    msg["To"] = email
    msg["Subject"] = _SUBJECT[(kind, lang)].format(code=code)
    msg.set_content(_text_body(parts))
    msg.add_alternative(_html_body(parts, lang), subtype="html")
    try:
        host = os.environ.get("SMTP_HOST", "smtp-relay.brevo.com")
        with smtplib.SMTP(host, env_int("SMTP_PORT", 587), timeout=15) as smtp:
            smtp.starttls()
            smtp.login(user, password)
            smtp.send_message(msg)
    except (OSError, smtplib.SMTPException) as exc:
        _note_failure(exc)
        return False
    return True


# --- Brevo credit watch -------------------------------------------------------
# The free plan allows 300 mails a day and the relay simply refuses the 301st.
# GET /v3/account reports the credits left, so the app can page while there is
# still time to act. Two levels, each paged once per crossing and re-armed when
# the daily reset lifts the count back above the line.
BREVO_WARN_CREDITS = env_int("BREVO_WARN_CREDITS", 50)
BREVO_POLL_SECONDS = env_int("BREVO_POLL_SECONDS", 3600)
_credits: int | None = None
_credits_at: str | None = None
_credits_level = 0


def status() -> dict:
    """In-memory only - /health calls this."""
    return {"credits": _credits, "creditsAt": _credits_at, "sendFailures": _failures}


async def _fetch_credits(client: httpx.AsyncClient, key: str) -> int | None:
    base = os.environ.get("BREVO_API_URL", "https://api.brevo.com").rstrip("/")
    resp = await client.get(
        f"{base}/v3/account", headers={"api-key": key, "accept": "application/json"}
    )
    resp.raise_for_status()
    for plan in resp.json().get("plan") or []:
        if plan.get("creditsType") == "sendLimit":
            return int(plan["credits"])
    return None


def _note_credits(credits: int | None) -> str | None:
    """Record the reading; return the alert text to send, if any."""
    global _credits, _credits_at, _credits_level
    _credits = credits
    _credits_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if credits is None:
        return None
    level = 2 if credits <= 0 else 1 if credits <= BREVO_WARN_CREDITS else 0
    crossed = level > _credits_level
    _credits_level = level
    if not crossed:
        return None
    if level == 2:
        return ("Brevo email credits EXHAUSTED: 0 left today. Login-code mails "
                "are failing until the daily reset (or an upgrade).")
    return (f"Brevo email credits low: {credits} left today "
            f"(warning below {BREVO_WARN_CREDITS}).")


async def watch_credits() -> None:
    """Background task for the app's lifespan; returns at once without
    BREVO_API_KEY. Cancelled on shutdown."""
    key = os.environ.get("BREVO_API_KEY")
    if not key:
        log.info("BREVO_API_KEY unset: email credits are not watched")
        return
    async with httpx.AsyncClient(timeout=15) as client:
        while True:
            try:
                credits = await _fetch_credits(client, key)
            except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
                log.warning("Brevo account poll failed: %s", exc)
            else:
                if credits is None:
                    log.warning("Brevo account has no sendLimit plan; nothing to watch")
                text = _note_credits(credits)
                if text:
                    await asyncio.to_thread(
                        _alert, text, "urgent" if _credits_level == 2 else "high"
                    )
            await asyncio.sleep(BREVO_POLL_SECONDS)
