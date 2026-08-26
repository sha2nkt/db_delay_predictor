"""Shared fixtures: a controllable clock, a scripted stand-in for the curl_cffi
session, and per-test isolation of bahn_api's module state. No test ever
reaches the live bahn.de."""

import asyncio
import inspect
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app import auth, bahn_api, stories


class FakeAdmin:
    """The slice of firebase_admin.auth this app uses, on a dict of users.
    Custom tokens are just a readable marker: nothing here signs anything,
    and the browser is the only side that would redeem one."""

    class UserNotFoundError(Exception):
        pass

    def __init__(self):
        self.users: dict[str, SimpleNamespace] = {}
        self.minted: list[str] = []
        self._next = 0

    def _record(self, uid, email=None, verified=False):
        rec = SimpleNamespace(uid=uid, email=email, email_verified=verified)
        self.users[uid] = rec
        return rec

    def get_user_by_email(self, email, app=None):
        for rec in self.users.values():
            if rec.email == email:
                return rec
        raise self.UserNotFoundError(email)

    def get_user(self, uid, app=None):
        if uid not in self.users:
            raise self.UserNotFoundError(uid)
        return self.users[uid]

    def create_user(self, email=None, email_verified=False, app=None):
        self._next += 1
        return self._record(f"uid-new-{self._next}", email, email_verified)

    def update_user(self, uid, email_verified=None, display_name=None, app=None):
        rec = self.users[uid]
        if email_verified is not None:
            rec.email_verified = email_verified
        return rec

    def create_custom_token(self, uid, app=None):
        self.minted.append(uid)
        return f"custom-token-for-{uid}".encode()

    def delete_user(self, uid, app=None):
        if uid not in self.users:
            raise self.UserNotFoundError(uid)
        del self.users[uid]


@pytest.fixture
def anyio_backend():
    return "asyncio"


class FakeRegistry:
    """auth._Registry on plain dicts: the same contract, no Firestore."""

    def __init__(self, clock=None):
        self.names: dict[str, str] = {}   # lowercased name -> uid
        self.users: dict[str, str] = {}   # uid -> name as claimed
        self.codes: dict[str, dict] = {}  # email key -> pending login
        self.swept = 0                    # abandoned logins cleared
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    # --- pending email logins (mirrors the Firestore transactions) ---
    def _sweep_expired(self, now):
        stale = [k for k, d in self.codes.items() if d["expires"] < now]
        for key in stale[:auth.SWEEP_LIMIT]:
            del self.codes[key]
        self.swept += len(stale[:auth.SWEEP_LIMIT])

    def issue_code(self, email, code_hash):
        now = self._clock()
        today = now.date().isoformat()
        self._sweep_expired(now)
        doc = self.codes.get(auth._email_key(email), {})
        sent = doc.get("sent_today", 0) if doc.get("day") == today else 0
        last = doc.get("sent_at")
        if sent >= auth.MAX_CODES_PER_DAY:
            return False
        if last is not None and (now - last).total_seconds() < auth.RESEND_COOLDOWN_SECONDS:
            return False
        self.codes[auth._email_key(email)] = {
            "code_hash": code_hash,
            "expires": now + timedelta(minutes=auth.CODE_TTL_MINUTES),
            "tries": 0, "sent_at": now, "day": today, "sent_today": sent + 1,
        }
        return True

    def redeem_code(self, email, code_hash):
        key = auth._email_key(email)
        doc = self.codes.get(key)
        if doc is None:
            return False
        if doc["expires"] < self._clock() or doc["tries"] >= auth.MAX_TRIES:
            self.codes.pop(key, None)
            return False
        if doc["code_hash"] != code_hash:
            doc["tries"] += 1
            if doc["tries"] >= auth.MAX_TRIES:
                self.codes.pop(key, None)
            return False
        self.codes.pop(key, None)
        return True

    def refund_code(self, email):
        self.codes.pop(auth._email_key(email), None)

    def taken(self, names):
        return {n.lower() for n in names if n.lower() in self.names}

    def claim(self, uid, name):
        if uid in self.users:
            return "named"
        if name.lower() in self.names:
            return "taken"
        self.names[name.lower()] = uid
        self.users[uid] = name
        return "ok"

    def release(self, uid):
        name = self.users.pop(uid, None)
        if name is not None:
            self.names.pop(name.lower(), None)
        return name


def claims(uid="u-jonas", handle=None, verified=True, provider="google.com"):
    """The decoded ID token Firebase would hand back. A phone sign-in carries
    no email at all, so `verified` only applies to the other providers."""
    token = {"sub": uid, "firebase": {"sign_in_provider": provider}}
    if provider != "phone":
        token["email"] = f"{uid}@example.org"
        token["email_verified"] = verified
    if handle:
        token["handle"] = handle
    return token


