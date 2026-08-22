"""Transactional email over SMTP - currently only the magic login links for
the stories board. Configured via SMTP_HOST/PORT/USER/PASS and SMTP_FROM; the
defaults fit Brevo, so a deploy only needs the credentials. Without
SMTP_USER/SMTP_PASS sending is a logged no-op, same contract as the ntfy
pushes, so dev setups run without an email account.

Every mail goes out as multipart/alternative: an HTML part with a real button
and a large, selectable code, and a plain-text part carrying the same link and
code for clients that refuse HTML. Both are rendered from one set of strings
below, so the two halves cannot drift apart.

Blocking (smtplib) - run it off the event loop.
"""

import logging
import os
import smtplib
from email.message import EmailMessage
from html import escape

from app.auth import MAGIC_LINK_HOURS, UNVERIFIED_DAYS
from app.config import env_int

log = logging.getLogger(__name__)

# "welcome" confirms a fresh registration; "login" is every later sign-in.
# One mechanism, two wordings - a welcome mail talking about "your login
# request" would read like phishing.
_SUBJECT = {
    ("welcome", "de"): "DelayBahn: Konto bestätigen und anmelden",
    ("welcome", "en"): "DelayBahn: confirm your account and log in",
    ("login", "de"): "DelayBahn: Dein Anmelde-Link",
    ("login", "en"): "DelayBahn: your login link",
}

_INTRO = {
    ("welcome", "de"): "willkommen bei den Bahnhofs-Geschichten! Bestätige dein "
                       "Konto und melde dich an:",
    ("welcome", "en"): "welcome to Station Stories! Confirm your account and log in:",
    ("login", "de"): "hier ist dein Anmelde-Link für die Bahnhofs-Geschichten:",
    ("login", "en"): "here is your login link for Station Stories:",
}

_IGNORE = {
    ("welcome", "de"): "Du hast kein Konto erstellt? Dann ignoriere diese E-Mail "
                       "einfach – das unbestätigte Konto samt Adresse wird nach "
                       "{days} Tagen automatisch gelöscht.",
    ("welcome", "en"): "Didn't create an account? Just ignore this email – the "
                       "unconfirmed account and the address are deleted "
                       "automatically after {days} days.",
    ("login", "de"): "Falls du das nicht angefordert hast, kannst du diese E-Mail "
                     "ignorieren – ohne Link und Code passiert nichts.",
    ("login", "en"): "If you didn't request this, you can ignore this email – "
                     "nothing happens without the link or the code.",
}

_T = {
    "de": {
        "greeting": "Hallo {name},",
        "button": "Jetzt anmelden",
        "code_lead": "Oder gib diesen Code auf der Anmeldeseite ein:",
        "fallback": "Falls der Button nicht funktioniert, kopiere diesen Link in "
                    "deinen Browser:",
        "validity_one": "Link und Code sind eine Stunde gültig und funktionieren "
                        "nur einmal.",
        "validity_many": "Link und Code sind {hours} Stunden gültig und "
                         "funktionieren nur einmal.",
    },
    "en": {
        "greeting": "Hi {name},",
        "button": "Log in now",
        "code_lead": "Or enter this code on the login page:",
        "fallback": "If the button doesn't work, copy this link into your browser:",
        "validity_one": "The link and the code are valid for one hour and work "
                        "only once.",
        "validity_many": "The link and the code are valid for {hours} hours and "
                         "work only once.",
    },
}

# DelayBahn palette, matching static/style.css
_RED, _TEXT, _MUTED, _BG, _BORDER = "#fd1c17", "#282d37", "#646973", "#f0f3f5", "#d7dce1"
_FONT = "-apple-system,'Segoe UI',Helvetica,Arial,sans-serif"
_MONO = "'SFMono-Regular',Consolas,'Liberation Mono',Menlo,monospace"


def _parts(name: str, link: str, code: str, lang: str, kind: str) -> dict:
    t = _T[lang]
    validity = (
        t["validity_one"] if MAGIC_LINK_HOURS == 1
        else t["validity_many"].format(hours=MAGIC_LINK_HOURS)
    )
    return {
        "greeting": t["greeting"].format(name=name),
        "intro": _INTRO[(kind, lang)],
        "button": t["button"],
        "code_lead": t["code_lead"],
        "fallback": t["fallback"],
        "validity": validity,
        "ignore": _IGNORE[(kind, lang)].format(days=UNVERIFIED_DAYS),
        "link": link,
        "code": code,
    }


