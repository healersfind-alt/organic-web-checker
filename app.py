"""
Organic Web Checker — Flask web app
"""

import os
import uuid
import hashlib
import secrets
import threading
import json
import time
import requests as req_http
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from flask import Flask, request, render_template_string, send_from_directory, jsonify, Response, session
from werkzeug.security import generate_password_hash, check_password_hash
from checker import run_check, get_oid_cert, _clean_oid_search, verify_certifying_agent
import stripe
import psycopg2

ADMIN_EMAIL = 'healersfind@gmail.com'

app = Flask(__name__)
app.secret_key = os.environ.get('FLASK_SECRET_KEY', 'dev-secret-change-in-prod')

# ---------------------------------------------------------------------------
# Stripe config
# ---------------------------------------------------------------------------

stripe.api_key         = os.environ.get('STRIPE_SECRET_KEY', '')
STRIPE_PK              = os.environ.get('STRIPE_PUBLISHABLE_KEY', '')
STRIPE_WH_SECRET       = os.environ.get('STRIPE_WEBHOOK_SECRET', '')
# If DATABASE_PUBLIC_URL is set, always prefer it — Railway's internal
# postgres.railway.internal hostname only resolves with private networking,
# which is not enabled on the hobby plan.  PUBLIC URL works from anywhere.
_db_url     = os.environ.get('DATABASE_URL', '')
_db_pub_url = os.environ.get('DATABASE_PUBLIC_URL', '')
DATABASE_URL = _db_pub_url or _db_url
APP_BASE_URL           = os.environ.get('APP_BASE_URL', 'https://www.organicwebchecker.com')

# Email — Resend HTTP API (Railway blocks outbound SMTP)
RESEND_API_KEY   = os.environ.get('RESEND_API_KEY', '')
# Use verified domain sender once organicwebchecker.com is added in Resend dashboard;
REPORT_FROM      = 'Organic Web Checker <report@organicwebchecker.com>'
HELLO_FROM       = 'Organic Web Checker <hello@organicwebchecker.com>'

# ---------------------------------------------------------------------------
# Postgres helpers
# ---------------------------------------------------------------------------

@contextmanager
def db_conn():
    conn = None
    try:
        url = DATABASE_URL
        if url.startswith('postgres://'):
            url = url.replace('postgres://', 'postgresql://', 1)
        conn = psycopg2.connect(url)
        yield conn
    finally:
        if conn:
            conn.close()

