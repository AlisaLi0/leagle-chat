"""SQLite store for leagle accounts and saved research sessions.

Two tables, one small file (LEAGLE_DB_PATH, default ./leagle.db):

* users             — one row per signed-in account (OAuth identity). Carries
                      forward-looking `plan` / `credits` columns so billing can
                      be layered on later without a migration, but nothing here
                      charges money.
* research_sessions — a user's saved research threads (the durable version of
                      the browser-only history), each a JSON snapshot of the
                      conversation + retrieved authorities.

Access is synchronous sqlite3 wrapped in asyncio.to_thread by the callers; the
data is tiny (accounts + short JSON threads) so this is more than enough and
keeps the dependency footprint at zero beyond the standard library.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from dataclasses import dataclass

DB_PATH = os.getenv("LEAGLE_DB_PATH", os.path.join(os.path.dirname(__file__), "..", "leagle.db"))

# Plan -> monthly research-question allowance. "research" = one /api/chat ask.
# Free is generous enough to prove value but nudges heavy users to upgrade;
# paid tiers map to the Freemius plans (pro/max). day_pass is a one-off purchase
# that grants Max-level access for a few days (see PASS_DAYS) then lapses to free.
PLANS: dict[str, dict] = {
    "free":     {"label": "Free", "monthly_questions": 10,     "price": 0},
    "pro":      {"label": "Pro",  "monthly_questions": 300,    "price": 9.98},
    "max":      {"label": "Max",  "monthly_questions": 100000, "price": 29.98},   # "soft" unlimited
    "day_pass": {"label": "3-Day Pass", "monthly_questions": 100000, "price": 2.98},  # Max-level, time-boxed
}
DEFAULT_PLAN = "free"
PASS_DAYS = 3  # how long a one-off day_pass stays active

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    provider          TEXT NOT NULL,
    provider_user_id  TEXT NOT NULL,
    email             TEXT,
    name              TEXT,
    avatar_url        TEXT,
    plan              TEXT NOT NULL DEFAULT 'free',
    credits           INTEGER NOT NULL DEFAULT 0,
    created_at        TEXT NOT NULL,
    last_login        TEXT,
    UNIQUE(provider, provider_user_id)
);
CREATE TABLE IF NOT EXISTS oauth_identities (
    user_id           INTEGER NOT NULL,
    provider          TEXT NOT NULL,
    provider_user_id  TEXT NOT NULL,
    email             TEXT,
    name              TEXT,
    avatar_url        TEXT,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    PRIMARY KEY (provider, provider_user_id),
    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_oauth_user ON oauth_identities(user_id);
CREATE TABLE IF NOT EXISTS research_sessions (
    id          TEXT PRIMARY KEY,
    user_id     INTEGER NOT NULL,
    title       TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    payload     TEXT NOT NULL,
    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON research_sessions(user_id, updated_at DESC);
CREATE TABLE IF NOT EXISTS usage_monthly (
    user_id     INTEGER NOT NULL,
    month       TEXT NOT NULL,            -- 'YYYY-MM' (UTC)
    questions   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, month)
);
CREATE TABLE IF NOT EXISTS subscriptions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       INTEGER NOT NULL,
    plan          TEXT NOT NULL,
    provider      TEXT,                   -- 'freemius'
    provider_ref  TEXT,
    status        TEXT NOT NULL DEFAULT 'pending',
    period_end    INTEGER,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sub_user ON subscriptions(user_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_sub_ref ON subscriptions(provider, provider_ref);
CREATE TABLE IF NOT EXISTS pending_billing (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    email         TEXT NOT NULL,          -- the payer email Freemius reported
    plan          TEXT NOT NULL,
    provider      TEXT,                   -- 'freemius'
    provider_ref  TEXT,
    period_end    INTEGER,
    event_type    TEXT,
    created_at    TEXT NOT NULL,
    resolved_at   TEXT                    -- set when matched to an account
);
CREATE INDEX IF NOT EXISTS idx_pending_email ON pending_billing(email) WHERE resolved_at IS NULL;
CREATE TABLE IF NOT EXISTS billing_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    provider    TEXT NOT NULL,
    event_id    TEXT NOT NULL,
    event_type  TEXT,
    email       TEXT,
    plan        TEXT,
    user_id     INTEGER,
    action      TEXT NOT NULL DEFAULT 'received',
    payload     TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    UNIQUE(provider, event_id)
);
CREATE INDEX IF NOT EXISTS idx_billing_events_user ON billing_events(user_id, created_at DESC);
CREATE TABLE IF NOT EXISTS account_deletion_requests (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    email       TEXT,
    status      TEXT NOT NULL DEFAULT 'pending',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_delete_requests_user ON account_deletion_requests(user_id, status, created_at DESC);
"""