def _text_body(p: dict) -> str:
    return (
        f"{p['greeting']}\n\n"
        f"{p['intro']}\n\n"
        f"{p['link']}\n\n"
        f"{p['code_lead']}\n\n"
        f"    {p['code']}\n\n"
        f"{p['validity']}\n\n"
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
<title>{e['greeting']}</title>
</head>
<body style="margin:0;padding:0;background:{_BG};">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"
       style="background:{_BG};padding:24px 12px;">
<tr><td align="center">
<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="480"
       style="width:100%;max-width:480px;background:#ffffff;border-radius:10px;
              padding:32px 28px;font-family:{_FONT};color:{_TEXT};">
<tr><td style="font-size:16px;line-height:1.5;padding-bottom:14px;">{e['greeting']}</td></tr>
<tr><td style="font-size:16px;line-height:1.5;padding-bottom:26px;">{e['intro']}</td></tr>
<tr><td align="center" style="padding-bottom:28px;">
  <a href="{e['link']}"
     style="display:inline-block;background:{_RED};color:#ffffff;font-size:16px;
            font-weight:600;text-decoration:none;padding:14px 34px;border-radius:8px;">
    {e['button']}</a>
</td></tr>
<tr><td style="font-size:15px;line-height:1.5;color:{_MUTED};padding-bottom:10px;">{e['code_lead']}</td></tr>
<tr><td align="center" style="padding-bottom:26px;">
  <div style="font-family:{_MONO};font-size:30px;font-weight:700;letter-spacing:8px;
              text-indent:8px;color:{_TEXT};background:{_BG};border:1px solid {_BORDER};
              border-radius:8px;padding:16px 12px;">{e['code']}</div>
</td></tr>
<tr><td style="font-size:13px;line-height:1.5;color:{_MUTED};padding-bottom:18px;">{e['validity']}</td></tr>
<tr><td style="font-size:13px;line-height:1.5;color:{_MUTED};border-top:1px solid {_BORDER};
               padding-top:18px;">{e['ignore']}</td></tr>
<tr><td style="font-size:12px;line-height:1.5;color:{_MUTED};padding-top:14px;">
  {e['fallback']}<br>
  <span style="word-break:break-all;">{e['link']}</span>
</td></tr>
</table>
</td></tr>
</table>
</body>
</html>
"""


_warned_unconfigured = False


def send_magic_link(
    email: str, name: str, token: str, code: str, lang: str, kind: str
) -> None:
    """kind is "welcome" or "login". Never raises: a lost email must not take
    the request down with it, and asking for a new link covers the gap."""
    global _warned_unconfigured
    user = os.environ.get("SMTP_USER")
    password = os.environ.get("SMTP_PASS")
    if not user or not password:
        if not _warned_unconfigured:
            _warned_unconfigured = True
            log.warning("SMTP_USER/SMTP_PASS unset: magic-link emails are not sent")
        return

    base = os.environ.get("PUBLIC_BASE_URL", "https://delaybahn.com").rstrip("/")
    if lang not in ("de", "en"):
        lang = "de"
    parts = _parts(name, f"{base}/verify?token={token}", code, lang, kind)

    msg = EmailMessage()
    msg["From"] = os.environ.get("SMTP_FROM", "DelayBahn <kontakt@delaybahn.com>")
    msg["To"] = email
    msg["Subject"] = _SUBJECT[(kind, lang)]
    msg.set_content(_text_body(parts))
    msg.add_alternative(_html_body(parts, lang), subtype="html")
    try:
        host = os.environ.get("SMTP_HOST", "smtp-relay.brevo.com")
        with smtplib.SMTP(host, env_int("SMTP_PORT", 587), timeout=15) as smtp:
            smtp.starttls()
            smtp.login(user, password)
            smtp.send_message(msg)
    except (OSError, smtplib.SMTPException) as exc:
        log.warning("magic-link email to %s failed: %s", email, exc)
