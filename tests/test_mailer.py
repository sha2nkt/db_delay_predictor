"""The login-code mail. It carries a code and nothing to click: no link means
nothing for a phishing lookalike to imitate and nothing for a mail scanner to
burn, which is the whole reason the flow moved off links."""

import pytest

from app import mailer

KINDS = ("welcome", "login")
LANGS = ("de", "en")


@pytest.fixture(autouse=True)
def no_smtp(monkeypatch):
    monkeypatch.delenv("SMTP_USER", raising=False)
    monkeypatch.delenv("SMTP_PASS", raising=False)


def render(code="048512", lang="de", kind="login"):
    parts = mailer._parts(code, lang, kind)
    return mailer._html_body(parts, lang), mailer._text_body(parts)


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("lang", LANGS)
def test_the_code_is_in_both_halves(lang, kind):
    html, text = render(lang=lang, kind=kind)
    assert "048512" in html and "048512" in text


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("lang", LANGS)
def test_there_is_nothing_to_click(lang, kind):
    """No anchor, no URL, no leftover /verify route - the code is the whole
    mechanism."""
    html, text = render(lang=lang, kind=kind)
    assert "<a " not in html and "href" not in html
    assert "http://" not in html and "https://" not in html
    assert "verify?token" not in html and "verify?token" not in text
    assert "http" not in text


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("lang", LANGS)
def test_the_subject_leads_with_the_code(lang, kind):
    """A phone shows the subject in the notification, so the code belongs at
    the front of it - most people never open the mail at all."""
    subject = mailer._SUBJECT[(kind, lang)].format(code="048512")
    assert subject.startswith("048512")
    assert "DelayBahn" in subject


@pytest.mark.parametrize("lang", LANGS)
def test_the_code_is_selectable_text_not_an_image(lang):
    html, _text = render(lang=lang)
    assert "<img" not in html
    # one tap takes the whole code rather than a digit at a time
    assert "user-select:all" in html


@pytest.mark.parametrize("lang", LANGS)
def test_the_preview_line_repeats_the_code(lang):
    html, _text = render(lang=lang)
    preheader = html.split("<table", 1)[0]
    assert "048512" in preheader


@pytest.mark.parametrize("lang", LANGS)
def test_it_warns_against_handing_the_code_on(lang):
    html, text = render(lang=lang)
    warning = "niemals" if lang == "de" else "never"
    assert warning in html and warning in text


@pytest.mark.parametrize("lang", LANGS)
def test_the_validity_matches_the_code_that_was_minted(lang):
    html, _text = render(lang=lang)
    assert str(mailer.CODE_TTL_MINUTES) in html


def test_a_welcome_and_a_login_do_not_read_alike():
    """A first mail calling itself a login request would read like phishing."""
    welcome, _ = render(kind="welcome")
    login, _ = render(kind="login")
    assert welcome != login


def test_sending_without_credentials_is_a_logged_no_op():
    """A dev box has no SMTP account; that must not look like a failure the
    caller should report to the user."""
    assert mailer.send_login_code("jonas@example.org", "048512", "de", "login") is True
    assert mailer.status()["sendFailures"] == 0


# --- the report mail ----------------------------------------------------------

class FakeSMTP:
    """smtplib.SMTP with the four calls _deliver makes, recording the message."""
    sent: list = []
    fail: Exception | None = None

    def __init__(self, host, port, timeout=None):
        self.host, self.port = host, port

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self):
        pass

    def login(self, user, password):
        pass

    def send_message(self, msg):
        if FakeSMTP.fail is not None:
            raise FakeSMTP.fail
        FakeSMTP.sent.append(msg)


@pytest.fixture
def relay(monkeypatch):
    monkeypatch.setenv("SMTP_USER", "u")
    monkeypatch.setenv("SMTP_PASS", "p")
    monkeypatch.delenv("NTFY_TOPIC", raising=False)
    monkeypatch.setattr(mailer.smtplib, "SMTP", FakeSMTP)
    FakeSMTP.sent, FakeSMTP.fail = [], None
    return FakeSMTP


HEADERS = {
    "List-Unsubscribe": "<https://delaybahn.com/r/unsubscribe?token=T>",
    "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
}


def test_a_report_without_credentials_is_a_logged_no_op():
    assert mailer.send_report("jonas@example.org", "Jonas", "Report", "t", "<p>h</p>", HEADERS) is True


def test_a_report_carries_both_parts_and_the_list_headers(relay):
    assert mailer.send_report("jonas@example.org", "Jonas", "Dein Report", "plain", "<p>rich</p>",
                              HEADERS) is True
    (msg,) = relay.sent
    assert msg["To"] == "Jonas <jonas@example.org>"
    assert msg["From"] == "DelayBahn <kontakt@delaybahn.com>"
    assert msg["Subject"] == "Dein Report"
    assert msg["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
    assert [p.get_content_type() for p in msg.iter_parts()] == ["text/plain", "text/html"]


def test_a_nameless_account_is_addressed_by_address_alone(relay):
    mailer.send_report("jonas@example.org", "", "Report", "t", "<p>h</p>")
    assert relay.sent[0]["To"] == "jonas@example.org"


def test_a_refused_report_is_counted_and_remembered(relay):
    relay.fail = mailer.smtplib.SMTPException("451 daily quota exceeded")
    before = mailer.status()["sendFailures"]
    assert mailer.send_report("jonas@example.org", "", "Report", "t", "<p>h</p>") is False
    assert mailer.status()["sendFailures"] == before + 1
    assert "quota" in mailer.status()["lastError"]
    assert relay.sent == []