@dataclass(slots=True)
class User:
    id: int
    provider: str
    provider_user_id: str
    email: str
    name: str
    avatar_url: str
    plan: str
    credits: int

    def to_public(self) -> dict:
        """Fields safe to expose to the browser (no internal flags)."""
        return {
            "id": self.id,
            "email": self.email,
            "name": self.name,
            "avatar_url": self.avatar_url,
            "plan": self.plan,
            "credits": self.credits,
        }


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    # Wait (instead of erroring) when another writer holds the lock — required
    # for the BEGIN IMMEDIATE used by the atomic quota counter under concurrency.
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init_db() -> None:
    conn = _connect()
    try:
        # WAL lets readers and the single writer proceed concurrently and avoids
        # the reader/writer stalls of the default rollback journal. Persisted on
        # the db file, so setting it once here is enough.
        try:
            conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.DatabaseError:
            pass
        conn.executescript(_SCHEMA)
        _migrate_oauth_identities(conn)
        _merge_duplicate_email_accounts(conn)
        conn.commit()
    finally:
        conn.close()


def _plan_rank(plan: str) -> int:
    return {"free": 0, "pro": 1, "day_pass": 2, "max": 3}.get(plan, 0)


def _migrate_oauth_identities(conn: sqlite3.Connection) -> None:
    """Backfill the identity table from legacy users(provider, provider_user_id)
    rows. Safe to run every startup."""
    rows = conn.execute(
        "SELECT id, provider, provider_user_id, email, name, avatar_url, created_at, last_login FROM users"
    ).fetchall()
    now = _now()
    for r in rows:
        if not r["provider"] or not r["provider_user_id"]:
            continue
        conn.execute(
            """INSERT OR IGNORE INTO oauth_identities
               (user_id, provider, provider_user_id, email, name, avatar_url, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (r["id"], r["provider"], r["provider_user_id"], r["email"] or "",
             r["name"] or "", r["avatar_url"] or "", r["created_at"] or now,
             r["last_login"] or now),
        )


def _merge_account_rows(conn: sqlite3.Connection, source_id: int, target_id: int) -> None:
    """Move all account-owned state from source -> target, then delete source.
    Used only for same-email account repair/linking."""
    if source_id == target_id:
        return
    target = conn.execute("SELECT * FROM users WHERE id=?", (target_id,)).fetchone()
    source = conn.execute("SELECT * FROM users WHERE id=?", (source_id,)).fetchone()
    if not target or not source:
        return
    best_plan = target["plan"]
    if _plan_rank(source["plan"]) > _plan_rank(target["plan"]):
        best_plan = source["plan"]
    name = target["name"] or source["name"] or ""
    avatar = target["avatar_url"] or source["avatar_url"] or ""
    email = target["email"] or source["email"] or ""
    conn.execute(
        "UPDATE users SET email=?, name=?, avatar_url=?, plan=?, credits=credits+?, last_login=? WHERE id=?",
        (email, name, avatar, best_plan, int(source["credits"] or 0), _now(), target_id),
    )
    conn.execute("UPDATE research_sessions SET user_id=? WHERE user_id=?", (target_id, source_id))
    rows = conn.execute(
        "SELECT month, questions FROM usage_monthly WHERE user_id=?", (source_id,)
    ).fetchall()
    for r in rows:
        conn.execute(
            """INSERT INTO usage_monthly (user_id, month, questions) VALUES (?, ?, ?)
               ON CONFLICT(user_id, month) DO UPDATE SET questions = questions + excluded.questions""",
            (target_id, r["month"], int(r["questions"] or 0)),
        )
    conn.execute("DELETE FROM usage_monthly WHERE user_id=?", (source_id,))
    conn.execute("UPDATE subscriptions SET user_id=? WHERE user_id=?", (target_id, source_id))
    conn.execute("UPDATE oauth_identities SET user_id=? WHERE user_id=?", (target_id, source_id))
    conn.execute("UPDATE billing_events SET user_id=? WHERE user_id=?", (target_id, source_id))
    conn.execute("DELETE FROM users WHERE id=?", (source_id,))


def _merge_duplicate_email_accounts(conn: sqlite3.Connection) -> None:
    """Repair legacy duplicate accounts that share the same non-empty email.
    The earliest id becomes canonical; all sessions/usage/subs/identities move
    onto it. Empty emails are intentionally not merged."""
    groups = conn.execute(
        """SELECT lower(email) AS e, GROUP_CONCAT(id) AS ids FROM users
           WHERE email IS NOT NULL AND email <> ''
           GROUP BY lower(email) HAVING COUNT(*) > 1"""
    ).fetchall()
    for g in groups:
        ids = sorted(int(x) for x in str(g["ids"]).split(",") if x)
        if not ids:
            continue
        target = ids[0]
        for source in ids[1:]:
            _merge_account_rows(conn, source, target)


def _row_to_user(r: sqlite3.Row) -> User:
    return User(
        id=r["id"], provider=r["provider"], provider_user_id=r["provider_user_id"],
        email=r["email"] or "", name=r["name"] or "", avatar_url=r["avatar_url"] or "",
        plan=r["plan"], credits=r["credits"],
    )


# ── Users ──────────────────────────────────────────────────────────────────

def upsert_user(provider: str, provider_user_id: str, *, email: str = "",
                name: str = "", avatar_url: str = "") -> User:
    """Create or update the account for an OAuth identity, returning the User.

    Identity is keyed by (provider, provider_user_id), but the account is keyed
    by a verified non-empty email when available. This lets the same person sign
    in with Google and X and land on one account instead of splitting billing,
    saved sessions, and quota across two rows.
    """
    conn = _connect()
    try:
        now = _now()
        pid = str(provider_user_id)
        clean_email = (email or "").strip().lower()
        conn.isolation_level = None
        conn.execute("BEGIN IMMEDIATE")
        try:
            ident = conn.execute(
                "SELECT user_id FROM oauth_identities WHERE provider=? AND provider_user_id=?",
                (provider, pid),
            ).fetchone()
            if ident:
                user_id = int(ident["user_id"])
                if clean_email:
                    other = conn.execute(
                        "SELECT id FROM users WHERE lower(email)=lower(?) AND id<>? ORDER BY id LIMIT 1",
                        (clean_email, user_id),
                    ).fetchone()
                    if other:
                        target_id = int(other["id"])
                        _merge_account_rows(conn, user_id, target_id)
                        user_id = target_id
                    conn.execute(
                        "UPDATE users SET email=?, name=?, avatar_url=?, last_login=? WHERE id=?",
                        (clean_email, name, avatar_url, now, user_id),
                    )
                else:
                    conn.execute(
                        "UPDATE users SET name=?, avatar_url=?, last_login=? WHERE id=?",
                        (name, avatar_url, now, user_id),
                    )
            else:
                existing = None
                if clean_email:
                    existing = conn.execute(
                        "SELECT id FROM users WHERE lower(email)=lower(?) ORDER BY id LIMIT 1",
                        (clean_email,),
                    ).fetchone()
                if existing:
                    user_id = int(existing["id"])
                    conn.execute(
                        "UPDATE users SET email=?, name=COALESCE(NULLIF(name,''), ?), "
                        "avatar_url=COALESCE(NULLIF(avatar_url,''), ?), last_login=? WHERE id=?",
                        (clean_email, name, avatar_url, now, user_id),
                    )
                else:
                    cur = conn.execute(
                        """INSERT INTO users (provider, provider_user_id, email, name, avatar_url,
                                              created_at, last_login)
                           VALUES (?, ?, ?, ?, ?, ?, ?)""",
                        (provider, pid, clean_email, name, avatar_url, now, now),
                    )
                    user_id = int(cur.lastrowid)
                conn.execute(
                    """INSERT INTO oauth_identities
                       (user_id, provider, provider_user_id, email, name, avatar_url, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (user_id, provider, pid, clean_email, name, avatar_url, now, now),
                )
            conn.execute(
                "UPDATE oauth_identities SET email=?, name=?, avatar_url=?, updated_at=? "
                "WHERE provider=? AND provider_user_id=?",
                (clean_email, name, avatar_url, now, provider, pid),
            )
            conn.execute("COMMIT")
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        r = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        return _row_to_user(r)
    finally:
        conn.close()


def get_user(user_id: int) -> User | None:
    conn = _connect()
    try:
        r = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        return _row_to_user(r) if r else None
    finally:
        conn.close()


# ── Research sessions ──────────────────────────────────────────────────────

def list_sessions(user_id: int, *, limit: int = 50) -> list[dict]:
    """Session metadata (no payload) for the sidebar, newest first."""
    conn = _connect()
    try:
        rows = conn.execute(
            """SELECT id, title, created_at, updated_at FROM research_sessions
               WHERE user_id=? ORDER BY updated_at DESC LIMIT ?""",
            (user_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_session(user_id: int, session_id: str) -> dict | None:
    conn = _connect()
    try:
        r = conn.execute(
            "SELECT * FROM research_sessions WHERE id=? AND user_id=?",
            (session_id, user_id),
        ).fetchone()
        if not r:
            return None
        d = dict(r)
        try:
            d["payload"] = json.loads(d["payload"])
        except (ValueError, TypeError):
            d["payload"] = []
        return d
    finally:
        conn.close()


def save_session(user_id: int, *, session_id: str | None, title: str,
                 payload: list | dict) -> str:
    """Insert or update a saved research thread; returns its id."""
    conn = _connect()
    try:
        now = _now()
        sid = session_id or uuid.uuid4().hex
        body = json.dumps(payload, ensure_ascii=False)
        title = (title or "Untitled research")[:200]
        exists = conn.execute(
            "SELECT 1 FROM research_sessions WHERE id=? AND user_id=?",
            (sid, user_id),
        ).fetchone()
        if exists:
            conn.execute(
                "UPDATE research_sessions SET title=?, payload=?, updated_at=? "
                "WHERE id=? AND user_id=?",
                (title, body, now, sid, user_id),
            )
        else:
            conn.execute(
                """INSERT INTO research_sessions (id, user_id, title, created_at,
                                                  updated_at, payload)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (sid, user_id, title, now, now, body),
            )
        conn.commit()
        return sid
    finally:
        conn.close()


def delete_session(user_id: int, session_id: str) -> bool:
    conn = _connect()
    try:
        cur = conn.execute(
            "DELETE FROM research_sessions WHERE id=? AND user_id=?",
            (session_id, user_id),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


# ── Billing: plan, usage quota, subscriptions ───────────────────────────────

def get_user_by_email(email: str) -> User | None:
    """Look up an account by email (used by the Freemius webhook)."""
    if not email:
        return None
    conn = _connect()
    try:
        r = conn.execute(
            "SELECT * FROM users WHERE lower(email)=lower(?) ORDER BY id LIMIT 1",
            (email.strip(),),
        ).fetchone()
        return _row_to_user(r) if r else None
    finally:
        conn.close()


def set_plan(user_id: int, plan: str) -> None:
    if plan not in PLANS:
        plan = DEFAULT_PLAN
    conn = _connect()
    try:
        conn.execute("UPDATE users SET plan=? WHERE id=?", (plan, user_id))
        conn.commit()
    finally:
        conn.close()


def active_day_pass_end(user_id: int) -> int | None:
    """Return the unix expiry of an unexpired one-off day pass, or None."""
    conn = _connect()
    try:
        r = conn.execute(
            "SELECT MAX(period_end) AS e FROM subscriptions "
            "WHERE user_id=? AND plan='day_pass' AND status='active' "
            "AND period_end IS NOT NULL AND period_end > ?",
            (user_id, int(time.time())),
        ).fetchone()
        return int(r["e"]) if r and r["e"] else None
    finally:
        conn.close()


def expire_day_passes(user_id: int | None = None) -> int:
    """Mark lapsed one-off day passes as 'expired' so the subscriptions table
    reflects reality (the quota path already ignores them via period_end, but
    leaving them 'active' is misleading). Returns the number updated. Called
    opportunistically from the billing webhook; safe to run anytime."""
    conn = _connect()
    try:
        now = int(time.time())
        if user_id is None:
            cur = conn.execute(
                "UPDATE subscriptions SET status='expired' "
                "WHERE plan='day_pass' AND status='active' "
                "AND period_end IS NOT NULL AND period_end <= ?",
                (now,),
            )
        else:
            cur = conn.execute(
                "UPDATE subscriptions SET status='expired' "
                "WHERE user_id=? AND plan='day_pass' AND status='active' "
                "AND period_end IS NOT NULL AND period_end <= ?",
                (user_id, now),
            )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def effective_plan(user: "User") -> str:
    """The plan whose quota applies right now: an active day pass beats the
    stored subscription plan; otherwise the user's own plan."""
    if active_day_pass_end(user.id):
        return "day_pass"
    return user.plan if user.plan in PLANS else DEFAULT_PLAN


def _month() -> str:
    return time.strftime("%Y-%m", time.gmtime())


def usage_this_month(user_id: int) -> int:
    conn = _connect()
    try:
        r = conn.execute(
            "SELECT questions FROM usage_monthly WHERE user_id=? AND month=?",
            (user_id, _month()),
        ).fetchone()
        return r["questions"] if r else 0
    finally:
        conn.close()


def try_consume_question(user_id: int, limit: int) -> bool:
    """Atomically count one research question if under the monthly limit.

    Returns False (and counts nothing) when the user is already at their limit.
    Uses BEGIN IMMEDIATE so the read-check-write is a single serialized write
    transaction — two concurrent requests can't both slip past the limit (no
    TOCTOU race).
    """
    month = _month()
    conn = _connect()
    try:
        conn.isolation_level = None  # take explicit control of the transaction
        conn.execute("BEGIN IMMEDIATE")
        try:
            r = conn.execute(
                "SELECT questions FROM usage_monthly WHERE user_id=? AND month=?",
                (user_id, month),
            ).fetchone()
            used = r["questions"] if r else 0
            if used >= limit:
                conn.execute("ROLLBACK")
                return False
            conn.execute(
                "INSERT INTO usage_monthly (user_id, month, questions) VALUES (?, ?, 1) "
                "ON CONFLICT(user_id, month) DO UPDATE SET questions = questions + 1",
                (user_id, month),
            )
            conn.execute("COMMIT")
            return True
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
    finally:
        conn.close()


def refund_question(user_id: int) -> None:
    """Give back one counted question (e.g. when the request errored out)."""
    conn = _connect()
    try:
        conn.execute(
            "UPDATE usage_monthly SET questions = MAX(0, questions - 1) "
            "WHERE user_id=? AND month=?",
            (user_id, _month()),
        )
        conn.commit()
    finally:
        conn.close()


def upsert_subscription(user_id: int, plan: str, provider: str,
                        provider_ref: str, status: str,
                        period_end: int | None = None) -> None:
    """Record/refresh a subscription keyed on (provider, provider_ref)."""
    now = _now()
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT id FROM subscriptions WHERE provider=? AND provider_ref=?",
            (provider, provider_ref),
        ).fetchone()
        if row:
            conn.execute(
                "UPDATE subscriptions SET user_id=?, plan=?, status=?, "
                "period_end=?, updated_at=? WHERE id=?",
                (user_id, plan, status, period_end, now, row["id"]),
            )
        else:
            conn.execute(
                "INSERT INTO subscriptions (user_id, plan, provider, provider_ref, "
                "status, period_end, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
                (user_id, plan, provider, provider_ref, status, period_end, now, now),
            )
        conn.commit()
    finally:
        conn.close()


def add_pending_billing(email: str, plan: str, provider: str, provider_ref: str,
                        period_end: int | None, event_type: str) -> None:
    """Park a paid event whose payer email matches no account yet, so the
    purchase is never silently lost (e.g. a user who signed in with X under a
    synthetic email then paid with their real email). Reconciled on next login
    with a matching email. Deduped on (provider, provider_ref)."""
    if not email:
        return
    conn = _connect()
    try:
        dup = conn.execute(
            "SELECT 1 FROM pending_billing WHERE provider=? AND provider_ref=? "
            "AND resolved_at IS NULL",
            (provider, provider_ref),
        ).fetchone()
        if dup:
            return
        conn.execute(
            "INSERT INTO pending_billing (email, plan, provider, provider_ref, "
            "period_end, event_type, created_at) VALUES (?,?,?,?,?,?,?)",
            (email.strip().lower(), plan, provider, provider_ref, period_end,
             event_type, _now()),
        )
        conn.commit()
    finally:
        conn.close()


def take_pending_billing(email: str) -> list[dict]:
    """Return and mark-resolved all unresolved pending purchases for an email
    (called when a user with that email signs in)."""
    if not email:
        return []
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT * FROM pending_billing WHERE lower(email)=lower(?) "
            "AND resolved_at IS NULL ORDER BY id",
            (email.strip(),),
        ).fetchall()
        if rows:
            conn.execute(
                "UPDATE pending_billing SET resolved_at=? "
                "WHERE lower(email)=lower(?) AND resolved_at IS NULL",
                (_now(), email.strip()),
            )
            conn.commit()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def request_account_deletion(user_id: int, email: str) -> int:
    """Create or return the open account-deletion request for a user."""
    now = _now()
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT id FROM account_deletion_requests "
            "WHERE user_id=? AND status='pending' ORDER BY id DESC LIMIT 1",
            (user_id,),
        ).fetchone()
        if row:
            return int(row["id"])
        cur = conn.execute(
            "INSERT INTO account_deletion_requests (user_id, email, status, created_at, updated_at) "
            "VALUES (?, ?, 'pending', ?, ?)",
            (user_id, (email or "").strip().lower(), now, now),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def add_billing_event(provider: str, event_id: str, event_type: str,
                      email: str, payload: dict) -> bool:
    """Insert a received billing webhook event. Returns False if this exact
    provider/event_id was already seen, making webhook handling idempotent."""
    if not event_id:
        return True
    body = json.dumps(payload or {}, ensure_ascii=False, sort_keys=True)
    now = _now()
    conn = _connect()
    try:
        try:
            conn.execute(
                """INSERT INTO billing_events
                   (provider, event_id, event_type, email, payload, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (provider, event_id, event_type, email or "", body, now, now),
            )
            conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False
    finally:
        conn.close()


def finish_billing_event(provider: str, event_id: str, *, user_id: int | None,
                         plan: str | None, action: str) -> None:
    """Update the audit row for a processed billing webhook event."""
    if not event_id:
        return
    conn = _connect()
    try:
        conn.execute(
            """UPDATE billing_events SET user_id=?, plan=?, action=?, updated_at=?
               WHERE provider=? AND event_id=?""",
            (user_id, plan or "", action or "processed", _now(), provider, event_id),
        )
        conn.commit()
    finally:
        conn.close()