@pytest.fixture
def firebase(monkeypatch, tmp_path):
    """Firebase stood in by dicts: `tokens` maps the bearer strings the
    verifier accepts to their claims, `registry` holds the usernames, and
    `stamped` records what would have become a custom claim - which the
    test then has to put on a fresh token itself, exactly as the SDK does
    only on refresh. The stories DB is a temp file."""
    monkeypatch.setattr(stories, "DB_PATH", tmp_path / "stories.db")
    tokens: dict[str, dict] = {}
    now = SimpleNamespace(value=datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc))
    registry = FakeRegistry(clock=lambda: now.value)
    admin = FakeAdmin()
    stamped: dict[str, str] = {}
    monkeypatch.setattr(auth, "_verify", lambda token: tokens.get(token))
    monkeypatch.setattr(auth, "_registry", lambda: registry)
    monkeypatch.setattr(auth, "_stamp", lambda uid, name: stamped.__setitem__(uid, name))
    # kept so the handful of tests that are ABOUT wiring up the real SDK can
    # put it back; everything else wants the fake
    real_firebase = auth._firebase
    monkeypatch.setattr(auth, "_firebase", lambda: (admin, None))
    monkeypatch.setattr(auth, "_now", lambda: now.value)

    def token(name, **kw):
        """Register a bearer string and return it."""
        tokens[name] = claims(**kw)
        return name

    def advance(**kw):
        now.value += timedelta(**kw)

    return SimpleNamespace(
        tokens=tokens, registry=registry, admin=admin, stamped=stamped,
        token=token, claims=claims, advance=advance, now=now,
        real_firebase=real_firebase,
    )


class FakeClock:
    def __init__(self, start: float = 1000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeResponse:
    def __init__(self, status_code: int = 200, payload=None, headers: dict | None = None):
        self.status_code = status_code
        self._payload = {"ok": True} if payload is None else payload
        self.headers = headers or {}
        self.text = ""

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class FakeSession:
    """Serves scripted outcomes in order, repeating the last one forever.
    An item may be a FakeResponse, an exception to raise, or an (async)
    callable producing either — the latter lets a test hold a request open."""

    def __init__(self, *script):
        self.script = list(script) or [FakeResponse()]
        self.calls = 0
        self.closed = False

    async def close(self):
        self.closed = True

    async def _respond(self, url, **kwargs):
        self.calls += 1
        item = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        if callable(item) and not isinstance(item, Exception):
            item = item()
            if inspect.isawaitable(item):
                item = await item
        if isinstance(item, Exception):
            raise item
        return item

    async def get(self, url, **kwargs):
        return await self._respond(url, **kwargs)

    async def post(self, url, **kwargs):
        return await self._respond(url, **kwargs)


@pytest.fixture
def clock(monkeypatch):
    """Deterministic time inside bahn_api (caches, stale window, alerts) and a
    fresh circuit breaker driven by the same clock."""
    fake = FakeClock()
    monkeypatch.setattr(bahn_api, "time", SimpleNamespace(monotonic=fake))
    monkeypatch.setattr(bahn_api, "_breaker", bahn_api.CircuitBreaker(
        threshold=bahn_api.CIRCUIT_FAILURE_THRESHOLD,
        window=bahn_api.CIRCUIT_FAILURE_WINDOW,
        base_cooldown=bahn_api.RATE_BASE_COOLDOWN,
        max_cooldown=bahn_api.RATE_MAX_COOLDOWN,
        probes=bahn_api.HALF_OPEN_PROBES,
        clock=fake,
    ))
    return fake


@pytest.fixture
def bahn(clock, monkeypatch):
    """Isolated bahn_api state per test; returns a namespace whose `session`
    the test points at a FakeSession via use()."""
    holder = SimpleNamespace(session=FakeSession(), clock=clock)
    # factory indirection: recycling tests replace holder.factory to observe
    # or vary session construction; everyone else keeps the single shared fake
    holder.factory = lambda profile: holder.session
    monkeypatch.setattr(bahn_api, "_session", lambda profile: holder.factory(profile))
    # asyncio primitives bind to the running loop on first use; each test gets
    # its own loop, so they must be re-created
    monkeypatch.setattr(bahn_api, "_upstream_sem",
                        asyncio.Semaphore(bahn_api.MAX_UPSTREAM_CONCURRENCY))
    monkeypatch.setattr(bahn_api, "_rotate_lock", asyncio.Lock())
    monkeypatch.setattr(bahn_api, "_ident", None)
    monkeypatch.setattr(bahn_api, "_profile_idx", 0)
    monkeypatch.setattr(bahn_api, "_last_alert", float("-inf"))
    monkeypatch.setattr(bahn_api, "_upstream_logged_at", clock())
    monkeypatch.setattr(bahn_api, "_upstream_logged_429", 0)
    bahn_api._retiring.clear()
    bahn_api._close_tasks.clear()
    bahn_api._upstream_since.clear()
    bahn_api._cache.clear()
    bahn_api._stale.clear()
    bahn_api._stale_route.clear()
    bahn_api._rate_events.clear()
    bahn_api.metrics.clear()

    def use(*script):
        holder.session = FakeSession(*script)
        # drop the cached identity so the new script takes effect immediately,
        # preserving the pre-recycling semantics where every request consulted
        # the (monkeypatched) _session factory
        bahn_api._ident = None
        return holder.session

    holder.use = use
    yield holder
    # deferred-close timers hold real 30s sleeps; cancel them so no task
    # outlives its test's event loop
    for task in bahn_api._close_tasks:
        task.cancel()
    bahn_api._close_tasks.clear()
    bahn_api._retiring.clear()
    bahn_api._upstream_since.clear()
    bahn_api._cache.clear()
    bahn_api._stale.clear()
    bahn_api._stale_route.clear()