def init_db():
    if not DATABASE_URL:
        return
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS credit_accounts (
                    token TEXT PRIMARY KEY,
                    credits_remaining INTEGER NOT NULL DEFAULT 0,
                    total_purchased   INTEGER NOT NULL DEFAULT 0,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS purchases (
                    id SERIAL PRIMARY KEY,
                    token             TEXT NOT NULL,
                    stripe_session_id TEXT UNIQUE NOT NULL,
                    tier_name         TEXT,
                    credits_purchased INTEGER NOT NULL,
                    amount_paid_cents INTEGER NOT NULL,
                    promo_code        TEXT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    email         TEXT PRIMARY KEY,
                    password_hash TEXT NOT NULL,
                    credits       INTEGER NOT NULL DEFAULT 0,
                    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS oid_cache (
                    cache_key  TEXT PRIMARY KEY,
                    operation  TEXT NOT NULL,
                    cert_data  JSONB NOT NULL,
                    cached_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
            cur.execute("ALTER TABLE purchases ADD COLUMN IF NOT EXISTS promo_code TEXT")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS password_resets (
                    token      TEXT PRIMARY KEY,
                    email      TEXT NOT NULL,
                    expires_at TIMESTAMPTZ NOT NULL,
                    used       BOOLEAN NOT NULL DEFAULT FALSE,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS certifier_requests (
                    id           SERIAL PRIMARY KEY,
                    first_name   TEXT NOT NULL,
                    last_name    TEXT NOT NULL,
                    organization TEXT NOT NULL,
                    nop_number   TEXT NOT NULL,
                    email        TEXT NOT NULL,
                    status       TEXT NOT NULL DEFAULT 'pending',
                    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS api_keys (
                    key_id       TEXT PRIMARY KEY,
                    key_hash     TEXT NOT NULL UNIQUE,
                    email        TEXT NOT NULL,
                    name         TEXT,
                    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    last_used_at TIMESTAMPTZ
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS scheduled_checks (
                    id             TEXT PRIMARY KEY,
                    user_email     TEXT NOT NULL,
                    operation_name TEXT NOT NULL,
                    website_url    TEXT NOT NULL,
                    scheduled_at   TIMESTAMPTZ NOT NULL,
                    status         TEXT NOT NULL DEFAULT 'scheduled',
                    job_id         TEXT,
                    report         JSONB,
                    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
            cur.execute("DROP INDEX IF EXISTS uq_scheduled_active_slot")
            cur.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS uq_scheduled_active_slot
                ON scheduled_checks (user_email, scheduled_at)
                WHERE status NOT IN ('cancelled')
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS job_history (
                    job_id       TEXT PRIMARY KEY,
                    user_email   TEXT,
                    operation    TEXT NOT NULL,
                    website      TEXT NOT NULL,
                    status       TEXT NOT NULL,
                    flags        INTEGER DEFAULT 0,
                    caution      INTEGER DEFAULT 0,
                    submitted_at TIMESTAMPTZ,
                    finished_at  TIMESTAMPTZ DEFAULT NOW(),
                    report       JSONB
                )
            """)
            cur.execute("""
                ALTER TABLE job_history ADD COLUMN IF NOT EXISTS report JSONB
            """)
            cur.execute("""
                ALTER TABLE scheduled_checks ADD COLUMN IF NOT EXISTS report_format TEXT DEFAULT 'html'
            """)
            cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS timezone TEXT DEFAULT 'UTC'")
            cur.execute("ALTER TABLE job_history ADD COLUMN IF NOT EXISTS unlocked BOOLEAN DEFAULT FALSE")
            cur.execute("ALTER TABLE job_history ADD COLUMN IF NOT EXISTS last_emailed_at TIMESTAMPTZ")
        conn.commit()

try:
    init_db()
except Exception as _db_err:
    print(f'[WARN] DB init skipped: {_db_err}')


# ---------------------------------------------------------------------------
# OID cert cache helpers (Postgres, 24-hour TTL)
# ---------------------------------------------------------------------------

def _oid_cache_key(name: str) -> str:
    return _clean_oid_search(name).lower().strip()


def get_cached_oid(operation_name: str):
    """Return {'cert': dict, 'cached_at': str} if a fresh cache entry exists, else None."""
    if not DATABASE_URL:
        return None
    key = _oid_cache_key(operation_name)
    try:
        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT cert_data, cached_at FROM oid_cache "
                    "WHERE cache_key = %s AND cached_at > NOW() - INTERVAL '24 hours'",
                    (key,)
                )
                row = cur.fetchone()
        if row:
            return {'cert': row[0], 'cached_at': row[1].strftime('%Y-%m-%d %H:%M UTC')}
    except Exception as e:
        print(f'[WARN] oid_cache read failed: {e}')
    return None


def save_oid_cache(operation_name: str, cert: dict):
    """Upsert cert data into oid_cache."""
    if not DATABASE_URL:
        return
    key = _oid_cache_key(operation_name)
    try:
        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO oid_cache (cache_key, operation, cert_data, cached_at)
                       VALUES (%s, %s, %s::jsonb, NOW())
                       ON CONFLICT (cache_key) DO UPDATE SET
                           cert_data = EXCLUDED.cert_data,
                           cached_at = NOW()""",
                    (key, operation_name, json.dumps(cert))
                )
            conn.commit()
    except Exception as e:
        print(f'[WARN] oid_cache save failed: {e}')


# ---------------------------------------------------------------------------
# API key helpers
# Keys are SHA-256 hashed for fast lookup; shown to user only once at creation.
# Format: owc_live_<32 hex chars>
# ---------------------------------------------------------------------------

def _hash_api_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode()).hexdigest()


def generate_api_key(email: str, name: str = '') -> dict:
    """Create a new API key, persist hash to DB, return {key_id, raw_key}."""
    raw_key = 'owc_live_' + secrets.token_hex(32)
    key_id  = uuid.uuid4().hex[:12]
    key_hash = _hash_api_key(raw_key)
    if DATABASE_URL:
        try:
            with db_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO api_keys (key_id, key_hash, email, name) VALUES (%s, %s, %s, %s)",
                        (key_id, key_hash, email.lower(), name or 'My API Key')
                    )
                conn.commit()
        except Exception as e:
            print(f'[WARN] api_key save failed: {e}')
    return {'key_id': key_id, 'raw_key': raw_key}


def _get_api_key_from_request() -> str | None:
    """Extract raw API key from Authorization: Bearer or X-API-Key header."""
    auth = request.headers.get('Authorization', '')
    if auth.startswith('Bearer '):
        return auth[7:].strip() or None
    return request.headers.get('X-API-Key', '').strip() or None


def _api_rate_limit_ok(email: str, per_hour: int = 60) -> bool:
    """Return True if fewer than per_hour jobs were submitted in the last hour."""
    if not DATABASE_URL:
        return True
    try:
        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT COUNT(*) FROM job_history
                       WHERE user_email = %s AND submitted_at > NOW() - INTERVAL '1 hour'""",
                    (email.lower(),)
                )
                row = cur.fetchone()
        return (row[0] if row else 0) < per_hour
    except Exception:
        return True  # fail open on DB error


def verify_api_key(raw_key: str) -> str | None:
    """Return the email associated with a valid API key, or None."""
    if not raw_key or not DATABASE_URL:
        return None
    key_hash = _hash_api_key(raw_key)
    try:
        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT email FROM api_keys WHERE key_hash = %s",
                    (key_hash,)
                )
                row = cur.fetchone()
            if row:
                # Update last_used_at asynchronously — fire and forget
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE api_keys SET last_used_at = NOW() WHERE key_hash = %s",
                        (key_hash,)
                    )
                conn.commit()
        return row[0] if row else None
    except Exception:
        return None


def list_api_keys(email: str) -> list:
    """Return list of {key_id, name, created_at, last_used_at, key_prefix} for an email."""
    if not DATABASE_URL:
        return []
    try:
        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT key_id, key_hash, name, created_at, last_used_at "
                    "FROM api_keys WHERE email = %s ORDER BY created_at DESC",
                    (email.lower(),)
                )
                rows = cur.fetchall()
        return [
            {
                'key_id':      r[0],
                'key_prefix':  'owc_live_' + r[1][:8] + '…',
                'name':        r[2],
                'created_at':  r[3].strftime('%Y-%m-%d') if r[3] else '',
                'last_used_at': r[4].strftime('%Y-%m-%d %H:%M UTC') if r[4] else 'Never',
            }
            for r in rows
        ]
    except Exception:
        return []


def revoke_api_key(email: str, key_id: str):
    """Delete an API key owned by the given email."""
    if not DATABASE_URL:
        return
    try:
        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM api_keys WHERE key_id = %s AND email = %s",
                    (key_id, email.lower())
                )
            conn.commit()
    except Exception:
        pass


def get_session_token():
    if 'token' not in session:
        session['token'] = uuid.uuid4().hex
    return session['token']


def get_logged_in_email():
    return session.get('user_email')

def is_admin(email):
    return bool(email) and email.lower() == ADMIN_EMAIL.lower()

def get_user_credits(email):
    if is_admin(email):
        return 99999
    if not DATABASE_URL:
        return 0
    try:
        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute('SELECT credits FROM users WHERE email = %s', (email.lower(),))
                row = cur.fetchone()
        return row[0] if row else 0
    except Exception:
        return 0

def deduct_user_credit(email):
    if is_admin(email) or not DATABASE_URL:
        return
    try:
        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    'UPDATE users SET credits = GREATEST(0, credits - 1) WHERE email = %s',
                    (email.lower(),)
                )
            conn.commit()
    except Exception:
        pass


def _mark_job_unlocked(job_id: str):
    """Persist unlocked=True to DB so the gate survives app restarts."""
    if not DATABASE_URL:
        return
    try:
        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE job_history SET unlocked = TRUE WHERE job_id = %s",
                    (job_id,)
                )
            conn.commit()
    except Exception as exc:
        print(f'[DB] _mark_job_unlocked({job_id}): {exc}')


# ---------------------------------------------------------------------------
# Scheduled checker — slot helpers
# ---------------------------------------------------------------------------

def _snap_to_slot(dt: datetime) -> datetime:
    """Round a datetime UP to the next 10-minute aligned UTC slot."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    if dt.second > 0 or dt.microsecond > 0:
        dt = dt.replace(second=0, microsecond=0) + timedelta(minutes=1)
    else:
        dt = dt.replace(second=0, microsecond=0)
    rem = dt.minute % 10
    if rem == 0:
        return dt
    return dt + timedelta(minutes=(10 - rem))


def get_booked_slots_for_day(date_str: str) -> list:
    """Return list of ISO UTC strings of booked/running slots for a given date (YYYY-MM-DD)."""
    if not DATABASE_URL:
        return []
    try:
        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT scheduled_at FROM scheduled_checks
                       WHERE DATE(scheduled_at AT TIME ZONE 'UTC') = %s::date
                       AND status NOT IN ('cancelled')""",
                    (date_str,)
                )
                rows = cur.fetchall()
        return [r[0].strftime('%Y-%m-%dT%H:%M:00Z') for r in rows]
    except Exception as e:
        print(f'[SCHED] get_booked_slots error: {e}')
        return []


SLOT_START_HOUR = 8   # 8am UTC — first slot of day
SLOT_END_HOUR   = 21  # 9pm UTC — exclusive (last slot: 20:50)

def _advance_to_op_hours(dt: datetime) -> datetime:
    """Snap dt to the next valid 10-min slot within 8am–9pm UTC operating hours."""
    dt = _snap_to_slot(dt)
    while True:
        if dt.hour < SLOT_START_HOUR:
            dt = dt.replace(hour=SLOT_START_HOUR, minute=0, second=0, microsecond=0)
        elif dt.hour >= SLOT_END_HOUR:
            dt = (dt + timedelta(days=1)).replace(hour=SLOT_START_HOUR, minute=0, second=0, microsecond=0)
        else:
            break
    return dt


def get_next_available_slot() -> datetime | None:
    """Return the next available 10-min slot in 8am–9pm UTC, starting >= now + 5 minutes."""
    now       = datetime.now(timezone.utc)
    candidate = _advance_to_op_hours(now + timedelta(minutes=5))
    end       = now + timedelta(days=30)
    booked    = set()
    if DATABASE_URL:
        try:
            with db_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """SELECT scheduled_at FROM scheduled_checks
                           WHERE scheduled_at BETWEEN %s AND %s
                           AND status NOT IN ('cancelled')""",
                        (candidate, end)
                    )
                    rows = cur.fetchall()
            booked = {
                (r[0].replace(tzinfo=timezone.utc) if r[0].tzinfo is None else r[0])
                for r in rows
            }
        except Exception as e:
            print(f'[SCHED] get_next_available error: {e}')
    while candidate <= end:
        if candidate not in booked:
            return candidate
        candidate = _advance_to_op_hours(candidate + timedelta(minutes=10))
    return None


def _resend_send(to_email: str, subject: str, html_body: str, from_addr: str = None,
                 attachments: list = None):
    """Send email via Resend HTTP API. Returns True on success, error string on failure."""
    import requests as _req
    if not RESEND_API_KEY:
        print('[EMAIL] RESEND_API_KEY not set')
        return 'RESEND_API_KEY not configured'
    payload = {
        'from':    from_addr or REPORT_FROM,
        'to':      [to_email],
        'subject': subject,
        'html':    html_body,
    }
    if attachments:
        payload['attachments'] = attachments
    try:
        resp = _req.post(
            'https://api.resend.com/emails',
            headers={'Authorization': f'Bearer {RESEND_API_KEY}', 'Content-Type': 'application/json'},
            json=payload,
            timeout=15,
        )
        if resp.status_code in (200, 201):
            print(f'[EMAIL] Sent via Resend to {to_email}: {subject}')
            return True
        err = resp.text[:200]
        print(f'[EMAIL] Resend error {resp.status_code}: {err}')
        return err
    except Exception as exc:
        err = str(exc)
        print(f'[EMAIL] Resend exception: {err}')
        return err


# Keep _smtp_send as an alias so nothing else breaks — now routes through Resend
def _smtp_send(smtp_user, smtp_pass, to_email, subject, html_body):
    return _resend_send(to_email, subject, html_body)


def _build_report_html(operation: str, report: dict, report_url: str) -> tuple[str, str]:
    """Return (subject, html_body) for a completed check report email."""
    flags   = len(report.get('flagged', []))
    caution = len(report.get('caution', []))
    if flags > 0:
        badge   = (f'<span style="background:#FEE2E2;color:#DC2626;padding:3px 10px;'
                   f'border-radius:8px;font-weight:700">{flags} flag{"s" if flags!=1 else ""} found</span>')
        summary = f'{flags} potential non-compliance item{"s" if flags!=1 else ""} may need your review.'
    elif caution > 0:
        badge   = (f'<span style="background:#FEF3C7;color:#D97706;padding:3px 10px;'
                   f'border-radius:8px;font-weight:700">{caution} caution item{"s" if caution!=1 else ""}</span>')
        summary = f'{caution} item{"s" if caution!=1 else ""} surfaced for review. No definitive violations found.'
    else:
        badge   = ('<span style="background:#E0F7E9;color:#3DAD62;padding:3px 10px;'
                   'border-radius:8px;font-weight:700">No flags found</span>')
        summary = 'All organic claims appear to match the OID certificate.'
    subject   = f'Your Organic Web Check \u2014 {operation}'
    html_body = f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"></head>
<body style="font-family:system-ui,sans-serif;background:#F9FAFB;margin:0;padding:0">
<div style="max-width:580px;margin:40px auto;background:#fff;border:1px solid #DDE5DD;border-radius:20px;overflow:hidden">
  <div style="background:#2B2438;padding:26px 30px">
    <h1 style="color:#fff;font-size:1.15rem;margin:0;font-weight:800">Organic Web Checker</h1>
    <p style="color:rgba(255,255,255,.65);font-size:.82rem;margin:5px 0 0">Your compliance check is ready</p>
  </div>
  <div style="padding:26px 30px">
    <p style="font-size:.78rem;color:#6B7280;margin-bottom:4px;text-transform:uppercase;letter-spacing:.06em">Operation</p>
    <p style="font-size:1.05rem;font-weight:700;color:#2B2438;margin-bottom:18px">{operation}</p>
    <p style="font-size:.78rem;color:#6B7280;margin-bottom:6px;text-transform:uppercase;letter-spacing:.06em">Result</p>
    <p style="margin-bottom:6px">{badge}</p>
    <p style="font-size:.82rem;color:#6B7280;margin-bottom:26px">{summary}</p>
    <a href="{report_url}" style="display:inline-block;background:#6F5EF7;color:#fff;text-decoration:none;padding:13px 26px;border-radius:14px;font-weight:700;font-size:.88rem;box-shadow:0 4px 14px rgba(111,94,247,.25)">View Full Report &rarr;</a>
    <p style="font-size:.72rem;color:#9CA3AF;margin-top:26px;padding-top:18px;border-top:1px solid #DDE5DD">
      Generated by Organic Web Checker &mdash; identifies potential items for review, not a legal determination.<br>
      &copy; 2026 Healer&rsquo;s Find LLC
    </p>
  </div>
</div>
</body></html>"""
    return subject, html_body


def _send_report_email(user_email: str, operation: str, check_id: str, report: dict) -> bool:
    """Send a completed-check report email."""
    report_url = f'{APP_BASE_URL}/scheduled-report/{check_id}'
    subject, html_body = _build_report_html(operation, report, report_url)
    return _resend_send(user_email, subject, html_body) is True


def _send_report_email_md(user_email: str, operation: str, check_id: str, report: dict) -> bool:
    """Send report as a .md file attachment via Resend."""
    import base64
    md_text    = report_to_markdown(report)
    flags      = len(report.get('flagged', []))
    caution    = len(report.get('caution', []))
    subject    = f'Organic Web Checker — {operation} ({flags} flags, {caution} cautions)'
    filename   = f'owc-report-{check_id}.md'
    report_url = f'{APP_BASE_URL}/scheduled-report/{check_id}'
    body_html  = (f'<p>Your scheduled check for <strong>{operation}</strong> is complete.</p>'
                  f'<p>{flags} flag(s) &middot; {caution} caution(s)</p>'
                  f'<p><a href="{report_url}">View full report online</a></p>'
                  f'<p>Markdown report attached as <code>{filename}</code>.</p>')
    attachments = [{'filename': filename,
                    'content': base64.b64encode(md_text.encode()).decode()}]
    r = _resend_send(user_email, subject, body_html, attachments=attachments)
    return r is True


def _send_welcome_email(to_email: str) -> bool:
    """Send a welcome email from hello@organicwebchecker.com on new account creation."""
    html_body = f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"></head>
<body style="font-family:system-ui,sans-serif;background:#F9FAFB;margin:0;padding:0">
<div style="max-width:560px;margin:40px auto;background:#fff;border:1px solid #DDE5DD;border-radius:20px;overflow:hidden">
  <div style="background:#2B2438;padding:26px 30px">
    <h1 style="color:#fff;font-size:1.15rem;margin:0;font-weight:800">Organic Web Checker</h1>
    <p style="color:rgba(255,255,255,.65);font-size:.82rem;margin:5px 0 0">Welcome — your account is ready</p>
  </div>
  <div style="padding:26px 30px">
    <p style="font-size:1rem;color:#1F2937;margin-bottom:14px">Thanks for signing up.</p>
    <p style="font-size:.88rem;color:#6B7280;line-height:1.6;margin-bottom:22px">
      Your account includes free trial credits to run your first checks. Each check compares a certified
      operation&rsquo;s website claims against their live USDA OID certificate and returns a categorized compliance report.
    </p>
    <a href="{APP_BASE_URL}" style="display:inline-block;background:#6F5EF7;color:#fff;text-decoration:none;padding:13px 26px;border-radius:14px;font-weight:700;font-size:.88rem;box-shadow:0 4px 14px rgba(111,94,247,.25)">Run Your First Check &rarr;</a>
    <p style="font-size:.78rem;color:#6B7280;margin-top:22px;line-height:1.5">
      Questions? Reply to <a href="mailto:support@organicwebchecker.com" style="color:#6F5EF7">support@organicwebchecker.com</a> — we&rsquo;re here to help.
    </p>
    <p style="font-size:.72rem;color:#9CA3AF;margin-top:18px;padding-top:14px;border-top:1px solid #DDE5DD">
      &copy; 2026 Healer&rsquo;s Find LLC &mdash; Organic Web Checker
    </p>
  </div>
</div>
</body></html>"""
    return _resend_send(to_email, 'Welcome to Organic Web Checker', html_body, from_addr=HELLO_FROM) is True


@app.context_processor
def inject_user():
    email = session.get('user_email')
    return {
        'user_email': email,
        'user_is_admin': is_admin(email) if email else False,
        'user_credits': get_user_credits(email) if email else 0,
    }


# ---------------------------------------------------------------------------
# Job queue (in-memory, single-server)
# ---------------------------------------------------------------------------

jobs = {}
jobs_lock = threading.Lock()
_check_semaphore = threading.Semaphore(1)

# Certifier verification background jobs (in-memory; cleared on restart)
_cert_verify_jobs  = {}
_cert_verify_lock  = threading.Lock()
# Separate semaphore so certifier checks don't queue behind long compliance checks
_cert_semaphore    = threading.Semaphore(1)

# In-memory session stats (resets on redeploy; persistent DB coming with Stripe)
stats = {'checks_run': 0, 'flags_found': 0, 'caution_found': 0}


def _run_cert_verify(request_id: str, org_name: str, db_id: int):
    """Background thread: verify certifying agent via USDA OID, update DB."""
    with _cert_semaphore:
        result = verify_certifying_agent(org_name)

    db_status = 'approved' if result == 'verified' else 'pending'
    with _cert_verify_lock:
        if request_id in _cert_verify_jobs:
            _cert_verify_jobs[request_id]['status'] = result

    if DATABASE_URL:
        try:
            with db_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE certifier_requests SET status = %s WHERE id = %s",
                        (db_status, db_id)
                    )
                conn.commit()
        except Exception as e:
            print(f'[WARN] cert verify DB update failed: {e}')

    print(f'[CERT VERIFY] {org_name} → {result} (db_id={db_id})')


def _run_scheduled_job(check_id: str, user_email: str, operation: str, website: str, report_format: str = 'html'):
    """Wrapper: creates a job, runs it, saves result to DB, sends email."""
    job_id = uuid.uuid4().hex[:8]
    with jobs_lock:
        jobs[job_id] = {
            'id': job_id, 'operation': operation, 'website': website,
            'status': 'queued', 'report': None,
            'submitted_at': datetime.now(timezone.utc).isoformat(),
            'finished_at': None, 'unlocked': True,
        }
    # Record job_id in DB
    if DATABASE_URL:
        try:
            with db_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute('UPDATE scheduled_checks SET job_id=%s WHERE id=%s',
                                (job_id, check_id))
                conn.commit()
        except Exception as e:
            print(f'[SCHED] job_id update failed: {e}')
    # Run the check (blocks; uses _check_semaphore)
    _run_job(job_id, operation, website)
    # Collect result
    with jobs_lock:
        job = dict(jobs.get(job_id, {}))
    report = job.get('report', {})
    status = 'done' if job.get('status') == 'done' and 'error' not in report else 'error'
    # Deduct credit on success
    if status == 'done':
        deduct_user_credit(user_email)
    # Persist to DB
    if DATABASE_URL:
        try:
            with db_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        'UPDATE scheduled_checks SET status=%s, report=%s::jsonb WHERE id=%s',
                        (status, json.dumps(report), check_id)
                    )
                conn.commit()
        except Exception as e:
            print(f'[SCHED] result save failed: {e}')
    # Send email on success
    if status == 'done':
        if report_format == 'md':
            _send_report_email_md(user_email, operation, check_id, report)
        else:
            _send_report_email(user_email, operation, check_id, report)
    print(f'[SCHED] check_id={check_id} status={status} op={operation} fmt={report_format}')


def _scheduler_loop():
    """Poll every 60s for scheduled checks that are due, fire them as threads."""
    time.sleep(30)  # brief startup delay
    print('[SCHED] Loop started.')
    iteration = 0
    while True:
        iteration += 1
        if DATABASE_URL:
            try:
                with db_conn() as conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            """SELECT id, user_email, operation_name, website_url,
                                      COALESCE(report_format, 'html')
                               FROM scheduled_checks
                               WHERE status = 'scheduled' AND scheduled_at <= NOW()
                               ORDER BY scheduled_at LIMIT 5"""
                        )
                        rows = cur.fetchall()
                if rows:
                    print(f'[SCHED] tick={iteration} found {len(rows)} due job(s)')
                elif iteration % 10 == 0:
                    # Heartbeat every ~10 minutes so logs show the loop is alive
                    with db_conn() as conn:
                        with conn.cursor() as cur:
                            cur.execute("SELECT COUNT(*) FROM scheduled_checks WHERE status='scheduled'")
                            pending = cur.fetchone()[0]
                    print(f'[SCHED] heartbeat tick={iteration} pending_scheduled={pending}')
                for row in rows:
                    check_id, user_email, operation, website, report_format = row
                    # Atomic claim — prevents double-fire on restart
                    with db_conn() as conn:
                        with conn.cursor() as cur:
                            cur.execute(
                                "UPDATE scheduled_checks SET status='running' "
                                "WHERE id=%s AND status='scheduled'",
                                (check_id,)
                            )
                            claimed = cur.rowcount
                        conn.commit()
                    if claimed:
                        threading.Thread(
                            target=_run_scheduled_job,
                            args=(check_id, user_email, operation, website, report_format),
                            daemon=True
                        ).start()
                        print(f'[SCHED] Fired check_id={check_id} op={operation} fmt={report_format}')
            except Exception as e:
                print(f'[SCHED] Loop error: {e}')
        time.sleep(60)


_scheduler_thread = threading.Thread(target=_scheduler_loop, daemon=True)
_scheduler_thread.start()


def _run_job(job_id: str, operation: str, website: str, use_cache: bool = False):
    def _progress(step: int, msg: str):
        with jobs_lock:
            jobs[job_id]['step'] = step
            jobs[job_id]['step_msg'] = msg

    with _check_semaphore:
        with jobs_lock:
            jobs[job_id]['status'] = 'running'
            jobs[job_id]['step'] = 1
            jobs[job_id]['step_msg'] = 'Connecting to USDA Organic Integrity Database…'
        try:
            cert          = None
            oid_source    = 'live'
            oid_cached_at = None

            # ── Cache-first: user opted to skip live OID ──────────────────────
            if use_cache:
                cached = get_cached_oid(operation)
                if cached:
                    cert          = cached['cert']
                    oid_source    = 'cached'
                    oid_cached_at = cached['cached_at']
                    _progress(2, f"OID data loaded from cache ({oid_cached_at})")
                else:
                    raise ValueError(
                        f"No cached OID data found for '{operation}'. "
                        "Uncheck 'Use cached OID data' and run a live check first to build the cache."
                    )

            # ── Live OID fetch (with auto-fallback to cache on timeout) ────────
            if cert is None:
                try:
                    cert = get_oid_cert(operation)
                    if 'error' not in cert:
                        save_oid_cache(operation, cert)
                except Exception as oid_err:
                    if 'timeout' in str(oid_err).lower():
                        fallback = get_cached_oid(operation)
                        if fallback:
                            cert          = fallback['cert']
                            oid_source    = 'cached'
                            oid_cached_at = fallback['cached_at']
                            _progress(2, f"OID timed out — using cached data from {oid_cached_at}")
                        else:
                            raise
                    else:
                        raise

            report = run_check(operation, website, cert=cert, progress_callback=_progress)
            report['oid_source']    = oid_source
            report['oid_cached_at'] = oid_cached_at

            with jobs_lock:
                jobs[job_id]['status'] = 'done'
                jobs[job_id]['report'] = report
            if 'error' not in report:
                with jobs_lock:
                    stats['checks_run'] += 1
                    stats['flags_found']   += len(report.get('flagged', []))
                    stats['caution_found'] += len(report.get('caution', []))
        except Exception as e:
            with jobs_lock:
                jobs[job_id]['status'] = 'error'
                jobs[job_id]['report'] = {'error': str(e)}
        finally:
            with jobs_lock:
                jobs[job_id]['finished_at'] = datetime.now(timezone.utc).isoformat()
            if DATABASE_URL:
                try:
                    with jobs_lock:
                        j = dict(jobs.get(job_id, {}))
                    report = j.get('report') or {}
                    flags   = len(report.get('flagged',  [])) if 'error' not in report else 0
                    caution = len(report.get('caution',  [])) if 'error' not in report else 0
                    with db_conn() as conn:
                        with conn.cursor() as cur:
                            cur.execute("""
                                INSERT INTO job_history
                                    (job_id, user_email, operation, website, status, flags, caution,
                                     submitted_at, finished_at, report)
                                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                                ON CONFLICT (job_id) DO NOTHING
                            """, (
                                job_id, j.get('user_email', ''), operation, website,
                                j.get('status', 'error'), flags, caution,
                                j.get('submitted_at'), j.get('finished_at'),
                                json.dumps(report) if 'error' not in report else None,
                            ))
                        conn.commit()
                except Exception as _he:
                    print(f'[HISTORY] save failed: {_he}')


# ---------------------------------------------------------------------------
# Markdown report generator
# ---------------------------------------------------------------------------

def report_to_markdown(report: dict) -> str:
    now      = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    flagged  = sorted(report.get('flagged',   []), key=lambda x: x['title'])
    caution  = sorted(report.get('caution',   []), key=lambda x: x['title'])
    marketing= sorted(report.get('marketing', []), key=lambda x: x['title'])
    verified = sorted(report.get('verified',  []), key=lambda x: x['title'])
    certs    = sorted(report.get('cert_products', []))

    lines = [
        "# ORGANIC WEB CHECKER — COMPLIANCE REPORT",
        f"Generated: {now}",
        "Regulatory reference: 7 CFR Part 205 — USDA National Organic Program",
        "",
        "## Operation Details",
        f"- **Name:** {report.get('operation', '')}",
        f"- **Certifier:** {report.get('certifier', '')}",
        f"- **Status:** {report.get('status', '')}",
        f"- **Location:** {report.get('location', '')}",
        f"- **Cert Scope:** {', '.join(report.get('scope', [])) or 'Not detected'}"
        + (f" — {report['business_type']}" if report.get('business_type') else ""),
        f"- **Website:** {report.get('website_url', '')}",
        f"- **OID Data:** {'Cached (' + report.get('oid_cached_at', '') + ') — verify live at organic.ams.usda.gov' if report.get('oid_source') == 'cached' else 'Live'}",
        "",
        "## Summary",
        "| Category | Count | Severity |",
        "|----------|-------|----------|",
        f"| OID Certified Products | {report.get('cert_product_count', 0)} | — |",
        f"| Website Organic Claims | {report.get('website_organic_count', 0)} | — |",
        f"| ✅ Confirmed on Certificate | {len(verified)} | None |",
        f"| 🔴 Non-Compliance Risk | {len(flagged)} | High — ref § 205.307 |",
        f"| 🟡 Name Variation / Caution | {len(caution)} | Review advised |",
        f"| 🟠 Marketing Language | {len(marketing)} | Review — ref § 205.305–306 |",
        "",
    ]

    # ── Handling scope notice ──────────────────────────────────────────────
    if 'HANDLING' in report.get('scope', []):
        bt = report.get('business_type', '')
        if bt == 'Retailer':
            lines += [
                "## ⓘ HANDLING OPERATION SCOPE NOTICE — CERTIFIED RETAILER",
                "This operation is an NOP-certified handler (Retailer type). Its retail website is part of",
                "the certified operation and must be reflected in the Organic System Plan (OSP).",
                "The retail exemption (7 CFR § 205.101(a)(2)) does NOT apply — it covers only non-certified",
                "entities selling finished pre-certified products with no handling activity.",
                "NOTE: A website operating under a brand name different from the operation's registered name",
                "does not constitute a separate entity exempt from OSP coverage.",
                "Flagged items require verification that organic claims are congruent with the OID certificate;",
                "further OSP review with the certifier may be required.",
                "Ref: 7 CFR § 205.307 (misrepresentation); § 205.201 (OSP); SOE Rule 88 FR 2799 (eff. March 19, 2024)",
                "",
            ]
        elif bt == 'Importer':
            lines += [
                "## ⓘ HANDLING OPERATION SCOPE NOTICE — IMPORTER",
                "Post-SOE (eff. March 19, 2024), all importers of organic products must hold NOP handler",
                "certification. Website organic claims require both the importer's own NOP cert coverage",
                "and verified foreign certification for each imported product.",
                "Ref: 7 CFR § 205.201 (OSP); SOE Rule 88 FR 2799; § 205.2 (importer definition)",
                "",
            ]
        elif bt in ('Broker', 'Trader'):
            lines += [
                f"## ⓘ HANDLING OPERATION SCOPE NOTICE — {bt.upper()}",
                f"Post-SOE (eff. March 19, 2024), {bt.lower()}s handling organic products must be",
                "NOP-certified handlers. Website organic product listings require upstream supplier",
                "certification documented in the Organic System Plan (OSP).",
                "Ref: 7 CFR § 205.201 (OSP); SOE Rule 88 FR 2799; § 205.2 (handler definition)",
                "",
            ]
        else:
            lines += [
                "## ⓘ HANDLING OPERATION SCOPE NOTICE",
                "This operation is certified as a handler (broker, distributor, importer, or processor).",
                "Handler certificates typically list products at a general category level; specific branded",
                "products are documented in the Organic System Plan (OSP) held by the certifier.",
                "Flagged items below require verification of upstream supplier certification in the OSP.",
                "Ref: 7 CFR § 205.201 (OSP requirements); SOE Rule (eff. March 19, 2024)",
                "",
            ]

    # ── Non-compliance flags ───────────────────────────────────────────────
    if flagged:
        if 'HANDLING' in report.get('scope', []):
            flag_note = ("Products marketed as organic on the website but not found on this operation's "
                         "OID certificate. For handling operations: verify upstream supplier certification "
                         "in the Organic System Plan (OSP).")
            flag_ref  = "Ref: 7 CFR § 205.307 (misrepresentation); § 205.201 (OSP supplier documentation); SOE Rule § 205.2"
        else:
            flag_note = "Products marketed as organic on the website but NOT found on the OID certificate."
            flag_ref  = "Ref: 7 CFR § 205.307 (misrepresentation); § 205.303–305 (labeling requirements)"
        lines += [
            f"## 🔴 NON-COMPLIANCE RISK ({len(flagged)} items)",
            flag_note,
            flag_ref,
            "",
        ]
        for i, item in enumerate(flagged, 1):
            url = f" → {item['url']}" if item.get('url') else ""
            lines.append(f"{i}. **{item['title']}**{url}")
        lines.append("")
    else:
        lines += ["## ✅ NO NON-COMPLIANCE FLAGS",
                  "All specific organic product claims match the OID certificate.", ""]

    # ── Caution — name variations / general cert ──────────────────────────
    if caution:
        lines += [
            f"## 🟡 NAME VARIATION / CAUTION ({len(caution)} items)",
            "Products that closely resemble certified items but with possible name differences.",
            "Not an NOP violation — requires certifier alignment review.",
            "",
        ]
        for i, item in enumerate(caution, 1):
            url = f" → {item['url']}" if item.get('url') else ""
            reason = " [GENERAL CERT]" if item.get('_reason') == 'general_cert' else ""
            lines.append(f"{i}. {item['title']}{reason}{url}")
        lines.append("")

    # ── Marketing language ─────────────────────────────────────────────────
    if marketing:
        lines += [
            f"## 🟠 MARKETING LANGUAGE REVIEW ({len(marketing)} items)",
            "Use of 'organic' in marketing/category context rather than specific product claims.",
            "Certifier should have approved this language in the Organic System Plan.",
            "Ref: 7 CFR § 205.305–306; USDA NOP Guidance Document 5001",
            "",
        ]
        for i, item in enumerate(marketing, 1):
            url = f" → {item['url']}" if item.get('url') else ""
            lines.append(f"{i}. {item['title']}{url}")
        lines.append("")

    # ── Verified ───────────────────────────────────────────────────────────
    lines += [f"## ✅ CONFIRMED ON CERTIFICATE ({len(verified)} items)",
              "Website organic products that match the current OID certificate.", ""]
    for i, item in enumerate(verified, 1):
        url = f" → {item['url']}" if item.get('url') else ""
        lines.append(f"{i}. {item['title']}{url}")
    lines.append("")

    # ── OID certificate ────────────────────────────────────────────────────
    lines += [f"## OID CERTIFICATE PRODUCTS ({len(certs)} items)",
              "All products on the current USDA Organic Integrity Database certificate.", ""]
    for i, item in enumerate(certs, 1):
        lines.append(f"{i}. {item}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Global CSS — bioluminescent theme
# ---------------------------------------------------------------------------

GLOBAL_CSS = """
  /* ── Design tokens ───────────────────────────────────────────────────── */
  :root {
    --bg:           #F7F4FF;
    --surface:      #FFFFFF;
    --card-bg:      #FFFFFF;
    --border:       #E4DEFF;
    --primary:      #8B7CFF;
    --primary-dim:  #A99DFF;
    --primary-dark: #6F5EF7;
    --teal:         #8B7CFF;
    --green:        #7BCF8A;
    --amber:        #D97706;
    --red:          #DC2626;
    --lavender:     #EEE8FF;
    --text:         #2B2438;
    --muted:        #6E6780;
    --dim:          #C4B8E8;
    /* Legacy aliases kept for backward compat with existing templates */
    --neon:         #A99DFF;
    --neon-dim:     #8B7CFF;
    --neon-dark:    #6F5EF7;
    --cyan:         #8B7CFF;
    --glow:         rgba(139, 124, 255, 0.06);
    --red-glow:     rgba(220, 38, 38, 0.08);
    --soft-bg:      #EEE8FF;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  html { scroll-behavior: smooth; }
  body {
    font-family: 'Inter', system-ui, -apple-system, sans-serif;
    background: var(--bg); color: var(--text);
    min-height: 100vh;
  }

  /* ── Header ─────────────────────────────────────────────────────────── */
  header {
    background: #2B2438;
    border-bottom: 1px solid rgba(255,255,255,.08);
    box-shadow: 0 2px 16px rgba(43,36,56,.18);
    padding: 14px 32px;
    display: grid; grid-template-columns: 1fr auto 1fr; align-items: center; gap: 16px;
    position: sticky; top: 0; z-index: 100;
  }
  .header-logo {
    display: flex; align-items: center; gap: 12px; text-decoration: none;
    overflow: hidden;
  }
  .header-logo-icon { width: 144px; height: 144px; flex-shrink: 0; border-radius: 14px; }
  .header-wordmark  { font-size: 1.75rem; font-weight: 800; }
  header h1 { font-size: 1.75rem; font-weight: 800; }
  /* Two-color wordmark — "Organic" accent lavender, "Web Checker" white */
  .wm-organic { color: #A99DFF; }
  .wm-checker { color: #ffffff; }
  /* Footer is dark bg — keep white */
  .footer-brand-name .wm-checker { color: rgba(255,255,255,.85); }
  header p  { font-size: .78rem; color: rgba(255,255,255,.55); margin-top: 2px; }

  .header-center { display: flex; flex-direction: column; align-items: center; gap: 4px; }
  .header-credits-wrap { display: flex; justify-content: center; }
  .header-credit-badge {
    font-size: .7rem; font-weight: 700; letter-spacing: .02em;
    background: rgba(139,124,255,.18); color: #C4B8E8;
    border: 1px solid rgba(139,124,255,.3); border-radius: 20px;
    padding: 2px 12px; white-space: nowrap;
  }

  .header-right { display: flex; align-items: center; gap: 10px; justify-self: end; }
  .header-nav   { display: flex; gap: 2px; justify-content: center; }
  .nav-link {
    color: rgba(255,255,255,.7); font-size: .82rem; text-decoration: none;
    padding: 6px 12px; border-radius: 8px;
    transition: color .15s, background .15s; font-weight: 500;
  }
  .nav-link:hover { color: #fff; background: rgba(255,255,255,.1); }
  .nav-link.active { color: #fff; background: rgba(255,255,255,.14); font-weight: 600; }

  .header-cta-btn {
    background: #6F5EF7; color: #fff;
    border: none; border-radius: 14px;
    padding: 10px 22px; font-size: .82rem; font-weight: 700;
    cursor: pointer; text-decoration: none;
    transition: background .15s, box-shadow .15s;
    box-shadow: 0 4px 14px rgba(111,94,247,.3);
    height: 40px; display: inline-flex; align-items: center;
  }
  .header-cta-btn:hover { background: #5A4CE0; box-shadow: 0 6px 20px rgba(111,94,247,.4); }

  .header-icon-wrap { position: relative; flex-shrink: 0; }
  .header-icon-btn {
    background: none; border: 1px solid rgba(255,255,255,.18); cursor: pointer;
    padding: 5px; border-radius: 9px; display: block;
    transition: border-color .15s, box-shadow .15s;
    overflow: hidden;
  }
  .header-icon-btn:hover { border-color: rgba(139,124,255,.5); box-shadow: 0 0 0 3px rgba(139,124,255,.12); }
  .header-icon { width: 48px; height: 48px; display: block; }
  .header-dropdown {
    position: absolute; top: calc(100% + 8px); right: 0;
    background: var(--surface); border: 1px solid var(--border);
    border-radius: 16px;
    box-shadow: 0 8px 30px rgba(31,41,55,.10);
    min-width: 190px; z-index: 200; display: none; overflow: hidden;
  }
  .header-dropdown.open { display: block; }
  .dropdown-item {
    display: block; padding: 11px 17px;
    font-size: .84rem; color: var(--text);
    text-decoration: none; border-bottom: 1px solid var(--border);
  }
  .dropdown-item:last-child { border-bottom: none; }
  .dropdown-item:hover { background: var(--lavender); color: var(--primary-dark); }

  /* ── Layout ──────────────────────────────────────────────────────────── */
  .page-main { max-width: 920px; margin: 36px auto; padding: 0 24px; }

  /* ── Cards ───────────────────────────────────────────────────────────── */
  .card {
    background: var(--card-bg);
    border: 1px solid var(--border);
    border-radius: 20px; padding: 28px 32px;
    box-shadow: 0 8px 30px rgba(31,41,55,.06);
    margin-bottom: 22px;
    transition: box-shadow .2s;
  }
  .card:hover { box-shadow: 0 14px 36px rgba(31,41,55,.10); }

  /* ── Page headers ────────────────────────────────────────────────────── */
  .page-title    { font-size: 1.35rem; font-weight: 800; color: var(--primary); margin-bottom: 6px; font-family: 'Manrope', system-ui, sans-serif; }
  .page-subtitle { font-size: .87rem; color: var(--muted); margin-bottom: 28px; line-height: 1.6; }

  /* ── Form ────────────────────────────────────────────────────────────── */
  label {
    display: block; font-size: .78rem; font-weight: 700; margin-bottom: 6px;
    color: var(--muted); text-transform: uppercase; letter-spacing: .06em;
  }
  input[type=text], input[type=email], input[type=password] {
    width: 100%; padding: 11px 14px;
    background: var(--surface); border: 1.5px solid var(--border);
    border-radius: 14px; font-size: .94rem; margin-bottom: 16px; color: var(--text);
    transition: border-color .2s, box-shadow .2s;
  }
  input[type=text]::placeholder,
  input[type=email]::placeholder,
  input[type=password]::placeholder { color: var(--dim); }
  input:focus {
    outline: none; border-color: var(--primary);
    box-shadow: 0 0 0 3px rgba(111,94,247,.12);
  }
  .hint { font-size: .76rem; color: var(--muted); margin-top: -10px; margin-bottom: 16px; line-height: 1.5; }

  button[type=submit], .btn-primary {
    background: #6F5EF7; color: #fff;
    border: none; border-radius: 14px;
    padding: 13px 28px; font-size: .9rem;
    cursor: pointer; font-weight: 700;
    transition: background .15s, box-shadow .15s;
    box-shadow: 0 8px 20px rgba(111,94,247,.18);
    height: 48px; display: inline-flex; align-items: center;
  }
  button[type=submit]:hover, .btn-primary:hover {
    background: #5A4CE0;
    box-shadow: 0 10px 26px rgba(111,94,247,.26);
  }
  button[type=submit]:disabled { background: var(--dim); color: #fff; box-shadow: none; cursor: default; }

  /* ── Queue panel ─────────────────────────────────────────────────────── */
  .queue-panel {
    background: var(--card-bg); border: 1px solid var(--border);
    border-radius: 20px; margin-bottom: 22px;
    box-shadow: 0 8px 30px rgba(31,41,55,.06);
    position: relative;
  }
  .queue-header {
    padding: 12px 20px;
    background: var(--lavender); border-bottom: 1px solid var(--border);
    font-size: .72rem; font-weight: 700; text-transform: uppercase;
    letter-spacing: .09em; color: var(--primary);
    display: flex; align-items: center; gap: 8px;
    border-radius: 20px 20px 0 0;
  }
  .queue-count {
    background: rgba(111,94,247,.12); color: var(--primary);
    border: 1px solid rgba(111,94,247,.2); border-radius: 10px;
    padding: 1px 8px; font-size: .7rem; font-weight: 700;
  }
  .queue-list { list-style: none; }
  .queue-item {
    padding: 12px 20px; border-bottom: 1px solid var(--border);
    display: flex; align-items: center; gap: 12px; font-size: .875rem;
  }
  .queue-item:last-child { border-bottom: none; border-radius: 0 0 20px 20px; }
  .op-name { font-weight: 700; flex: 1; min-width: 0; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .site    { font-size: .76rem; color: var(--muted); }

  .status-pill {
    font-size: .68rem; font-weight: 700; padding: 3px 9px;
    border-radius: 20px; white-space: nowrap; flex-shrink: 0; border: 1px solid;
  }
  .status-queued  { background: #F1F5F9; color: var(--muted);  border-color: var(--border); }
  .status-running { background: #FFF8E1; color: #D97706; border-color: #FDE68A; animation: pulse 1.4s ease-in-out infinite; }
  .status-done    { background: #E8F5E9; color: #2E7D32; border-color: #A5D6A7; }
  .status-error   { background: #FEE2E2; color: #DC2626; border-color: #FECACA; }
  @keyframes pulse { 0%,100% { opacity:1; } 50% { opacity:.5; } }

  /* Animated checker loader */
  .checker-loader {
    position: relative; width: 40px; height: 40px; flex-shrink: 0;
    border-radius: 10px; overflow: hidden;
    background: var(--lavender); border: 1px solid rgba(111,94,247,.2);
  }
  .cb-p {
    position: absolute; width: 6px; height: 6px; border-radius: 50%;
    background: var(--primary);
  }
  .cb-p.p1 { animation: cb1 2s ease-in-out infinite; }
  .cb-p.p2 { animation: cb2 2s ease-in-out infinite .67s; }
  .cb-p.p3 { animation: cb3 2s ease-in-out infinite 1.33s; }
  @keyframes cb1 { 0%,100%{top:1px;left:9px}  50%{top:17px;left:25px} }
  @keyframes cb2 { 0%,100%{top:9px;left:33px} 50%{top:25px;left:17px} }
  @keyframes cb3 { 0%,100%{top:25px;left:1px} 50%{top:9px;left:17px} }

  .view-btn {
    font-size: .73rem; padding: 5px 12px;
    border: 1.5px solid rgba(111,94,247,.25); border-radius: 8px;
    color: var(--primary); background: var(--lavender);
    cursor: pointer; flex-shrink: 0; text-decoration: none;
    transition: background .15s; font-weight: 600;
  }
  .view-btn:hover { background: rgba(111,94,247,.14); }

  .empty-queue { padding: 22px; text-align: center; color: var(--muted); font-size: .84rem; }

  /* ── Report ──────────────────────────────────────────────────────────── */
  .report-header {
    display: flex; align-items: flex-start; justify-content: space-between;
    gap: 16px; margin-bottom: 22px;
    padding-bottom: 14px; border-bottom: 1px solid var(--border);
  }
  .report-op-name  { font-size: 1rem; font-weight: 800; color: var(--primary); font-family: 'Manrope', system-ui, sans-serif; }
  .report-meta-sub { font-size: .74rem; color: var(--muted); margin-top: 2px; }
  .download-btns { display: flex; gap: 7px; flex-shrink: 0; }
  .dl-btn {
    font-size: .7rem; padding: 5px 12px; border-radius: 10px;
    text-decoration: none; font-weight: 700; border: 1px solid;
    transition: background .15s; white-space: nowrap;
  }
  .dl-btn.md  { color: var(--primary); border-color: rgba(111,94,247,.3); background: rgba(111,94,247,.06); }
  .dl-btn.md:hover  { background: rgba(111,94,247,.14); }
  .dl-btn.pdf { color: var(--amber); border-color: rgba(217,119,6,.25);  background: rgba(217,119,6,.06); }
  .dl-btn.pdf:hover { background: rgba(217,119,6,.14); }

  .meta-grid {
    display: grid; grid-template-columns: 1fr 1fr;
    gap: 10px 28px; margin-bottom: 22px;
  }
  .meta-item label { color: var(--muted); font-size: .7rem; text-transform: uppercase; letter-spacing: .06em; margin-bottom: 2px; }
  .meta-item span  { font-size: .9rem; font-weight: 600; color: var(--text); }

  .stats {
    display: grid; grid-template-columns: repeat(4,1fr);
    gap: 10px; margin-bottom: 26px;
  }
  .stat {
    background: var(--lavender); border: 1px solid var(--border);
    border-radius: 16px; padding: 14px; text-align: center;
  }
  .stat .num { font-size: 1.9rem; font-weight: 900; line-height: 1; font-variant-numeric: tabular-nums; font-family: 'Manrope', system-ui, sans-serif; }
  .stat .lbl { font-size: .66rem; color: var(--muted); margin-top: 5px; text-transform: uppercase; letter-spacing: .06em; }
  .stat.flagged  { border-color: rgba(220,38,38,.15); background: #FEF2F2; }
  .stat.flagged  .num { color: var(--red); }
  .stat.verified .num { color: #7BCF8A; }

  .section-label {
    font-size: .7rem; font-weight: 700; text-transform: uppercase; letter-spacing: .1em;
    margin-bottom: 10px; margin-top: 24px;
    display: flex; align-items: center; gap: 8px;
  }
  .section-label:first-of-type { margin-top: 0; }
  .section-label.red    { color: var(--red); }
  .section-label.green  { color: #7BCF8A; }
  .section-label.cyan   { color: #7BCF8A; }
  .section-label.amber  { color: var(--amber); }
  .section-label.orange { color: #EA580C; }
  .badge {
    font-size: .66rem; padding: 1px 7px; border-radius: 20px; font-weight: 700; border: 1px solid;
  }
  .red    .badge { background: #FEE2E2; border-color: #FECACA; color: var(--red); }
  .green  .badge { background: #E8F5F0; border-color: #A5D6B4; color: #7BCF8A; }
  .cyan   .badge { background: #E8F5F0; border-color: #A5D6B4; color: #7BCF8A; }
  .amber  .badge { background: #FFF8E1; border-color: #FDE68A; color: var(--amber); }
  .orange .badge { background: #FFEDD5; border-color: #FED7AA; color: #EA580C; }

  .product-list { list-style: none; display: grid; gap: 4px; }
  .product-list li {
    padding: 8px 13px; border-radius: 0 10px 10px 0;
    font-size: .86rem; display: flex; align-items: center; gap: 9px;
  }
  .product-list li.flag-item     { background: #FEF2F2; border-left: 3px solid #FCA5A5; }
  .product-list li.ok-item       { background: #E8F5E9; border-left: 3px solid #81C784; }
  .product-list li.cert-item     { background: #F0FDFA; border-left: 3px solid #81C784; color: var(--muted); }
  .product-list li.caution-item  { background: #FFF8E1; border-left: 3px solid #FCD34D; }
  .product-list li.marketing-item{ background: #FFF7ED; border-left: 3px solid #FDBA74; }
  .caution-icon   { color: var(--amber); flex-shrink: 0; }
  .marketing-icon { color: #EA580C; flex-shrink: 0; }
  .product-list a { color: var(--red); font-weight: 600; text-decoration: none; border-bottom: 1px solid rgba(220,38,38,.2); }
  .product-list a:hover { border-bottom-color: var(--red); }
  .product-list .no-link { color: var(--text); }
  .verify-btn {
    margin-left: auto; flex-shrink: 0; font-size: .7rem;
    border: 1px solid #FECACA; border-radius: 6px;
    padding: 2px 7px; color: var(--red); text-decoration: none;
    background: none; white-space: nowrap;
  }
  .verify-btn:hover { background: #FEE2E2; }

  .scrollable-list { max-height: 340px; overflow-y: auto; padding-right: 4px; }
  .scrollable-list::-webkit-scrollbar { width: 4px; }
  .scrollable-list::-webkit-scrollbar-track { background: var(--bg); }
  .scrollable-list::-webkit-scrollbar-thumb { background: #C4B8E8; border-radius: 2px; }

  .clean     { color: #7BCF8A; font-weight: 700; padding: 14px 0; }
  .error-msg { background: #FEF2F2; border-left: 3px solid #FCA5A5; padding: 13px 17px; border-radius: 0 10px 10px 0; color: var(--red); }

  /* ── Pricing ─────────────────────────────────────────────────────────── */
  .pricing-intro {
    background: var(--card-bg); border: 1px solid var(--border);
    border-radius: 20px; padding: 28px 32px; margin-bottom: 24px;
    display: flex; align-items: center; gap: 20px;
    box-shadow: 0 8px 30px rgba(31,41,55,.06);
  }
  .pricing-intro-text h2 { font-size: 1.05rem; font-weight: 800; color: var(--primary); margin-bottom: 6px; font-family: 'Manrope', system-ui, sans-serif; }
  .pricing-intro-text p  { font-size: .84rem; color: var(--muted); line-height: 1.55; }
  .pricing-big-icon { width: 110px; height: 110px; flex-shrink: 0; }

  .pricing-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(220px,1fr)); gap: 24px; }
  .pricing-card {
    background: var(--card-bg); border: 1px solid var(--border);
    border-radius: 20px; padding: 28px;
    display: flex; flex-direction: column;
    transition: transform .15s, box-shadow .15s, border-color .15s;
    box-shadow: 0 8px 30px rgba(31,41,55,.06);
  }
  .pricing-card:hover {
    transform: translateY(-3px);
    box-shadow: 0 14px 36px rgba(31,41,55,.10);
    border-color: rgba(111,94,247,.25);
  }
  .pricing-card.featured {
    border-color: rgba(111,94,247,.35);
    box-shadow: 0 8px 30px rgba(111,94,247,.12);
  }
  .pricing-icon-row  { display: flex; align-items: center; gap: 10px; margin-bottom: 14px; }
  .pricing-icon      { width: 56px; height: 56px; }
  .pricing-mult      { font-size: 1.4rem; font-weight: 900; color: var(--primary); line-height: 1; font-family: 'Manrope', system-ui, sans-serif; }
  .pricing-tier-name { font-size: .72rem; font-weight: 700; text-transform: uppercase; letter-spacing: .08em; color: var(--muted); margin-bottom: 6px; }
  .pricing-price     { font-size: 2rem; font-weight: 900; color: var(--text); line-height: 1; margin-bottom: 3px; font-family: 'Manrope', system-ui, sans-serif; }
  .pricing-per       { font-size: .76rem; color: var(--muted); margin-bottom: 4px; }
  .pricing-disc      { font-size: .7rem; font-weight: 700; color: #8B7CFF; margin-bottom: 14px; }
  .pricing-disc.none { color: var(--muted); }
  .pricing-desc      { font-size: .81rem; color: var(--muted); line-height: 1.55; flex: 1; margin-bottom: 16px; }
  .pricing-cta {
    display: block; text-align: center;
    background: #6F5EF7; color: #fff;
    border: none; border-radius: 14px; padding: 13px 10px;
    font-weight: 700; font-size: .84rem;
    text-decoration: none; cursor: pointer;
    transition: background .15s, box-shadow .15s;
    box-shadow: 0 8px 20px rgba(111,94,247,.18);
  }
  .pricing-cta:hover { background: #5A4CE0; box-shadow: 0 10px 26px rgba(111,94,247,.26); }
  .pricing-cta.contact {
    background: var(--lavender); color: var(--primary-dark);
    border: 1.5px solid rgba(111,94,247,.25); box-shadow: none;
  }
  .pricing-cta.contact:hover { background: #E4DEFF; }
  .coming-soon-note {
    text-align: center; margin-top: 28px;
    background: #FFF8E1; border: 1px dashed #FDE68A;
    border-radius: 16px; padding: 16px 20px;
    font-size: .82rem; color: var(--muted); line-height: 1.55;
  }
  .coming-soon-note strong { color: var(--amber); }

  /* ── History ─────────────────────────────────────────────────────────── */
  .history-empty { text-align: center; color: var(--muted); padding: 40px; font-size: .88rem; }
  .history-item {
    background: var(--card-bg); border: 1px solid var(--border);
    border-radius: 16px; padding: 15px 20px; margin-bottom: 10px;
    display: flex; align-items: center; gap: 16px;
    box-shadow: 0 4px 16px rgba(31,41,55,.05);
    transition: border-color .15s, box-shadow .15s;
  }
  .history-item:hover { border-color: rgba(111,94,247,.25); box-shadow: 0 8px 24px rgba(31,41,55,.08); }
  .h-main { flex: 1; min-width: 0; }
  .h-op   { font-weight: 700; font-size: .9rem; color: var(--text); }
  .h-site { font-size: .74rem; color: var(--muted); margin-top: 1px; }
  .h-ts   { font-size: .7rem; color: var(--dim); margin-top: 2px; }
  .h-stats { display: flex; gap: 12px; }
  .h-stat  { font-size: .74rem; font-weight: 700; }
  .h-stat.flags { color: var(--red); }
  .h-stat.vf    { color: #7BCF8A; }
  .h-actions { display: flex; gap: 6px; flex-shrink: 0; }

  /* ── Account ─────────────────────────────────────────────────────────── */
  .account-wrap { max-width: 420px; margin: 0 auto; }
  .coming-soon-badge {
    display: inline-block; font-size: .68rem; font-weight: 700;
    text-transform: uppercase; letter-spacing: .08em;
    padding: 2px 8px; border-radius: 20px;
    background: #FFF8E1; border: 1px solid #FDE68A; color: var(--amber);
  }
  .divider {
    text-align: center; color: var(--muted); font-size: .76rem;
    margin: 14px 0; display: flex; align-items: center; gap: 12px;
  }
  .divider::before, .divider::after { content:''; flex:1; height:1px; background: var(--border); }

  /* ── Settings ────────────────────────────────────────────────────────── */
  .setting-row {
    display: flex; align-items: center; justify-content: space-between;
    padding: 16px 0; border-bottom: 1px solid var(--border);
  }
  .setting-row:last-child { border-bottom: none; }
  .setting-info  { flex: 1; }
  .setting-label { font-size: .88rem; font-weight: 700; color: var(--text); }
  .setting-desc  { font-size: .75rem; color: var(--muted); margin-top: 2px; }
  .toggle {
    width: 40px; height: 22px; background: var(--dim);
    border: none; border-radius: 11px;
    cursor: pointer; position: relative; flex-shrink: 0; margin-left: 16px;
    transition: background .25s;
  }
  .toggle.on { background: var(--primary); }
  .toggle::after {
    content:''; position: absolute; top: 3px; left: 3px;
    width: 16px; height: 16px; border-radius: 50%;
    background: #fff; box-shadow: 0 1px 4px rgba(0,0,0,.15);
    transition: transform .25s;
  }
  .toggle.on::after { transform: translateX(18px); }
  .settings-note {
    margin-top: 24px; padding: 14px 18px;
    background: var(--lavender); border: 1px dashed rgba(111,94,247,.2);
    border-radius: 14px; font-size: .8rem; color: var(--muted); line-height: 1.5;
  }

  /* ── Session stats counter ──────────────────────────────────────────── */
  .stats-counter {
    display: grid; grid-template-columns: repeat(3,1fr);
    gap: 10px; margin-bottom: 22px;
  }
  .sc-box {
    background: var(--card-bg); border: 1px solid var(--border);
    border-radius: 16px; padding: 14px; text-align: center;
    box-shadow: 0 4px 16px rgba(31,41,55,.04);
  }
  .sc-num { font-size: 1.55rem; font-weight: 900; line-height: 1; font-variant-numeric: tabular-nums; font-family: 'Manrope', system-ui, sans-serif; }
  .sc-lbl { font-size: .62rem; color: var(--muted); margin-top: 4px; text-transform: uppercase; letter-spacing: .07em; }
  .sc-box.sc-checks .sc-num { color: var(--text); }
  .sc-box.sc-flags  .sc-num { color: var(--red); }
  .sc-box.sc-fines  .sc-num { color: #8B7CFF; }

  /* ── Below-form session totals strip ────────────────────────────────── */
  .form-stats {
    display: flex; justify-content: center; gap: 28px;
    margin-top: 10px; margin-bottom: 4px;
    font-size: .73rem; color: var(--muted); padding: 9px 14px;
  }
  .form-stat-item { display: flex; align-items: center; gap: 7px; }
  .form-stat-num  { font-variant-numeric: tabular-nums; font-weight: 700; color: var(--text); }
  .form-stat-num.red { color: var(--red); }

  /* ── Progress panel ──────────────────────────────────────────────────── */
  .ps-header {
    font-size: .72rem; font-weight: 700; text-transform: uppercase;
    letter-spacing: .09em; color: var(--muted); margin-bottom: 14px;
    display: flex; align-items: center; gap: 8px;
  }
  .ps-spin { display: inline-block; animation: spin 1.1s linear infinite; }
  @keyframes spin { to { transform: rotate(360deg); } }

  .progress-step {
    display: flex; align-items: center; gap: 14px;
    padding: 9px 0; border-bottom: 1px solid var(--border);
    transition: opacity .3s;
  }
  .progress-step:last-child { border-bottom: none; }

  .ps-num {
    width: 26px; height: 26px; border-radius: 50%; flex-shrink: 0;
    display: flex; align-items: center; justify-content: center;
    font-size: .72rem; font-weight: 800; transition: all .3s;
  }
  .ps-pending .ps-num { background: var(--bg); color: var(--dim); border: 1px solid var(--border); }
  .ps-active  .ps-num { background: var(--lavender); color: var(--primary); border: 1px solid rgba(111,94,247,.3); animation: pulse 1.4s ease-in-out infinite; }
  .ps-done    .ps-num { background: #E8F5F0; color: #7BCF8A; border: 1px solid #A5D6B4; }

  .ps-info  { flex: 1; }
  .ps-name  { font-size: .83rem; font-weight: 600; transition: color .3s; }
  .ps-msg   { font-size: .72rem; color: var(--muted); margin-top: 2px; min-height: 14px; }
  .ps-pending .ps-name { color: var(--muted); }
  .ps-active  .ps-name { color: var(--primary); }
  .ps-done    .ps-name { color: var(--muted); }

  .ps-bar  { width: 90px; height: 3px; background: var(--border); border-radius: 2px; overflow: hidden; flex-shrink: 0; }
  .ps-fill { height: 100%; border-radius: 2px; transition: width .6s ease, background .4s; }
  .ps-pending .ps-fill { width: 0; background: transparent; }
  .ps-active  .ps-fill {
    width: 65%;
    background: linear-gradient(90deg, #5A4CE0 0%, #8B7CFF 50%, #5A4CE0 100%);
    background-size: 200% 100%;
    animation: shimmer 1.6s ease-in-out infinite;
  }
  @keyframes shimmer { 0%,100%{background-position:0% 50%} 50%{background-position:100% 50%} }
  .ps-done .ps-fill { width: 100%; background: #7BCF8A; }

  /* ── About page ──────────────────────────────────────────────────────── */
  .about-hero {
    text-align: center; padding: 40px 28px; position: relative; overflow: hidden;
    background: linear-gradient(135deg, #F7F4FF 0%, #EEE8FF 100%);
  }
  .about-tagline {
    font-size: 1.3rem; font-weight: 900; line-height: 1.35;
    color: var(--primary); margin-bottom: 30px; font-family: 'Manrope', system-ui, sans-serif;
  }
  .about-penalty-num {
    font-size: 4rem; font-weight: 900; line-height: 1; font-variant-numeric: tabular-nums;
    color: var(--red); font-family: 'Manrope', system-ui, sans-serif;
  }
  .about-penalty-label { font-size: .95rem; color: var(--text); font-weight: 700; margin-top: 10px; }
  .about-penalty-sub   { font-size: .76rem; color: var(--muted); margin-top: 4px; }

  .about-section-title {
    font-size: .73rem; font-weight: 800; text-transform: uppercase;
    letter-spacing: .1em; color: var(--muted); margin-bottom: 14px;
    display: flex; align-items: center; gap: 8px;
  }
  .about-p    { font-size: .88rem; color: var(--muted); line-height: 1.65; margin-bottom: 14px; }
  .about-lead { font-size: .98rem; font-weight: 700; color: var(--text); line-height: 1.5; margin-bottom: 6px; }

  .risk-list, .feature-list, .consequence-list { list-style: none; display: grid; gap: 5px; margin-top: 4px; }
  .risk-list li {
    font-size: .86rem; padding: 8px 14px;
    background: #FEF2F2; border-left: 3px solid #FCA5A5;
    border-radius: 0 10px 10px 0; color: var(--text);
    display: flex; align-items: flex-start; gap: 9px; line-height: 1.45;
  }
  .feature-list li {
    font-size: .86rem; padding: 8px 14px;
    background: #E8F5E9; border-left: 3px solid #81C784;
    border-radius: 0 10px 10px 0; color: var(--text);
    display: flex; align-items: flex-start; gap: 9px; line-height: 1.45;
  }
  .consequence-list li {
    font-size: .86rem; padding: 8px 14px;
    background: #FFF8E1; border-left: 3px solid #FCD34D;
    border-radius: 0 10px 10px 0; color: var(--text);
    display: flex; align-items: flex-start; gap: 9px; line-height: 1.45;
  }
  .audience-grid {
    display: grid; grid-template-columns: repeat(auto-fit, minmax(186px,1fr)); gap: 10px;
  }
  .audience-card {
    font-size: .84rem; padding: 14px 16px;
    background: var(--lavender); border: 1px solid var(--border);
    border-radius: 14px; color: var(--text); line-height: 1.45;
    display: flex; align-items: flex-start; gap: 10px;
  }
  .certbridge-badge {
    display: inline-block; font-size: .7rem; font-weight: 700; text-transform: uppercase;
    letter-spacing: .08em; padding: 3px 10px; border-radius: 20px; margin-bottom: 12px;
    background: var(--lavender); border: 1px solid var(--border); color: var(--primary);
  }
  .about-bottom-line {
    font-size: 1.02rem; font-weight: 800; color: var(--primary);
    line-height: 1.45; text-align: center; margin-bottom: 12px; font-family: 'Manrope', system-ui, sans-serif;
  }
  .about-bottom-sub { font-size: .86rem; color: var(--muted); text-align: center; line-height: 1.6; }
  .cta-btn {
    display: inline-block; margin-top: 22px;
    background: #6F5EF7; color: #fff;
    border: none; border-radius: 14px;
    padding: 13px 32px; font-size: .92rem; font-weight: 700;
    text-decoration: none;
    transition: background .15s, box-shadow .15s;
    box-shadow: 0 8px 20px rgba(111,94,247,.18);
  }
  .cta-btn:hover { background: #5A4CE0; box-shadow: 0 10px 26px rgba(111,94,247,.26); }

  /* ── Site footer ─────────────────────────────────────────────────────── */
  .site-footer { background: #2B2438; color: rgba(255,255,255,.7); padding: 40px 0 0; }
  .site-footer-inner {
    max-width: 1100px; margin: 0 auto; padding: 0 24px 32px;
    display: grid; grid-template-columns: 2fr 1fr 1fr; gap: 40px;
  }
  .footer-brand-name { font-size: 1rem; font-weight: 800; color: #fff; margin-bottom: 6px; font-family: 'Manrope', system-ui, sans-serif; }
  .footer-brand-desc { font-size: .82rem; line-height: 1.6; color: rgba(255,255,255,.5); margin-bottom: 12px; }
  .footer-disclaimer {
    font-size: .73rem; color: rgba(255,255,255,.35); line-height: 1.55;
    padding: 10px 14px; background: rgba(255,255,255,.04);
    border: 1px solid rgba(255,255,255,.07); border-radius: 10px; margin-top: 14px;
  }
  .footer-col h4 { font-size: .72rem; font-weight: 700; text-transform: uppercase; letter-spacing: .1em; color: rgba(255,255,255,.4); margin-bottom: 12px; }
  .footer-col a  { display: block; font-size: .84rem; color: rgba(255,255,255,.6); text-decoration: none; margin-bottom: 8px; transition: color .15s; }
  .footer-col a:hover { color: #fff; }
  .footer-bottom {
    max-width: 1100px; margin: 0 auto; padding: 18px 24px;
    border-top: 1px solid rgba(255,255,255,.08);
    display: flex; align-items: center; justify-content: space-between; gap: 16px; flex-wrap: wrap;
  }
  .footer-bottom-text { font-size: .76rem; color: rgba(255,255,255,.35); }

  /* ── Mobile ──────────────────────────────────────────────────────────── */
  @media (max-width: 768px) {
    header { padding: 10px 16px; gap: 8px; grid-template-columns: 1fr auto; }
    .header-nav { display: none; }
    .header-credits-wrap { display: none; }
    .header-wordmark { font-size: .88rem; }
    .header-logo-icon { width: 72px; height: 72px; border-radius: 10px; }
    .header-icon { width: 36px; height: 36px; }
    .header-cta-btn { padding: 8px 14px; font-size: .8rem; }
    .nav-user-email { font-size: .75rem; max-width: 110px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .site-footer-inner { grid-template-columns: 1fr; gap: 28px; }
    .page-main { padding: 0 16px; margin-top: 24px; }
    .card { padding: 18px 16px; }
  }

  /* ── Auth modal ──────────────────────────────────────────────────────── */
  .modal-overlay {
    position: fixed; inset: 0; background: rgba(43,36,56,.55); z-index: 9000;
    display: flex; align-items: center; justify-content: center;
    backdrop-filter: blur(4px);
  }
  .modal-card {
    background: var(--surface); border-radius: 20px; padding: 32px 28px;
    width: 100%; max-width: 420px; position: relative;
    box-shadow: 0 20px 60px rgba(0,0,0,.15); border: 1px solid var(--border);
  }
  .modal-close {
    position: absolute; top: 14px; right: 16px; background: none; border: none;
    font-size: 1.4rem; color: var(--muted); cursor: pointer; line-height: 1;
  }
  .modal-close:hover { color: var(--text); }
  .modal-title { font-size: 1.1rem; font-weight: 800; color: var(--text); margin-bottom: 20px; font-family: 'Manrope', system-ui, sans-serif; }
  .auth-tabs { display: flex; border-bottom: 2px solid var(--border); margin-bottom: 20px; }
  .auth-tab {
    flex: 1; text-align: center; padding: 9px; font-size: .88rem; font-weight: 600;
    color: var(--muted); cursor: pointer; border-bottom: 3px solid transparent;
    margin-bottom: -2px; transition: color .15s, border-color .15s;
  }
  .auth-tab.active { color: var(--primary); border-color: var(--primary); }
  .auth-msg {
    font-size: .82rem; padding: 10px 14px; border-radius: 10px; margin-bottom: 14px;
  }
  .auth-msg.error   { background: #FEF2F2; border: 1px solid #FCA5A5; color: var(--red); }
  .auth-msg.success { background: #E8F5F0; border: 1px solid #A5D6B4; color: #7BCF8A; }
  .nav-user-email {
    font-size: .82rem; font-weight: 600; color: #C4B8E8;
    padding: 5px 10px; background: rgba(255,255,255,.08); border-radius: 8px;
  }
  .nav-signout {
    font-size: .78rem; background: none; border: 1px solid rgba(255,255,255,.2);
    border-radius: 8px; padding: 4px 10px; color: rgba(255,255,255,.7); cursor: pointer;
    transition: color .15s, border-color .15s;
  }
  .nav-signout:hover { color: #fff; border-color: rgba(255,255,255,.5); }

  /* ── Payment gate / teaser ───────────────────────────────────────────── */
  .gate-teaser {
    display: grid; grid-template-columns: repeat(4, 1fr); gap: 8px; margin-bottom: 28px;
  }
  @media (max-width: 600px) { .gate-teaser { grid-template-columns: repeat(2, 1fr); } }
  .gate-stat {
    padding: 16px 10px; border-radius: 16px; text-align: center;
    background: var(--lavender); border: 1px solid var(--border);
  }
  .gate-stat .g-num { font-size: 2rem; font-weight: 900; color: var(--text); line-height: 1; font-family: 'Manrope', system-ui, sans-serif; }
  .gate-stat .g-lbl { font-size: .72rem; color: var(--muted); margin-top: 4px; }
  .gate-stat.red-stat { background: #FEF2F2; border-color: #FCA5A5; }
  .gate-stat.red-stat .g-num { color: var(--red); }
  .gate-divider {
    border: none; border-top: 1px solid var(--border); margin: 0 0 24px;
  }
  .gate-wrap { text-align: center; padding: 8px 0 28px; }
  .gate-lock { font-size: 2.2rem; margin-bottom: 12px; }
  .gate-title { font-size: 1.15rem; font-weight: 800; color: var(--text); margin-bottom: 8px; font-family: 'Manrope', system-ui, sans-serif; }
  .gate-sub {
    font-size: .88rem; color: var(--muted); margin-bottom: 24px;
    line-height: 1.6; max-width: 360px; margin-left: auto; margin-right: auto;
  }
  .gate-actions { display: flex; gap: 10px; justify-content: center; flex-wrap: wrap; }
  .gate-btn-primary {
    background: #6F5EF7; color: #fff; border: none; border-radius: 14px;
    padding: 13px 24px; font-size: .9rem; font-weight: 700; cursor: pointer;
    transition: background .15s; box-shadow: 0 8px 20px rgba(111,94,247,.18);
  }
  .gate-btn-primary:hover { background: #5A4CE0; }
  .gate-btn-secondary {
    background: #fff; color: #6F5EF7; border: 1.5px solid #6F5EF7;
    border-radius: 14px; padding: 12px 22px; font-size: .9rem; font-weight: 600;
    cursor: pointer; text-decoration: none; transition: background .15s;
  }
  .gate-btn-secondary:hover { background: #F0EDFF; }
  .gate-meta-block {
    background: var(--lavender); border-radius: 14px; padding: 16px 20px;
    margin-bottom: 24px; text-align: left; font-size: .84rem;
  }
  .gate-meta-block strong { color: var(--text); }
  .gate-meta-block span   { color: var(--muted); }

  /* ── Primary/Secondary/Ghost buttons — global ────────────────────────── */
  .btn-glass {
    background: #6F5EF7;
    color: #fff;
    border: none;
    border-radius: 14px;
    padding: 13px 28px;
    font-size: .9rem; font-weight: 700;
    cursor: pointer; width: 100%;
    box-shadow: 0 8px 20px rgba(111,94,247,0.18);
    transition: transform .15s, box-shadow .15s, background .15s;
    text-decoration: none; display: block; text-align: center;
    letter-spacing: -.01em;
    height: 48px; line-height: 1;
    display: flex; align-items: center; justify-content: center;
  }
  .btn-glass:hover {
    background: #5A4CE0;
    box-shadow: 0 10px 26px rgba(111,94,247,0.26);
    transform: translateY(-1px);
  }
  .btn-glass:active { transform: translateY(0); }

  /* ── Volume Calculator ───────────────────────────────────────────────── */
  .calc-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(140px, 1fr)); gap: 12px; margin-bottom: 16px; }
  .calc-box  { background: var(--lavender); border-radius: 14px; padding: 12px 16px; }
  .calc-lbl  { font-size: .7rem; font-weight: 700; text-transform: uppercase; letter-spacing: .06em; color: var(--muted); margin-bottom: 4px; }
  .calc-val  { font-size: .9rem; font-weight: 800; color: var(--text); }
  .calc-sub  { font-size: .72rem; color: var(--muted); margin-top: 2px; }

  /* ── Scheduling ──────────────────────────────────────────────────────── */
  .next-avail-box {
    background: linear-gradient(135deg,var(--lavender),#DDD6FF);
    border: 1.5px solid rgba(111,94,247,.3); border-radius: 14px;
    padding: 14px 18px; margin-bottom: 16px;
    display: flex; align-items: center; justify-content: space-between; gap: 14px;
    cursor: pointer; transition: all .15s;
  }
  .next-avail-box:hover { background: linear-gradient(135deg,#DDD6FF,#C9BEFF); border-color: var(--primary); }
  .next-avail-label { font-size: .75rem; font-weight: 700; color: var(--primary-dark); text-transform: uppercase; letter-spacing: .06em; }
  .next-avail-time  { font-size: 1rem; font-weight: 800; color: var(--text); margin-top: 2px; }
  .next-avail-arrow { font-size: 1.2rem; color: var(--primary); flex-shrink: 0; }
  /* ── Calendar ─────────────────────────────────────────────────────────── */
  .cal-header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 14px; }
  .cal-month-label { font-size: 1rem; font-weight: 800; color: var(--text); }
  .cal-nav-btn {
    width: 32px; height: 32px; border-radius: 8px; border: 1.5px solid var(--border);
    background: none; color: var(--muted); cursor: pointer; font-size: .9rem;
    display: flex; align-items: center; justify-content: center; transition: all .12s;
  }
  .cal-nav-btn:hover:not(:disabled) { border-color: var(--primary); color: var(--primary); background: var(--lavender); }
  .cal-nav-btn:disabled { opacity: .3; cursor: default; }
  .cal-weekdays {
    display: grid; grid-template-columns: repeat(7, 1fr);
    gap: 2px; margin-bottom: 4px;
  }
  .cal-weekdays div { text-align: center; font-size: .68rem; font-weight: 700; color: var(--muted); padding: 4px 0; text-transform: uppercase; letter-spacing: .05em; }
  .cal-grid { display: grid; grid-template-columns: repeat(7, 1fr); gap: 4px; }
  .cal-day {
    aspect-ratio: 1; display: flex; align-items: center; justify-content: center;
    border-radius: 10px; font-size: .85rem; font-weight: 600;
    border: 1.5px solid transparent; transition: all .12s;
  }
  .cal-blank { border: none; }
  .cal-day-past { color: var(--dim); cursor: default; }
  .cal-day-avail {
    cursor: pointer; color: var(--text); border-color: var(--border);
    background: white;
  }
  .cal-day-avail:hover { background: var(--lavender); border-color: var(--primary); color: var(--primary); }
  .cal-day-selected { background: var(--primary) !important; border-color: var(--primary-dark) !important; color: white !important; }
  /* ── Slot grid ────────────────────────────────────────────────────────── */
  .slot-wrap { max-height: 340px; overflow-y: auto; padding-right: 4px; margin-bottom: 4px; }
  .slot-wrap::-webkit-scrollbar { width: 4px; }
  .slot-wrap::-webkit-scrollbar-track { background: var(--bg); }
  .slot-wrap::-webkit-scrollbar-thumb { background: rgba(111,94,247,.3); border-radius: 2px; }
  #slotGrid { display: grid; grid-template-columns: repeat(auto-fill, minmax(106px, 1fr)); gap: 5px; padding: 2px; }
  .slot-btn {
    padding: 7px 4px; font-size: .73rem; font-weight: 600;
    border-radius: 8px; border: 1px solid; cursor: pointer;
    text-align: center; transition: all .12s; background: none; white-space: nowrap;
    overflow: hidden; text-overflow: ellipsis;
  }
  .slot-btn.avail    { background: var(--lavender); border-color: rgba(111,94,247,.3); color: var(--primary-dark); }
  .slot-btn.avail:hover { background: #DDD6FF; border-color: var(--primary); }
  .slot-btn.booked   { background: var(--bg); border-color: var(--border); color: var(--dim); cursor: not-allowed; font-size: .66rem; }
  .slot-btn.past     { background: var(--bg); border-color: var(--border); color: var(--dim); cursor: not-allowed; opacity: .45; }
  .slot-btn.selected { background: var(--primary); border-color: var(--primary-dark); color: white; font-weight: 800; }
  /* ── Scheduled checks list ────────────────────────────────────────────── */
  .sched-list-item {
    display: flex; align-items: center; gap: 14px;
    padding: 12px 16px; border-radius: 14px;
    background: var(--bg); border: 1px solid var(--border); margin-bottom: 8px;
  }
  .sched-item-info  { flex: 1; min-width: 0; }
  .sched-item-op    { font-size: .88rem; font-weight: 700; color: var(--text); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .sched-item-when  { font-size: .73rem; color: var(--muted); margin-top: 2px; }
  .sched-item-badge { font-size: .68rem; font-weight: 700; padding: 2px 8px; border-radius: 20px; border: 1px solid; flex-shrink: 0; }
  .badge-scheduled  { background: var(--lavender); border-color: rgba(111,94,247,.3); color: var(--primary-dark); }
  .badge-running    { background: #FFF8E1; border-color: #FDE68A; color: #D97706; animation: pulse 1.4s ease-in-out infinite; }
  .badge-done       { background: #E8F5E9; border-color: #A5D6A7; color: #2E7D32; }
  .badge-error      { background: #FEF2F2; border-color: #FECACA; color: var(--red); }
  .sched-cancel-btn {
    font-size: .7rem; padding: 4px 10px; border-radius: 8px;
    border: 1px solid #FECACA; color: var(--red); background: none;
    cursor: pointer; flex-shrink: 0; transition: background .12s;
  }
  .sched-cancel-btn:hover { background: #FEF2F2; }
  .sched-view-btn {
    font-size: .7rem; padding: 4px 10px; border-radius: 8px;
    border: 1.5px solid rgba(111,94,247,.25); color: var(--primary);
    background: var(--lavender); text-decoration: none; flex-shrink: 0;
  }
"""


# ---------------------------------------------------------------------------
# Shared page shell (for non-main pages)
# ---------------------------------------------------------------------------

BASE_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{{ page_title }} — Organic Web Checker</title>
  <link rel="icon" type="image/png" href="/static/favicon.png?v=5">
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Manrope:wght@400;500;600;700;800&family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
  <style>{{ css | safe }}</style>
</head>
<body>
<header>
  <a href="/" class="header-logo">
    <img src="/static/icon-header.png" class="header-logo-icon" alt="Organic Web Checker">
    <span class="header-wordmark"><span class="wm-organic">Organic</span> <span class="wm-checker">Web Checker</span></span>
  </a>
  <div class="header-center">
    <nav class="header-nav">
      <a href="/"         class="nav-link {{ 'active' if active == 'home'     else '' }}">Product</a>
      <a href="/schedule" class="nav-link {{ 'active' if active == 'schedule' else '' }}">Schedule</a>
      <a href="/about"    class="nav-link {{ 'active' if active == 'about'    else '' }}">How It Works</a>
      <a href="/pricing"  class="nav-link {{ 'active' if active == 'pricing'  else '' }}">Pricing</a>
      <a href="/history"  class="nav-link {{ 'active' if active == 'history'  else '' }}">History</a>
      <a href="/agents"   class="nav-link {{ 'active' if active == 'agents'   else '' }}">API</a>
    </nav>
    <div class="header-credits-wrap" id="headerCredits" {% if not user_email %}style="display:none"{% endif %}>
      <span class="header-credit-badge" id="headerCreditText">{% if user_email %}{% if user_is_admin %}Admin &mdash; Unlimited{% else %}{{ user_credits }} Checker{{ 's' if user_credits != 1 else '' }} Available{% endif %}{% endif %}</span>
    </div>
  </div>
  <div class="header-right">
    <div id="navUserArea">
      {% if user_email %}
        <span class="nav-user-email">{{ user_email }}</span>
        <button class="nav-signout" onclick="doLogout()">Sign Out</button>
      {% endif %}
    </div>
    <a href="/#run-check" class="header-cta-btn">Run Checker</a>
    <div class="header-icon-wrap">
      <button class="header-icon-btn" id="iconBtn" onclick="toggleDd(event)">
        <img src="/static/icon-header.png" class="header-icon" alt="">
      </button>
      <div class="header-dropdown" id="hDd">
        <a class="dropdown-item" href="/account">Account</a>
        <a class="dropdown-item" href="/schedule">Schedule Checker</a>
        <a class="dropdown-item" href="/history">Check History</a>
        <a class="dropdown-item" href="/pricing">Pricing</a>
        <a class="dropdown-item" href="/agents">Agents &amp; API</a>
        <a class="dropdown-item" href="/settings">Settings</a>
      </div>
    </div>
  </div>
</header>
<main class="page-main">
  {{ body | safe }}
</main>
<footer class="site-footer" style="margin-top:48px">
  <div class="footer-bottom">
    <span class="footer-bottom-text">&copy; 2026 Healer&rsquo;s Find LLC &mdash; Organic Web Checker</span>
    <span class="footer-bottom-text">Not a certifier. Decision-support tool for compliance review.</span>
  </div>
</footer>
<script>
function toggleDd(e){e.stopPropagation();document.getElementById('hDd').classList.toggle('open');}
document.addEventListener('click',()=>{const d=document.getElementById('hDd');if(d)d.classList.remove('open');});
</script>
<!-- Auth Modal -->
<div id="authModal" style="display:none" class="modal-overlay" onclick="if(event.target===this)closeAuthModal()">
  <div class="modal-card">
    <button class="modal-close" onclick="closeAuthModal()">&times;</button>
    <div class="auth-tabs">
      <div class="auth-tab active" id="tabSignin"    onclick="switchAuthTab('signin')">Sign In</div>
      <div class="auth-tab"        id="tabRegister"  onclick="switchAuthTab('register')">Create Account</div>
    </div>
    <div id="authMsg" style="display:none" class="auth-msg"></div>
    <div id="authFormSignin">
      <label>Email</label>
      <input type="email" id="siEmail" placeholder="you@example.com" style="width:100%;box-sizing:border-box;margin-bottom:10px">
      <label>Password</label>
      <input type="password" id="siPw" placeholder="&bull;&bull;&bull;&bull;&bull;&bull;&bull;&bull;" style="width:100%;box-sizing:border-box;margin-bottom:14px" onkeydown="if(event.key==='Enter')doLogin()">
      <button onclick="doLogin()" class="btn-glass">Sign In</button>
      <div style="text-align:right;margin-top:10px"><a href="/forgot-password" style="font-size:.78rem;color:var(--muted);text-decoration:none">Forgot password?</a></div>
    </div>
    <div id="authFormRegister" style="display:none">
      <label>Email</label>
      <input type="email" id="rgEmail" placeholder="you@example.com" style="width:100%;box-sizing:border-box;margin-bottom:10px">
      <label>Password</label>
      <input type="password" id="rgPw" placeholder="At least 8 characters" style="width:100%;box-sizing:border-box;margin-bottom:10px">
      <label>Confirm password</label>
      <input type="password" id="rgPw2" placeholder="Repeat password" style="width:100%;box-sizing:border-box;margin-bottom:14px" onkeydown="if(event.key==='Enter')doRegister()">
      <button onclick="doRegister()" class="btn-glass">Create Account</button>
    </div>
  </div>
</div>
<script>
function openAuthModal(tab){switchAuthTab(tab||'signin');document.getElementById('authModal').style.display='flex';}
function closeAuthModal(){document.getElementById('authModal').style.display='none';document.getElementById('authMsg').style.display='none';}
function switchAuthTab(tab){
  document.getElementById('tabSignin').classList.toggle('active', tab==='signin');
  document.getElementById('tabRegister').classList.toggle('active', tab==='register');
  document.getElementById('authFormSignin').style.display = tab==='signin' ? '' : 'none';
  document.getElementById('authFormRegister').style.display = tab==='register' ? '' : 'none';
}
function showAuthMsg(msg,type){const el=document.getElementById('authMsg');el.textContent=msg;el.className='auth-msg '+type;el.style.display='';}
async function doLogin(){
  const email=document.getElementById('siEmail').value.trim();
  const pw=document.getElementById('siPw').value;
  if(!email||!pw){showAuthMsg('Email and password required.','error');return;}
  try{
    const res=await fetch('/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({email,password:pw})});
    const data=await res.json();
    if(data.ok){closeAuthModal();updateAuthUI(data.email,data.credits);if(window.viewingJobId)loadResult(window.viewingJobId);}
    else showAuthMsg(data.error||'Sign-in failed.','error');
  }catch(e){showAuthMsg('Network error — try again.','error');}
}
async function doRegister(){
  const email=document.getElementById('rgEmail').value.trim();
  const pw=document.getElementById('rgPw').value;
  const pw2=document.getElementById('rgPw2').value;
  if(!email||!pw){showAuthMsg('Email and password required.','error');return;}
  if(pw!==pw2){showAuthMsg('Passwords do not match.','error');return;}
  if(pw.length<8){showAuthMsg('Password must be at least 8 characters.','error');return;}
  try{
    const res=await fetch('/register',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({email,password:pw})});
    const data=await res.json();
    if(data.ok){closeAuthModal();updateAuthUI(data.email,data.credits);if(window.viewingJobId)loadResult(window.viewingJobId);}
    else showAuthMsg(data.error||'Registration failed.','error');
  }catch(e){showAuthMsg('Network error — try again.','error');}
}
async function doLogout(){
  await fetch('/logout',{method:'POST'});
  location.reload();
}
function updateAuthUI(email,credits){
  const el=document.getElementById('navUserArea');
  if(!el)return;
  const credBadge=document.getElementById('headerCredits');
  const credText=document.getElementById('headerCreditText');
  if(email){
    el.innerHTML='<span class="nav-user-email">'+email+'</span>&nbsp;<button class="nav-signout" onclick="doLogout()">Sign Out</button>';
    if(credBadge&&credText){
      credText.textContent=credits>=99999?'Admin — Unlimited':(credits+' Checker'+(credits!==1?'s':'')+' Available');
      credBadge.style.display='flex';
    }
  }else{
    el.innerHTML='';
    if(credBadge)credBadge.style.display='none';
  }
}
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Report partial — Jinja template (server-side render per job)
# ---------------------------------------------------------------------------

REPORT_PARTIAL = """
{% if report.get('error') %}
  <div class="error-msg">{{ report['error'] }}</div>
  {% if 'not found' in (report.get('error') or '') | lower %}
  <div style="margin-top:14px;padding:13px 16px;border-radius:12px;background:#F5F3FF;border:1px solid rgba(111,94,247,.2);font-size:.78rem;line-height:1.65;color:var(--text)">
    <strong style="color:var(--primary)">&#9432; Retail Exemption May Apply</strong><br>
    If this operation sells only <strong>finished, pre-certified organic products</strong> to end consumers and
    performs no handling, repackaging, or relabeling, it may qualify for the retail exemption and is not
    required to hold its own NOP certificate or appear in OID.<br>
    <span style="color:var(--muted);font-size:.72rem">
      Ref: 7 CFR &sect;&nbsp;205.101(a)(2) (retail exemption) &bull;
      If handling or processing occurs through this channel, handler certification is required under 7 CFR &sect;&nbsp;205.201.
      Also verify the operation name matches the registered OID name exactly.
    </span>
  </div>
  {% endif %}
{% else %}

  <div class="report-header">
    <div>
      <div class="report-op-name">{{ report.operation }}</div>
      <div class="report-meta-sub">{{ report.certifier }} &middot; {{ report.status }} &middot; {{ report.location }}</div>
    <div style="margin-top:6px">
      {% if report.get('oid_source') == 'cached' %}
        <span style="font-size:.68rem;padding:2px 9px;border-radius:20px;background:#FFF8E1;color:#D97706;border:1px solid #FDE68A;font-weight:700">&#128190; Cached OID &middot; {{ report.oid_cached_at }}</span>
        <span style="font-size:.68rem;color:var(--muted);margin-left:8px">Verify live at organic.ams.usda.gov</span>
      {% else %}
        <span style="font-size:.68rem;padding:2px 9px;border-radius:20px;background:#E8F5E9;color:#2E7D32;border:1px solid #A5D6A7;font-weight:700">&#10003; Live OID data</span>
      {% endif %}
    </div>
    </div>
    <div class="download-btns">
      <a class="dl-btn md"  href="/job/{{ job_id }}/download/md"  target="_blank">&#8595; .md</a>
      <a class="dl-btn pdf" href="/job/{{ job_id }}/download/pdf" target="_blank">&#8595; PDF</a>
    </div>
  </div>

  <div class="meta-grid">
    <div class="meta-item"><label>Operation</label><span>{{ report.operation }}</span></div>
    <div class="meta-item"><label>Certifier</label><span>{{ report.certifier }}</span></div>
    <div class="meta-item"><label>Status</label><span>{{ report.status }}</span></div>
    <div class="meta-item"><label>Location</label><span>{{ report.location }}</span></div>
    <div class="meta-item"><label>Cert Scope</label><span>
      {% if report.get('scope') %}
        {% for s in report.scope %}
          <span style="display:inline-block;font-size:.68rem;padding:2px 8px;border-radius:20px;margin-right:4px;font-weight:700;
            {% if s == 'HANDLING' %}background:#E8F5E9;color:#2E7D32;border:1px solid #A5D6A7
            {% elif s == 'CROPS' %}background:#E8F5E9;color:#2E7D32;border:1px solid #A5D6A7
            {% elif s == 'LIVESTOCK' %}background:#FFF8E1;color:#D97706;border:1px solid #FDE68A
            {% else %}background:#F9FAFB;color:var(--muted);border:1px solid var(--border){% endif %}">{{ s }}</span>
        {% endfor %}
        {% if report.get('business_type') %}
          <span style="display:inline-block;font-size:.65rem;padding:2px 8px;border-radius:20px;font-weight:600;background:#E8F5E9;color:#2E7D32;border:1px solid #A5D6A7">{{ report.business_type }}</span>
        {% endif %}
      {% else %}<span style="color:var(--muted);font-size:.78rem">Not detected</span>{% endif %}
    </span></div>
    <div class="meta-item"><label>Website</label><span><a href="{{ report.website_url }}" target="_blank" rel="noopener" style="color:var(--primary);text-decoration:none;border-bottom:1px solid rgba(111,94,247,.25)">{{ report.website_url }}</a></span></div>
  </div>

  {# ── Handling scope notice ─────────────────────────────────────── #}
  {% if 'HANDLING' in (report.get('scope') or []) %}
  <div style="margin-bottom:18px;padding:12px 16px;border-radius:14px;background:#EEE8FF;border:1px solid rgba(111,94,247,.2);font-size:.78rem;line-height:1.6;color:var(--text)">
    <strong style="color:var(--primary)">&#9432; Handling Operation Scope Notice</strong>
    {% if report.get('business_type') %}<span style="margin-left:6px;font-size:.65rem;padding:1px 7px;border-radius:20px;font-weight:600;background:#E4DEFF;color:#6F5EF7;border:1px solid rgba(111,94,247,.3)">{{ report.business_type }}</span>{% endif %}<br>
    {% if report.get('business_type') == 'Retailer' %}
      This operation is an NOP-certified handler classified as a <strong>Retailer</strong>. Because it holds
      an active organic certificate, its retail website is considered part of the certified operation and
      must be reflected in the <strong>Organic System Plan (OSP)</strong>. The retail exemption
      (7 CFR &sect;&nbsp;205.101(a)(2)) applies only to <em>non-certified</em> entities selling
      finished pre-certified products with no handling activity &mdash; it does <strong>not</strong> apply
      to certified operations. Note: a website operating under a brand name different from the operation&rsquo;s
      registered name does not constitute a separate entity exempt from OSP coverage.
      Flagged items below require verification that organic claims are congruent with the OID certificate;
      further review with the certifier may be required to confirm OSP coverage.
    {% elif report.get('business_type') == 'Importer' %}
      This operation is certified as an <strong>importer</strong>. Post-SOE (eff. March&nbsp;19,&nbsp;2024), all importers of
      organic products must hold NOP handler certification. Website organic claims require both the importer&rsquo;s
      own NOP cert coverage and verified foreign certification for each imported product.
    {% elif report.get('business_type') in ('Broker', 'Trader') %}
      This operation is certified as a <strong>{{ report.business_type|lower }}</strong>. Post-SOE (eff. March&nbsp;19,&nbsp;2024),
      brokers and traders handling organic products must be NOP-certified handlers. Website organic product
      listings require upstream supplier certification documented in the Organic System Plan (OSP).
    {% elif report.get('business_type') == 'Distributor' %}
      This operation is certified as a <strong>distributor</strong>. Organic products distributed must be covered
      by upstream certified supplier documentation in the Organic System Plan (OSP).
    {% else %}
      This operation is certified as a <strong>handler</strong> (processor, distributor, broker, or importer).
      Some handler certificates list products at a general category level (e.g., &ldquo;Organic Eggs&rdquo;);
      others list specific products. Detailed product and supplier information is in the operation&rsquo;s
      <strong>Organic System Plan (OSP)</strong> held by their certifier &mdash; not publicly visible in OID.
    {% endif %}<br>
    <span style="color:var(--muted);font-size:.72rem">
      {% if report.get('cert_retailer') %}
        Flagged items indicate organic claims on this operation&rsquo;s website not found on its OID certificate.
        As a certified operation, this website is part of the OSP regardless of brand name used.
        Verify organic claims are congruent with the OID certificate; further OSP review with the certifier may be required.
        Ref: 7 CFR &sect;&nbsp;205.307 (misrepresentation) &bull; &sect;&nbsp;205.201 (OSP) &bull; SOE Rule (eff. March&nbsp;19,&nbsp;2024)
      {% else %}
        Flagged items below indicate products not found on this operation&rsquo;s OID certificate.
        The compliance question is whether each product is covered by documented upstream supplier certification in the OSP.
        Ref: 7 CFR &sect;&nbsp;205.201 (OSP requirements) &bull; SOE Rule (eff. March&nbsp;19,&nbsp;2024)
      {% endif %}
    </span>
  </div>
  {% endif %}

  {# 6-box summary grid #}
  <div style="display:grid;grid-template-columns:repeat(3,1fr) repeat(3,1fr);gap:8px;margin-bottom:26px">
    <div class="stat" style="grid-column:span 2">
      <div class="num" style="font-size:1.5rem">{{ report.cert_product_count }}</div>
      <div class="lbl">Cert Products</div>
    </div>
    <div class="stat" style="grid-column:span 2">
      <div class="num" style="font-size:1.5rem">{{ report.website_organic_count }}</div>
      <div class="lbl">Site Organic Claims</div>
    </div>
    <div class="stat verified" style="grid-column:span 2">
      <div class="num" style="font-size:1.5rem">{{ report.verified | length }}</div>
      <div class="lbl">Confirmed OK &#10003;</div>
    </div>
    <div class="stat flagged" style="grid-column:span 2">
      <div class="num" style="font-size:1.5rem">{{ report.flagged | length }}</div>
      <div class="lbl">&#128308; Non-Compliance Risk</div>
    </div>
    <div class="stat" style="grid-column:span 2;border-color:rgba(255,208,96,.18);background:rgba(255,208,96,.04)">
      <div class="num" style="font-size:1.5rem;color:var(--amber)">{{ report.caution | length }}</div>
      <div class="lbl">&#128993; Caution</div>
    </div>
    <div class="stat" style="grid-column:span 2;border-color:rgba(255,140,0,.18);background:rgba(255,140,0,.03)">
      <div class="num" style="font-size:1.5rem;color:#D97706">{{ report.marketing | length }}</div>
      <div class="lbl">&#128992; Marketing Review</div>
    </div>
  </div>

  {# ── 🔴 Non-compliance risk ────────────────────────────────── #}
  {% if report.flagged %}
    <div class="section-label red">
      &#128308; Non-Compliance Risk
      <span class="badge">{{ report.flagged | length }}</span>
    </div>
    <p style="font-size:.76rem;color:var(--muted);margin-bottom:10px;line-height:1.55">
      {% if 'HANDLING' in (report.get('scope') or []) %}
        Specific products marketed as organic on the website but <strong>not found on this operation&rsquo;s OID certificate</strong>.
        For handling operations, verify that each product is covered by an upstream supplier&rsquo;s certification
        documented in the operation&rsquo;s Organic System Plan (OSP).<br>
        <span style="font-size:.7rem;opacity:.7">Ref: 7 CFR &sect;&nbsp;205.307 (misrepresentation) &bull; &sect;&nbsp;205.201 (OSP supplier documentation) &bull; SOE Rule &sect;&nbsp;205.2 (handling scope)</span>
      {% else %}
        Specific products labeled <em>organic</em> on the website but <strong>NOT found on the current OID certificate</strong>.<br>
        <span style="font-size:.7rem;opacity:.7">Ref: 7 CFR &sect;&nbsp;205.307 (misrepresentation &amp; fraud) &bull; &sect;&nbsp;205.303&ndash;305 (labeling)</span>
      {% endif %}
    </p>
    <ul class="product-list">
      {% for item in report.flagged | sort(attribute='title') %}
        <li class="flag-item">
          &#9888;
          {% if item.get('source') == 'image_alt' %}
            <span style="font-size:.62rem;padding:1px 6px;border-radius:20px;background:#E8F5E9;color:#2E7D32;border:1px solid #A5D6A7;margin-right:5px" title="Found in image alt text">&#128247; img alt</span>
          {% endif %}
          {% if item.url %}
            <a href="{{ item.url }}" target="_blank" rel="noopener">{{ item.title }}</a>
            <a href="{{ item.url }}" target="_blank" rel="noopener" class="verify-btn">Verify &rarr;</a>
          {% else %}
            <span class="no-link">{{ item.title }}</span>
          {% endif %}
        </li>
      {% endfor %}
    </ul>
  {% else %}
    <div class="clean">&#10003; No non-compliance flags &mdash; all specific organic product claims match the OID certificate.</div>
  {% endif %}

  {# ── 🟡 Caution — name variations + general cert ──────────── #}
  {% if report.caution %}
    <div class="section-label amber" style="margin-top:22px">
      &#128993; Caution
      <span class="badge">{{ report.caution | length }}</span>
    </div>

    {# General cert notice (when ALL cert products are generic category terms) #}
    {% if report.get('cert_general') %}
    <div style="margin-bottom:10px;padding:10px 14px;border-radius:12px;background:#FFF8E1;border:1px solid #FDE68A;font-size:.76rem;line-height:1.55;color:var(--text)">
      <strong style="color:#D97706">&#128220; General-Terms Certificate</strong> &mdash;
      This operation&rsquo;s OID certificate lists only general commodity terms (e.g. &ldquo;Eggs&rdquo;, &ldquo;Wine&rdquo;).
      Specific website products cannot be confirmed or denied from OID alone.
      Detailed product coverage is documented in the Organic System Plan held by the certifier.
      Items below are shown as caution rather than red flag.
      <span style="display:block;margin-top:4px;font-size:.7rem;color:var(--muted)">Ref: 7 CFR &sect;&nbsp;205.201 (OSP product list) &bull; SOE Rule &sect;&nbsp;205.2</span>
    </div>
    {% endif %}

    <p style="font-size:.76rem;color:var(--muted);margin-bottom:10px;line-height:1.55">
      {% if report.get('cert_general') %}
        Products on the website that cannot be verified against the OID certificate because the cert lists only general category terms.
        Also includes close name variations requiring certifier review.<br>
      {% else %}
        Products that closely resemble certified items but may have name differences (e.g.&nbsp;<em>Flax Oil</em> vs&nbsp;<em>Flaxseed Oil</em>).
        Not an NOP violation &mdash; requires certifier review for alignment.<br>
      {% endif %}
      <span style="font-size:.7rem;opacity:.7">Sub-category: name variation &bull; general cert scope &bull; certifier judgment required</span>
    </p>
    <ul class="product-list scrollable-list">
      {% for item in report.caution | sort(attribute='title') %}
        <li class="caution-item">
          <span class="caution-icon">&#126;</span>
          {% if item.get('_reason') == 'retailer_exemption_review' %}
            <span style="font-size:.62rem;padding:1px 6px;border-radius:20px;background:#F5F3FF;color:#6F5EF7;border:1px solid rgba(111,94,247,.3);margin-right:5px;font-weight:700">RETAILER REVIEW</span>
          {% elif item.get('_reason') == 'general_cert' %}
            <span style="font-size:.62rem;padding:1px 6px;border-radius:20px;background:#FFF8E1;color:#A16207;border:1px solid #FDE68A;margin-right:5px;font-weight:700">GENERAL CERT</span>
          {% endif %}
          {% if item.get('source') == 'image_alt' %}
            <span style="font-size:.62rem;padding:1px 6px;border-radius:20px;background:#E8F5E9;color:#2E7D32;border:1px solid #A5D6A7;margin-right:5px" title="Found in image alt text">&#128247; img alt</span>
          {% endif %}
          {% if item.url %}
            <a href="{{ item.url }}" target="_blank" rel="noopener" style="color:var(--amber);text-decoration:none;border-bottom:1px solid rgba(217,119,6,.25)">{{ item.title }}</a>
          {% else %}
            <span class="no-link">{{ item.title }}</span>
          {% endif %}
        </li>
      {% endfor %}
    </ul>
  {% endif %}

  {# ── 🟠 Marketing language ─────────────────────────────────── #}
  {% if report.marketing %}
    <div class="section-label orange" style="margin-top:22px">
      &#128992; Marketing Language Review
      <span class="badge">{{ report.marketing | length }}</span>
    </div>
    <p style="font-size:.76rem;color:var(--muted);margin-bottom:10px;line-height:1.55">
      Uses <em>organic</em> as marketing / category language rather than a specific product claim.
      Certifier should have approved this language in the Organic System Plan.<br>
      <span style="font-size:.7rem;opacity:.7">Ref: 7 CFR &sect;&nbsp;205.305&ndash;306 (<em>made with organic</em>) &bull; USDA NOP Guidance Doc&nbsp;5001</span>
    </p>
    <ul class="product-list scrollable-list">
      {% for item in report.marketing | sort(attribute='title') %}
        <li class="marketing-item">
          <span class="marketing-icon">&#9900;</span>
          {% if item.url %}
            <a href="{{ item.url }}" target="_blank" rel="noopener" style="color:#EA580C;text-decoration:none;border-bottom:1px solid rgba(234,88,12,.25)">{{ item.title }}</a>
          {% else %}
            <span class="no-link">{{ item.title }}</span>
          {% endif %}
        </li>
      {% endfor %}
    </ul>
  {% endif %}

  {# ── ✅ Confirmed compliant ────────────────────────────────── #}
  <div class="section-label green" style="margin-top:22px">
    &#10003; Confirmed on Certificate
    <span class="badge">{{ report.verified | length }}</span>
  </div>
  <p style="font-size:.76rem;color:var(--muted);margin-bottom:10px">
    Website organic products that match the current OID certificate &mdash; confirmed compliant.
  </p>
  <ul class="product-list scrollable-list">
    {% for item in report.verified | sort(attribute='title') %}
      <li class="ok-item">
        <span class="check-icon">&#10003;</span>
        {% if item.url %}
          <a href="{{ item.url }}" target="_blank" rel="noopener" style="color:#2E7D32;text-decoration:none">{{ item.title }}</a>
        {% else %}
          <span class="no-link">{{ item.title }}</span>
        {% endif %}
      </li>
    {% endfor %}
  </ul>

  {# ── OID certificate products ──────────────────────────────── #}
  <div class="section-label cyan" style="margin-top:22px">
    OID Certificate Products
    <span class="badge">{{ report.cert_product_count }}</span>
  </div>
  <p style="font-size:.76rem;color:var(--muted);margin-bottom:10px">
    All products on the current USDA Organic Integrity Database certificate
  </p>
  <ul class="product-list scrollable-list">
    {% for item in report.cert_products | sort %}
      <li class="cert-item">&#9654; {{ item }}</li>
    {% endfor %}
  </ul>

{% endif %}
"""


GATE_PARTIAL = """
<div class="gate-meta-block">
  <strong>{{ report.operation }}</strong><br>
  <span>{{ report.certifier }} &middot; {{ report.status }} &middot; {{ report.location }}</span>
</div>
<div class="gate-teaser">
  <div class="gate-stat {{ 'red-stat' if report.flagged|length > 0 else '' }}">
    <div class="g-num">{{ report.flagged|length }}</div>
    <div class="g-lbl">&#128308; Possible Issues</div>
  </div>
  <div class="gate-stat">
    <div class="g-num">{{ report.caution|length }}</div>
    <div class="g-lbl">&#128993; Caution</div>
  </div>
  <div class="gate-stat">
    <div class="g-num">{{ report.marketing|length }}</div>
    <div class="g-lbl">&#128992; Marketing Review</div>
  </div>
  <div class="gate-stat">
    <div class="g-num">{{ report.verified|length }}</div>
    <div class="g-lbl">&#10003; Confirmed OK</div>
  </div>
</div>
<hr class="gate-divider">
<div class="gate-wrap">
  <div class="gate-lock">&#128274;</div>
  <div class="gate-title">Unlock the Full Report</div>
  <div class="gate-sub">
    See exactly which products may need review, with direct links to each product page.
    One checker credit unlocks this report permanently.
  </div>
  <div class="gate-actions">
    <button class="gate-btn-primary" onclick="openAuthModal('signin')">Sign In to Unlock</button>
    <a href="/pricing" class="gate-btn-secondary">Buy Checker Credits</a>
  </div>
</div>
"""


# ---------------------------------------------------------------------------
# Main page HTML (Jinja template — includes dynamic ACTIVE_JOB injection)
# ---------------------------------------------------------------------------

MAIN_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Organic Web Checker — Compliance Review for Organic Brands &amp; Certifiers</title>
  <meta name="description" content="Compare your website's organic product claims against your live USDA Organic Integrity Database certificate. Surface items for review before they become issues.">
  <meta property="og:title" content="Organic Web Checker — Organic Compliance Review Tool">
  <meta property="og:description" content="AI-assisted review comparing organic website claims against live OID certificate data.">
  <link rel="icon" type="image/png" href="/static/favicon.png?v=5">
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Manrope:wght@400;500;600;700;800&family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
  <style>""" + GLOBAL_CSS + """
  /* ── Landing page extras ─────────────────────────────────────────────── */
  .report-title {
    font-size: .73rem; font-weight: 700; text-transform: uppercase;
    letter-spacing: .1em; color: var(--muted); margin-bottom: 22px;
    padding-bottom: 13px; border-bottom: 1px solid var(--border);
  }
  .landing-wrap { max-width: 1200px; margin: 0 auto; padding: 0 24px; }

  /* Hero */
  .hero-section {
    padding: 96px 0 80px;
    background: linear-gradient(180deg, #FFFFFF 0%, #F7F4FF 60%, #EEE8FF 100%);
    border-bottom: 1px solid var(--border);
  }
  .hero-inner {
    max-width: 1200px; margin: 0 auto; padding: 0 24px;
    display: grid; grid-template-columns: 1fr 1fr; gap: 56px; align-items: center;
  }
  .hero-eyebrow {
    display: inline-flex; align-items: center; gap: 7px;
    background: var(--lavender); color: var(--primary-dark);
    border: 1px solid rgba(111,94,247,.2);
    border-radius: 20px; padding: 5px 14px;
    font-size: .74rem; font-weight: 700; letter-spacing: .02em; margin-bottom: 20px;
  }
  .hero-eyebrow-dot { width: 6px; height: 6px; border-radius: 50%; background: #8B7CFF; flex-shrink: 0; }
  .hero-h1 {
    font-size: 3.5rem; font-weight: 800; line-height: 1.14; letter-spacing: -.03em;
    color: var(--text); margin-bottom: 20px; font-family: 'Manrope', system-ui, sans-serif;
  }
  .hero-h1 em { font-style: normal; color: #6F5EF7; }
  .hero-sub {
    font-size: 1.05rem; color: var(--muted); line-height: 1.7;
    margin-bottom: 32px; max-width: 480px;
  }
  .hero-ctas { display: flex; gap: 12px; flex-wrap: wrap; margin-bottom: 28px; }
  .hero-btn-primary {
    background: #6F5EF7; color: #fff;
    border: none; border-radius: 14px; padding: 14px 28px;
    font-size: .94rem; font-weight: 700; text-decoration: none; cursor: pointer;
    box-shadow: 0 8px 20px rgba(111,94,247,.22);
    transition: background .15s, box-shadow .15s, transform .1s; display: inline-block;
    height: 48px; line-height: 1; display: inline-flex; align-items: center;
  }
  .hero-btn-primary:hover { background: #5A4CE0; box-shadow: 0 10px 26px rgba(111,94,247,.32); transform: translateY(-1px); }
  .hero-btn-secondary {
    background: #fff; color: #6F5EF7;
    border: 1.5px solid #6F5EF7; border-radius: 14px; padding: 13px 26px;
    font-size: .94rem; font-weight: 600; text-decoration: none; cursor: pointer;
    transition: background .15s, transform .1s; display: inline-block;
    height: 48px; line-height: 1; display: inline-flex; align-items: center;
  }
  .hero-btn-secondary:hover { background: #F0EDFF; transform: translateY(-1px); }
  .hero-trust { display: flex; flex-wrap: wrap; gap: 16px; font-size: .81rem; color: var(--muted); }
  .hero-trust-item { display: flex; align-items: center; gap: 6px; }
  .hero-trust-check { color: #8B7CFF; font-weight: 700; }

  /* Hero mock card */
  .hero-mock-card {
    background: var(--surface); border: 1px solid var(--border);
    border-radius: 20px; padding: 28px;
    box-shadow: 0 20px 60px rgba(31,41,55,.08), 0 2px 12px rgba(31,41,55,.04);
  }
  .hero-card-header {
    font-size: .72rem; font-weight: 700; text-transform: uppercase;
    letter-spacing: .09em; color: var(--muted); margin-bottom: 18px;
    padding-bottom: 14px; border-bottom: 1px solid var(--border);
    display: flex; align-items: center; gap: 8px;
  }
  .hero-card-dot { width: 8px; height: 8px; border-radius: 50%; background: #8B7CFF; }
  .mock-row {
    display: flex; align-items: center; justify-content: space-between;
    padding: 11px 0; border-bottom: 1px solid var(--border);
  }
  .mock-row:last-of-type { border-bottom: none; }
  .mock-row-label { font-size: .86rem; color: var(--text); font-weight: 500; }
  .chip { font-size: .72rem; font-weight: 700; padding: 4px 11px; border-radius: 20px; border: 1px solid; }
  .chip-green   { background: #E8F5E9; color: #2E7D32; border-color: #A5D6A7; }
  .chip-amber   { background: #FFF8E1; color: #D97706; border-color: #FDE68A; }
  .chip-red     { background: #FEE2E2; color: #DC2626; border-color: #FCA5A5; }
  .chip-neutral { background: var(--lavender); color: var(--primary); border-color: rgba(111,94,247,.2); }
  .mock-divider { font-size: .7rem; font-weight: 700; text-transform: uppercase; letter-spacing: .08em; color: var(--dim); margin: 14px 0 10px; }
  .mock-flag-item {
    font-size: .82rem; padding: 8px 12px;
    background: #FEF2F2; border-left: 3px solid #FCA5A5;
    border-radius: 0 10px 10px 0; margin-bottom: 5px; color: var(--text);
  }
  .mock-ok-item {
    font-size: .82rem; padding: 8px 12px;
    background: #E8F5E9; border-left: 3px solid #81C784;
    border-radius: 0 10px 10px 0; color: var(--text);
  }
  .hero-card-footer {
    margin-top: 16px; padding-top: 14px; border-top: 1px solid var(--border);
    font-size: .72rem; color: var(--dim); text-align: center;
  }

  /* Trust bar */
  .trust-bar { background: var(--surface); border-bottom: 1px solid var(--border); padding: 20px 0; }
  .trust-bar-inner {
    max-width: 1200px; margin: 0 auto; padding: 0 24px;
    display: flex; align-items: center; flex-wrap: wrap; justify-content: center; gap: 8px;
  }
  .trust-bar-label { font-size: .78rem; font-weight: 600; color: var(--muted); margin-right: 24px; white-space: nowrap; }
  .trust-bar-items { display: flex; gap: 32px; flex-wrap: wrap; justify-content: center; }
  .trust-bar-item  { font-size: .82rem; color: var(--muted); display: flex; align-items: center; gap: 8px; font-weight: 500; }
  .trust-bar-icon  {
    width: 26px; height: 26px; border-radius: 7px;
    background: var(--lavender); display: flex; align-items: center; justify-content: center;
    color: #8B7CFF; flex-shrink: 0;
  }

  /* Features */
  .features-section { padding: 96px 0; background: var(--bg); }
  .section-header   { text-align: center; margin-bottom: 56px; }
  .section-label-sm {
    display: inline-block; font-size: .72rem; font-weight: 700; text-transform: uppercase;
    letter-spacing: .1em; color: var(--primary); margin-bottom: 12px;
  }
  .section-h2 {
    font-size: 2.5rem; font-weight: 700; color: var(--text);
    letter-spacing: -.02em; line-height: 1.2; margin-bottom: 14px;
    font-family: 'Manrope', system-ui, sans-serif;
  }
  .section-sub { font-size: 1rem; color: var(--muted); line-height: 1.6; max-width: 520px; margin: 0 auto; }
  .features-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(230px,1fr)); gap: 24px; }
  .feature-card {
    background: var(--surface); border: 1px solid var(--border);
    border-radius: 20px; padding: 28px 26px;
    transition: transform .15s, box-shadow .15s, border-color .15s;
    box-shadow: 0 8px 30px rgba(31,41,55,.06);
  }
  .feature-card:hover { transform: translateY(-3px); box-shadow: 0 14px 36px rgba(31,41,55,.10); border-color: rgba(111,94,247,.2); }
  .feature-icon {
    width: 44px; height: 44px; border-radius: 14px;
    background: var(--lavender); display: flex; align-items: center; justify-content: center;
    color: #6F5EF7; margin-bottom: 18px; flex-shrink: 0;
  }
  .feature-card-title { font-size: .95rem; font-weight: 700; color: var(--text); margin-bottom: 8px; font-family: 'Manrope', system-ui, sans-serif; }
  .feature-card-desc  { font-size: .84rem; color: var(--muted); line-height: 1.6; }

  /* How it works */
  .hiw-section {
    padding: 96px 0;
    background: linear-gradient(180deg, #F7F4FF 0%, #EEE8FF 100%);
    border-top: 1px solid var(--border); border-bottom: 1px solid var(--border);
  }
  .hiw-steps { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px,1fr)); gap: 0; }
  .hiw-step  { padding: 24px 28px 24px 0; }
  .hiw-step-num {
    width: 36px; height: 36px; border-radius: 50%;
    background: #6F5EF7; color: #fff;
    display: flex; align-items: center; justify-content: center;
    font-size: .84rem; font-weight: 800; margin-bottom: 14px;
    font-family: 'Manrope', system-ui, sans-serif;
  }
  .hiw-step-title { font-size: .95rem; font-weight: 700; color: var(--text); margin-bottom: 6px; font-family: 'Manrope', system-ui, sans-serif; }
  .hiw-step-desc  { font-size: .84rem; color: var(--muted); line-height: 1.6; }

  /* Sample output */
  .sample-section { padding: 96px 0; background: var(--bg); }
  .sample-inner {
    max-width: 1200px; margin: 0 auto; padding: 0 24px;
    display: grid; grid-template-columns: 1fr 1.2fr; gap: 56px; align-items: start;
  }
  .sample-left { padding-top: 12px; }
  .sample-left p { font-size: .92rem; color: var(--muted); line-height: 1.7; margin-top: 14px; }
  .sample-report {
    background: var(--surface); border: 1px solid var(--border);
    border-radius: 20px; overflow: hidden; box-shadow: 0 8px 30px rgba(31,41,55,.06);
  }
  .sample-report-header {
    background: var(--lavender); padding: 16px 22px; border-bottom: 1px solid var(--border);
    display: flex; align-items: center; justify-content: space-between;
  }
  .sample-op-name { font-size: .92rem; font-weight: 800; color: var(--text); font-family: 'Manrope', system-ui, sans-serif; }
  .sample-op-meta { font-size: .72rem; color: var(--muted); margin-top: 2px; }
  .sample-summary { display: grid; grid-template-columns: repeat(3,1fr); border-bottom: 1px solid var(--border); }
  .sample-stat { padding: 16px 18px; text-align: center; border-right: 1px solid var(--border); }
  .sample-stat:last-child { border-right: none; }
  .sample-stat-num { font-size: 1.5rem; font-weight: 900; line-height: 1; font-variant-numeric: tabular-nums; font-family: 'Manrope', system-ui, sans-serif; }
  .sample-stat-lbl { font-size: .65rem; color: var(--muted); text-transform: uppercase; letter-spacing: .07em; margin-top: 4px; }
  .sample-stat.red   .sample-stat-num { color: var(--red); }
  .sample-stat.amber .sample-stat-num { color: var(--amber); }
  .sample-stat.green .sample-stat-num { color: #2E7D32; }
  .sample-items { padding: 16px 22px; }
  .sample-section-head { font-size: .66rem; font-weight: 700; text-transform: uppercase; letter-spacing: .1em; color: var(--muted); margin-bottom: 8px; }
  .sample-flag-item {
    font-size: .84rem; padding: 9px 13px; background: #FEF2F2; border-left: 3px solid #FCA5A5;
    border-radius: 0 10px 10px 0; margin-bottom: 5px; color: var(--text);
  }
  .sample-ok-item {
    font-size: .84rem; padding: 9px 13px; background: #E8F5E9; border-left: 3px solid #81C784;
    border-radius: 0 10px 10px 0; margin-bottom: 5px; color: var(--text);
  }
  .sample-report-footer {
    padding: 12px 22px; background: var(--lavender); border-top: 1px solid var(--border);
    font-size: .72rem; color: var(--muted); text-align: center;
  }

  /* AI Transparency */
  .ai-section {
    padding: 96px 0; background: var(--surface);
    border-top: 1px solid var(--border); border-bottom: 1px solid var(--border);
  }
  .ai-inner {
    max-width: 1200px; margin: 0 auto; padding: 0 24px;
    display: grid; grid-template-columns: 1fr 1fr; gap: 56px; align-items: center;
  }
  .ai-cards { display: grid; gap: 14px; }
  .ai-card  { background: var(--lavender); border: 1px solid var(--border); border-radius: 20px; padding: 22px 24px; }
  .ai-card-icon  { display: flex; align-items: center; margin-bottom: 10px; color: #6F5EF7; }
  .ai-card-title { font-size: .9rem; font-weight: 700; color: var(--text); margin-bottom: 6px; font-family: 'Manrope', system-ui, sans-serif; }
  .ai-card-desc  { font-size: .83rem; color: var(--muted); line-height: 1.6; }

  /* Schedule button */
  .btn-schedule {
    display: flex; align-items: center; justify-content: center; gap: 8px;
    padding: 13px 24px; border-radius: 14px; font-weight: 700;
    font-size: .92rem; cursor: pointer; text-decoration: none;
    background: var(--lavender); color: #6F5EF7;
    border: 1.5px solid rgba(111,94,247,.25);
    transition: background .15s, box-shadow .15s; width: 100%; margin-top: 10px;
    box-shadow: none; font-family: 'Manrope', system-ui, sans-serif;
  }
  .btn-schedule:hover { background: #E4DEFF; box-shadow: 0 4px 14px rgba(111,94,247,.12); }

  /* Email-to dropdown on queue items */

  /* Run-check section */
  .run-check-section { padding: 96px 0; background: var(--bg); }
  .run-check-inner   { max-width: 640px; margin: 0 auto; padding: 0 24px; }

  /* ── Mobile (landing page) ───────────────────────────────────────────── */
  @media (max-width: 768px) {
    .hero-inner   { grid-template-columns: 1fr; gap: 32px; padding: 0 20px; }
    .hero-h1      { font-size: 2.2rem; }
    .hero-right   { display: none; }
    .hero-section { padding: 64px 0 48px; }
    .hero-sub     { font-size: .96rem; max-width: 100%; }
    .hero-ctas    { flex-direction: column; }
    .hero-btn-primary, .hero-btn-secondary { text-align: center; justify-content: center; }

    .trust-bar-inner { flex-direction: column; align-items: flex-start; gap: 12px; padding: 0 20px; }
    .trust-bar-label { margin-right: 0; }
    .trust-bar-items { gap: 12px; }

    .features-section, .hiw-section, .sample-section,
    .ai-section, .run-check-section { padding: 64px 0; }
    .features-grid { grid-template-columns: 1fr; gap: 16px; }
    .feature-card  { padding: 22px 18px; }

    .hiw-inner  { padding: 0 20px; }
    .hiw-steps  { grid-template-columns: 1fr; gap: 0; }
    .hiw-step   { padding: 20px 0; border-bottom: 1px solid var(--border); }
    .hiw-step:last-child { border-bottom: none; }

    .sample-inner { grid-template-columns: 1fr; gap: 28px; padding: 0 20px; }
    .sample-summary { grid-template-columns: repeat(3,1fr); }

    .ai-inner  { grid-template-columns: 1fr; gap: 28px; padding: 0 20px; }
    .ai-cards  { gap: 10px; }

    .section-inner, .landing-wrap { padding: 0 20px; }
    .section-h2 { font-size: 2rem; }

    .run-check-inner { padding: 0 20px; }
    .check-form-card { padding: 20px 16px; }

    .queue-panel { margin: 0 0 16px; }
    .meta-grid   { grid-template-columns: 1fr 1fr; }

    .pricing-grid { grid-template-columns: 1fr; }
  }

  @media (max-width: 480px) {
    .hero-h1     { font-size: 1.85rem; }
    .section-h2  { font-size: 1.6rem; }
    .hero-eyebrow { font-size: .7rem; }
    .sample-summary { grid-template-columns: 1fr 1fr 1fr; }
    .trust-bar-items { flex-direction: column; gap: 8px; }
    header { padding: 8px 14px; }
    .header-wordmark { display: none; }
    .nav-user-email  { max-width: 80px; }
    .modal-card { padding: 24px 18px; }
    .gate-teaser { grid-template-columns: repeat(2,1fr); }
  }
  </style>
</head>
<body>

<header>
  <a href="/" class="header-logo">
    <img src="/static/icon-header.png" class="header-logo-icon" alt="Organic Web Checker">
    <span class="header-wordmark"><span class="wm-organic">Organic</span> <span class="wm-checker">Web Checker</span></span>
  </a>
  <div class="header-center">
    <nav class="header-nav">
      <a href="/"         class="nav-link active">Product</a>
      <a href="/schedule" class="nav-link">Schedule</a>
      <a href="/about"    class="nav-link">How It Works</a>
      <a href="/pricing"  class="nav-link">Pricing</a>
      <a href="/history"  class="nav-link">History</a>
      <a href="/agents"   class="nav-link">API</a>
    </nav>
    <div class="header-credits-wrap" id="headerCredits" {% if not user_email %}style="display:none"{% endif %}>
      <span class="header-credit-badge" id="headerCreditText">{% if user_email %}{% if user_is_admin %}Admin &mdash; Unlimited{% else %}{{ user_credits }} Checker{{ 's' if user_credits != 1 else '' }} Available{% endif %}{% endif %}</span>
    </div>
  </div>
  <div class="header-right">
    <div id="navUserArea">
      {% if user_email %}
        <span class="nav-user-email">{{ user_email }}</span>
        <button class="nav-signout" onclick="doLogout()">Sign Out</button>
      {% endif %}
    </div>
    <a href="#run-check" class="header-cta-btn">Run Checker</a>
    <div class="header-icon-wrap">
      <button class="header-icon-btn" id="iconBtn" onclick="toggleDd(event)">
        <img src="/static/icon-header.png" class="header-icon" alt="">
      </button>
      <div class="header-dropdown" id="hDd">
        <a class="dropdown-item" href="/account">Account</a>
        <a class="dropdown-item" href="/schedule">Schedule Checker</a>
        <a class="dropdown-item" href="/history">Check History</a>
        <a class="dropdown-item" href="/pricing">Pricing</a>
        <a class="dropdown-item" href="/agents">Agents &amp; API</a>
        <a class="dropdown-item" href="/settings">Settings</a>
      </div>
    </div>
  </div>
</header>

<!-- HERO -->
<section class="hero-section">
  <div class="hero-inner">
    <div class="hero-left">
      <div class="hero-eyebrow">
        <span class="hero-eyebrow-dot"></span>
        Live USDA OID Certificate Comparison
      </div>
      <h1 class="hero-h1">Organic compliance<br>intelligence for<br><em>modern brands</em></h1>
      <p class="hero-sub">Compare your website&rsquo;s organic product claims against your live USDA certificate. Surface items for review before they become issues.</p>
      <div class="hero-ctas">
        <a href="#run-check" class="hero-btn-primary">Run Checker</a>
        <a href="#sample-output" class="hero-btn-secondary">View Sample Results</a>
      </div>
      <div class="hero-trust">
        <span class="hero-trust-item"><span class="hero-trust-check">&#10003;</span> AI-assisted review</span>
        <span class="hero-trust-item"><span class="hero-trust-check">&#10003;</span> Live certificate comparison</span>
        <span class="hero-trust-item"><span class="hero-trust-check">&#10003;</span> Built for certifiers, handlers &amp; consultants</span>
      </div>
    </div>
    <div class="hero-right">
      <div class="hero-mock-card">
        <div class="hero-card-header">
          <span class="hero-card-dot"></span>
          Compliance Review &mdash; Sunnycrest Naturals LLC
        </div>
        <div class="mock-row">
          <span class="mock-row-label">Certificate Status</span>
          <span class="chip chip-green">Verified</span>
        </div>
        <div class="mock-row">
          <span class="mock-row-label">Potential Review Flags</span>
          <span class="chip chip-amber">2 items</span>
        </div>
        <div class="mock-row">
          <span class="mock-row-label">Claim Consistency</span>
          <span class="chip chip-neutral">94%</span>
        </div>
        <div class="mock-row">
          <span class="mock-row-label">Products Verified</span>
          <span class="chip chip-green">18 matched</span>
        </div>
        <div class="mock-divider">Items surfaced for review</div>
        <div class="mock-flag-item">&#9888; Organic Sunflower Seed Butter</div>
        <div class="mock-ok-item">&#10003; Organic Oat Flour &mdash; on certificate</div>
        <div class="hero-card-footer">AI-assisted review &bull; Human judgment required for compliance decisions</div>
      </div>
    </div>
  </div>
</section>

<!-- TRUST BAR -->
<section class="trust-bar">
  <div class="trust-bar-inner">
    <span class="trust-bar-label">Built for</span>
    <div class="trust-bar-items">
      <span class="trust-bar-item"><span class="trust-bar-icon"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 9l9-7 9 7v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/><polyline points="9 22 9 12 15 12 15 22"/></svg></span> Organic handlers &amp; brands</span>
      <span class="trust-bar-item"><span class="trust-bar-icon"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="8" r="6"/><path d="M15.477 12.89 17 22l-5-3-5 3 1.523-9.11"/></svg></span> Certifying agents</span>
      <span class="trust-bar-item"><span class="trust-bar-icon"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/></svg></span> Compliance consultants</span>
      <span class="trust-bar-item"><span class="trust-bar-icon"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2H2v10l9.29 9.29c.94.94 2.48.94 3.42 0l6.58-6.58c.94-.94.94-2.48 0-3.42L12 2Z"/><path d="M7 7h.01"/></svg></span> Private label operations</span>
    </div>
  </div>
</section>

<!-- FEATURES -->
<section class="features-section">
  <div class="landing-wrap">
    <div class="section-header">
      <div class="section-label-sm">What it does</div>
      <h2 class="section-h2">A complete compliance review workflow</h2>
      <p class="section-sub">From website scan to structured report &mdash; every step designed for organic certification workflows.</p>
    </div>
    <div class="features-grid">
      <div class="feature-card">
        <div class="feature-icon"><svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/><line x1="11" y1="8" x2="15" y2="8"/><line x1="11" y1="11" x2="15" y2="11"/><line x1="11" y1="14" x2="13" y2="14"/></svg></div>
        <div class="feature-card-title">Website Claim Review</div>
        <div class="feature-card-desc">Scans product pages on Shopify, WooCommerce, BigCommerce, and most product websites for organic claims.</div>
      </div>
      <div class="feature-card">
        <div class="feature-icon"><svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M15 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7Z"/><path d="M14 2v4a2 2 0 0 0 2 2h4"/><path d="m9 15 2 2 4-4"/></svg></div>
        <div class="feature-card-title">Certificate Comparison</div>
        <div class="feature-card-desc">Pulls the live certificate directly from the USDA Organic Integrity Database and compares it against what&rsquo;s published online.</div>
      </div>
      <div class="feature-card">
        <div class="feature-icon"><svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg></div>
        <div class="feature-card-title">Compliance Flagging</div>
        <div class="feature-card-desc">Surfaces products that may not appear on the current certificate scope &mdash; flagged, caution, and marketing language categories.</div>
      </div>
      <div class="feature-card">
        <div class="feature-icon"><svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect width="8" height="4" x="8" y="2" rx="1"/><path d="M16 4h2a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h2"/><line x1="12" y1="11" x2="16" y2="11"/><line x1="12" y1="16" x2="16" y2="16"/><line x1="8" y1="11" x2="8.01" y2="11"/><line x1="8" y1="16" x2="8.01" y2="16"/></svg></div>
        <div class="feature-card-title">Results &amp; Audit Trail</div>
        <div class="feature-card-desc">Exports structured reports as Markdown or PDF. Check history is retained for repeat reviews and audit documentation.</div>
      </div>
    </div>
  </div>
</section>

<!-- HOW IT WORKS -->
<section class="hiw-section">
  <div class="landing-wrap">
    <div class="section-header">
      <div class="section-label-sm">The process</div>
      <h2 class="section-h2">How a check works</h2>
      <p class="section-sub">Enter an operation name and website. The checker handles the rest in about 60 seconds.</p>
    </div>
    <div class="hiw-steps">
      <div class="hiw-step">
        <div class="hiw-step-num">1</div>
        <div class="hiw-step-title">Identify the operation</div>
        <div class="hiw-step-desc">Enter the operation name as it appears in the USDA Organic Integrity Database and the website URL to check.</div>
      </div>
      <div class="hiw-step">
        <div class="hiw-step-num">2</div>
        <div class="hiw-step-title">Load certificate data</div>
        <div class="hiw-step-desc">The checker connects to the live OID and retrieves the current certificate scope &mdash; the products actually approved.</div>
      </div>
      <div class="hiw-step">
        <div class="hiw-step-num">3</div>
        <div class="hiw-step-title">Scan organic claims</div>
        <div class="hiw-step-desc">Every product page on the website is scanned for organic claims &mdash; titles, labels, and product descriptions.</div>
      </div>
      <div class="hiw-step">
        <div class="hiw-step-num">4</div>
        <div class="hiw-step-title">Surface review items</div>
        <div class="hiw-step-desc">Claims are compared against the certificate. Items that may need review are surfaced in a structured, exportable report.</div>
      </div>
    </div>
  </div>
</section>

<!-- SAMPLE OUTPUT -->
<section class="sample-section" id="sample-output">
  <div class="sample-inner">
    <div class="sample-left">
      <div class="section-label-sm">Sample output</div>
      <h2 class="section-h2">What a report looks like</h2>
      <p>Each check produces a structured compliance review: operation details, certificate scope, and a categorized list of items surfaced for review.</p>
      <p>Flagged items link directly to the product page on the website for fast manual verification.</p>
      <div style="margin-top:28px">
        <a href="#run-check" class="hero-btn-primary" style="font-size:.88rem;padding:11px 24px">Run a check on your operation &rarr;</a>
      </div>
    </div>
    <div class="sample-report">
      <div class="sample-report-header">
        <div>
          <div class="sample-op-name">Sunnycrest Naturals LLC</div>
          <div class="sample-op-meta">Example Certifier &bull; Certified &bull; Example State &bull; sunnycrestnaturals.com</div>
        </div>
        <span class="chip chip-amber">2 items for review</span>
      </div>
      <div class="sample-summary">
        <div class="sample-stat red"><div class="sample-stat-num">2</div><div class="sample-stat-lbl">Review Flags</div></div>
        <div class="sample-stat amber"><div class="sample-stat-num">1</div><div class="sample-stat-lbl">Caution</div></div>
        <div class="sample-stat green"><div class="sample-stat-num">18</div><div class="sample-stat-lbl">Verified</div></div>
      </div>
      <div class="sample-items">
        <div class="sample-section-head">&#128308; Items for review &mdash; not found on certificate</div>
        <div class="sample-flag-item">&#9888; Organic Sunflower Seed Butter &mdash; 12oz</div>
        <div class="sample-flag-item">&#9888; Organic Elderberry Syrup</div>
        <div style="margin-top:14px">
          <div class="sample-section-head">&#10003; Confirmed on certificate</div>
          <div class="sample-ok-item">&#10003; Organic Oat Flour &mdash; Stone Ground</div>
          <div class="sample-ok-item">&#10003; Organic Hemp Seed Oil</div>
        </div>
      </div>
      <div class="sample-report-footer">AI-assisted review &bull; Regulatory ref: 7 CFR Part 205 &bull; Human review required</div>
    </div>
  </div>
</section>

<!-- AI TRANSPARENCY -->
<section class="ai-section">
  <div class="ai-inner">
    <div>
      <div class="section-label-sm">How AI is used</div>
      <h2 class="section-h2">Transparent by design</h2>
      <p style="font-size:.95rem;color:var(--muted);line-height:1.7;margin-top:14px;margin-bottom:20px">
        Organic Web Checker uses AI to assist the review process &mdash; not to replace the judgment of certifiers, compliance specialists, or handlers. Every output is structured for human review.
      </p>
      <p style="font-size:.84rem;color:var(--muted);line-height:1.7">
        This tool surfaces potential items for consideration. It does not make compliance determinations, does not issue certifications, and is not a substitute for a qualified certifier&rsquo;s review.
      </p>
    </div>
    <div class="ai-cards">
      <div class="ai-card">
        <div class="ai-card-icon"><svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="4" y="4" width="16" height="16" rx="2"/><rect x="9" y="9" width="6" height="6"/><line x1="15" y1="2" x2="15" y2="4"/><line x1="15" y1="20" x2="15" y2="22"/><line x1="2" y1="15" x2="4" y2="15"/><line x1="2" y1="9" x2="4" y2="9"/><line x1="20" y1="15" x2="22" y2="15"/><line x1="20" y1="9" x2="22" y2="9"/><line x1="9" y1="2" x2="9" y2="4"/><line x1="9" y1="20" x2="9" y2="22"/></svg></div>
        <div class="ai-card-title">AI assists the review</div>
        <div class="ai-card-desc">Automated scanning and matching identifies potential gaps between what&rsquo;s marketed as organic and what&rsquo;s on the current OID certificate.</div>
      </div>
      <div class="ai-card">
        <div class="ai-card-icon"><svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><polyline points="16 11 18 13 22 9"/></svg></div>
        <div class="ai-card-title">Humans make the call</div>
        <div class="ai-card-desc">Flagged items require human review. Name variations, reformulations, and context all require certifier judgment &mdash; not automation.</div>
      </div>
      <div class="ai-card">
        <div class="ai-card-icon"><svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><circle cx="11.5" cy="14.5" r="2.5"/><path d="M13.25 16.25 15 18"/></svg></div>
        <div class="ai-card-title">Decision support, not a certifier</div>
        <div class="ai-card-desc">Results are structured for use in a broader compliance workflow. Organic Web Checker is a review tool, not a regulatory authority.</div>
      </div>
    </div>
  </div>
</section>

<!-- RUN A CHECK -->
<section class="run-check-section" id="run-check">
  <div class="run-check-inner">
    <div style="margin-bottom:28px">
      <div class="section-label-sm">Run a check</div>
      <h2 class="section-h2" style="font-size:1.6rem">Check an operation now</h2>
      <p style="font-size:.92rem;color:var(--muted);margin-top:8px;line-height:1.6">Enter the operation name exactly as it appears in OID and the website URL to check.</p>
    </div>

    <div class="card" id="progressPanel" style="display:none">
      <div class="ps-header">Running Check <span class="ps-spin">&#8635;</span></div>
      <div id="pstep1" class="progress-step ps-pending">
        <div class="ps-num">1</div>
        <div class="ps-info">
          <div class="ps-name">Connect to USDA Organic Integrity Database</div>
          <div class="ps-msg" id="pmsg1">Waiting&hellip;</div>
        </div>
        <div class="ps-bar"><div class="ps-fill"></div></div>
      </div>
      <div id="pstep2" class="progress-step ps-pending">
        <div class="ps-num">2</div>
        <div class="ps-info">
          <div class="ps-name">Load OID certificate</div>
          <div class="ps-msg" id="pmsg2">&mdash;</div>
        </div>
        <div class="ps-bar"><div class="ps-fill"></div></div>
      </div>
      <div id="pstep3" class="progress-step ps-pending">
        <div class="ps-num">3</div>
        <div class="ps-info">
          <div class="ps-name">Scan website for organic product claims</div>
          <div class="ps-msg" id="pmsg3">&mdash;</div>
        </div>
        <div class="ps-bar"><div class="ps-fill"></div></div>
      </div>
      <div id="pstep4" class="progress-step ps-pending">
        <div class="ps-num">4</div>
        <div class="ps-info">
          <div class="ps-name">Compare claims against certificate</div>
          <div class="ps-msg" id="pmsg4">&mdash;</div>
        </div>
        <div class="ps-bar"><div class="ps-fill"></div></div>
      </div>
    </div>

    <div class="card">
      <form id="checkForm">
        <label for="operation">Operation Name (as listed in OID)</label>
        <input type="text" id="operation" name="operation"
               placeholder="e.g. SUNNYCREST NATURALS LLC"
               value="{{ prefill_op or '' }}" required>
        <div class="hint" style="margin-top:-10px;margin-bottom:10px">&#9432; Use the exact legal name from the USDA OID certificate — trade names or abbreviations may return no results.</div>
        <label for="website">Website URL</label>
        <input type="text" id="website" name="website"
               placeholder="e.g. https://greenridgeorganics.com"
               value="{{ prefill_url or '' }}" required>
        <div class="hint">Supports Shopify, WooCommerce, BigCommerce, and most product websites</div>
        <label style="display:flex;align-items:center;gap:8px;margin-bottom:16px;cursor:pointer;font-size:.82rem;text-transform:none;letter-spacing:0;color:var(--muted);font-weight:500">
          <input type="checkbox" name="use_cache" value="1" style="width:15px;height:15px;accent-color:var(--primary);cursor:pointer;flex-shrink:0">
          Use cached OID data if available &mdash; skips live lookup (same cost, ~5s vs ~60s)
        </label>
        <button type="submit" id="submitBtn">Run Checker</button>
        <a href="/schedule" class="btn-schedule"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="18" rx="2" ry="2"/><line x1="16" y1="2" x2="16" y2="6"/><line x1="8" y1="2" x2="8" y2="6"/><line x1="3" y1="10" x2="21" y2="10"/></svg> Schedule Checker</a>
      </form>
    </div>

    <div class="form-stats" id="formStats">
      <div class="form-stat-item">
        <span>Total checks run:</span>
        <span class="form-stat-num" id="fsChecks">0</span>
      </div>
      <div class="form-stat-item">
        <span>Possible violations surfaced:</span>
        <span class="form-stat-num red" id="fsFlags">0</span>
      </div>
    </div>

    <div class="queue-panel" id="queuePanel" style="display:none">
      <div class="queue-header">
        Checks <span class="queue-count" id="queueCount">0</span>
      </div>
      <ul class="queue-list" id="queueList">
        <li class="empty-queue">No checks yet</li>
      </ul>
    </div>

    <div class="card" id="reportCard" style="display:none">
      <div class="report-title" id="reportTitle">Results</div>
      <div id="reportBody"></div>
    </div>

    <!-- Hidden stat elements kept for JS compat -->
    <div id="statsCounter" style="display:none">
      <span id="scChecks">0</span><span id="scFlags">0</span><span id="scFines">0</span>
    </div>
  </div>
</section>

<!-- FOOTER -->
<footer class="site-footer">
  <div class="site-footer-inner">
    <div class="footer-brand">
      <div class="footer-brand-name"><span class="wm-organic">Organic</span> <span class="wm-checker">Web Checker</span></div>
      <div class="footer-brand-desc">AI-assisted organic compliance review for handlers, certifiers, and compliance teams. Compares website claims against live USDA OID certificate data.</div>
      <div class="footer-disclaimer">
        Not a certifying agent. Results are decision-support only and require human review.<br>
        Not a substitute for a qualified certifier&rsquo;s judgment. Regulatory reference: 7 CFR Part 205 &mdash; USDA National Organic Program.
      </div>
    </div>
    <div class="footer-col">
      <h4>Product</h4>
      <a href="#run-check">Run Checker</a>
      <a href="/pricing">Pricing</a>
      <a href="/history">History</a>
      <a href="/agents">Agents &amp; API</a>
      <a href="/api">API Docs</a>
    </div>
    <div class="footer-col">
      <h4>Account</h4>
      <a href="/account">Sign In</a>
      <a href="/pricing">Purchase Credits</a>
      <a href="mailto:hello@organicwebchecker.com">Contact</a>
      <a href="/about">About</a>
    </div>
  </div>
  <div class="footer-bottom">
    <span class="footer-bottom-text">&copy; 2026 Healer&rsquo;s Find LLC &mdash; Organic Web Checker. All rights reserved.</span>
    <span class="footer-bottom-text">Not a certifier. Decision-support tool for compliance review.</span>
  </div>
</footer>

<script>
  const ACTIVE_JOB = {{ ('"' + active_job + '"') | safe if active_job else 'null' }};
  let pollTimer = null;
  let viewingJobId = ACTIVE_JOB;

  function toggleDd(e){e.stopPropagation();document.getElementById('hDd').classList.toggle('open');}
  document.addEventListener('click',()=>{const d=document.getElementById('hDd');if(d)d.classList.remove('open');});

  // ── Form submit ──────────────────────────────────────────────────────────
  document.getElementById('checkForm').addEventListener('submit', async (e) => {
    e.preventDefault();
    const btn = document.getElementById('submitBtn');
    btn.disabled = true; btn.textContent = 'Submitting\u2026';
    const res  = await fetch('/check', {method:'POST', body: new URLSearchParams(new FormData(e.target))});
    const data = await res.json();
    viewingJobId = data.job_id;
    btn.disabled = false; btn.textContent = 'Run Checker';
    refreshQueue();
    startPolling(data.job_id);
  });

  // ── Progress panel ───────────────────────────────────────────────────────
  const STEP_NAMES = [
    '',
    'Connect to USDA Organic Integrity Database',
    'Load OID certificate',
    'Scan website for organic product claims',
    'Compare claims against certificate',
  ];
  function updateProgress(job) {
    const panel = document.getElementById('progressPanel');
    if (!panel) return;
    const cur = job.step || 0;
    if (job.status === 'queued' || job.status === 'running') {
      panel.style.display = 'block';
      for (let i = 1; i <= 4; i++) {
        const el  = document.getElementById('pstep' + i);
        const msg = document.getElementById('pmsg' + i);
        if (!el) continue;
        if (i < cur) {
          el.className = 'progress-step ps-done';
          el.querySelector('.ps-num').textContent = '\u2713';
          if (msg) msg.textContent = 'Done';
        } else if (i === cur) {
          el.className = 'progress-step ps-active';
          el.querySelector('.ps-num').textContent = String(i);
          if (msg && job.step_msg) msg.textContent = job.step_msg;
        } else {
          el.className = 'progress-step ps-pending';
          el.querySelector('.ps-num').textContent = String(i);
          if (msg) msg.textContent = '\u2014';
        }
      }
    } else {
      panel.style.display = 'none';
    }
  }

  // ── Stats counter ────────────────────────────────────────────────────────
  async function refreshStats() {
    try {
      const s = await (await fetch('/stats')).json();
      document.getElementById('scChecks').textContent = s.checks_run.toLocaleString();
      document.getElementById('scFlags').textContent  = s.flags_found.toLocaleString();
      document.getElementById('scFines').textContent  = (s.flags_found + s.caution_found).toLocaleString();
      document.getElementById('fsChecks').textContent = s.checks_run.toLocaleString();
      document.getElementById('fsFlags').textContent  = s.flags_found.toLocaleString();
    } catch(e) {}
  }

  // ── Poll active job ───────────────────────────────────────────────────────
  function startPolling(jobId) {
    clearInterval(pollTimer);
    document.getElementById('progressPanel').style.display = 'block';
    document.getElementById('progressPanel').scrollIntoView({behavior:'smooth', block:'start'});
    pollTimer = setInterval(async () => {
      const res = await fetch('/job/' + jobId);
      const job = await res.json();
      updateProgress(job);
      if (job.status === 'done' || job.status === 'error') {
        clearInterval(pollTimer);
        document.getElementById('progressPanel').style.display = 'none';
        loadResult(jobId);
        refreshQueue();
        refreshStats();
      }
    }, 2000);
  }

  async function loadResult(jobId) {
    const res  = await fetch('/job/' + jobId + '/result');
    const html = await res.text();
    const job  = await (await fetch('/job/' + jobId)).json();
    const card  = document.getElementById('reportCard');
    document.getElementById('reportTitle').textContent = job.operation || 'Results';
    document.getElementById('reportBody').innerHTML = html;
    card.style.display = 'block';
    card.scrollIntoView({behavior:'smooth', block:'start'});
  }

  // ── Queue panel ──────────────────────────────────────────────────────────
  async function refreshQueue() {
    const res  = await fetch('/jobs');
    const jobs = await res.json();
    const panel = document.getElementById('queuePanel');
    const list  = document.getElementById('queueList');
    const count = document.getElementById('queueCount');
    if (jobs.length === 0) { panel.style.display = 'none'; return; }
    panel.style.display = 'block';
    count.textContent = jobs.length;
    list.innerHTML = jobs.map(j => {
      let pill;
      if (j.status === 'queued' || j.status === 'running') {
        pill = '<div class="checker-loader"><div class="cb-p p1"></div><div class="cb-p p2"></div><div class="cb-p p3"></div></div>';
      } else {
        pill = '<span class="status-pill status-' + j.status + '">' + j.status + '</span>';
      }
      const viewBtn = (j.status === 'done' || j.status === 'error')
        ? '<button class="view-btn" onclick="showJob(\\'' + j.id + '\\')">View</button>' : '';
      const emailBtn = (j.status === 'done')
        ? '<button class="view-btn" style="color:var(--primary);border-color:rgba(111,94,247,.25);margin-left:4px" id="ebtn-' + j.id + '" onclick="emailReport(\\'' + j.id + '\\',this)">&#9993; Email</button>' : '';
      return '<li class="queue-item"><div style="flex:1;min-width:0"><div class="op-name">' + j.operation + '</div><div class="site">' + j.website + '</div></div>' + pill + viewBtn + emailBtn + '</li>';
    }).join('');
  }

  async function showJob(jobId) { viewingJobId = jobId; await loadResult(jobId); }

  async function emailReport(jobId, btn) {
    const orig = btn.textContent;
    btn.disabled = true; btn.textContent = 'Sending…';
    try {
      const r = await fetch('/job/' + jobId + '/email-report', {method: 'POST'});
      const d = await r.json();
      if (d.ok) {
        btn.textContent = '✓ Sent';
      } else {
        btn.textContent = '✗ ' + (d.error || 'Failed');
        setTimeout(() => { btn.textContent = orig; btn.disabled = false; }, 4000);
      }
    } catch(e) { btn.textContent = '✗ Error'; setTimeout(() => { btn.textContent = orig; btn.disabled = false; }, 3000); }
  }

  if (ACTIVE_JOB) startPolling(ACTIVE_JOB);
  refreshQueue();
  refreshStats();
  setInterval(refreshQueue, 5000);
  setInterval(refreshStats, 10000);
</script>
<!-- Auth Modal -->
<div id="authModal" style="display:none" class="modal-overlay" onclick="if(event.target===this)closeAuthModal()">
  <div class="modal-card">
    <button class="modal-close" onclick="closeAuthModal()">&times;</button>
    <div class="auth-tabs">
      <div class="auth-tab active" id="tabSignin"    onclick="switchAuthTab('signin')">Sign In</div>
      <div class="auth-tab"        id="tabRegister"  onclick="switchAuthTab('register')">Create Account</div>
    </div>
    <div id="authMsg" style="display:none" class="auth-msg"></div>
    <div id="authFormSignin">
      <label>Email</label>
      <input type="email" id="siEmail" placeholder="you@example.com" style="width:100%;box-sizing:border-box;margin-bottom:10px">
      <label>Password</label>
      <input type="password" id="siPw" placeholder="&bull;&bull;&bull;&bull;&bull;&bull;&bull;&bull;" style="width:100%;box-sizing:border-box;margin-bottom:14px" onkeydown="if(event.key==='Enter')doLogin()">
      <button onclick="doLogin()" class="btn-glass">Sign In</button>
      <div style="text-align:right;margin-top:10px"><a href="/forgot-password" style="font-size:.78rem;color:var(--muted);text-decoration:none">Forgot password?</a></div>
    </div>
    <div id="authFormRegister" style="display:none">
      <label>Email</label>
      <input type="email" id="rgEmail" placeholder="you@example.com" style="width:100%;box-sizing:border-box;margin-bottom:10px">
      <label>Password</label>
      <input type="password" id="rgPw" placeholder="At least 8 characters" style="width:100%;box-sizing:border-box;margin-bottom:10px">
      <label>Confirm password</label>
      <input type="password" id="rgPw2" placeholder="Repeat password" style="width:100%;box-sizing:border-box;margin-bottom:14px" onkeydown="if(event.key==='Enter')doRegister()">
      <button onclick="doRegister()" class="btn-glass">Create Account</button>
    </div>
  </div>
</div>
<script>
function openAuthModal(tab){switchAuthTab(tab||'signin');document.getElementById('authModal').style.display='flex';}
function closeAuthModal(){document.getElementById('authModal').style.display='none';document.getElementById('authMsg').style.display='none';}
function switchAuthTab(tab){
  document.getElementById('tabSignin').classList.toggle('active', tab==='signin');
  document.getElementById('tabRegister').classList.toggle('active', tab==='register');
  document.getElementById('authFormSignin').style.display = tab==='signin' ? '' : 'none';
  document.getElementById('authFormRegister').style.display = tab==='register' ? '' : 'none';
}
function showAuthMsg(msg,type){const el=document.getElementById('authMsg');el.textContent=msg;el.className='auth-msg '+type;el.style.display='';}
async function doLogin(){
  const email=document.getElementById('siEmail').value.trim();
  const pw=document.getElementById('siPw').value;
  if(!email||!pw){showAuthMsg('Email and password required.','error');return;}
  try{
    const res=await fetch('/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({email,password:pw})});
    const data=await res.json();
    if(data.ok){closeAuthModal();updateAuthUI(data.email,data.credits);if(viewingJobId)loadResult(viewingJobId);}
    else showAuthMsg(data.error||'Sign-in failed.','error');
  }catch(e){showAuthMsg('Network error — try again.','error');}
}
async function doRegister(){
  const email=document.getElementById('rgEmail').value.trim();
  const pw=document.getElementById('rgPw').value;
  const pw2=document.getElementById('rgPw2').value;
  if(!email||!pw){showAuthMsg('Email and password required.','error');return;}
  if(pw!==pw2){showAuthMsg('Passwords do not match.','error');return;}
  if(pw.length<8){showAuthMsg('Password must be at least 8 characters.','error');return;}
  try{
    const res=await fetch('/register',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({email,password:pw})});
    const data=await res.json();
    if(data.ok){closeAuthModal();updateAuthUI(data.email,data.credits);if(viewingJobId)loadResult(viewingJobId);}
    else showAuthMsg(data.error||'Registration failed.','error');
  }catch(e){showAuthMsg('Network error — try again.','error');}
}
async function doLogout(){
  await fetch('/logout',{method:'POST'});
  location.reload();
}
function updateAuthUI(email,credits){
  const el=document.getElementById('navUserArea');
  if(!el)return;
  const credBadge=document.getElementById('headerCredits');
  const credText=document.getElementById('headerCreditText');
  if(email){
    el.innerHTML='<span class="nav-user-email">'+email+'</span>&nbsp;<button class="nav-signout" onclick="doLogout()">Sign Out</button>';
    if(credBadge&&credText){
      credText.textContent=credits>=99999?'Admin — Unlimited':(credits+' Checker'+(credits!==1?'s':'')+' Available');
      credBadge.style.display='flex';
    }
  }else{
    el.innerHTML='';
    if(credBadge)credBadge.style.display='none';
  }
}
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Pricing page
# ---------------------------------------------------------------------------

TIERS = [
    {"count": 1,   "price": 25,   "per": 25.00, "disc": None,    "name": "Single Check",   "desc": "One compliance check against a live USDA OID certificate. Ideal for a first self-review, a pre-renewal spot check, or verifying a specific product claim before launch.", "featured": False},
    {"count": 10,  "price": 220,  "per": 22.00, "disc": "12% off","name": "Small Operation","desc": "Ten checks for handlers and brands doing periodic self-audits, reviewing seasonal product updates, or spot-checking specific sections of a product catalog.", "featured": False},
    {"count": 25,  "price": 500,  "per": 20.00, "disc": "20% off","name": "Active Brand",   "desc": "Regular compliance reviews for growing operations. Ideal for brands with ongoing product changes, pre-certification prep, or structured quarterly reviews.", "featured": True},
    {"count": 50,  "price": 900,  "per": 18.00, "disc": "28% off","name": "Full Audit",     "desc": "Comprehensive coverage for established organic brands. Run full catalog sweeps, multi-site reviews, or pre-retailer-audit checks across a large product portfolio.", "featured": False},
    {"count": 100, "price": 1500, "per": 15.00, "disc": "40% off — best value","name": "High Volume", "desc": "For large handlers and multi-brand portfolios. Maximum volume discount — $15/check is the floor across all tiers. Credits never expire.", "featured": False},
]

def pricing_page_html():
    cards = ""
    for i, t in enumerate(TIERS):
        feat = ' featured' if t['featured'] else ''
        disc = t['disc'] if t['disc'] else '—'
        disc_cls = '' if t['disc'] else ' none'
        cards += f"""
        <div class="pricing-card{feat}">
          <div class="pricing-icon-row">
            <img src="/static/icon.png" class="pricing-icon" alt="">
            <div class="pricing-mult">&times;{t['count']}</div>
          </div>
          <div class="pricing-tier-name">{t['name']}</div>
          <div class="pricing-price">${int(t['price']) if t['price'] == int(t['price']) else t['price']}</div>
          <div class="pricing-per">${t['per']:.2f} per checker</div>
          <div class="pricing-disc{disc_cls}">{disc}</div>
          <div class="pricing-desc">{t['desc']}</div>
          <a href="#" class="pricing-cta" id="buy-{i}" onclick="return buyTier({i})">Purchase</a>
        </div>"""

    custom = """
        <div class="pricing-card" style="border-color:rgba(111,94,247,.3);background:linear-gradient(135deg,rgba(111,94,247,.04) 0%,transparent 100%)">
          <div class="pricing-icon-row">
            <img src="/static/icon.png" class="pricing-icon" alt="">
            <div class="pricing-mult" style="color:#6F5EF7">CA</div>
          </div>
          <div class="pricing-tier-name">Certifying Agents</div>
          <div style="display:inline-block;background:rgba(111,94,247,.1);border:1px solid rgba(111,94,247,.3);border-radius:20px;padding:5px 11px;font-size:.78rem;font-weight:700;color:#6F5EF7;margin-bottom:10px">Exclusive discount</div>
          <div class="pricing-price" style="font-size:1.7rem;color:#6F5EF7">50% off</div>
          <div class="pricing-per">any tier &mdash; verified certifiers only</div>
          <div class="pricing-disc" style="color:#8B7CFF">Promo code sent after verification</div>
          <div class="pricing-desc">USDA-accredited certifying agents get 50% off any package. Run desk-review compliance checks before sending inspectors &mdash; bake it into your cert fees. Submit your NOP accreditation number for verification.</div>
          <button onclick="openCertModal()" class="pricing-cta" style="background:transparent;border:2px solid #6F5EF7;color:#6F5EF7">Request Certifier Access &rarr;</button>
        </div>"""

    # Build the JS tier array without needing f-string brace escaping inside
    calc_tiers_js = "const CALC_TIERS=[" + ",".join(
        f"{{count:{t['count']},price:{t['price']},per:{t['per']},name:'{t['name']}',idx:{i}}}"
        for i, t in enumerate(TIERS)
    ) + "];"

    return f"""
    <div id="certModal" style="display:none" class="modal-overlay" onclick="if(event.target===this)closeCertModal()">
      <div class="modal-card" style="max-width:480px">
        <button class="modal-close" onclick="closeCertModal()">&times;</button>
        <div class="modal-title">Certifying Agent Verification</div>
        <div style="font-size:.8rem;color:var(--muted);margin-bottom:18px;line-height:1.6">Submit your information for verification. Once confirmed you&rsquo;re a USDA-accredited certifying agent, we&rsquo;ll email your 50% discount code &mdash; typically within 1 business day.</div>
        <div id="certModalMsg" style="display:none;margin-bottom:14px" class="auth-msg"></div>
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:10px">
          <div><label style="font-size:.78rem;font-weight:600;color:var(--muted)">First name</label><input type="text" id="certFirst" placeholder="Jane" style="margin-top:4px"></div>
          <div><label style="font-size:.78rem;font-weight:600;color:var(--muted)">Last name</label><input type="text" id="certLast" placeholder="Smith" style="margin-top:4px"></div>
        </div>
        <label style="font-size:.78rem;font-weight:600;color:var(--muted)">Certifying agency / organization</label>
        <input type="text" id="certOrg" placeholder="e.g. MOSA, CCOF, OCIA" style="margin-top:4px;margin-bottom:10px">
        <label style="font-size:.78rem;font-weight:600;color:var(--muted)">USDA NOP accreditation number</label>
        <input type="text" id="certNOP" placeholder="e.g. USDA-AMS-NOP-12-0001" style="margin-top:4px;margin-bottom:10px">
        <label style="font-size:.78rem;font-weight:600;color:var(--muted)">Work email</label>
        <input type="email" id="certEmail" placeholder="you@youragency.org" style="margin-top:4px;margin-bottom:16px">
        <button onclick="submitCertRequest()" class="btn-glass" style="width:100%">Submit Verification Request</button>
      </div>
    </div>
    <div class="pricing-intro">
      <img src="/static/icon.png" class="pricing-big-icon" alt="Organic Web Checker">
      <div class="pricing-intro-text">
        <h2>One checker. One compliance review.</h2>
        <p>Each checker runs a live comparison of a website against a live USDA Organic Integrity Database certificate &mdash; flagging products marketed as organic that may not be authorized on the cert. $25 per check. Volume packs available. Credits never expire.</p>
      </div>
    </div>
    <div id="pricing-grid" class="pricing-grid">
      {cards}
      {custom}
    </div>

    <div class="card" style="max-width:680px;margin:32px auto 0">
      <div style="font-size:1rem;font-weight:800;color:var(--text);margin-bottom:4px">Volume Calculator</div>
      <div style="font-size:.8rem;color:var(--muted);margin-bottom:20px">Estimate how many checks you need &mdash; we&rsquo;ll recommend the right tier.</div>
      <div style="display:flex;gap:14px;align-items:center;margin-bottom:20px;flex-wrap:wrap">
        <label style="font-size:.82rem;font-weight:600;color:var(--muted);white-space:nowrap">Checks needed:</label>
        <input type="range" id="calcSlider" min="1" max="200" value="10" oninput="syncCalc('slider')" style="flex:1;min-width:120px;accent-color:var(--primary)">
        <input type="number" id="calcNumber" min="1" max="500" value="10" oninput="syncCalc('number')" style="width:80px;text-align:center;font-weight:700;font-size:.95rem;padding:6px;border:1.5px solid var(--border);border-radius:14px">
      </div>
      <div id="calcResult"></div>
    </div>

    <div class="card" style="max-width:680px;margin:22px auto 0;background:#FFFBEB;border-color:#FDE68A">
      <div style="font-size:.9rem;font-weight:800;color:#92400E;margin-bottom:10px">Refund Policy</div>
      <div style="font-size:.8rem;color:#78350F;line-height:1.75">
        <strong>Single checks</strong> &mdash; Non-refundable once the check has been initiated.<br>
        <strong>Volume packs</strong> &mdash; Non-refundable after any check in the bundle has been run. If you purchase a pack and use zero credits, contact us within 7 days for a refund.<br>
        <strong>No partial refunds on partially-used bundles</strong> &mdash; Volume pricing reflects a commitment to the quantity purchased. The per-check discount is earned by purchasing that volume, not by using it. Refunds are not issued for unused credits in a partially-used pack.<br>
        <span style="color:#92400E;opacity:.75">Questions? <a href="mailto:hello@organicwebchecker.com" style="color:#92400E">hello@organicwebchecker.com</a></span>
      </div>
    </div>

    <script>
    {calc_tiers_js}
    const CALC_FLOOR = 12.00;

    function syncCalc(src) {{
      const slider = document.getElementById('calcSlider');
      const number = document.getElementById('calcNumber');
      let v = parseInt(src === 'slider' ? slider.value : number.value) || 1;
      v = Math.max(1, Math.min(v, 500));
      slider.value = Math.min(v, 200);
      number.value = v;
      renderCalc(v);
    }}

    function renderCalc(qty) {{
      let tier = CALC_TIERS.find(function(t) {{ return t.count >= qty; }});
      let tierName, tierCount, tierPrice, perCheck, extra, tierIdx;
      if (tier) {{
        perCheck  = Math.max(CALC_FLOOR, tier.per);
        extra     = tier.count - qty;
        tierName  = tier.name;
        tierCount = tier.count;
        tierPrice = tier.price;
        tierIdx   = tier.idx;
      }} else {{
        const packs = Math.ceil(qty / 100);
        tierName  = packs + '\u00d7 High Volume';
        tierCount = packs * 100;
        tierPrice = packs * 1500;
        perCheck  = 15.00;
        extra     = packs * 100 - qty;
        tierIdx   = 4;
      }}
      let html = '<div class="calc-grid">';
      html += '<div class="calc-box"><div class="calc-lbl">Tier</div><div class="calc-val">' + tierName + '</div><div class="calc-sub">' + tierCount + ' credits</div></div>';
      html += '<div class="calc-box"><div class="calc-lbl">Total price</div><div class="calc-val">$' + tierPrice.toLocaleString() + '</div></div>';
      html += '<div class="calc-box"><div class="calc-lbl">Per check</div><div class="calc-val" style="color:var(--primary)">$' + perCheck.toFixed(2) + '</div></div>';
      if (extra > 0) {{
        html += '<div class="calc-box"><div class="calc-lbl">Leftover</div><div class="calc-val" style="color:var(--primary)">' + extra + ' credits</div><div class="calc-sub">never expire</div></div>';
      }}
      html += '</div>';
      if (qty > 100) {{
        html += '<div style="font-size:.75rem;color:var(--muted);margin-bottom:12px">For &gt;100 checks, purchase multiple High Volume packs. Need a custom enterprise plan? <a href="mailto:hello@organicwebchecker.com" style="color:var(--primary)">Contact us</a>.</div>';
      }}
      html += '<button onclick="buyTier(' + tierIdx + ')" class="pricing-cta" style="width:100%">Buy ' + tierName + ' \u2192</button>';
      document.getElementById('calcResult').innerHTML = html;
    }}

    function openCertModal() {{
      document.getElementById('certModal').style.display = 'flex';
      document.getElementById('certModalMsg').style.display = 'none';
    }}
    function closeCertModal() {{
      document.getElementById('certModal').style.display = 'none';
    }}
    async function submitCertRequest() {{
      const first = document.getElementById('certFirst').value.trim();
      const last  = document.getElementById('certLast').value.trim();
      const org   = document.getElementById('certOrg').value.trim();
      const nop   = document.getElementById('certNOP').value.trim();
      const email = document.getElementById('certEmail').value.trim();
      const msg   = document.getElementById('certModalMsg');
      if (!first || !last || !org || !nop || !email) {{
        msg.style.display = 'block'; msg.style.background = '#FEE2E2'; msg.style.color = '#B91C1C';
        msg.textContent = 'Please fill in all fields.'; return;
      }}
      // Disable submit button while verifying
      const submitBtn = document.querySelector('#certModal .btn-glass');
      if (submitBtn) {{ submitBtn.disabled = true; submitBtn.textContent = 'Verifying\u2026'; }}
      msg.style.display = 'block'; msg.style.background = '#EFF6FF'; msg.style.color = '#1D4ED8';
      msg.innerHTML = '\u23f3 Verifying your NOP number with USDA\u2014this takes up to 30 seconds\u2026';
      try {{
        const res  = await fetch('/api/certifier-request', {{
          method: 'POST', headers: {{'Content-Type': 'application/json'}},
          body: JSON.stringify({{first_name: first, last_name: last, organization: org, nop_number: nop, email}})
        }});
        const data = await res.json();
        if (!data.ok) {{
          msg.style.background = '#FEE2E2'; msg.style.color = '#B91C1C';
          msg.textContent = data.error || 'Submission failed. Please try again.';
          if (submitBtn) {{ submitBtn.disabled = false; submitBtn.textContent = 'Submit Verification Request'; }}
          return;
        }}
        // Poll for verification result
        let attempts = 0;
        const poll = setInterval(async function() {{
          attempts++;
          if (attempts > 20) {{  // 60s timeout
            clearInterval(poll);
            msg.style.background = '#FEF3C7'; msg.style.color = '#92400E';
            msg.textContent = 'Verification is taking longer than expected \u2014 we\u2019ll review your request manually and email your code within 1 business day.';
            if (submitBtn) {{ submitBtn.disabled = false; submitBtn.textContent = 'Submit Verification Request'; }}
            return;
          }}
          try {{
            const sr   = await fetch('/api/certifier-verify-status/' + data.request_id);
            const sd   = await sr.json();
            if (sd.status === 'verified') {{
              clearInterval(poll);
              msg.style.background = '#F0FDF4'; msg.style.color = '#2D8049';
              msg.innerHTML = '\u2705 Verified! Your discount code is: <strong style="font-family:monospace;font-size:1.05rem;background:#E0F7E9;padding:2px 8px;border-radius:4px">CERTIFIER50</strong><br><span style="font-size:.8rem;color:var(--muted)">Enter this at checkout for 50% off any tier.</span>';
              document.querySelectorAll('#certFirst,#certLast,#certOrg,#certNOP,#certEmail').forEach(function(el) {{ el.value = ''; }});
              if (submitBtn) {{ submitBtn.style.display = 'none'; }}
            }} else if (sd.status === 'not_found') {{
              clearInterval(poll);
              msg.style.background = '#FEF3C7'; msg.style.color = '#92400E';
              msg.textContent = 'We couldn\u2019t automatically match your organization in the USDA certifier directory. We\u2019ll review your NOP number manually and email your code within 1 business day.';
              if (submitBtn) {{ submitBtn.disabled = false; submitBtn.textContent = 'Submit Verification Request'; }}
            }} else if (sd.status === 'error') {{
              clearInterval(poll);
              msg.style.background = '#FEF3C7'; msg.style.color = '#92400E';
              msg.textContent = 'USDA verification service is temporarily unavailable. Your request was saved \u2014 we\u2019ll verify manually and email your code within 1 business day.';
              if (submitBtn) {{ submitBtn.disabled = false; submitBtn.textContent = 'Submit Verification Request'; }}
            }}
            // else still 'verifying' — keep polling
          }} catch(e) {{ /* network hiccup, keep polling */ }}
        }}, 3000);
      }} catch(e) {{
        msg.style.background = '#FEE2E2'; msg.style.color = '#B91C1C';
        msg.textContent = 'Network error \u2014 please try again.';
        if (submitBtn) {{ submitBtn.disabled = false; submitBtn.textContent = 'Submit Verification Request'; }}
      }}
    }}

    async function buyTier(i) {{
      const btn = document.getElementById('buy-' + i);
      if (btn) {{ btn.textContent = 'Redirecting\u2026'; btn.style.opacity = '0.7'; btn.style.pointerEvents = 'none'; }}
      try {{
        const res  = await fetch('/create-checkout-session', {{
          method: 'POST',
          headers: {{'Content-Type': 'application/json'}},
          body: JSON.stringify({{tier_index: i}})
        }});
        const data = await res.json();
        if (data.url) {{
          window.location.href = data.url;
        }} else {{
          alert('Checkout error: ' + (data.error || 'Please try again.'));
          if (btn) {{ btn.textContent = 'Purchase'; btn.style.opacity = ''; btn.style.pointerEvents = ''; }}
        }}
      }} catch(e) {{
        alert('Network error \u2014 please try again.');
        if (btn) {{ btn.textContent = 'Purchase'; btn.style.opacity = ''; btn.style.pointerEvents = ''; }}
      }}
      return false;
    }}

    renderCalc(10);
    </script>"""


# ---------------------------------------------------------------------------
# Account page
# ---------------------------------------------------------------------------

ACCOUNT_BODY = """
<div class="account-wrap">
  <div id="acctLoggedOut">
    <div class="page-title">Account</div>
    <div class="page-subtitle">Sign in to access your check history, saved reports, and purchased checker credits.</div>
    <div class="card" style="max-width:420px;margin:0 auto">
      <div class="auth-tabs">
        <div class="auth-tab active" id="acctTabSignin"   onclick="acctSwitchTab('signin')">Sign In</div>
        <div class="auth-tab"        id="acctTabRegister" onclick="acctSwitchTab('register')">Create Account</div>
      </div>
      <div id="acctMsg" style="display:none" class="auth-msg"></div>
      <div id="acctFormSignin">
        <label>Email</label>
        <input type="email" id="acctSiEmail" placeholder="you@example.com">
        <label style="margin-top:10px">Password</label>
        <input type="password" id="acctSiPw" placeholder="&bull;&bull;&bull;&bull;&bull;&bull;&bull;&bull;" onkeydown="if(event.key==='Enter')acctDoLogin()">
        <button onclick="acctDoLogin()" class="btn-glass" style="width:100%;margin-top:14px">Sign In</button>
        <div style="text-align:right;margin-top:10px"><a href="/forgot-password" style="font-size:.78rem;color:var(--muted);text-decoration:none">Forgot password?</a></div>
      </div>
      <div id="acctFormRegister" style="display:none">
        <label>Email</label>
        <input type="email" id="acctRgEmail" placeholder="you@example.com">
        <label style="margin-top:10px">Password</label>
        <input type="password" id="acctRgPw" placeholder="At least 8 characters">
        <label style="margin-top:10px">Confirm password</label>
        <input type="password" id="acctRgPw2" placeholder="Repeat password" onkeydown="if(event.key==='Enter')acctDoRegister()">
        <button onclick="acctDoRegister()" class="btn-glass" style="width:100%;margin-top:14px">Create Account</button>
      </div>
    </div>
  </div>
  <div id="acctLoggedIn" style="display:none">
    <div class="page-title">Account</div>
    <div class="card" style="max-width:540px;margin:0 auto 22px">
      <div style="font-size:.82rem;color:var(--muted);margin-bottom:6px">Signed in as</div>
      <div id="acctEmail" style="font-size:1rem;font-weight:700;color:var(--text);margin-bottom:18px"></div>
      <div style="display:flex;gap:12px;align-items:center;padding:18px;background:var(--lavender);border-radius:16px;margin-bottom:20px">
        <div>
          <div style="font-size:.75rem;color:var(--muted)">Checker credits</div>
          <div id="acctCredits" style="font-size:1.5rem;font-weight:900;color:var(--primary);font-family:'Manrope',system-ui,sans-serif">—</div>
        </div>
        <a href="/pricing" style="margin-left:auto;font-size:.85rem;font-weight:600;color:#6F5EF7;text-decoration:none;background:#fff;padding:9px 18px;border-radius:12px;border:1.5px solid #6F5EF7">Buy more &rarr;</a>
      </div>
      <button onclick="acctDoLogout()" style="background:none;border:1px solid var(--border);border-radius:10px;padding:8px 16px;color:var(--muted);font-size:.84rem;cursor:pointer;transition:color .15s,border-color .15s" onmouseover="this.style.color='var(--red)';this.style.borderColor='var(--red)'" onmouseout="this.style.color='var(--muted)';this.style.borderColor='var(--border)'">Sign Out</button>
    </div>

    <div class="card" style="max-width:540px;margin:0 auto">
      <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:18px">
        <div>
          <div style="font-size:.9rem;font-weight:800;color:var(--text)">API Keys</div>
          <div style="font-size:.75rem;color:var(--muted);margin-top:2px">Use these keys to call the checker programmatically &mdash; <a href="/api" style="color:var(--primary)">API docs</a></div>
        </div>
        <button id="apiKeyCreateBtn" onclick="apiKeyCreate()" style="font-size:.8rem;font-weight:700;padding:8px 18px;background:#6F5EF7;color:#fff;border:none;border-radius:12px;cursor:pointer;box-shadow:0 4px 12px rgba(111,94,247,.2)">+ New Key</button>
      </div>
      <div id="apiKeyNewBox" style="display:none;padding:14px;background:#EEE8FF;border:1px solid #C4B8E8;border-radius:14px;margin-bottom:16px;font-size:.82rem">
        <strong style="color:#6F5EF7">Key created — copy it now, it won&rsquo;t be shown again:</strong><br>
        <div style="display:flex;gap:8px;margin-top:8px;align-items:center">
          <code id="apiKeyNewValue" style="background:#E4DEFF;padding:6px 10px;border-radius:8px;font-size:.78rem;word-break:break-all;flex:1"></code>
          <button onclick="navigator.clipboard.writeText(document.getElementById('apiKeyNewValue').textContent);this.textContent='Copied!'" style="font-size:.72rem;padding:5px 10px;background:#6F5EF7;color:#fff;border:none;border-radius:8px;cursor:pointer;white-space:nowrap">Copy</button>
        </div>
        <div style="margin-top:10px;font-size:.72rem;color:var(--muted)">Include this key as <code style="background:var(--bg);padding:1px 4px;border-radius:3px">X-API-Key</code> header when calling <code style="background:var(--bg);padding:1px 4px;border-radius:3px">POST /mcp</code>.</div>
      </div>
      <div id="apiKeyNameBox" style="display:none;margin-bottom:14px">
        <input type="text" id="apiKeyName" placeholder="Key name (e.g. Production, Claude Agent)" style="margin-bottom:8px">
        <div style="display:flex;gap:8px">
          <button onclick="apiKeyConfirmCreate()" style="font-size:.8rem;font-weight:700;padding:8px 18px;background:#6F5EF7;color:#fff;border:none;border-radius:12px;cursor:pointer">Create</button>
          <button onclick="document.getElementById('apiKeyNameBox').style.display='none'" style="font-size:.8rem;padding:8px 14px;background:none;border:1.5px solid var(--border);border-radius:12px;cursor:pointer;color:var(--muted)">Cancel</button>
        </div>
      </div>
      <div id="apiKeyList"><div style="font-size:.8rem;color:var(--muted)">Loading keys&hellip;</div></div>
    </div>
  </div>
</div>
<script>
function acctSwitchTab(tab){
  document.getElementById('acctTabSignin').classList.toggle('active',tab==='signin');
  document.getElementById('acctTabRegister').classList.toggle('active',tab==='register');
  document.getElementById('acctFormSignin').style.display=tab==='signin'?'':'none';
  document.getElementById('acctFormRegister').style.display=tab==='register'?'':'none';
  document.getElementById('acctMsg').style.display='none';
}
function showAcctMsg(msg,type){const el=document.getElementById('acctMsg');el.textContent=msg;el.className='auth-msg '+type;el.style.display='';}
async function acctDoLogin(){
  const email=document.getElementById('acctSiEmail').value.trim();
  const pw=document.getElementById('acctSiPw').value;
  if(!email||!pw){showAcctMsg('Email and password required.','error');return;}
  const res=await fetch('/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({email,password:pw})});
  const data=await res.json();
  if(data.ok){location.reload();}
  else showAcctMsg(data.error||'Sign-in failed.','error');
}
async function acctDoRegister(){
  const email=document.getElementById('acctRgEmail').value.trim();
  const pw=document.getElementById('acctRgPw').value;
  const pw2=document.getElementById('acctRgPw2').value;
  if(!email||!pw){showAcctMsg('Email and password required.','error');return;}
  if(pw!==pw2){showAcctMsg('Passwords do not match.','error');return;}
  if(pw.length<8){showAcctMsg('Password must be at least 8 characters.','error');return;}
  const res=await fetch('/register',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({email,password:pw})});
  const data=await res.json();
  if(data.ok){location.reload();}
  else showAcctMsg(data.error||'Registration failed.','error');
}
async function acctDoLogout(){
  await fetch('/logout',{method:'POST'});
  location.reload();
}

// ── API Keys ─────────────────────────────────────────────────────────────────
async function apiKeyLoadList(){
  const res=await fetch('/api/keys/list');
  const d=await res.json();
  const el=document.getElementById('apiKeyList');
  if(!d.keys||d.keys.length===0){
    el.innerHTML='<div style="font-size:.8rem;color:var(--muted);padding:8px 0">No API keys yet. Create one above.</div>';
    return;
  }
  el.innerHTML=d.keys.map(k=>`
    <div style="display:flex;align-items:center;gap:10px;padding:10px 0;border-bottom:1px solid var(--border)">
      <div style="flex:1;min-width:0">
        <div style="font-size:.84rem;font-weight:600;color:var(--text)">${k.name}</div>
        <div style="font-size:.72rem;color:var(--muted);margin-top:2px"><code style="background:var(--bg);padding:1px 5px;border-radius:3px">${k.key_prefix}</code> &middot; Created ${k.created_at} &middot; Last used: ${k.last_used_at}</div>
      </div>
      <button onclick="apiKeyRevoke('${k.key_id}')" style="font-size:.72rem;padding:4px 10px;background:none;border:1px solid #FECACA;border-radius:6px;color:var(--red);cursor:pointer">Revoke</button>
    </div>
  `).join('');
}
function apiKeyCreate(){
  document.getElementById('apiKeyNameBox').style.display='';
  document.getElementById('apiKeyNewBox').style.display='none';
  document.getElementById('apiKeyName').focus();
}
async function apiKeyConfirmCreate(){
  const name=document.getElementById('apiKeyName').value.trim()||'My API Key';
  const res=await fetch('/api/keys/create',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name})});
  const d=await res.json();
  document.getElementById('apiKeyNameBox').style.display='none';
  if(d.ok){
    document.getElementById('apiKeyNewValue').textContent=d.raw_key;
    document.getElementById('apiKeyNewBox').style.display='';
    apiKeyLoadList();
  }
}
async function apiKeyRevoke(key_id){
  if(!confirm('Revoke this key? Apps using it will lose access.'))return;
  await fetch('/api/keys/'+key_id+'/revoke',{method:'POST'});
  apiKeyLoadList();
}

// Extend acctInit to also load keys
const _origAcctInit=typeof acctInit==='function'?acctInit:null;
async function acctInit(){
  const res=await fetch('/api/user');
  const d=await res.json();
  if(d.logged_in){
    document.getElementById('acctLoggedOut').style.display='none';
    document.getElementById('acctLoggedIn').style.display='';
    document.getElementById('acctEmail').textContent=d.email;
    document.getElementById('acctCredits').textContent=d.is_admin?'Unlimited (Admin)':(d.credits+' credit'+(d.credits!==1?'s':''));
    apiKeyLoadList();
  }
}
acctInit();
</script>
"""


# ---------------------------------------------------------------------------
# Settings page
# ---------------------------------------------------------------------------

SETTINGS_BODY = """
<div class="page-title">Settings</div>
<div class="page-subtitle">Preferences and configuration for your Organic Web Checker experience.</div>
<div class="card">
  <div class="setting-row">
    <div class="setting-info">
      <div class="setting-label">Email Notifications</div>
      <div class="setting-desc">Get an email when a checker finishes running. Requires account sign-in.</div>
    </div>
    <div class="toggle" onclick="this.classList.toggle('on')"></div>
  </div>
  <div class="setting-row">
    <div class="setting-info">
      <div class="setting-label">Auto-Download on Completion</div>
      <div class="setting-desc">Automatically download the .md report when a checker finishes.</div>
    </div>
    <div class="toggle" onclick="this.classList.toggle('on')"></div>
  </div>
  <div class="setting-row">
    <div class="setting-info">
      <div class="setting-label">Default Download Format</div>
      <div class="setting-desc">Preferred format when using auto-download.</div>
    </div>
    <select style="background:var(--surface);border:1.5px solid var(--border);border-radius:10px;padding:6px 10px;color:var(--text);font-size:.84rem;margin-left:16px">
      <option>.md (Markdown)</option>
      <option>PDF (Print)</option>
    </select>
  </div>
  <div class="setting-row">
    <div class="setting-info">
      <div class="setting-label">API Access</div>
      <div class="setting-desc">Generate an API key to run checkers programmatically. Requires account.</div>
    </div>
    <button style="font-size:.76rem;padding:6px 14px;background:var(--lavender);border:1.5px solid rgba(111,94,247,.25);border-radius:10px;color:var(--primary);cursor:pointer;margin-left:16px;font-weight:600" onclick="alert('API access coming soon with account launch.')">Generate Key</button>
  </div>
  <div class="setting-row">
    <div class="setting-info">
      <div class="setting-label">Session History Retention</div>
      <div class="setting-desc">Keep check history until browser closes, or clear on each new session.</div>
    </div>
    <div class="toggle on" onclick="this.classList.toggle('on')"></div>
  </div>
</div>
<div class="settings-note">
  Settings are currently UI-only and reset on page reload. Full persistence launches with account sign-in.
  Have a feature you&rsquo;d like to see? <a href="mailto:hello@organicwebchecker.com" style="color:var(--primary);text-decoration:none">Email us</a>.
</div>
"""


# ---------------------------------------------------------------------------
# About page
# ---------------------------------------------------------------------------

ABOUT_BODY = """
<div class="card about-hero">
  <div class="about-tagline">
    Review your organic claims.<br>Surface potential gaps.<br>Support your compliance process.
  </div>
  <div class="about-penalty-num">$22,974</div>
  <div class="about-penalty-label">per violation &bull; per claim &bull; per instance</div>
  <div class="about-penalty-sub">Maximum civil penalty under the USDA National Organic Program</div>
</div>

<div class="card">
  <div class="about-section-title">&#9888; The Problem</div>
  <div class="about-p">
    Every organic claim you publish on your website carries real regulatory weight.
    Under the USDA National Organic Program, misuse or misrepresentation of organic status can result in civil penalties <strong>per product, per claim, per instance</strong>.
    Even well-managed operations can unknowingly drift out of alignment with their current certificate scope.
  </div>
  <ul class="risk-list">
    <li>&#9654; Outdated product listings that no longer match certification scope</li>
    <li>&#9654; Claims like &ldquo;organic&rdquo; or &ldquo;certified organic&rdquo; applied inconsistently</li>
    <li>&#9654; Products no longer approved but still live on the website</li>
    <li>&#9654; Websites evolving faster than compliance reviews can follow</li>
  </ul>
</div>

<div class="card">
  <div class="about-section-title">&#10003; The Solution</div>
  <div class="about-lead">
    Organic Web Checker compares your website&rsquo;s organic product claims against your current OID certificate scope&thinsp;&mdash;&thinsp;surfacing items that may need review.
  </div>
  <div class="about-p">
    Each <strong style="color:var(--primary)">checker</strong> is one automated web check. Enter an operation name and website URL. The checker pulls the live OID certificate, scans every product page, and instantly flags any organic claim that doesn&rsquo;t match the cert.
  </div>
  <ul class="feature-list">
    <li>&#10003; Scans your website for product titles containing organic claims</li>
    <li>&#10003; Compares those titles against the live USDA Organic Integrity Database certificate</li>
    <li>&#10007; Flags product titles not found on the current OID certificate</li>
    <li>&#10007; Surfaces products that may no longer appear on the certificate scope</li>
    <li>&#8595; Exports structured reports as Markdown or PDF for review by certifiers or consultants</li>
  </ul>
</div>

<div class="card">
  <div class="about-section-title">&#9888; Why It Matters</div>
  <div class="about-p">Identifying gaps early gives operations and certifiers a chance to review and address issues before they escalate into:</div>
  <ul class="consequence-list">
    <li>&#9654; Civil penalties ($22,974 maximum per violation)</li>
    <li>&#9654; Certification suspension or revocation</li>
    <li>&#9654; Retailer, buyer, and partner fallout</li>
    <li>&#9654; Public enforcement listings</li>
  </ul>
</div>

<div class="card">
  <div class="about-section-title">&#9670; Built for the Organic Industry</div>
  <div class="about-p">Designed for operations where compliance is critical:</div>
  <div class="audience-grid">
    <div class="audience-card">&#127807; Certified organic handlers &amp; brands managing product portfolios</div>
    <div class="audience-card">&#128269; Certifiers scaling compliance oversight across client rosters</div>
    <div class="audience-card">&#128101; Consultants managing multiple operations simultaneously</div>
    <div class="audience-card">&#127991; Private label operations with complex or co-packed products</div>
  </div>
</div>

<div class="card">
  <div class="about-section-title">&#9432; Understanding Certification Scope</div>
  <div class="about-lead">Organic compliance is not one-size-fits-all &mdash; it depends on scope.</div>
  <div class="about-p">
    Under the USDA National Organic Program, the <strong>scope</strong> of an organic certificate defines exactly which
    products, activities, locations, and supply chain roles are covered. A product claim that falls outside certified
    scope &mdash; even unintentionally &mdash; can constitute misrepresentation under 7 CFR &sect;&nbsp;205.307.
  </div>
  <div class="about-p">
    <strong>Certified producers</strong> (CROPS or LIVESTOCK scope) are operations that grow or raise organic
    products &mdash; farms, ranches, and growers. Their OID certificate lists the specific crops or livestock
    products they are certified to produce. Organic Web Checker compares website claims directly against
    those listed products.
  </div>
  <div class="about-p">
    <strong>Certified handlers</strong> (HANDLING scope) are any operations that process, package, store,
    distribute, or market organic products &mdash; including <strong>brand owners</strong> who sell products
    under their own label. Under USDA NOP (7&nbsp;CFR&nbsp;&sect;&nbsp;205.101) and the
    Strengthening Organic Enforcement rule (eff.&nbsp;March&nbsp;19,&nbsp;2024), all entities that make or use
    an organic claim &mdash; including brand owners who source from upstream certified suppliers rather than
    producing themselves &mdash; must hold organic handler certification. Some OID certificates list
    general product categories; others list specific products. Full product and supplier detail is documented in their
    <strong>Organic System Plan (OSP)</strong> on file with their certifying agent, not publicly visible in the OID.
  </div>
  <div class="about-p" style="font-size:.8rem;color:var(--muted)">
    When Organic Web Checker detects a HANDLING scope operation, flagged items are labeled for upstream supplier
    certification verification rather than direct certificate non-compliance. The compliance question shifts from
    &ldquo;is this on the cert?&rdquo; to &ldquo;is this documented in the OSP with verified supplier certification?&rdquo;
    Ref: 7 CFR &sect;&nbsp;205.201 &bull; SOE Final Rule (88 FR 2799, Jan.&nbsp;19,&nbsp;2023)
  </div>
</div>

<div class="card">
  <div class="about-section-title">&#9733; Scope Validator</div>
  <div class="about-lead">Phase 1 scope analysis is built into every checker run.</div>
  <div class="about-p">
    Every Organic Web Checker report automatically detects the operation&rsquo;s certified scope (CROPS, LIVESTOCK,
    HANDLING, WILD CROPS) from the USDA OID and adjusts how results are presented:
  </div>
  <ul class="feature-list">
    <li>&#10003; Scope badges shown on every report (CROPS, LIVESTOCK, HANDLING, WILD CROPS)</li>
    <li>&#10003; Handling operations get a scope notice explaining OSP and upstream cert context</li>
    <li>&#10003; Certificates listing only general commodity terms surface results as caution, not red flag</li>
    <li>&#10003; Red flag descriptions are scope-aware &mdash; producer flags vs handler flags cite different CFR sections</li>
    <li>&#10003; Image alt-text scanned for organic label claims on product pages</li>
  </ul>
  <div class="about-p" style="font-size:.78rem;color:var(--muted);margin-top:8px">
    Future Scope Validator phases will include OSP product list comparison, facility location cross-check,
    private label relationship mapping, and inspector prep summaries.
  </div>
</div>

<div class="card" style="text-align:center;padding:36px 28px">
  <div class="about-bottom-line">
    Your website is part of your Organic System Plan&thinsp;&mdash;&thinsp;whether you treat it that way or not.
  </div>
  <div class="about-bottom-sub">
    Organic Web Checker helps you identify what needs a closer look.
  </div>
  <a href="/" class="cta-btn">Run Your First Checker &rarr;</a>
</div>
"""


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route('/', methods=['GET'])
def index():
    return render_template_string(MAIN_HTML, active_job=None, prefill_op='', prefill_url='')


@app.route('/check', methods=['POST'])
def check():
    operation = request.form.get('operation', '').strip()
    website   = request.form.get('website', '').strip()
    use_cache = request.form.get('use_cache') == '1'
    if not website.startswith('http'):
        website = 'https://' + website

    job_id     = uuid.uuid4().hex[:8]
    user_email = get_logged_in_email() or ''
    with jobs_lock:
        jobs[job_id] = {
            'id': job_id, 'operation': operation, 'website': website,
            'status': 'queued', 'report': None,
            'submitted_at': datetime.now(timezone.utc).isoformat(),
            'finished_at': None,
            'unlocked': False,
            'user_email': user_email,
        }

    thread = threading.Thread(target=_run_job, args=(job_id, operation, website, use_cache), daemon=True)
    thread.start()
    return jsonify({'job_id': job_id})


@app.route('/job/<job_id>')
def job_status(job_id):
    with jobs_lock:
        job = dict(jobs.get(job_id, {}))
    if not job:
        return jsonify({'error': 'not found'}), 404
    job.pop('report', None)
    return jsonify(job)


@app.route('/job/<job_id>/result')
def job_result(job_id):
    with jobs_lock:
        job = dict(jobs.get(job_id, {}))

    # ── Fall back to DB when job not in memory (after redeploy) ─────────────
    db_unlocked   = False
    db_owner_email = ''
    if not job and DATABASE_URL:
        try:
            with db_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT report, unlocked, user_email FROM job_history WHERE job_id = %s",
                        (job_id,)
                    )
                    row = cur.fetchone()
                    if row:
                        job = {
                            'id': job_id, 'status': 'done',
                            'report': row[0],
                        }
                        db_unlocked    = bool(row[1])
                        db_owner_email = row[2] or ''
        except Exception:
            pass

    if not job:
        return 'Not found', 404
    if job['status'] not in ('done', 'error'):
        return 'Not ready', 202
    report = job.get('report', {})
    if 'error' in report:
        return render_template_string(REPORT_PARTIAL, report=report, job_id=job_id)

    # ── Gate check ──────────────────────────────────────────────────────────
    email = get_logged_in_email()

    # Ownership check (Fix 2): non-admin users can only view their own jobs
    job_owner = job.get('user_email') or db_owner_email
    if job_owner and not is_admin(email) and email != job_owner:
        return render_template_string(GATE_PARTIAL, report=report, job_id=job_id)

    already_unlocked = job.get('unlocked', False) or db_unlocked

    if already_unlocked or is_admin(email):
        return render_template_string(REPORT_PARTIAL, report=report, job_id=job_id)

    if email and get_user_credits(email) > 0:
        deduct_user_credit(email)
        with jobs_lock:
            if job_id in jobs:
                jobs[job_id]['unlocked'] = True
        _mark_job_unlocked(job_id)
        return render_template_string(REPORT_PARTIAL, report=report, job_id=job_id)

    # Check session-token credits (anonymous purchasers)
    token = session.get('token')
    if token and DATABASE_URL:
        try:
            with db_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        'SELECT credits_remaining FROM credit_accounts WHERE token = %s',
                        (token,)
                    )
                    row = cur.fetchone()
                    if row and row[0] > 0:
                        cur.execute(
                            'UPDATE credit_accounts SET credits_remaining = GREATEST(0, credits_remaining - 1) WHERE token = %s',
                            (token,)
                        )
                conn.commit()
            if row and row[0] > 0:
                with jobs_lock:
                    if job_id in jobs:
                        jobs[job_id]['unlocked'] = True
                _mark_job_unlocked(job_id)
                return render_template_string(REPORT_PARTIAL, report=report, job_id=job_id)
        except Exception:
            pass

    # No access — show teaser
    return render_template_string(GATE_PARTIAL, report=report, job_id=job_id)


@app.route('/jobs')
def jobs_list():
    with jobs_lock:
        result = [{k: v for k, v in j.items() if k != 'report'} for j in jobs.values()]
    return jsonify(sorted(result, key=lambda x: x['submitted_at'], reverse=True))


@app.route('/stats')
def get_stats():
    with jobs_lock:
        s = dict(stats)
    s['items_for_review'] = s['flags_found'] + s['caution_found']
    return jsonify(s)


# ---------------------------------------------------------------------------
# Stripe payment routes
# ---------------------------------------------------------------------------

@app.route('/create-checkout-session', methods=['POST'])
def create_checkout_session():
    try:
        data = request.get_json(force=True) or {}
        tier_index = int(data.get('tier_index', -1))
        if tier_index < 0 or tier_index >= len(TIERS):
            return jsonify({'error': 'Invalid tier'}), 400
        tier  = TIERS[tier_index]
        token = get_logged_in_email() or get_session_token()
        checkout = stripe.checkout.Session.create(
            payment_method_types=['card'],
            line_items=[{
                'price_data': {
                    'currency': 'usd',
                    'product_data': {
                        'name': f'Organic Web Checker \u2014 {tier["name"]}',
                        'description': f'{tier["count"]} checker{"s" if tier["count"] > 1 else ""} \u00b7 credits never expire',
                    },
                    'unit_amount': round(tier['price'] * 100),
                },
                'quantity': 1,
            }],
            mode='payment',
            allow_promotion_codes=True,
            client_reference_id=token,
            metadata={'tier_name': tier['name'], 'credits': str(tier['count'])},
            success_url=APP_BASE_URL + '/success?session_id={CHECKOUT_SESSION_ID}',
            cancel_url=APP_BASE_URL + '/pricing',
        )
        return jsonify({'url': checkout.url})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/stripe-webhook', methods=['POST'])
def stripe_webhook():
    payload    = request.get_data()
    sig_header = request.headers.get('Stripe-Signature', '')
    try:
        event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WH_SECRET)
    except (ValueError, stripe.error.SignatureVerificationError):
        return '', 400

    if event['type'] == 'checkout.session.completed':
        obj               = event['data']['object']
        ref               = obj.get('client_reference_id', '')
        credits           = int(obj.get('metadata', {}).get('credits', 0))
        tier_name         = obj.get('metadata', {}).get('tier_name', '')
        stripe_session_id = obj['id']
        amount_cents      = obj.get('amount_total', 0)
        # Extract promo code if one was applied
        promo_code = None
        discounts = obj.get('discounts') or []
        if discounts and discounts[0].get('promotion_code'):
            try:
                pc = stripe.PromotionCode.retrieve(discounts[0]['promotion_code'])
                promo_code = pc.get('code')
            except Exception:
                pass
        if ref and credits > 0 and DATABASE_URL:
            try:
                with db_conn() as conn:
                    with conn.cursor() as cur:
                        if '@' in ref:
                            # logged-in user purchase → add to users table
                            cur.execute("""
                                INSERT INTO users (email, password_hash, credits)
                                VALUES (%s, '', %s)
                                ON CONFLICT (email) DO UPDATE SET
                                    credits = users.credits + EXCLUDED.credits
                            """, (ref.lower(), credits))
                        else:
                            # anonymous session token purchase
                            cur.execute("""
                                INSERT INTO credit_accounts (token, credits_remaining, total_purchased)
                                VALUES (%s, %s, %s)
                                ON CONFLICT (token) DO UPDATE SET
                                    credits_remaining = credit_accounts.credits_remaining + EXCLUDED.credits_remaining,
                                    total_purchased   = credit_accounts.total_purchased   + EXCLUDED.total_purchased
                            """, (ref, credits, credits))
                        cur.execute("""
                            INSERT INTO purchases (token, stripe_session_id, tier_name, credits_purchased, amount_paid_cents, promo_code)
                            VALUES (%s, %s, %s, %s, %s, %s)
                            ON CONFLICT (stripe_session_id) DO NOTHING
                        """, (ref, stripe_session_id, tier_name, credits, amount_cents, promo_code))
                    conn.commit()
            except Exception as e:
                print(f'[WARN] Webhook DB write failed: {e}')
    return '', 200


@app.route('/api/credits')
def api_credits():
    token = session.get('token')
    if not token or not DATABASE_URL:
        return jsonify({'credits': 0, 'total_purchased': 0, 'has_account': False})
    try:
        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    'SELECT credits_remaining, total_purchased FROM credit_accounts WHERE token = %s',
                    (token,)
                )
                row = cur.fetchone()
        if row:
            return jsonify({'credits': row[0], 'total_purchased': row[1], 'has_account': True})
    except Exception:
        pass
    return jsonify({'credits': 0, 'total_purchased': 0, 'has_account': False})


@app.route('/api/init-db', methods=['POST'])
def api_init_db():
    """Admin-only: force run init_db and report result."""
    if not is_admin(get_logged_in_email()):
        return jsonify({'error': 'admin only'}), 403
    try:
        init_db()
        # Check which tables exist
        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT table_name FROM information_schema.tables
                    WHERE table_schema = 'public'
                    ORDER BY table_name
                """)
                tables = [r[0] for r in cur.fetchall()]
        return jsonify({'ok': True, 'tables': tables})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/health')
def health():
    """Public health check — tests DB connectivity without exposing secrets."""
    db_ok  = False
    db_err = None
    if DATABASE_URL:
        try:
            with db_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute('SELECT 1')
            db_ok = True
        except Exception as e:
            db_err = str(e)
    return jsonify({
        'db_url_set':  bool(DATABASE_URL),
        'db_ok':       db_ok,
        'db_error':    db_err,
        'using_public_url': bool(_db_pub_url and DATABASE_URL == _db_pub_url),
    })


@app.route('/api/config-check')
def config_check():
    """Admin-only diagnostic — shows whether env vars are loaded (not their values)."""
    if not is_admin(get_logged_in_email()):
        return jsonify({'error': 'admin only'}), 403
    return jsonify({
        'stripe_key_loaded':   bool(stripe.api_key and len(stripe.api_key) > 10),
        'stripe_key_prefix':   stripe.api_key[:7] if stripe.api_key else '(empty)',
        'stripe_wh_loaded':    bool(STRIPE_WH_SECRET),
        'stripe_wh_prefix':    STRIPE_WH_SECRET[:12] if STRIPE_WH_SECRET else '(empty)',
        'db_loaded':           bool(DATABASE_URL),
        'using_public_url':    bool(_db_pub_url and DATABASE_URL == _db_pub_url),
        'app_base_url':        APP_BASE_URL,
    })


@app.route('/api/user')
def api_user():
    email = get_logged_in_email()
    if not email:
        return jsonify({'logged_in': False})
    credits = get_user_credits(email)
    return jsonify({
        'logged_in': True,
        'email': email,
        'is_admin': is_admin(email),
        'credits': credits,
    })


@app.route('/api/user/timezone', methods=['POST'])
def api_set_timezone():
    email = get_logged_in_email()
    if not email:
        return jsonify({'ok': False, 'error': 'Not logged in'}), 401
    tz = ((request.get_json(force=True, silent=True) or {}).get('timezone') or '').strip()
    if not tz or len(tz) > 80:
        return jsonify({'ok': False, 'error': 'Invalid timezone'}), 400
    try:
        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE users SET timezone = %s WHERE email = %s", (tz, email))
            conn.commit()
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500
    return jsonify({'ok': True})


@app.route('/api/queue-depth')
def api_queue_depth():
    """Return number of pending scheduled checks in the next hour."""
    depth = 0
    if DATABASE_URL:
        try:
            with db_conn() as conn:
                with conn.cursor() as cur:
                    now = datetime.now(timezone.utc)
                    cur.execute(
                        """SELECT COUNT(*) FROM scheduled_checks
                           WHERE status IN ('scheduled','running')
                           AND scheduled_at >= %s AND scheduled_at <= %s""",
                        (now, now + timedelta(hours=24))
                    )
                    row = cur.fetchone()
                    depth = row[0] if row else 0
        except Exception:
            pass
    return jsonify({'depth': depth})


# ---------------------------------------------------------------------------
# REST API v1 — key-authenticated, JSON in/out
# ---------------------------------------------------------------------------

@app.route('/api/v1/status')
def api_v1_status():
    raw_key = _get_api_key_from_request()
    email   = verify_api_key(raw_key) if raw_key else None
    if not email:
        return jsonify({'ok': False, 'error': 'Invalid or missing API key'}), 401
    credits = get_user_credits(email)
    return jsonify({
        'ok':          True,
        'email':       email,
        'credits':     credits,
        'rate_limit':  '60 checks per rolling hour',
        'docs':        f'{APP_BASE_URL}/api',
    })


@app.route('/api/v1/check', methods=['POST'])
def api_v1_check_submit():
    raw_key = _get_api_key_from_request()
    email   = verify_api_key(raw_key) if raw_key else None
    if not email:
        return jsonify({'ok': False, 'error': 'Invalid or missing API key'}), 401

    if not is_admin(email) and get_user_credits(email) < 1:
        return jsonify({'ok': False, 'error': 'No credits remaining — purchase at /pricing'}), 402

    if not is_admin(email) and not _api_rate_limit_ok(email):
        return jsonify({'ok': False, 'error': 'Rate limit exceeded: 60 checks per hour'}), 429

    data      = request.get_json(force=True, silent=True) or {}
    operation = data.get('operation', '').strip()
    website   = data.get('website',   '').strip()
    if not operation or not website:
        return jsonify({'ok': False, 'error': 'operation and website are required'}), 400
    if not website.startswith('http'):
        website = 'https://' + website

    job_id = uuid.uuid4().hex[:16]
    now    = datetime.now(timezone.utc)
    with jobs_lock:
        jobs[job_id] = {
            'id': job_id, 'operation': operation, 'website': website,
            'status': 'queued', 'report': None,
            'submitted_at': now.isoformat(), 'finished_at': None,
            'unlocked': True, 'user_email': email, 'source': 'api',
        }

    deduct_user_credit(email)
    threading.Thread(target=_run_job, args=(job_id, operation, website), daemon=True).start()

    return jsonify({
        'ok':       True,
        'job_id':   job_id,
        'status':   'queued',
        'poll_url': f'{APP_BASE_URL}/api/v1/check/{job_id}',
    }), 202


@app.route('/api/v1/check/<job_id>')
def api_v1_check_result(job_id):
    raw_key = _get_api_key_from_request()
    email   = verify_api_key(raw_key) if raw_key else None
    if not email:
        return jsonify({'ok': False, 'error': 'Invalid or missing API key'}), 401

    with jobs_lock:
        job = dict(jobs.get(job_id, {}))

    # Fall back to DB if not in memory
    if not job and DATABASE_URL:
        try:
            with db_conn() as conn:
                with conn.cursor() as cur:
                    q = ("SELECT job_id,user_email,operation,website,status,report,submitted_at,finished_at"
                         " FROM job_history WHERE job_id=%s" + ("" if is_admin(email) else " AND user_email=%s"))
                    params = (job_id,) if is_admin(email) else (job_id, email.lower())
                    cur.execute(q, params)
                    row = cur.fetchone()
            if row:
                job = {'id': row[0], 'user_email': row[1], 'operation': row[2], 'website': row[3],
                       'status': row[4], 'report': row[5],
                       'submitted_at': str(row[6]) if row[6] else None,
                       'finished_at':  str(row[7]) if row[7] else None}
        except Exception:
            pass

    if not job:
        return jsonify({'ok': False, 'error': 'Job not found'}), 404
    if not is_admin(email) and job.get('user_email', '').lower() != email.lower():
        return jsonify({'ok': False, 'error': 'Job not found'}), 404

    out = {
        'ok':           True,
        'job_id':       job_id,
        'status':       job.get('status'),
        'operation':    job.get('operation'),
        'website':      job.get('website'),
        'submitted_at': job.get('submitted_at'),
        'finished_at':  job.get('finished_at'),
    }
    if job.get('status') == 'done':
        rpt = job.get('report') or {}
        out['report'] = {
            'flags':         len(rpt.get('flagged',   [])),
            'cautions':      len(rpt.get('caution',   [])),
            'flagged':       rpt.get('flagged',   []),
            'caution':       rpt.get('caution',   []),
            'verified':      rpt.get('verified',  []),
            'marketing':     rpt.get('marketing', []),
            'cert':          rpt.get('cert',      {}),
            'cert_products': rpt.get('cert_products', []),
            'summary':       rpt.get('summary',   ''),
        }
    elif job.get('status') == 'error':
        rpt = job.get('report') or {}
        out['error'] = rpt.get('error', 'Check failed')

    return jsonify(out)


# ---------------------------------------------------------------------------
# API documentation page
# ---------------------------------------------------------------------------

API_DOCS_HTML = """
<div class="page-title">API Documentation</div>
<div class="page-subtitle">Integrate organic compliance checks into your own systems using the OWC REST API.</div>

<div class="card">
  <div style="font-size:.78rem;font-weight:700;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);margin-bottom:12px">Authentication</div>
  <p style="font-size:.88rem;color:var(--text);margin-bottom:10px">All API requests require your API key, passed as a Bearer token:</p>
  <pre style="background:var(--bg);border:1px solid var(--border);border-radius:10px;padding:14px;font-size:.8rem;overflow-x:auto">Authorization: Bearer owc_live_YOUR_KEY</pre>
  <p style="font-size:.82rem;color:var(--muted);margin-top:10px">Generate a key in your <a href="/account" style="color:var(--primary)">Account</a> settings. Keys never expire — revoke them there if compromised.</p>
</div>

<div class="card">
  <div style="font-size:.78rem;font-weight:700;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);margin-bottom:14px">Endpoints</div>

  <div style="border:1px solid var(--border);border-radius:12px;overflow:hidden;margin-bottom:18px">
    <div style="background:var(--lavender);padding:12px 16px;border-bottom:1px solid var(--border)">
      <code style="font-size:.82rem;font-weight:700;color:var(--primary-dark)">GET /api/v1/status</code>
      <span style="font-size:.75rem;color:var(--muted);margin-left:10px">Validate key &amp; check credits</span>
    </div>
    <div style="padding:14px 16px">
      <pre style="font-size:.77rem;background:var(--bg);padding:12px;border-radius:8px;overflow-x:auto">curl -H "Authorization: Bearer owc_live_YOUR_KEY" \\
     https://www.organicwebchecker.com/api/v1/status

{
  "ok": true,
  "email": "you@example.com",
  "credits": 10,
  "rate_limit": "60 checks per rolling hour"
}</pre>
    </div>
  </div>

  <div style="border:1px solid var(--border);border-radius:12px;overflow:hidden;margin-bottom:18px">
    <div style="background:var(--lavender);padding:12px 16px;border-bottom:1px solid var(--border)">
      <code style="font-size:.82rem;font-weight:700;color:var(--primary-dark)">POST /api/v1/check</code>
      <span style="font-size:.75rem;color:var(--muted);margin-left:10px">Submit a compliance check</span>
    </div>
    <div style="padding:14px 16px">
      <p style="font-size:.82rem;color:var(--muted);margin-bottom:10px">Returns immediately with a <code>job_id</code>. Poll the result endpoint until <code>status</code> is <code>done</code>. Costs 1 credit per call.</p>
      <pre style="font-size:.77rem;background:var(--bg);padding:12px;border-radius:8px;overflow-x:auto">curl -X POST \\
     -H "Authorization: Bearer owc_live_YOUR_KEY" \\
     -H "Content-Type: application/json" \\
     -d '{"operation": "Green Hills Farm", "website": "https://example.com"}' \\
     https://www.organicwebchecker.com/api/v1/check

{
  "ok": true,
  "job_id": "a1b2c3d4e5f6...",
  "status": "queued",
  "poll_url": "https://www.organicwebchecker.com/api/v1/check/a1b2c3d4e5f6..."
}</pre>
    </div>
  </div>

  <div style="border:1px solid var(--border);border-radius:12px;overflow:hidden;margin-bottom:18px">
    <div style="background:var(--lavender);padding:12px 16px;border-bottom:1px solid var(--border)">
      <code style="font-size:.82rem;font-weight:700;color:var(--primary-dark)">GET /api/v1/check/{job_id}</code>
      <span style="font-size:.75rem;color:var(--muted);margin-left:10px">Poll for results</span>
    </div>
    <div style="padding:14px 16px">
      <p style="font-size:.82rem;color:var(--muted);margin-bottom:10px">Poll every 3&ndash;5 seconds. <code>status</code> progresses: <code>queued</code> → <code>running</code> → <code>done</code> or <code>error</code>.</p>
      <pre style="font-size:.77rem;background:var(--bg);padding:12px;border-radius:8px;overflow-x:auto">curl -H "Authorization: Bearer owc_live_YOUR_KEY" \\
     https://www.organicwebchecker.com/api/v1/check/a1b2c3d4e5f6...

{
  "ok": true,
  "job_id": "a1b2c3d4e5f6...",
  "status": "done",
  "operation": "Green Hills Farm",
  "website": "https://example.com",
  "report": {
    "flags": 2,
    "cautions": 1,
    "flagged":  [{ "title": "...", "detail": "..." }],
    "caution":  [{ "title": "...", "detail": "..." }],
    "verified": [...],
    "cert":     { "operation": "...", "certifier": "...", "status": "..." },
    "cert_products": ["Organic Apples", "Organic Pears"]
  }
}</pre>
    </div>
  </div>
</div>

<div class="card">
  <div style="font-size:.78rem;font-weight:700;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);margin-bottom:12px">Rate Limits &amp; Credits</div>
  <ul style="font-size:.86rem;color:var(--text);line-height:1.8;padding-left:18px">
    <li>60 checks per rolling hour per API key</li>
    <li>Each successful check costs 1 credit</li>
    <li>Credits are deducted at submission, not completion</li>
    <li>Failed checks (OID not found, network error) still consume a credit</li>
  </ul>
  <p style="font-size:.82rem;color:var(--muted);margin-top:12px">Need higher limits? <a href="mailto:hello@organicwebchecker.com" style="color:var(--primary)">Contact us</a> for enterprise pricing.</p>
</div>

<div class="card">
  <div style="font-size:.78rem;font-weight:700;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);margin-bottom:12px">Python Example</div>
  <pre style="font-size:.77rem;background:var(--bg);border:1px solid var(--border);border-radius:10px;padding:14px;overflow-x:auto">import requests, time

API_KEY = "owc_live_YOUR_KEY"
BASE    = "https://www.organicwebchecker.com"
HEADERS = {"Authorization": f"Bearer {API_KEY}"}

# Submit
r = requests.post(f"{BASE}/api/v1/check",
    headers=HEADERS,
    json={"operation": "Green Hills Farm", "website": "https://example.com"})
job_id = r.json()["job_id"]

# Poll
while True:
    r = requests.get(f"{BASE}/api/v1/check/{job_id}", headers=HEADERS)
    data = r.json()
    if data["status"] == "done":
        print(f'{data[\"report\"][\"flags\"]} flags, {data[\"report\"][\"cautions\"]} cautions')
        break
    elif data["status"] == "error":
        print("Error:", data.get("error"))
        break
    time.sleep(4)</pre>
</div>
"""


@app.route('/api')
def api_docs():
    return render_template_string(BASE_TEMPLATE, css=GLOBAL_CSS,
                                  page_title='API Documentation', active='', body=API_DOCS_HTML)


# ---------------------------------------------------------------------------
# Password reset helpers + routes
# ---------------------------------------------------------------------------

def _send_reset_email(to_email: str, token: str) -> bool:
    """Send password-reset email. Returns True on success. If SMTP is not
    configured, prints the link to logs and returns False so the caller can
    surface the link directly in the response."""
    import smtplib
    from email.mime.text import MIMEText
    from email.mime.multipart import MIMEMultipart

    reset_url = f"{APP_BASE_URL}/reset-password?token={token}"

    if not SMTP_USER or not SMTP_PASS:
        print(f'[RESET LINK — email not configured] {to_email}: {reset_url}')
        return False

    try:
        msg = MIMEMultipart('alternative')
        msg['Subject'] = 'Reset your Organic Web Checker password'
        msg['From']    = f'Organic Web Checker <{FROM_EMAIL}>'
        msg['To']      = to_email

        plain = (
            f"Hi,\n\nYou requested a password reset for your Organic Web Checker account.\n\n"
            f"Reset link (valid 1 hour):\n{reset_url}\n\n"
            f"If you didn't request this, ignore this email.\n\n— Organic Web Checker"
        )
        html = f"""<html><body style="font-family:Inter,sans-serif;color:#1F2937;max-width:480px;margin:0 auto;padding:32px 20px">
<img src="{APP_BASE_URL}/static/icon.png" width="60" alt="" style="margin-bottom:20px;border-radius:12px">
<h2 style="color:#6F5EF7;font-size:1.2rem;margin-bottom:8px">Password Reset</h2>
<p style="color:#6B7280;font-size:.9rem;line-height:1.6;margin-bottom:24px">
  You requested a password reset for your Organic Web Checker account.
  Click the button below — the link expires in 1 hour.
</p>
<a href="{reset_url}" style="display:inline-block;background:#6F5EF7;color:#fff;text-decoration:none;padding:13px 28px;border-radius:14px;font-weight:700;font-size:.9rem;box-shadow:0 4px 14px rgba(111,94,247,.25)">Reset Password</a>
<p style="color:#9CA3AF;font-size:.75rem;margin-top:28px">If you didn't request this, you can safely ignore this email.</p>
</body></html>"""

        msg.attach(MIMEText(plain, 'plain'))
        msg.attach(MIMEText(html,  'html'))

        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as smtp:
            smtp.ehlo()
            smtp.starttls()
            smtp.login(SMTP_USER, SMTP_PASS)
            smtp.sendmail(FROM_EMAIL, to_email, msg.as_string())
        return True
    except Exception as exc:
        print(f'[WARN] send_reset_email failed: {exc}')
        return False


@app.route('/forgot-password')
def forgot_password_page():
    body = """
    <div style="max-width:420px;margin:60px auto;padding:0 20px">
      <div class="page-title">Forgot Password</div>
      <div class="page-subtitle">Enter your email address and we&rsquo;ll send you a reset link.</div>
      <div class="card">
        <div id="fpMsg" style="display:none" class="auth-msg"></div>
        <label>Email</label>
        <input type="email" id="fpEmail" placeholder="you@example.com"
               onkeydown="if(event.key==='Enter')submitForgotPw()"
               style="width:100%;box-sizing:border-box;margin-bottom:14px">
        <button onclick="submitForgotPw()" class="btn-glass" id="fpBtn" style="width:100%">Send Reset Link</button>
        <div style="text-align:center;margin-top:16px">
          <a href="/account" style="font-size:.82rem;color:var(--primary);text-decoration:none">&larr; Back to sign in</a>
        </div>
      </div>
    </div>
    <script>
    async function submitForgotPw() {
      const email = document.getElementById('fpEmail').value.trim();
      const msg   = document.getElementById('fpMsg');
      const btn   = document.getElementById('fpBtn');
      if (!email) {
        msg.style.display=''; msg.className='auth-msg error';
        msg.textContent='Please enter your email address.'; return;
      }
      btn.disabled = true; btn.textContent = 'Sending\u2026';
      try {
        const res  = await fetch('/api/forgot-password', {
          method: 'POST', headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({email})
        });
        const data = await res.json();
        msg.style.display = '';
        if (data.ok) {
          msg.className   = 'auth-msg success';
          msg.innerHTML   = data.message;
          btn.style.display = 'none';
          document.getElementById('fpEmail').value = '';
        } else {
          msg.className = 'auth-msg error';
          msg.textContent = data.error || 'Request failed — please try again.';
          btn.disabled = false; btn.textContent = 'Send Reset Link';
        }
      } catch(e) {
        msg.style.display=''; msg.className='auth-msg error';
        msg.textContent='Network error \u2014 please try again.';
        btn.disabled = false; btn.textContent = 'Send Reset Link';
      }
    }
    </script>"""
    return render_template_string(BASE_TEMPLATE, css=GLOBAL_CSS,
                                  page_title='Forgot Password', active='', body=body)


@app.route('/api/forgot-password', methods=['POST'])
def api_forgot_password():
    import html as _html
    data  = request.get_json(force=True) or {}
    email = (data.get('email') or '').strip().lower()
    if not email or '@' not in email:
        return jsonify({'ok': False, 'error': 'Please enter a valid email address.'}), 400

    # Always respond with the same success message to prevent email enumeration
    generic_ok = {'ok': True,
                  'message': 'If that email is registered, a reset link is on its way. Check your inbox (and spam folder).'}

    if not DATABASE_URL:
        return jsonify(generic_ok)

    # Check the user exists
    try:
        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute('SELECT email FROM users WHERE email = %s', (email,))
                row = cur.fetchone()
        if not row:
            return jsonify(generic_ok)  # user not found — don't reveal this

        # Delete any existing unused tokens for this email, then create a new one
        token = secrets.token_urlsafe(32)
        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM password_resets WHERE email = %s AND used = FALSE",
                    (email,)
                )
                cur.execute("""
                    INSERT INTO password_resets (token, email, expires_at)
                    VALUES (%s, %s, NOW() + INTERVAL '1 hour')
                """, (token, email))
            conn.commit()
    except Exception as exc:
        print(f'[WARN] api_forgot_password DB error: {exc}')
        return jsonify(generic_ok)

    sent = _send_reset_email(email, token)
    if not sent:
        # SMTP not configured — surface the link directly (useful in dev / when
        # email is not yet set up, so the admin can copy-paste and send manually)
        reset_url = f"{APP_BASE_URL}/reset-password?token={token}"
        safe_url  = _html.escape(reset_url)
        return jsonify({
            'ok': True,
            'message': (
                'Email is not configured on this server. '
                f'Your reset link (copy and share manually): '
                f'<a href="{safe_url}" style="word-break:break-all;color:var(--primary)">{safe_url}</a>'
            )
        })

    return jsonify(generic_ok)


@app.route('/reset-password')
def reset_password_page():
    import html as _html
    token = request.args.get('token', '').strip()
    if not token:
        from flask import redirect
        return redirect('/forgot-password')

    # Validate token exists and isn't expired (don't reveal status in URL — let
    # the JS call handle the error message after form submission)
    safe_token = _html.escape(token)
    body = f"""
    <div style="max-width:420px;margin:60px auto;padding:0 20px">
      <div class="page-title">Reset Password</div>
      <div class="page-subtitle">Enter your new password below.</div>
      <div class="card">
        <div id="rpMsg" style="display:none" class="auth-msg"></div>
        <label>New password</label>
        <input type="password" id="rpPw" placeholder="At least 8 characters"
               style="width:100%;box-sizing:border-box;margin-bottom:10px">
        <label>Confirm password</label>
        <input type="password" id="rpPw2" placeholder="Repeat password"
               style="width:100%;box-sizing:border-box;margin-bottom:14px"
               onkeydown="if(event.key==='Enter')submitResetPw()">
        <button onclick="submitResetPw()" class="btn-glass" id="rpBtn" style="width:100%">Set New Password</button>
      </div>
    </div>
    <script>
    async function submitResetPw() {{
      const pw  = document.getElementById('rpPw').value;
      const pw2 = document.getElementById('rpPw2').value;
      const msg = document.getElementById('rpMsg');
      const btn = document.getElementById('rpBtn');
      msg.style.display = '';
      if (!pw) {{
        msg.className = 'auth-msg error'; msg.textContent = 'Please enter a new password.'; return;
      }}
      if (pw.length < 8) {{
        msg.className = 'auth-msg error'; msg.textContent = 'Password must be at least 8 characters.'; return;
      }}
      if (pw !== pw2) {{
        msg.className = 'auth-msg error'; msg.textContent = 'Passwords do not match.'; return;
      }}
      btn.disabled = true; btn.textContent = 'Saving\u2026';
      try {{
        const res  = await fetch('/api/reset-password', {{
          method: 'POST', headers: {{'Content-Type': 'application/json'}},
          body: JSON.stringify({{token: '{safe_token}', password: pw}})
        }});
        const data = await res.json();
        msg.style.display = '';
        if (data.ok) {{
          msg.className   = 'auth-msg success';
          msg.innerHTML   = 'Password updated! <a href="/account" style="color:var(--primary)">Sign in &rarr;</a>';
          btn.style.display = 'none';
        }} else {{
          msg.className   = 'auth-msg error';
          msg.textContent = data.error || 'Reset failed \u2014 the link may have expired.';
          btn.disabled = false; btn.textContent = 'Set New Password';
        }}
      }} catch(e) {{
        msg.className = 'auth-msg error'; msg.textContent = 'Network error \u2014 please try again.';
        btn.disabled = false; btn.textContent = 'Set New Password';
      }}
    }}
    </script>"""
    return render_template_string(BASE_TEMPLATE, css=GLOBAL_CSS,
                                  page_title='Reset Password', active='', body=body)


@app.route('/api/reset-password', methods=['POST'])
def api_reset_password():
    data     = request.get_json(force=True) or {}
    token    = (data.get('token')    or '').strip()
    password = (data.get('password') or '')
    if not token or not password:
        return jsonify({'ok': False, 'error': 'Missing token or password.'}), 400
    if len(password) < 8:
        return jsonify({'ok': False, 'error': 'Password must be at least 8 characters.'}), 400
    if not DATABASE_URL:
        return jsonify({'ok': False, 'error': 'Service temporarily unavailable.'}), 503

    try:
        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT email FROM password_resets
                    WHERE token = %s AND used = FALSE AND expires_at > NOW()
                """, (token,))
                row = cur.fetchone()
            if not row:
                return jsonify({'ok': False,
                                'error': 'This reset link is invalid or has expired. '
                                         'Please request a new one.'}), 400

            email     = row[0]
            new_hash  = generate_password_hash(password)
            with conn.cursor() as cur:
                cur.execute(
                    'UPDATE users SET password_hash = %s WHERE email = %s',
                    (new_hash, email)
                )
                cur.execute(
                    'UPDATE password_resets SET used = TRUE WHERE token = %s',
                    (token,)
                )
            conn.commit()

        print(f'[PASSWORD RESET] {email}')
        return jsonify({'ok': True})
    except Exception as exc:
        print(f'[WARN] api_reset_password error: {exc}')
        return jsonify({'ok': False, 'error': 'Server error — please try again.'}), 500


@app.route('/register', methods=['POST'])
def register():
    data     = request.get_json(force=True) or {}
    email    = (data.get('email') or '').strip().lower()
    password = data.get('password') or ''
    if not email or '@' not in email:
        return jsonify({'ok': False, 'error': 'Valid email required.'}), 400
    if len(password) < 8:
        return jsonify({'ok': False, 'error': 'Password must be at least 8 characters.'}), 400
    if not DATABASE_URL:
        return jsonify({'ok': False, 'error': 'Account system unavailable.'}), 503
    try:
        pw_hash = generate_password_hash(password)
        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    'INSERT INTO users (email, password_hash) VALUES (%s, %s)',
                    (email, pw_hash)
                )
            conn.commit()
        session['user_email'] = email
        credits = get_user_credits(email)
        _send_welcome_email(email)
        return jsonify({'ok': True, 'email': email, 'credits': credits})
    except Exception as e:
        err = str(e)
        if 'unique' in err.lower() or 'duplicate' in err.lower():
            return jsonify({'ok': False, 'error': 'An account with that email already exists.'}), 409
        print(f'[ERROR] register: {err}')
        return jsonify({'ok': False, 'error': f'Registration failed: {err}'}), 500

@app.route('/login', methods=['POST'])
def login():
    data     = request.get_json(force=True) or {}
    email    = (data.get('email') or '').strip().lower()
    password = data.get('password') or ''
    if not email or not password:
        return jsonify({'ok': False, 'error': 'Email and password required.'}), 400
    if not DATABASE_URL:
        return jsonify({'ok': False, 'error': 'Account system unavailable.'}), 503
    try:
        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    'SELECT password_hash FROM users WHERE email = %s',
                    (email,)
                )
                row = cur.fetchone()
        if not row or not check_password_hash(row[0], password):
            return jsonify({'ok': False, 'error': 'Invalid email or password.'}), 401
        session['user_email'] = email
        credits = get_user_credits(email)
        return jsonify({'ok': True, 'email': email, 'credits': credits})
    except Exception as e:
        return jsonify({'ok': False, 'error': 'Sign-in failed. Please try again.'}), 500

@app.route('/logout', methods=['POST'])
def logout():
    session.pop('user_email', None)
    return jsonify({'ok': True})


@app.route('/success')
def success():
    stripe_session_id = request.args.get('session_id', '')
    token = session.get('token', '')
    credits = 0
    tier_name = ''
    if stripe_session_id and stripe.api_key:
        try:
            cs = stripe.checkout.Session.retrieve(stripe_session_id)
            credits   = int(cs.metadata.get('credits', 0))
            tier_name = cs.metadata.get('tier_name', '')
        except Exception:
            pass
    body = f"""
<div style="text-align:center;padding:60px 20px">
  <img src="/static/icon.png" style="width:100px;height:100px;margin-bottom:24px">
  <div style="font-size:1.7rem;font-weight:900;color:var(--primary);margin-bottom:10px">Payment confirmed</div>
  <div style="color:var(--text);font-size:1.05rem;margin-bottom:6px">
    {f'<strong>{credits} checker{"s" if credits != 1 else ""}</strong> ({tier_name}) added to your account.' if credits else 'Your purchase was successful.'}
  </div>
  <div style="color:var(--muted);font-size:.82rem;margin-bottom:16px">
    Credits are tied to your account and never expire.
  </div>
  <div style="font-size:.76rem;color:var(--muted);max-width:420px;margin:0 auto 32px;padding:12px 16px;background:var(--lavender);border-radius:10px;line-height:1.6">
    <strong style="color:var(--text)">Refund policy:</strong> Credits are non-refundable once any check has been run against your purchase. Unused packs may be refunded within 7 days if zero credits have been used &mdash; email <a href="mailto:hello@organicwebchecker.com" style="color:var(--primary)">hello@organicwebchecker.com</a>.
  </div>
  <a href="/" class="cta-btn">Run a Checker &rarr;</a>
</div>"""
    return render_template_string(BASE_TEMPLATE, css=GLOBAL_CSS,
                                  page_title='Payment Confirmed', active='', body=body)


@app.route('/cancel')
def cancel():
    from flask import redirect
    return redirect('/pricing')


@app.route('/job/<job_id>/download/md')
def download_md(job_id):
    with jobs_lock:
        job = dict(jobs.get(job_id, {}))
    if not job or job['status'] != 'done':
        return 'Not ready', 404
    md = report_to_markdown(job['report'])
    op = job['report'].get('operation', 'report').lower().replace(' ', '-')[:40]
    filename = f"owc-{op}.md"
    return Response(md, mimetype='text/markdown',
                    headers={'Content-Disposition': f'attachment; filename="{filename}"'})


@app.route('/job/<job_id>/download/pdf')
def download_pdf(job_id):
    with jobs_lock:
        job = dict(jobs.get(job_id, {}))
    if not job or job['status'] != 'done':
        return 'Not ready', 404
    report = job['report']
    flagged  = sorted(report.get('flagged', []),  key=lambda x: x['title'])
    verified = sorted(report.get('verified', []), key=lambda x: x['title'])
    certs    = sorted(report.get('cert_products', []))
    now = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')

    def flag_rows():
        if not flagged:
            return '<tr><td colspan="2" style="color:#666;font-style:italic">No flags — all products verified</td></tr>'
        return ''.join(f'<tr><td style="color:#c0392b">&#9888; {i["title"]}</td><td><a href="{i.get("url","")}">{i.get("url","")}</a></td></tr>' for i in flagged)

    def site_rows():
        rows = []
        all_titles = {i['title'] for i in flagged}
        for item in sorted(verified + flagged, key=lambda x: x['title']):
            flag = item['title'] in all_titles
            mark = '&#9888;' if flag else '&#10003;'
            color = '#c0392b' if flag else '#27ae60'
            rows.append(f'<tr><td style="color:{color}">{mark} {item["title"]}</td><td><a href="{item.get("url","")}">{item.get("url","")}</a></td></tr>')
        return ''.join(rows)

    def cert_rows():
        return ''.join(f'<tr><td>&#9654; {c}</td></tr>' for c in certs)

    html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<title>OWC Report — {report.get('operation','')}</title>
<style>
  body {{ font-family: Georgia, serif; max-width: 900px; margin: 0 auto; padding: 30px; color: #1a1a1a; }}
  h1 {{ font-size: 18px; border-bottom: 2px solid #2d5a27; padding-bottom: 8px; color: #2d5a27; }}
  h2 {{ font-size: 13px; text-transform: uppercase; letter-spacing: .06em; color: #555; margin: 24px 0 8px; border-bottom: 1px solid #e0e0e0; padding-bottom: 4px; }}
  table {{ width: 100%; border-collapse: collapse; margin-bottom: 6px; font-size: 12px; }}
  th {{ background: #f5f5f0; text-align: left; padding: 6px 10px; font-size: 11px; text-transform: uppercase; letter-spacing:.04em; color:#666; }}
  td {{ padding: 6px 10px; border-bottom: 1px solid #f0f0ee; vertical-align: top; }}
  td a {{ color: #2d5a27; font-size: 10px; word-break: break-all; }}
  .stats {{ display: flex; gap: 20px; margin: 14px 0; }}
  .stat {{ background: #f9f9f7; border-radius: 6px; padding: 12px 18px; text-align: center; min-width: 100px; }}
  .stat .n {{ font-size: 26px; font-weight: bold; }}
  .stat .l {{ font-size: 10px; color: #888; text-transform: uppercase; }}
  .flagged .n {{ color: #c0392b; }}
  .verified .n {{ color: #2d5a27; }}
  .meta {{ font-size: 12px; color: #444; line-height: 1.7; margin-bottom: 14px; }}
  .ts {{ font-size: 10px; color: #aaa; margin-top: 8px; }}
  @media print {{ body {{ padding: 15px; }} button {{ display: none; }} }}
</style>
</head>
<body>
<h1>Organic Web Checker — Compliance Report</h1>
<div class="meta">
  <strong>Operation:</strong> {report.get('operation','')}<br>
  <strong>Certifier:</strong> {report.get('certifier','')}<br>
  <strong>Status:</strong> {report.get('status','')}<br>
  <strong>Location:</strong> {report.get('location','')}<br>
  <strong>Website:</strong> {report.get('website_url','')}
</div>
<div class="stats">
  <div class="stat"><div class="n">{report.get('cert_product_count',0)}</div><div class="l">OID Certified</div></div>
  <div class="stat"><div class="n">{report.get('website_organic_count',0)}</div><div class="l">Website Organic</div></div>
  <div class="stat verified"><div class="n">{len(verified)}</div><div class="l">Verified</div></div>
  <div class="stat flagged"><div class="n">{len(flagged)}</div><div class="l">Flagged</div></div>
</div>
<button onclick="window.print()" style="padding:8px 20px;background:#2d5a27;color:white;border:none;border-radius:6px;cursor:pointer;margin-bottom:20px;font-size:13px">Print / Save as PDF</button>
<h2>&#9888; Non-Compliance Flags ({len(flagged)} items)</h2>
<table><tr><th>Product (on website, not on cert)</th><th>URL</th></tr>{flag_rows()}</table>
<h2>Website Organic Products ({report.get('website_organic_count',0)} items)</h2>
<table><tr><th>Status</th><th>Product / URL</th></tr>{site_rows()}</table>
<h2>OID Certificate Products ({report.get('cert_product_count',0)} items)</h2>
<table><tr><th>Certified Product</th></tr>{cert_rows()}</table>
<div class="ts">Generated {now} by Organic Web Checker &mdash; www.organicwebchecker.com</div>
<script>window.addEventListener('load',()=>setTimeout(()=>window.print(),600));</script>
</body>
</html>"""
    return Response(html, mimetype='text/html')


@app.route('/job/<job_id>/email-report', methods=['POST'])
def email_report(job_id):
    """Email a completed instant-check report. Sends to logged-in user or optional to_email."""
    email = get_logged_in_email()
    if not email:
        return jsonify({'ok': False, 'error': 'Sign in to email reports'}), 401
    body_data  = request.get_json(force=True, silent=True) or {}
    to_email   = (body_data.get('to_email') or '').strip() or email
    with jobs_lock:
        job = dict(jobs.get(job_id, {}))
    report = job.get('report') if job.get('status') == 'done' else None
    if report is None or 'error' in report:
        # job not in memory (app restarted) — try DB
        try:
            with db_conn() as conn:
                with conn.cursor() as cur:
                    if is_admin(email):
                        cur.execute(
                            "SELECT report FROM job_history WHERE job_id = %s",
                            (job_id,)
                        )
                    else:
                        cur.execute(
                            "SELECT report FROM job_history WHERE job_id = %s AND user_email = %s",
                            (job_id, email)
                        )
                    row = cur.fetchone()
                    if row and row[0]:
                        report = row[0]
        except Exception:
            pass
    if not report or 'error' in report:
        return jsonify({'ok': False, 'error': 'Report not ready'}), 400

    # ── Rate limit: 1 send per job per hour ──────────────────────────────────
    if DATABASE_URL:
        try:
            with db_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT last_emailed_at FROM job_history WHERE job_id = %s",
                        (job_id,)
                    )
                    row = cur.fetchone()
                    if row and row[0]:
                        age = datetime.now(timezone.utc) - row[0]
                        if age.total_seconds() < 3600:
                            mins_left = int((3600 - age.total_seconds()) / 60) + 1
                            return jsonify({'ok': False, 'error': f'Already sent — try again in {mins_left} min'}), 429
        except Exception:
            pass

    operation  = report.get('operation', 'Unknown Operation')
    report_url = f'{APP_BASE_URL}/job/{job_id}'
    subject, html_body = _build_report_html(operation, report, report_url)
    result = _resend_send(to_email, subject, html_body)
    if result is not True:
        detail = result if isinstance(result, str) else 'Send failed'
        return jsonify({'ok': False, 'error': detail}), 500

    # Stamp the send time
    if DATABASE_URL:
        try:
            with db_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE job_history SET last_emailed_at = NOW() WHERE job_id = %s",
                        (job_id,)
                    )
                conn.commit()
        except Exception:
            pass

    return jsonify({'ok': True})


@app.route('/pricing')
def pricing():
    body = pricing_page_html()
    return render_template_string(BASE_TEMPLATE, css=GLOBAL_CSS,
                                  page_title='Pricing', active='pricing', body=body)


@app.route('/account')
def account():
    return render_template_string(BASE_TEMPLATE, css=GLOBAL_CSS,
                                  page_title='Account', active='account', body=ACCOUNT_BODY)


@app.route('/history')
def history():
    email = get_logged_in_email()
    db_jobs = []
    if email and DATABASE_URL:
        try:
            with db_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        SELECT job_id, operation, website, status, flags, caution,
                               submitted_at, finished_at
                        FROM job_history
                        WHERE user_email = %s
                        ORDER BY finished_at DESC NULLS LAST
                        LIMIT 100
                    """, (email.lower(),))
                    for row in cur.fetchall():
                        db_jobs.append({
                            'id': row[0], 'operation': row[1], 'website': row[2],
                            'status': row[3], 'flags': row[4], 'caution': row[5],
                            'submitted_at': row[6].isoformat() if row[6] else '',
                            'finished_at':  row[7].isoformat() if row[7] else '',
                            'from_db': True,
                        })
        except Exception as _he:
            print(f'[HISTORY] query failed: {_he}')

    db_ids = {j['id'] for j in db_jobs}
    with jobs_lock:
        session_jobs = [
            {k: v for k, v in j.items() if k != 'report'}
            for j in jobs.values()
            if j['status'] in ('done', 'error') and j['id'] not in db_ids
            and (not email or j.get('user_email', '') == email or is_admin(email))
        ]

    all_jobs = db_jobs + session_jobs
    all_jobs.sort(key=lambda x: x.get('finished_at') or x.get('submitted_at') or '', reverse=True)

    def _row(j):
        raw_ts = j.get('finished_at') or j.get('submitted_at') or ''
        ts = (raw_ts[:19].replace('T', ' ') + ' UTC') if raw_ts else '—'
        status_color = '#7BCF8A' if j['status'] == 'done' else 'var(--red)'
        f, c = j.get('flags', 0) or 0, j.get('caution', 0) or 0
        flags_txt  = f'<span style="font-size:.75rem;color:var(--red);margin-left:8px">{f} flag{"s" if f!=1 else ""}</span>' if f else ''
        flags_txt += f'<span style="font-size:.75rem;color:#D97706;margin-left:6px">{c} caution</span>' if c else ''
        pdf_link = f'<a class="view-btn" href="/job/{j["id"]}/download/pdf" target="_blank">PDF</a>' if not j.get('from_db') else ''
        md_link  = f'<a class="view-btn" href="/job/{j["id"]}/download/md" style="color:var(--primary);border-color:rgba(111,94,247,.25)">&#8595; .md</a>' if not j.get('from_db') else ''
        return f"""<div class="history-item">
          <div class="h-main">
            <div class="h-op">{j['operation']}{flags_txt}</div>
            <div class="h-site">{j['website']}</div>
            <div class="h-ts">{ts} &middot; <span style="color:{status_color}">{j['status']}</span></div>
          </div>
          <div class="h-actions">{pdf_link}{md_link}</div>
        </div>"""

    if not all_jobs:
        empty_msg = 'No checks yet. <a href="/" style="color:var(--primary);text-decoration:none">Run your first checker &rarr;</a>' if email else \
                    'Sign in to see your saved history. <a href="/" style="color:var(--primary);text-decoration:none">Run a check &rarr;</a>'
        body = f'<div class="page-title">Check History</div><div class="card"><div class="history-empty">{empty_msg}</div></div>'
    else:
        subtitle = 'Your check history — saved to your account.' if email else \
                   'Session history only. <a href="/account" style="color:var(--primary);text-decoration:none">Sign in</a> to save permanently.'
        body = f'<div class="page-title">Check History</div><div class="page-subtitle">{subtitle}</div>' + ''.join(_row(j) for j in all_jobs)

    return render_template_string(BASE_TEMPLATE, css=GLOBAL_CSS,
                                  page_title='History', active='history', body=body)


@app.route('/settings')
def settings():
    return render_template_string(BASE_TEMPLATE, css=GLOBAL_CSS,
                                  page_title='Settings', active='settings', body=SETTINGS_BODY)


# ---------------------------------------------------------------------------
# Agents & API page
# ---------------------------------------------------------------------------

AGENTS_BODY = """
<div class="page-title">Agents &amp; API</div>
<div class="page-subtitle">Organic Web Checker is built to be used by AI agents and automated compliance workflows &mdash; not just humans.</div>

<div class="card">
  <div class="about-section-title">&#129302; AI Agent-Friendly</div>
  <div class="about-p">
    Every check you run through the web interface is also available programmatically. AI agents can submit checks, poll for results, and receive structured JSON reports &mdash; enabling automated compliance monitoring at scale.
  </div>
  <ul class="feature-list">
    <li>&#10003; Structured JSON output &mdash; every report is machine-readable</li>
    <li>&#10003; Async job queue &mdash; submit and poll, no blocking</li>
    <li>&#10003; Markdown and PDF export endpoints</li>
    <li>&#8680; API key authentication &mdash; coming soon</li>
    <li>&#8680; MCP (Model Context Protocol) server &mdash; coming soon</li>
  </ul>
</div>

<div class="card">
  <div class="about-section-title">&#9656; Current API Endpoints</div>
  <div class="about-p" style="margin-bottom:14px">These endpoints are used by the web interface and are accessible to agents during development. API key auth will be required once gating is live.</div>

  <div class="api-endpoint">
    <div class="api-method post">POST</div>
    <div class="api-path">/check</div>
    <div class="api-desc">Submit a new web check. Body: <code>operation</code> (OID name), <code>website</code> (URL). Returns: <code>&#123;"job_id": "..."&#125;</code></div>
  </div>

  <div class="api-endpoint">
    <div class="api-method get">GET</div>
    <div class="api-path">/job/&lt;job_id&gt;</div>
    <div class="api-desc">Poll job status. Returns full JSON report when <code>status</code> is <code>done</code>. Fields: <code>flagged</code>, <code>caution</code>, <code>verified</code>, <code>marketing</code>, <code>cert_product_count</code>, <code>website_organic_count</code>.</div>
  </div>

  <div class="api-endpoint">
    <div class="api-method get">GET</div>
    <div class="api-path">/jobs</div>
    <div class="api-desc">List all jobs in current queue. Returns array of job objects (without report data).</div>
  </div>

  <div class="api-endpoint">
    <div class="api-method get">GET</div>
    <div class="api-path">/job/&lt;job_id&gt;/download/md</div>
    <div class="api-desc">Download the compliance report as a Markdown file.</div>
  </div>

  <div class="api-endpoint">
    <div class="api-method get">GET</div>
    <div class="api-path">/api/credits</div>
    <div class="api-desc">Returns current session credit balance: <code>&#123;"credits": N, "has_account": bool&#125;</code></div>
  </div>
</div>

<div class="card">
  <div class="about-section-title">&#128274; Security</div>
  <div class="about-p">Current and planned protections:</div>
  <ul class="feature-list">
    <li>&#10003; Stripe webhook signature verification (HMAC-SHA256)</li>
    <li>&#10003; Input sanitization &mdash; operation name and URL validated before processing</li>
    <li>&#8680; API key authentication per request &mdash; coming with account system</li>
    <li>&#8680; Rate limiting per key &mdash; prevents runaway agent usage</li>
    <li>&#8680; Agent registration &mdash; named agents tied to an account for audit logging</li>
  </ul>
  <div class="about-p" style="margin-top:12px;font-size:.8rem;">
    There is no formal agent identity standard yet. We follow current best practice: API keys + HMAC-signed webhooks. We will adopt emerging standards (e.g., OpenID for agents) as they mature.
  </div>
</div>

<div class="card">
  <div class="about-section-title">&#9670; Coming Soon</div>
  <ul class="feature-list">
    <li>&#8680; <strong>MCP server</strong> &mdash; run checkers natively inside Claude, Cursor, and other MCP-compatible agents</li>
    <li>&#8680; <strong>API keys</strong> &mdash; generate and manage keys from your account dashboard</li>
    <li>&#8680; <strong>Webhooks</strong> &mdash; push results to your system when a check completes</li>
    <li>&#8680; <strong>Batch endpoint</strong> &mdash; submit multiple operations in one call</li>
    <li>&#8680; <strong>OpenAPI spec</strong> &mdash; machine-readable API documentation</li>
  </ul>
  <div style="margin-top:18px">
    <a href="mailto:hello@organicwebchecker.com" class="pricing-cta contact" style="display:inline-block;padding:10px 20px;font-size:.84rem">
      Request early API access
    </a>
  </div>
</div>
"""

AGENTS_CSS = """
  .api-endpoint {
    display: grid; grid-template-columns: 60px 220px 1fr;
    gap: 10px; align-items: start;
    padding: 12px 0; border-bottom: 1px solid rgba(111,94,247,.08);
    font-size: .82rem;
  }
  .api-endpoint:last-child { border-bottom: none; }
  .api-method {
    font-size: .65rem; font-weight: 800; text-transform: uppercase;
    letter-spacing: .08em; padding: 3px 7px; border-radius: 6px;
    text-align: center; width: fit-content;
  }
  .api-method.post { background: rgba(217,119,6,.10); color: var(--amber); border: 1px solid rgba(217,119,6,.2); }
  .api-method.get  { background: rgba(111,94,247,.08);  color: var(--primary);  border: 1px solid rgba(111,94,247,.2); }
  .api-path { font-family: monospace; font-size: .82rem; color: var(--primary); padding-top: 2px; }
  .api-desc { color: var(--muted); line-height: 1.5; }
  .api-desc code { background: rgba(111,94,247,.08); color: var(--primary); padding: 1px 5px; border-radius: 4px; font-size: .78rem; }
"""

@app.route('/agents')
def agents():
    combined_css = GLOBAL_CSS + AGENTS_CSS
    return render_template_string(BASE_TEMPLATE, css=combined_css,
                                  page_title='Agents & API', active='agents', body=AGENTS_BODY)


@app.route('/llms.txt')
def llms_txt():
    content = """# Organic Web Checker
# https://www.organicwebchecker.com
# Contact: hello@organicwebchecker.com

## What this tool does
Organic Web Checker compares a business's website organic product listings against their
live USDA Organic Integrity Database (OID) certificate. It identifies product titles that
appear as organic on the website but are not found on the current OID certificate scope.

Results are for review purposes only. This tool is informational — not a legal audit.

## Who it is for
- Organic certifiers auditing client operations
- Certified organic handlers and brands reviewing their own claims
- Compliance consultants managing multiple operations

## How to use the API (no auth required during development)

### Submit a check
POST /check
Content-Type: application/x-www-form-urlencoded
Body: operation=OPERATION+NAME+AS+IN+OID&website=https://example.com
Response: {"job_id": "abc12345"}

### Poll for results
GET /job/<job_id>
Response when running: {"status": "queued"|"running", ...}
Response when done:    {"status": "done", "report": {...}}

### Report JSON structure
{
  "operation": "string — operation name from OID",
  "certifier": "string — certifying agent name",
  "status": "string — certification status",
  "location": "string — city, state",
  "website_url": "string",
  "cert_product_count": integer,
  "website_organic_count": integer,
  "flagged":   [{"title": "...", "url": "..."}],
  "caution":   [{"title": "...", "url": "..."}],
  "verified":  [{"title": "...", "url": "..."}],
  "marketing": [{"title": "...", "url": "..."}]
}

### List all jobs
GET /jobs

### Download report as Markdown
GET /job/<job_id>/download/md

## Important limitations
- Matches only product titles containing the word "organic"
- Uses fuzzy/substring matching — results require human review
- One check runs at a time (queue-based, ~60 seconds per check)
- Job queue is in-memory — clears on server restart
- Credits required for production use (see /pricing)

## Coming soon
- API key authentication
- MCP (Model Context Protocol) server for native agent tool use
- Batch endpoint for multiple operations
- Webhooks for push results
- OpenAPI spec at /openapi.json
"""
    return Response(content, mimetype='text/plain')


@app.route('/about')
def about():
    return render_template_string(BASE_TEMPLATE, css=GLOBAL_CSS,
                                  page_title='About', active='about', body=ABOUT_BODY)


# ---------------------------------------------------------------------------
# Certifier verification
# ---------------------------------------------------------------------------

@app.route('/api/certifier-request', methods=['POST'])
def certifier_request():
    data         = request.get_json(force=True) or {}
    first_name   = (data.get('first_name')   or '').strip()[:80]
    last_name    = (data.get('last_name')    or '').strip()[:80]
    organization = (data.get('organization') or '').strip()[:200]
    nop_number   = (data.get('nop_number')   or '').strip()[:100]
    email        = (data.get('email')        or '').strip()[:200]
    if not all([first_name, last_name, organization, nop_number, email]):
        return jsonify({'ok': False, 'error': 'All fields are required.'}), 400
    if '@' not in email:
        return jsonify({'ok': False, 'error': 'Invalid email address.'}), 400
    if not DATABASE_URL:
        return jsonify({'ok': False, 'error': 'Service temporarily unavailable.'}), 503
    try:
        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO certifier_requests
                        (first_name, last_name, organization, nop_number, email, status)
                    VALUES (%s, %s, %s, %s, %s, 'verifying')
                    RETURNING id
                """, (first_name, last_name, organization, nop_number, email))
                db_id = cur.fetchone()[0]
            conn.commit()
    except Exception as e:
        print(f'[WARN] certifier_request DB write failed: {e}')
        return jsonify({'ok': False, 'error': 'Could not save request. Please try again.'}), 500

    # Start background USDA verification
    request_id = uuid.uuid4().hex[:12]
    with _cert_verify_lock:
        _cert_verify_jobs[request_id] = {'status': 'verifying'}
    threading.Thread(
        target=_run_cert_verify,
        args=(request_id, organization, db_id),
        daemon=True
    ).start()

    print(f'[CERTIFIER REQUEST] {first_name} {last_name} | {organization} | NOP: {nop_number} | {email}')
    return jsonify({'ok': True, 'request_id': request_id})


@app.route('/api/certifier-verify-status/<request_id>')
def certifier_verify_status(request_id):
    with _cert_verify_lock:
        job = _cert_verify_jobs.get(request_id)
    if not job:
        return jsonify({'status': 'unknown'}), 404
    return jsonify({'status': job['status']})


@app.route('/admin/certifier-requests')
def admin_certifier_requests():
    if get_logged_in_email() != ADMIN_EMAIL:
        return 'Unauthorized', 403
    rows = []
    try:
        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT id, first_name, last_name, organization, nop_number, email, status, created_at
                    FROM certifier_requests ORDER BY created_at DESC
                """)
                rows = cur.fetchall()
    except Exception as e:
        rows = []
    table_rows = ""
    for r in rows:
        rid, first, last, org, nop, email, status, created_at = r
        ts = created_at.strftime('%Y-%m-%d %H:%M UTC') if created_at else ''
        status_color = '#2D8049' if status == 'approved' else ('#B91C1C' if status == 'denied' else '#92400E')
        table_rows += f"""<tr style="border-bottom:1px solid var(--border)">
          <td style="padding:10px 12px;font-size:.82rem;color:var(--muted)">{ts}</td>
          <td style="padding:10px 12px;font-size:.84rem;font-weight:600">{first} {last}</td>
          <td style="padding:10px 12px;font-size:.84rem">{org}</td>
          <td style="padding:10px 12px;font-size:.82rem;font-family:monospace">{nop}</td>
          <td style="padding:10px 12px;font-size:.82rem"><a href="mailto:{email}" style="color:var(--primary)">{email}</a></td>
          <td style="padding:10px 12px"><span style="font-size:.76rem;font-weight:700;color:{status_color}">{status.upper()}</span></td>
          <td style="padding:10px 12px;font-size:.8rem">
            <button onclick="updateCertStatus({rid},'approved')" style="background:#2E7D32;color:#fff;border:none;border-radius:6px;padding:4px 10px;cursor:pointer;font-size:.75rem;margin-right:4px">Approve</button>
            <button onclick="updateCertStatus({rid},'denied')" style="background:var(--red);color:#fff;border:none;border-radius:6px;padding:4px 10px;cursor:pointer;font-size:.75rem">Deny</button>
          </td>
        </tr>"""
    body = f"""
    <div class="page-title">Certifier Requests</div>
    <div class="page-subtitle">Review NOP accreditation numbers before sending the promo code manually.</div>
    <div class="card" style="padding:0;overflow:hidden">
      <table style="width:100%;border-collapse:collapse">
        <thead><tr style="background:var(--lavender);text-align:left">
          <th style="padding:10px 12px;font-size:.75rem;font-weight:700;text-transform:uppercase;letter-spacing:.05em;color:var(--muted)">Submitted</th>
          <th style="padding:10px 12px;font-size:.75rem;font-weight:700;text-transform:uppercase;letter-spacing:.05em;color:var(--muted)">Name</th>
          <th style="padding:10px 12px;font-size:.75rem;font-weight:700;text-transform:uppercase;letter-spacing:.05em;color:var(--muted)">Organization</th>
          <th style="padding:10px 12px;font-size:.75rem;font-weight:700;text-transform:uppercase;letter-spacing:.05em;color:var(--muted)">NOP #</th>
          <th style="padding:10px 12px;font-size:.75rem;font-weight:700;text-transform:uppercase;letter-spacing:.05em;color:var(--muted)">Email</th>
          <th style="padding:10px 12px;font-size:.75rem;font-weight:700;text-transform:uppercase;letter-spacing:.05em;color:var(--muted)">Status</th>
          <th style="padding:10px 12px;font-size:.75rem;font-weight:700;text-transform:uppercase;letter-spacing:.05em;color:var(--muted)">Actions</th>
        </tr></thead>
        <tbody>{''.join([table_rows]) if table_rows else '<tr><td colspan="7" style="padding:24px;text-align:center;color:var(--muted);font-size:.84rem">No requests yet.</td></tr>'}</tbody>
      </table>
    </div>
    <script>
    async function updateCertStatus(id, status) {{
      const res = await fetch('/admin/certifier-requests/' + id + '/status', {{
        method: 'POST', headers: {{'Content-Type': 'application/json'}},
        body: JSON.stringify({{status}})
      }});
      if ((await res.json()).ok) location.reload();
    }}
    </script>"""
    return render_template_string(BASE_TEMPLATE, css=GLOBAL_CSS,
                                  page_title='Certifier Requests', active='', body=body)


@app.route('/admin/certifier-requests/<int:req_id>/status', methods=['POST'])
def admin_certifier_status(req_id):
    if get_logged_in_email() != ADMIN_EMAIL:
        return jsonify({'ok': False}), 403
    status = (request.get_json(force=True) or {}).get('status', '')
    if status not in ('pending', 'approved', 'denied'):
        return jsonify({'ok': False, 'error': 'Invalid status'}), 400
    try:
        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE certifier_requests SET status = %s WHERE id = %s",
                    (status, req_id)
                )
            conn.commit()
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/admin/test-email')
def admin_test_email():
    if get_logged_in_email() != ADMIN_EMAIL:
        return 'Unauthorized', 403
    r = _resend_send(ADMIN_EMAIL, 'OWC Email Test', '<p>Resend is working ✓</p>')
    status = 'OK ✓' if r is True else f'FAIL: {r}'
    lines = [f'<h2>Resend Test</h2><pre>→ {ADMIN_EMAIL}  {status}</pre>'
             f'<p>Key configured: {"Yes" if RESEND_API_KEY else "No"}</p>'
             f'<p>Check healersfind@gmail.com for test message.</p>']
    return '\n'.join(lines)


@app.route('/admin/scheduler-status')
def admin_scheduler_status():
    if get_logged_in_email() != ADMIN_EMAIL:
        return 'Unauthorized', 403
    rows = []
    error = None
    if DATABASE_URL:
        try:
            with db_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        SELECT id, user_email, status, scheduled_at, operation_name, website_url, job_id
                        FROM scheduled_checks
                        ORDER BY scheduled_at DESC LIMIT 30
                    """)
                    rows = cur.fetchall()
        except Exception as e:
            error = str(e)
    thread_alive = _scheduler_thread.is_alive()
    html = ['<html><body style="font-family:monospace;padding:20px">',
            f'<h2>Scheduler Diagnostics</h2>',
            f'<p><b>Thread alive:</b> {thread_alive}</p>',
            f'<p><b>DATABASE_URL set:</b> {"Yes" if DATABASE_URL else "No"}</p>']
    if error:
        html.append(f'<p style="color:red"><b>DB error:</b> {error}</p>')
    html.append(f'<p><b>scheduled_checks rows (last 30):</b></p>')
    html.append('<table border="1" cellpadding="4" style="border-collapse:collapse">')
    html.append('<tr><th>id</th><th>user_email</th><th>status</th><th>scheduled_at</th>'
                '<th>operation</th><th>website</th><th>job_id</th></tr>')
    for r in rows:
        html.append('<tr>' + ''.join(f'<td>{v}</td>' for v in r) + '</tr>')
    html.append('</table>')
    html.append('</body></html>')
    return '\n'.join(html)


# ---------------------------------------------------------------------------
# API key endpoints
# ---------------------------------------------------------------------------

@app.route('/api/keys/list')
def api_keys_list():
    email = get_logged_in_email()
    if not email:
        return jsonify({'error': 'Not signed in'}), 401
    return jsonify({'keys': list_api_keys(email)})


@app.route('/api/keys/create', methods=['POST'])
def api_keys_create():
    email = get_logged_in_email()
    if not email:
        return jsonify({'error': 'Not signed in'}), 401
    name = (request.json or {}).get('name', 'My API Key')[:80]
    result = generate_api_key(email, name)
    return jsonify({'ok': True, 'key_id': result['key_id'], 'raw_key': result['raw_key']})


@app.route('/api/keys/<key_id>/revoke', methods=['POST'])
def api_keys_revoke(key_id):
    email = get_logged_in_email()
    if not email:
        return jsonify({'error': 'Not signed in'}), 401
    revoke_api_key(email, key_id)
    return jsonify({'ok': True})


# ---------------------------------------------------------------------------
# MCP server (JSON-RPC 2.0 over HTTP)
# AI agents call POST /mcp with standard JSON-RPC requests.
# API key required via X-API-Key header or api_key body field.
# ---------------------------------------------------------------------------

_MCP_TOOLS = [
    {
        "name": "check_organic_compliance",
        "description": (
            "Check an organic operation's website for claims that may fall outside "
            "their current USDA OID certificate scope. Returns a structured compliance "
            "report with verified, flagged, caution, and marketing-language items. "
            "Ref: 7 CFR Part 205 — USDA National Organic Program."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "operation_name": {
                    "type": "string",
                    "description": "Operation name exactly as listed in the USDA Organic Integrity Database (OID)"
                },
                "website_url": {
                    "type": "string",
                    "description": "Full URL of the operation's website (e.g. https://example.com)"
                },
                "use_cache": {
                    "type": "boolean",
                    "description": "Use cached OID data if available (faster, ~5s vs ~60s). Default false.",
                    "default": False
                }
            },
            "required": ["operation_name", "website_url"]
        }
    },
    {
        "name": "get_oid_certificate",
        "description": (
            "Fetch an operation's current USDA OID certificate data including "
            "certified products, certifier, status, and location."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "operation_name": {
                    "type": "string",
                    "description": "Operation name as listed in the USDA Organic Integrity Database"
                }
            },
            "required": ["operation_name"]
        }
    }
]


@app.route('/mcp', methods=['POST'])
def mcp_server():
    # ── Auth ──────────────────────────────────────────────────────────────────
    raw_key = (request.headers.get('X-API-Key') or
               (request.json or {}).get('api_key', ''))
    email = verify_api_key(raw_key)
    if not email and not is_admin(get_logged_in_email()):
        return jsonify({
            "jsonrpc": "2.0",
            "error": {"code": -32001, "message": "Unauthorized — provide a valid API key via X-API-Key header"},
            "id": None
        }), 401

    data   = request.json or {}
    method = data.get('method', '')
    req_id = data.get('id')
    params = data.get('params', {})

    # ── initialize ────────────────────────────────────────────────────────────
    if method == 'initialize':
        return jsonify({
            "jsonrpc": "2.0",
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "organic-web-checker", "version": "1.0.0"}
            },
            "id": req_id
        })

    # ── tools/list ────────────────────────────────────────────────────────────
    elif method == 'tools/list':
        return jsonify({"jsonrpc": "2.0", "result": {"tools": _MCP_TOOLS}, "id": req_id})

    # ── tools/call ────────────────────────────────────────────────────────────
    elif method == 'tools/call':
        tool_name = params.get('name', '')
        args      = params.get('arguments', {})

        try:
            if tool_name == 'check_organic_compliance':
                op      = args.get('operation_name', '').strip()
                website = args.get('website_url', '').strip()
                use_c   = bool(args.get('use_cache', False))
                if not op or not website:
                    raise ValueError("operation_name and website_url are required")
                if not website.startswith('http'):
                    website = 'https://' + website

                # ── Credit gate ───────────────────────────────────────────────
                # Same cost as a web check: 1 credit deducted on success.
                # Admin email = unlimited; no credits = error before running.
                if not is_admin(email):
                    if get_user_credits(email) < 1:
                        return jsonify({
                            "jsonrpc": "2.0",
                            "error": {
                                "code": -32002,
                                "message": "Insufficient credits. Purchase more at "
                                           "https://www.organicwebchecker.com/pricing"
                            },
                            "id": req_id
                        }), 402

                # Run check — acquires semaphore, blocks until complete
                cert = None
                if use_c:
                    cached = get_cached_oid(op)
                    if cached:
                        cert = cached['cert']
                with _check_semaphore:
                    if cert is None:
                        raw_cert = get_oid_cert(op)
                        if 'error' not in raw_cert:
                            save_oid_cache(op, raw_cert)
                        cert = raw_cert
                    report = run_check(op, website, cert=cert)

                # Deduct credit after successful run
                if not is_admin(email) and 'error' not in report:
                    deduct_user_credit(email)

                text_out = json.dumps(report, indent=2, default=str)
                return jsonify({
                    "jsonrpc": "2.0",
                    "result": {"content": [{"type": "text", "text": text_out}]},
                    "id": req_id
                })

            elif tool_name == 'get_oid_certificate':
                op = args.get('operation_name', '').strip()
                if not op:
                    raise ValueError("operation_name is required")
                cached = get_cached_oid(op)
                if cached:
                    cert = cached['cert']
                    cert['_source'] = f"cached ({cached['cached_at']})"
                else:
                    with _check_semaphore:
                        cert = get_oid_cert(op)
                    if 'error' not in cert:
                        save_oid_cache(op, cert)
                        cert['_source'] = 'live'
                return jsonify({
                    "jsonrpc": "2.0",
                    "result": {"content": [{"type": "text", "text": json.dumps(cert, indent=2)}]},
                    "id": req_id
                })

            else:
                return jsonify({
                    "jsonrpc": "2.0",
                    "error": {"code": -32601, "message": f"Unknown tool: {tool_name}"},
                    "id": req_id
                }), 400

        except Exception as e:
            return jsonify({
                "jsonrpc": "2.0",
                "result": {
                    "content": [{"type": "text", "text": f"Error: {str(e)}"}],
                    "isError": True
                },
                "id": req_id
            })

    else:
        return jsonify({
            "jsonrpc": "2.0",
            "error": {"code": -32601, "message": f"Method not found: {method}"},
            "id": req_id
        }), 400


# ---------------------------------------------------------------------------
# Scheduled Checker page + API routes
# ---------------------------------------------------------------------------

def schedule_page_html(user_email: str) -> str:
    credits = get_user_credits(user_email) if user_email else 0
    admin   = is_admin(user_email)

    # Read saved timezone
    saved_tz = 'UTC'
    if user_email and DATABASE_URL:
        try:
            with db_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT timezone FROM users WHERE email = %s", (user_email,))
                    row = cur.fetchone()
                    if row and row[0]:
                        saved_tz = row[0]
        except Exception:
            pass

    # Pre-compute next available slot server-side so the page shows it instantly
    next_slot = get_next_available_slot()
    next_avail_iso_js = json.dumps(
        next_slot.strftime('%Y-%m-%dT%H:%M:00Z') if next_slot else None
    )

    if not user_email:
        return """
        <div class="page-title">Schedule Checker</div>
        <div class="page-subtitle">Book a specific time slot and receive your report by email when it&rsquo;s ready.</div>
        <div class="card" style="text-align:center;padding:40px">
          <div style="font-size:2.5rem;margin-bottom:16px">&#128197;</div>
          <div style="font-size:1.05rem;font-weight:800;color:var(--primary);margin-bottom:8px">Sign in to schedule a check</div>
          <div style="font-size:.85rem;color:var(--muted);margin-bottom:22px">A free account is required to book time slots and receive email reports.</div>
          <button class="btn-primary" onclick="openAuthModal('signin')">Sign In &nbsp;/&nbsp; Create Account</button>
        </div>"""

    if not admin and credits < 1:
        return """
        <div class="page-title">Schedule Checker</div>
        <div class="page-subtitle">Book a specific time slot and receive your report by email when it&rsquo;s ready.</div>
        <div class="card" style="text-align:center;padding:40px">
          <div style="font-size:2.5rem;margin-bottom:16px">&#128199;</div>
          <div style="font-size:1.05rem;font-weight:800;color:var(--primary);margin-bottom:8px">No credits remaining</div>
          <div style="font-size:.85rem;color:var(--muted);margin-bottom:22px">Purchase credits to schedule checks. Credits are deducted when your scheduled check runs.</div>
          <a href="/pricing" class="btn-primary" style="text-decoration:none;display:inline-block">View Pricing</a>
        </div>"""

    ALL_TZ = [
        'UTC',
        'America/New_York','America/Chicago','America/Denver','America/Los_Angeles',
        'America/Anchorage','Pacific/Honolulu','America/Toronto','America/Vancouver',
        'America/Phoenix','America/Sao_Paulo','America/Argentina/Buenos_Aires',
        'America/Mexico_City','America/Bogota','America/Lima','America/Santiago',
        'Europe/London','Europe/Paris','Europe/Berlin','Europe/Madrid','Europe/Rome',
        'Europe/Amsterdam','Europe/Zurich','Europe/Warsaw','Europe/Prague',
        'Europe/Athens','Europe/Helsinki','Europe/Stockholm','Europe/Oslo',
        'Europe/Lisbon','Europe/Dublin','Europe/Bucharest','Europe/Istanbul',
        'Europe/Moscow','Europe/Kiev','Europe/Minsk',
        'Asia/Dubai','Asia/Kolkata','Asia/Dhaka','Asia/Karachi',
        'Asia/Bangkok','Asia/Jakarta','Asia/Singapore','Asia/Shanghai',
        'Asia/Hong_Kong','Asia/Seoul','Asia/Tokyo',
        'Australia/Sydney','Australia/Melbourne','Australia/Brisbane',
        'Australia/Adelaide','Australia/Perth','Pacific/Auckland',
        'Pacific/Fiji','Pacific/Guam','Africa/Cairo','Africa/Nairobi',
        'Africa/Lagos','Africa/Johannesburg',
    ]
    tz_opts = ''.join(
        '<option value="' + tz + '"' + (' selected' if tz == saved_tz else '') + '>' + tz.replace('_', ' ') + '</option>'
        for tz in ALL_TZ
    )
    saved_tz_js = json.dumps(saved_tz)
    return f"""
    <div class="page-title">Schedule Checker</div>
    <div class="page-subtitle">Pick a date and time &mdash; your check runs automatically and the report arrives by email. 78 slots per day, first-come first-serve.</div>

    <!-- Timezone selector -->
    <div class="card" style="padding:14px 20px">
      <div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap">
        <span style="font-size:.78rem;font-weight:700;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);white-space:nowrap">Your Timezone</span>
        <select id="tzSelect" onchange="onTzChange(this.value)" style="flex:1;min-width:200px;padding:7px 10px;border-radius:8px;border:1.5px solid var(--border);font-size:.85rem;background:white;color:var(--text)">{tz_opts}</select>
        <span id="tzSaveStatus" style="font-size:.75rem;color:var(--muted)"></span>
      </div>
    </div>

    <!-- Next Available -->
    <div class="next-avail-box" id="nextAvailBox" onclick="clickNextAvail()">
      <div>
        <div class="next-avail-label">Next Available Slot</div>
        <div style="font-size:.76rem;color:var(--muted);margin-top:2px" id="nextAvailAhead">&nbsp;</div>
        <div class="next-avail-time" id="nextAvailTime">Loading&hellip;</div>
      </div>
      <div class="next-avail-arrow">&#8594;</div>
    </div>

    <!-- Calendar -->
    <div class="card">
      <div class="cal-header">
        <button class="cal-nav-btn" id="calPrev" onclick="calNav(-1)">&#8592;</button>
        <div class="cal-month-label" id="calMonthLabel"></div>
        <button class="cal-nav-btn" id="calNext" onclick="calNav(1)">&#8594;</button>
      </div>
      <div class="cal-weekdays">
        <div>Sun</div><div>Mon</div><div>Tue</div><div>Wed</div><div>Thu</div><div>Fri</div><div>Sat</div>
      </div>
      <div class="cal-grid" id="calGrid"></div>
    </div>

    <!-- Time slots (shown after day click) -->
    <div class="card" id="slotsCard" style="display:none">
      <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:14px">
        <div>
          <div style="font-size:.78rem;font-weight:700;text-transform:uppercase;letter-spacing:.08em;color:var(--muted)">Available Times</div>
          <div id="slotsDateLabel" style="font-size:.9rem;font-weight:700;color:var(--text);margin-top:2px"></div>
        </div>
      </div>
      <div id="slotLoading" style="text-align:center;padding:20px;color:var(--muted);font-size:.84rem">Loading slots&hellip;</div>
      <div class="slot-wrap" id="slotWrap" style="display:none"><div id="slotGrid"></div></div>
    </div>

    <!-- Booking form (shown after slot click) -->
    <div class="card" id="bookingCard" style="display:none">
      <div style="font-size:.78rem;font-weight:700;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);margin-bottom:14px">Booking Details</div>
      <div id="bookingSlotBadge" style="background:var(--lavender);border:1.5px solid rgba(111,94,247,.3);border-radius:10px;padding:8px 14px;font-size:.85rem;font-weight:700;color:var(--primary-dark);margin-bottom:16px;display:inline-block"></div>
      <label>Operation Name</label>
      <input type="text" id="schedOp" placeholder="e.g. Green Hills Farm" style="margin-bottom:14px">
      <label>Website URL</label>
      <input type="text" id="schedUrl" placeholder="https://example.com" style="margin-bottom:18px">
      <div style="display:flex;gap:20px;margin-bottom:18px">
        <label style="display:flex;align-items:center;gap:6px;font-size:.85rem;cursor:pointer">
          <input type="radio" name="rptFmt" value="html" checked> Email (HTML)
        </label>
        <label style="display:flex;align-items:center;gap:6px;font-size:.85rem;cursor:pointer">
          <input type="radio" name="rptFmt" value="md"> Email + Markdown (.md)
        </label>
      </div>
      <button class="btn-primary" id="confirmBtn" onclick="confirmBooking()" style="width:100%">Confirm Booking</button>
      <div style="font-size:.74rem;color:var(--muted);text-align:center;margin-top:10px">1 Checker used when your check runs &mdash; not at booking time</div>
    </div>

    <!-- My scheduled checks -->
    <div class="card">
      <div style="font-size:.78rem;font-weight:700;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);margin-bottom:14px">Your Scheduled Checks</div>
      <div id="myChecksList"><div style="text-align:center;color:var(--muted);font-size:.84rem;padding:20px">Loading&hellip;</div></div>
    </div>

    <script>
    // ── Config ────────────────────────────────────────────────────────────
    var SLOT_START_H = 8;
    var SLOT_END_H   = 21;
    var SLOT_MIN     = 10;
    var _userTz      = {saved_tz_js};
    var _selectedSlot = null;
    var _calYear, _calMonth;
    var _nextAvailIso = {next_avail_iso_js};
    var _selectedCalDay = null;

    function onTzChange(tz) {{
      _userTz = tz;
      var statusEl = document.getElementById('tzSaveStatus');
      if (statusEl) statusEl.textContent = 'Saving...';
      fetch('/api/user/timezone', {{method:'POST', headers:{{'Content-Type':'application/json'}}, body:JSON.stringify({{timezone:tz}})}})
        .then(function(r){{ return r.json(); }})
        .then(function(d){{
          if (statusEl) statusEl.textContent = d.ok ? 'Saved \u2713' : 'Save failed';
          setTimeout(function(){{ if (statusEl) statusEl.textContent = ''; }}, 2500);
        }})
        .catch(function(){{ if (statusEl) {{ statusEl.textContent = 'Save failed'; }} }});
      if (_nextAvailIso) document.getElementById('nextAvailTime').textContent = fmtSlotLocal(_nextAvailIso);
      renderCalendar(_calYear, _calMonth);
      if (_selectedCalDay) loadSlots(_selectedCalDay);
    }}

    // ── Date helpers ──────────────────────────────────────────────────────
    function fmtSlotLocal(isoUtc) {{
      var d = new Date(isoUtc);
      try {{
        return d.toLocaleString([], {{weekday:'short',month:'short',day:'numeric',hour:'2-digit',minute:'2-digit',timeZone:_userTz,timeZoneName:'short'}});
      }} catch(e) {{
        return d.toLocaleString([], {{weekday:'short',month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'}});
      }}
    }}

    function utcIsoToLocalDate(isoUtc) {{
      var d = new Date(isoUtc);
      try {{
        return new Intl.DateTimeFormat('en-CA', {{timeZone:_userTz,year:'numeric',month:'2-digit',day:'2-digit'}}).format(d);
      }} catch(e) {{ return isoUtc.slice(0,10); }}
    }}

    function localDateLabel(dateStr) {{
      var parts = dateStr.split('-');
      var d = new Date(parseInt(parts[0]), parseInt(parts[1])-1, parseInt(parts[2]));
      return d.toLocaleDateString([], {{weekday:'long',year:'numeric',month:'long',day:'numeric'}});
    }}

    // ── Calendar ──────────────────────────────────────────────────────────
    var MONTH_NAMES = ['January','February','March','April','May','June','July','August','September','October','November','December'];

    function calInit() {{
      var now = new Date();
      _calYear = now.getFullYear();
      _calMonth = now.getMonth();
      renderCalendar(_calYear, _calMonth);
    }}

    function calNav(dir) {{
      _calMonth += dir;
      if (_calMonth > 11) {{ _calMonth = 0; _calYear++; }}
      if (_calMonth < 0)  {{ _calMonth = 11; _calYear--; }}
      renderCalendar(_calYear, _calMonth);
    }}

    function renderCalendar(year, month) {{
      document.getElementById('calMonthLabel').textContent = MONTH_NAMES[month] + ' ' + year;
      var grid = document.getElementById('calGrid');
      grid.innerHTML = '';
      var todayLocal = utcIsoToLocalDate(new Date().toISOString());
      var firstDay = new Date(year, month, 1).getDay();
      var daysInMonth = new Date(year, month+1, 0).getDate();
      var nowM = new Date();
      document.getElementById('calPrev').disabled = (year === nowM.getFullYear() && month === nowM.getMonth());
      for (var i = 0; i < firstDay; i++) {{
        var blank = document.createElement('div');
        blank.className = 'cal-day cal-blank';
        grid.appendChild(blank);
      }}
      for (var d = 1; d <= daysInMonth; d++) {{
        var ds = year + '-' + String(month+1).padStart(2,'0') + '-' + String(d).padStart(2,'0');
        var cell = document.createElement('div');
        cell.className = 'cal-day';
        cell.textContent = d;
        cell.dataset.date = ds;
        if (ds < todayLocal) {{
          cell.classList.add('cal-day-past');
        }} else {{
          cell.classList.add('cal-day-avail');
          if (ds === _selectedCalDay) cell.classList.add('cal-day-selected');
          cell.onclick = (function(dateStr, el) {{
            return function() {{ selectCalDay(dateStr, el); }};
          }})(ds, cell);
        }}
        grid.appendChild(cell);
      }}
    }}

    function selectCalDay(dateStr, cellEl) {{
      _selectedCalDay = dateStr;
      document.querySelectorAll('.cal-day').forEach(function(c) {{ c.classList.remove('cal-day-selected'); }});
      if (cellEl) cellEl.classList.add('cal-day-selected');
      document.getElementById('slotsCard').style.display = '';
      document.getElementById('slotsDateLabel').textContent = localDateLabel(dateStr);
      loadSlots(dateStr);
    }}

    // ── Slot grid ─────────────────────────────────────────────────────────
    function loadSlots(localDateStr) {{
      document.getElementById('slotLoading').style.display = '';
      document.getElementById('slotWrap').style.display = 'none';
      var parts = localDateStr.split('-');
      var localY = parseInt(parts[0]), localM = parseInt(parts[1])-1, localD = parseInt(parts[2]);
      var utcDates = new Set();
      for (var offset = -1; offset <= 1; offset++) {{
        var d = new Date(Date.UTC(localY, localM, localD + offset));
        utcDates.add(d.toISOString().slice(0,10));
      }}
      var promises = Array.from(utcDates).map(function(utcDate) {{
        return fetch('/api/available-slots?date=' + utcDate).then(function(r){{ return r.json(); }});
      }});
      Promise.all(promises).then(function(results) {{
        var booked = new Set();
        results.forEach(function(data) {{ (data.booked || []).forEach(function(s) {{ booked.add(s); }}); }});
        if (results[0] && results[0].next_available) {{
          _nextAvailIso = results[0].next_available;
          document.getElementById('nextAvailTime').textContent = fmtSlotLocal(_nextAvailIso);
        }}
        renderSlotGrid(localDateStr, booked);
      }}).catch(function() {{
        document.getElementById('slotLoading').style.display = 'none';
        document.getElementById('slotGrid').innerHTML = '<div style="color:var(--muted);padding:12px;text-align:center">Unable to load slots.</div>';
        document.getElementById('slotWrap').style.display = '';
      }});
    }}

    function renderSlotGrid(localDateStr, bookedUtcSet) {{
      var grid = document.getElementById('slotGrid');
      grid.innerHTML = '';
      var now = new Date();
      var cutoff = new Date(now.getTime() + 5 * 60000);
      var parts = localDateStr.split('-');
      var localY = parseInt(parts[0]), localM = parseInt(parts[1])-1, localD = parseInt(parts[2]);
      var slots = [];
      for (var offset = -1; offset <= 1; offset++) {{
        var d = new Date(Date.UTC(localY, localM, localD + offset));
        var utcDate = d.toISOString().slice(0,10);
        for (var h = SLOT_START_H; h < SLOT_END_H; h++) {{
          for (var m = 0; m < 60; m += SLOT_MIN) {{
            var isoStr = utcDate + 'T' + String(h).padStart(2,'0') + ':' + String(m).padStart(2,'0') + ':00Z';
            if (utcIsoToLocalDate(isoStr) !== localDateStr) continue;
            var slotDt = new Date(isoStr);
            slots.push({{
              iso: isoStr, dt: slotDt,
              isPast: slotDt < cutoff,
              isBooked: bookedUtcSet.has(isoStr),
              label: slotDt.toLocaleTimeString([], {{hour:'numeric',minute:'2-digit',timeZone:_userTz}})
            }});
          }}
        }}
      }}
      if (slots.length === 0) {{
        grid.innerHTML = '<div style="color:var(--muted);padding:16px;text-align:center;font-size:.85rem">No slots this day &mdash; available 8am&ndash;9pm UTC</div>';
        document.getElementById('slotLoading').style.display = 'none';
        document.getElementById('slotWrap').style.display = '';
        return;
      }}
      slots.forEach(function(s) {{
        var btn = document.createElement('button');
        btn.dataset.iso = s.iso;
        if (s.isPast) {{
          btn.className = 'slot-btn past'; btn.disabled = true; btn.textContent = s.label;
        }} else if (s.isBooked) {{
          btn.className = 'slot-btn booked'; btn.disabled = true; btn.textContent = s.label + ' (scheduled)';
        }} else {{
          btn.className = 'slot-btn ' + (_selectedSlot === s.iso ? 'selected' : 'avail');
          btn.textContent = s.label;
          btn.onclick = (function(iso){{ return function(){{ selectSlot(iso); }}; }})(s.iso);
        }}
        grid.appendChild(btn);
      }});
      document.getElementById('slotLoading').style.display = 'none';
      document.getElementById('slotWrap').style.display = '';
      var firstAvail = grid.querySelector('.slot-btn.avail, .slot-btn.selected');
      if (firstAvail) firstAvail.scrollIntoView({{behavior:'smooth',block:'nearest'}});
    }}

    function selectSlot(isoStr) {{
      _selectedSlot = isoStr;
      document.querySelectorAll('.slot-btn').forEach(function(b) {{
        if (b.disabled) return;
        b.className = 'slot-btn ' + (b.dataset.iso === isoStr ? 'selected' : 'avail');
      }});
      var card = document.getElementById('bookingCard');
      card.style.display = '';
      document.getElementById('bookingSlotBadge').textContent = '\U0001F4C5 ' + fmtSlotLocal(isoStr);
      setTimeout(function() {{ card.scrollIntoView({{behavior:'smooth',block:'nearest'}}); }}, 80);
    }}

    // ── Next Available polling ────────────────────────────────────────────
    function pollNextAvail() {{
      fetch('/api/available-slots?date=' + new Date().toISOString().slice(0,10))
        .then(function(r){{ return r.json(); }})
        .then(function(data) {{
          if (data.next_available) {{
            _nextAvailIso = data.next_available;
            document.getElementById('nextAvailTime').textContent = fmtSlotLocal(data.next_available);
          }}
          return fetch('/api/queue-depth');
        }})
        .then(function(r){{ return r.json(); }})
        .then(function(data) {{
          var ahead = data.depth || 0;
          document.getElementById('nextAvailAhead').textContent =
            ahead === 0 ? 'No jobs ahead — available now' :
            (ahead + ' job' + (ahead !== 1 ? 's' : '') + ' ahead in queue');
        }})
        .catch(function() {{}});
    }}

    function clickNextAvail() {{
      if (!_nextAvailIso) return;
      var localDs = utcIsoToLocalDate(_nextAvailIso);
      var parts = localDs.split('-');
      _calYear = parseInt(parts[0]); _calMonth = parseInt(parts[1]) - 1;
      renderCalendar(_calYear, _calMonth);
      var cell = Array.from(document.querySelectorAll('.cal-day-avail')).find(function(c){{ return c.dataset.date === localDs; }});
      selectCalDay(localDs, cell || null);
      // After slots load, auto-select the specific slot and show booking card
      setTimeout(function() {{
        var btn = document.querySelector('.slot-btn[data-iso="' + _nextAvailIso + '"]');
        if (btn && !btn.disabled) {{
          btn.scrollIntoView({{behavior:'smooth', block:'center'}});
          selectSlot(_nextAvailIso);
        }}
      }}, 800);
    }}

    // ── Booking ───────────────────────────────────────────────────────────
    async function confirmBooking() {{
      var op  = document.getElementById('schedOp').value.trim();
      var url = document.getElementById('schedUrl').value.trim();
      if (!op || !url) {{ alert('Please enter operation name and website URL.'); return; }}
      if (!_selectedSlot) {{ alert('Please select a time slot.'); return; }}
      if (!url.startsWith('http')) url = 'https://' + url;
      var btn = document.getElementById('confirmBtn');
      btn.disabled = true; btn.textContent = 'Booking\u2026';
      try {{
        var fmt = document.querySelector('input[name="rptFmt"]:checked');
        var res = await fetch('/api/schedule-check', {{
          method: 'POST', headers: {{'Content-Type': 'application/json'}},
          body: JSON.stringify({{operation: op, website: url, scheduled_at: _selectedSlot, report_format: fmt ? fmt.value : 'html'}})
        }});
        var data = await res.json();
        if (data.ok) {{
          _selectedSlot = null;
          document.getElementById('bookingCard').style.display = 'none';
          document.getElementById('schedOp').value = '';
          document.getElementById('schedUrl').value = '';
          btn.textContent = 'Booked!'; btn.style.background = 'var(--green)';
          setTimeout(function() {{ btn.textContent = 'Confirm Booking'; btn.style.background = ''; btn.disabled = false; }}, 2500);
          if (_selectedCalDay) loadSlots(_selectedCalDay);
          loadMyChecks(); pollNextAvail();
        }} else {{
          alert(data.error || 'Booking failed. Please try again.');
          btn.disabled = false; btn.textContent = 'Confirm Booking';
        }}
      }} catch(e) {{
        alert('Network error. Please try again.');
        btn.disabled = false; btn.textContent = 'Confirm Booking';
      }}
    }}

    // ── My scheduled checks ───────────────────────────────────────────────
    async function loadMyChecks() {{
      try {{
        var res = await fetch('/api/my-scheduled-checks');
        var data = await res.json();
        var el = document.getElementById('myChecksList');
        if (!data.checks || data.checks.length === 0) {{
          el.innerHTML = '<div style="text-align:center;color:var(--muted);font-size:.84rem;padding:16px">No scheduled checks yet.</div>';
          return;
        }}
        var html = '';
        data.checks.forEach(function(c) {{
          var actions = '';
          if (c.status === 'scheduled') actions = '<button class="sched-cancel-btn" onclick="cancelCheck(\'' + c.id + '\')">Cancel</button>';
          else if (c.status === 'done') actions = '<a class="sched-view-btn" href="/scheduled-report/' + c.id + '">View</a>';
          html += '<div class="sched-list-item"><div class="sched-item-info">' +
            '<div class="sched-item-op">' + escHtml(c.operation_name) + '</div>' +
            '<div class="sched-item-when">' + fmtSlotLocal(c.scheduled_at) + '</div></div>' +
            '<span class="sched-item-badge badge-' + c.status + '">' + c.status + '</span>' + actions + '</div>';
        }});
        el.innerHTML = html;
      }} catch(e) {{
        document.getElementById('myChecksList').innerHTML = '<div style="color:var(--muted);font-size:.84rem;padding:12px">Unable to load.</div>';
      }}
    }}

    async function cancelCheck(id) {{
      if (!confirm('Cancel this scheduled check?')) return;
      var res = await fetch('/api/cancel-scheduled/' + id, {{method:'POST'}});
      var data = await res.json();
      if (data.ok) {{ loadMyChecks(); if (_selectedCalDay) loadSlots(_selectedCalDay); pollNextAvail(); }}
      else alert(data.error || 'Cancel failed.');
    }}

    function escHtml(s) {{
      return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
    }}

    // ── Init ──────────────────────────────────────────────────────────────
    calInit();
    // Show next available immediately from server-injected value
    if (_nextAvailIso) {{
      document.getElementById('nextAvailTime').textContent = fmtSlotLocal(_nextAvailIso);
    }} else {{
      document.getElementById('nextAvailTime').textContent = 'No slots available';
    }}
    // Auto-load today's slots so the grid is visible on arrival
    var todayLocalDs = utcIsoToLocalDate(new Date().toISOString());
    var todayCell = Array.from(document.querySelectorAll('.cal-day-avail')).find(function(c){{ return c.dataset.date === todayLocalDs; }});
    if (todayCell) {{
      selectCalDay(todayLocalDs, todayCell);
    }} else if (_nextAvailIso) {{
      // Today has no available slots — jump to next available day instead
      clickNextAvail();
    }}
    pollNextAvail();
    setInterval(pollNextAvail, 5000);
    loadMyChecks();
    </script>"""


@app.route('/schedule')
def schedule():
    email = get_logged_in_email()
    body  = schedule_page_html(email)
    return render_template_string(BASE_TEMPLATE, css=GLOBAL_CSS,
                                  page_title='Schedule Checker', active='schedule', body=body)


@app.route('/api/available-slots')
def api_available_slots():
    date_str = request.args.get('date', '')
    if not date_str:
        return jsonify({'error': 'date required'}), 400
    booked     = get_booked_slots_for_day(date_str)
    next_slot  = get_next_available_slot()
    next_iso   = next_slot.strftime('%Y-%m-%dT%H:%M:00Z') if next_slot else None
    return jsonify({'booked': booked, 'next_available': next_iso})


@app.route('/api/schedule-check', methods=['POST'])
def api_schedule_check():
    email = get_logged_in_email()
    if not email:
        return jsonify({'error': 'Sign in required'}), 401
    if not is_admin(email) and get_user_credits(email) < 1:
        return jsonify({'error': 'No credits remaining. Purchase credits to schedule checks.'}), 402
    data          = request.get_json(force=True) or {}
    operation     = data.get('operation', '').strip()
    website       = data.get('website', '').strip()
    scheduled_at  = data.get('scheduled_at', '').strip()
    report_format = data.get('report_format', 'html').strip()
    if report_format not in ('html', 'md'):
        report_format = 'html'
    if not operation or not website or not scheduled_at:
        return jsonify({'error': 'operation, website, and scheduled_at are required'}), 400
    if not website.startswith('http'):
        website = 'https://' + website
    # Parse and validate the slot
    try:
        slot_dt = datetime.fromisoformat(scheduled_at.replace('Z', '+00:00'))
    except ValueError:
        return jsonify({'error': 'Invalid scheduled_at format. Use ISO 8601 UTC.'}), 400
    if slot_dt < datetime.now(timezone.utc) + timedelta(minutes=3):
        return jsonify({'error': 'Slot must be at least 3 minutes in the future.'}), 400
    check_id = uuid.uuid4().hex[:12]
    if not DATABASE_URL:
        return jsonify({'error': 'Database not configured'}), 500
    try:
        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO scheduled_checks
                       (id, user_email, operation_name, website_url, scheduled_at, report_format)
                       VALUES (%s, %s, %s, %s, %s, %s)""",
                    (check_id, email, operation, website, slot_dt, report_format)
                )
            conn.commit()
    except psycopg2.IntegrityError:
        return jsonify({'error': 'That time slot is already booked. Please choose another.'}), 409
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    return jsonify({'ok': True, 'check_id': check_id,
                    'scheduled_at': slot_dt.strftime('%Y-%m-%dT%H:%M:00Z')})


@app.route('/api/my-scheduled-checks')
def api_my_scheduled_checks():
    email = get_logged_in_email()
    if not email:
        return jsonify({'error': 'Not logged in'}), 401
    if not DATABASE_URL:
        return jsonify({'checks': []})
    try:
        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT id, operation_name, website_url, scheduled_at, status, job_id
                       FROM scheduled_checks
                       WHERE user_email = %s AND status NOT IN ('cancelled')
                       ORDER BY scheduled_at DESC LIMIT 50""",
                    (email,)
                )
                rows = cur.fetchall()
        checks = [
            {'id': r[0], 'operation_name': r[1], 'website_url': r[2],
             'scheduled_at': r[3].strftime('%Y-%m-%dT%H:%M:00Z'),
             'status': r[4], 'job_id': r[5]}
            for r in rows
        ]
        return jsonify({'checks': checks})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/cancel-scheduled/<check_id>', methods=['POST'])
def api_cancel_scheduled(check_id):
    email = get_logged_in_email()
    if not email:
        return jsonify({'error': 'Not logged in'}), 401
    if not DATABASE_URL:
        return jsonify({'error': 'Database not configured'}), 500
    try:
        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """UPDATE scheduled_checks SET status='cancelled'
                       WHERE id=%s AND user_email=%s AND status='scheduled'""",
                    (check_id, email)
                )
                updated = cur.rowcount
            conn.commit()
        if not updated:
            return jsonify({'error': 'Check not found or already running/done'}), 404
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/scheduled-report/<check_id>')
def scheduled_report(check_id):
    if not DATABASE_URL:
        return 'Database not configured', 500
    try:
        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    'SELECT operation_name, website_url, status, report, user_email, scheduled_at, job_id '
                    'FROM scheduled_checks WHERE id=%s',
                    (check_id,)
                )
                row = cur.fetchone()
    except Exception as e:
        return f'Database error: {e}', 500
    if not row:
        return 'Report not found', 404
    op, website, status, report, owner_email, sched_at, job_id = row
    if status not in ('done', 'error'):
        body = f"""
        <div class="page-title">{op}</div>
        <div class="page-subtitle">{website}</div>
        <div class="card" style="text-align:center;padding:40px">
          <div style="font-size:2rem;margin-bottom:14px">&#128197;</div>
          <div style="font-size:1rem;font-weight:700;color:var(--primary);margin-bottom:8px">Check Scheduled</div>
          <div style="font-size:.85rem;color:var(--muted)">
            Scheduled for {sched_at.strftime('%Y-%m-%d %H:%M UTC') if sched_at else 'your selected slot'}.<br>
            You&rsquo;ll receive an email when the report is ready.
          </div>
        </div>"""
        return render_template_string(BASE_TEMPLATE, css=GLOBAL_CSS,
                                      page_title=op, active='schedule', body=body)
    if not report:
        return 'Report data not found', 404
    # Render using existing REPORT_PARTIAL; job_id may be None so use check_id for downloads
    display_job_id = job_id or check_id
    body = render_template_string(REPORT_PARTIAL, report=report, job_id=display_job_id)
    header = f"""
    <div class="page-title">{op}</div>
    <div class="page-subtitle" style="margin-bottom:10px">{website} &middot;
      Scheduled check &middot; {sched_at.strftime('%Y-%m-%d %H:%M UTC') if sched_at else ''}
    </div>"""
    return render_template_string(BASE_TEMPLATE, css=GLOBAL_CSS,
                                  page_title=op, active='schedule', body=header + body)


@app.route('/static/<path:filename>')
def static_files(filename):
    return send_from_directory('static', filename)


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(debug=False, host='0.0.0.0', port=port)
