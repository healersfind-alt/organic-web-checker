"""
Organic Web Checker — Flask web app
"""

import os
import uuid
import threading
import json
from contextlib import contextmanager
from datetime import datetime, timezone
from flask import Flask, request, render_template_string, send_from_directory, jsonify, Response, session
from werkzeug.security import generate_password_hash, check_password_hash
from checker import run_check
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
        conn.commit()

try:
    init_db()
except Exception as _db_err:
    print(f'[WARN] DB init skipped: {_db_err}')

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

# In-memory session stats (resets on redeploy; persistent DB coming with Stripe)
stats = {'checks_run': 0, 'flags_found': 0, 'caution_found': 0}


def _run_job(job_id: str, operation: str, website: str):
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
            report = run_check(operation, website, progress_callback=_progress)
            with jobs_lock:
                jobs[job_id]['status'] = 'done'
                jobs[job_id]['report'] = report
            # Update session stats
            if 'error' not in report:
                with jobs_lock:
                    stats['checks_run'] += 1
                    stats['flags_found'] += len(report.get('flagged', []))
                    stats['caution_found'] += len(report.get('caution', []))
        except Exception as e:
            with jobs_lock:
                jobs[job_id]['status'] = 'error'
                jobs[job_id]['report'] = {'error': str(e)}
        finally:
            with jobs_lock:
                jobs[job_id]['finished_at'] = datetime.now(timezone.utc).isoformat()


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
        f"- **Website:** {report.get('website_url', '')}",
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

    # ── Non-compliance flags ───────────────────────────────────────────────
    if flagged:
        lines += [
            f"## 🔴 NON-COMPLIANCE RISK ({len(flagged)} items)",
            "Products marketed as organic on the website but NOT found on the OID certificate.",
            "Ref: 7 CFR § 205.307 (misrepresentation); § 205.303–305 (labeling requirements)",
            "",
        ]
        for i, item in enumerate(flagged, 1):
            url = f" → {item['url']}" if item.get('url') else ""
            lines.append(f"{i}. **{item['title']}**{url}")
        lines.append("")
    else:
        lines += ["## ✅ NO NON-COMPLIANCE FLAGS",
                  "All specific organic product claims match the OID certificate.", ""]

    # ── Caution — name variations ──────────────────────────────────────────
    if caution:
        lines += [
            f"## 🟡 NAME VARIATION / CAUTION ({len(caution)} items)",
            "Products that closely resemble certified items but with possible name differences.",
            "Not an NOP violation — requires certifier alignment review.",
            "",
        ]
        for i, item in enumerate(caution, 1):
            url = f" → {item['url']}" if item.get('url') else ""
            lines.append(f"{i}. {item['title']}{url}")
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
    --bg:           #F8FAFC;
    --surface:      #FFFFFF;
    --card-bg:      #FFFFFF;
    --border:       #E2E8F0;
    --primary:      #5B3DF6;
    --primary-dim:  #6366F1;
    --primary-dark: #4F35D8;
    --teal:         #14B8A6;
    --green:        #22C55E;
    --amber:        #D97706;
    --red:          #DC2626;
    --lavender:     #EEF2FF;
    --text:         #0F172A;
    --muted:        #64748B;
    --dim:          #CBD5E1;
    /* Legacy aliases kept for backward compat with existing templates */
    --neon:         #22C55E;
    --neon-dim:     #16A34A;
    --neon-dark:    #15803D;
    --cyan:         #14B8A6;
    --glow:         rgba(91, 61, 246, 0.06);
    --red-glow:     rgba(220, 38, 38, 0.08);
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
    background: var(--surface);
    border-bottom: 1px solid var(--border);
    box-shadow: 0 1px 12px rgba(15,23,42,.06);
    padding: 14px 32px;
    display: grid; grid-template-columns: 1fr auto 1fr; align-items: center; gap: 16px;
    position: sticky; top: 0; z-index: 100;
  }
  .header-logo {
    display: flex; align-items: center; gap: 10px; text-decoration: none;
  }
  .header-logo-icon { width: 48px; height: 48px; flex-shrink: 0; }
  .header-wordmark  { font-size: 1rem; font-weight: 800; color: var(--primary); }
  header h1 { font-size: 1rem; font-weight: 800; color: var(--primary); }
  header p  { font-size: .78rem; color: var(--muted); margin-top: 2px; }

  .header-right { display: flex; align-items: center; gap: 10px; justify-self: end; }
  .header-nav   { display: flex; gap: 2px; justify-content: center; }
  .nav-link {
    color: var(--muted); font-size: .82rem; text-decoration: none;
    padding: 6px 12px; border-radius: 8px;
    transition: color .15s, background .15s; font-weight: 500;
  }
  .nav-link:hover { color: var(--primary); background: var(--lavender); }
  .nav-link.active { color: var(--primary); background: var(--lavender); font-weight: 600; }

  .header-cta-btn {
    background: var(--primary); color: #fff;
    border: none; border-radius: 8px;
    padding: 8px 18px; font-size: .82rem; font-weight: 700;
    cursor: pointer; text-decoration: none;
    transition: background .15s, box-shadow .15s;
    box-shadow: 0 2px 8px rgba(91,61,246,.25);
  }
  .header-cta-btn:hover { background: var(--primary-dark); box-shadow: 0 4px 16px rgba(91,61,246,.35); }

  .header-icon-wrap { position: relative; flex-shrink: 0; }
  .header-icon-btn {
    background: none; border: 1px solid var(--border); cursor: pointer;
    padding: 5px; border-radius: 9px; display: block;
    transition: border-color .15s, box-shadow .15s;
  }
  .header-icon-btn:hover { border-color: var(--primary); box-shadow: 0 0 0 3px rgba(91,61,246,.08); }
  .header-icon { width: 48px; height: 48px; display: block; }
  .header-dropdown {
    position: absolute; top: calc(100% + 8px); right: 0;
    background: var(--surface); border: 1px solid var(--border);
    border-radius: 12px;
    box-shadow: 0 8px 32px rgba(15,23,42,.12);
    min-width: 190px; z-index: 200; display: none; overflow: hidden;
  }
  .header-dropdown.open { display: block; }
  .dropdown-item {
    display: block; padding: 11px 17px;
    font-size: .84rem; color: var(--text);
    text-decoration: none; border-bottom: 1px solid var(--border);
  }
  .dropdown-item:last-child { border-bottom: none; }
  .dropdown-item:hover { background: var(--lavender); color: var(--primary); }

  /* ── Layout ──────────────────────────────────────────────────────────── */
  .page-main { max-width: 920px; margin: 36px auto; padding: 0 24px; }

  /* ── Cards ───────────────────────────────────────────────────────────── */
  .card {
    background: var(--card-bg);
    border: 1px solid var(--border);
    border-radius: 16px; padding: 26px 30px;
    box-shadow: 0 1px 16px rgba(15,23,42,.06);
    margin-bottom: 22px;
  }

  /* ── Page headers ────────────────────────────────────────────────────── */
  .page-title    { font-size: 1.35rem; font-weight: 800; color: var(--primary); margin-bottom: 6px; }
  .page-subtitle { font-size: .87rem; color: var(--muted); margin-bottom: 28px; line-height: 1.6; }

  /* ── Form ────────────────────────────────────────────────────────────── */
  label {
    display: block; font-size: .78rem; font-weight: 700; margin-bottom: 6px;
    color: var(--muted); text-transform: uppercase; letter-spacing: .06em;
  }
  input[type=text], input[type=email], input[type=password] {
    width: 100%; padding: 11px 14px;
    background: var(--bg); border: 1.5px solid var(--border);
    border-radius: 10px; font-size: .94rem; margin-bottom: 16px; color: var(--text);
    transition: border-color .2s, box-shadow .2s;
  }
  input[type=text]::placeholder,
  input[type=email]::placeholder,
  input[type=password]::placeholder { color: var(--dim); }
  input:focus {
    outline: none; border-color: var(--primary);
    box-shadow: 0 0 0 3px rgba(91,61,246,.1);
  }
  .hint { font-size: .76rem; color: var(--muted); margin-top: -10px; margin-bottom: 16px; line-height: 1.5; }

  button[type=submit], .btn-primary {
    background: var(--primary); color: #fff;
    border: none; border-radius: 10px;
    padding: 12px 28px; font-size: .9rem;
    cursor: pointer; font-weight: 700;
    transition: background .15s, box-shadow .15s;
    box-shadow: 0 2px 12px rgba(91,61,246,.3);
  }
  button[type=submit]:hover, .btn-primary:hover {
    background: var(--primary-dark);
    box-shadow: 0 4px 20px rgba(91,61,246,.4);
  }
  button[type=submit]:disabled { background: var(--dim); color: #fff; box-shadow: none; cursor: default; }

  /* ── Queue panel ─────────────────────────────────────────────────────── */
  .queue-panel {
    background: var(--card-bg); border: 1px solid var(--border);
    border-radius: 16px; margin-bottom: 22px; overflow: hidden;
    box-shadow: 0 1px 16px rgba(15,23,42,.06);
  }
  .queue-header {
    padding: 12px 20px;
    background: var(--bg); border-bottom: 1px solid var(--border);
    font-size: .72rem; font-weight: 700; text-transform: uppercase;
    letter-spacing: .09em; color: var(--muted);
    display: flex; align-items: center; gap: 8px;
  }
  .queue-count {
    background: var(--lavender); color: var(--primary);
    border: 1px solid rgba(91,61,246,.15); border-radius: 10px;
    padding: 1px 8px; font-size: .7rem; font-weight: 700;
  }
  .queue-list { list-style: none; }
  .queue-item {
    padding: 12px 20px; border-bottom: 1px solid var(--border);
    display: flex; align-items: center; gap: 12px; font-size: .875rem;
  }
  .queue-item:last-child { border-bottom: none; }
  .op-name { font-weight: 700; flex: 1; min-width: 0; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .site    { font-size: .76rem; color: var(--muted); }

  .status-pill {
    font-size: .68rem; font-weight: 700; padding: 3px 9px;
    border-radius: 10px; white-space: nowrap; flex-shrink: 0; border: 1px solid;
  }
  .status-queued  { background: #F1F5F9; color: var(--muted);  border-color: var(--border); }
  .status-running { background: #FEF3C7; color: #D97706; border-color: #FDE68A; animation: pulse 1.4s ease-in-out infinite; }
  .status-done    { background: #DCFCE7; color: #16A34A; border-color: #BBF7D0; }
  .status-error   { background: #FEE2E2; color: #DC2626; border-color: #FECACA; }
  @keyframes pulse { 0%,100% { opacity:1; } 50% { opacity:.5; } }

  /* Animated checker loader */
  .checker-loader {
    position: relative; width: 40px; height: 40px; flex-shrink: 0;
    border-radius: 8px; overflow: hidden;
    background: var(--lavender); border: 1px solid rgba(91,61,246,.15);
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
    font-size: .73rem; padding: 4px 11px;
    border: 1px solid rgba(91,61,246,.2); border-radius: 6px;
    color: var(--primary); background: var(--lavender);
    cursor: pointer; flex-shrink: 0; text-decoration: none;
    transition: background .15s; font-weight: 600;
  }
  .view-btn:hover { background: rgba(91,61,246,.14); }

  .empty-queue { padding: 22px; text-align: center; color: var(--muted); font-size: .84rem; }

  /* ── Report ──────────────────────────────────────────────────────────── */
  .report-header {
    display: flex; align-items: flex-start; justify-content: space-between;
    gap: 16px; margin-bottom: 22px;
    padding-bottom: 14px; border-bottom: 1px solid var(--border);
  }
  .report-op-name  { font-size: 1rem; font-weight: 800; color: var(--primary); }
  .report-meta-sub { font-size: .74rem; color: var(--muted); margin-top: 2px; }
  .download-btns { display: flex; gap: 7px; flex-shrink: 0; }
  .dl-btn {
    font-size: .7rem; padding: 5px 12px; border-radius: 8px;
    text-decoration: none; font-weight: 700; border: 1px solid;
    transition: background .15s; white-space: nowrap;
  }
  .dl-btn.md  { color: var(--teal);  border-color: rgba(20,184,166,.25); background: rgba(20,184,166,.06); }
  .dl-btn.md:hover  { background: rgba(20,184,166,.14); }
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
    background: var(--bg); border: 1px solid var(--border);
    border-radius: 12px; padding: 14px; text-align: center;
  }
  .stat .num { font-size: 1.9rem; font-weight: 900; line-height: 1; font-variant-numeric: tabular-nums; }
  .stat .lbl { font-size: .66rem; color: var(--muted); margin-top: 5px; text-transform: uppercase; letter-spacing: .06em; }
  .stat.flagged  { border-color: rgba(220,38,38,.15); background: #FEF2F2; }
  .stat.flagged  .num { color: var(--red); }
  .stat.verified .num { color: var(--green); }

  .section-label {
    font-size: .7rem; font-weight: 700; text-transform: uppercase; letter-spacing: .1em;
    margin-bottom: 10px; margin-top: 24px;
    display: flex; align-items: center; gap: 8px;
  }
  .section-label:first-of-type { margin-top: 0; }
  .section-label.red    { color: var(--red); }
  .section-label.green  { color: #16A34A; }
  .section-label.cyan   { color: var(--teal); }
  .section-label.amber  { color: var(--amber); }
  .section-label.orange { color: #EA580C; }
  .badge {
    font-size: .66rem; padding: 1px 7px; border-radius: 8px; font-weight: 700; border: 1px solid;
  }
  .red    .badge { background: #FEE2E2; border-color: #FECACA; color: var(--red); }
  .green  .badge { background: #DCFCE7; border-color: #BBF7D0; color: #16A34A; }
  .cyan   .badge { background: #CCFBF1; border-color: #99F6E4; color: var(--teal); }
  .amber  .badge { background: #FEF3C7; border-color: #FDE68A; color: var(--amber); }
  .orange .badge { background: #FFEDD5; border-color: #FED7AA; color: #EA580C; }

  .product-list { list-style: none; display: grid; gap: 4px; }
  .product-list li {
    padding: 8px 13px; border-radius: 0 8px 8px 0;
    font-size: .86rem; display: flex; align-items: center; gap: 9px;
  }
  .product-list li.flag-item     { background: #FEF2F2; border-left: 3px solid #FCA5A5; }
  .product-list li.ok-item       { background: #F0FDF4; border-left: 3px solid #86EFAC; }
  .product-list li.cert-item     { background: #F0FDFA; border-left: 3px solid #5EEAD4; color: var(--muted); }
  .product-list li.caution-item  { background: #FFFBEB; border-left: 3px solid #FCD34D; }
  .product-list li.marketing-item{ background: #FFF7ED; border-left: 3px solid #FDBA74; }
  .caution-icon   { color: var(--amber); flex-shrink: 0; }
  .marketing-icon { color: #EA580C; flex-shrink: 0; }
  .product-list a { color: var(--red); font-weight: 600; text-decoration: none; border-bottom: 1px solid rgba(220,38,38,.2); }
  .product-list a:hover { border-bottom-color: var(--red); }
  .product-list .no-link { color: var(--text); }
  .verify-btn {
    margin-left: auto; flex-shrink: 0; font-size: .7rem;
    border: 1px solid #FECACA; border-radius: 4px;
    padding: 2px 7px; color: var(--red); text-decoration: none;
    background: none; white-space: nowrap;
  }
  .verify-btn:hover { background: #FEE2E2; }

  .scrollable-list { max-height: 340px; overflow-y: auto; padding-right: 4px; }
  .scrollable-list::-webkit-scrollbar { width: 4px; }
  .scrollable-list::-webkit-scrollbar-track { background: var(--bg); }
  .scrollable-list::-webkit-scrollbar-thumb { background: var(--dim); border-radius: 2px; }

  .clean     { color: #16A34A; font-weight: 700; padding: 14px 0; }
  .error-msg { background: #FEF2F2; border-left: 3px solid #FCA5A5; padding: 13px 17px; border-radius: 0 8px 8px 0; color: var(--red); }

  /* ── Pricing ─────────────────────────────────────────────────────────── */
  .pricing-intro {
    background: var(--card-bg); border: 1px solid var(--border);
    border-radius: 16px; padding: 24px 28px; margin-bottom: 24px;
    display: flex; align-items: center; gap: 20px;
    box-shadow: 0 1px 16px rgba(15,23,42,.06);
  }
  .pricing-intro-text h2 { font-size: 1.05rem; font-weight: 800; color: var(--primary); margin-bottom: 6px; }
  .pricing-intro-text p  { font-size: .84rem; color: var(--muted); line-height: 1.55; }
  .pricing-big-icon { width: 110px; height: 110px; flex-shrink: 0; }

  .pricing-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(220px,1fr)); gap: 14px; }
  .pricing-card {
    background: var(--card-bg); border: 1px solid var(--border);
    border-radius: 16px; padding: 22px;
    display: flex; flex-direction: column;
    transition: transform .15s, box-shadow .15s, border-color .15s;
    box-shadow: 0 1px 12px rgba(15,23,42,.06);
  }
  .pricing-card:hover {
    transform: translateY(-3px);
    box-shadow: 0 8px 32px rgba(91,61,246,.1);
    border-color: rgba(91,61,246,.2);
  }
  .pricing-card.featured {
    border-color: rgba(91,61,246,.3);
    box-shadow: 0 4px 24px rgba(91,61,246,.12);
  }
  .pricing-icon-row  { display: flex; align-items: center; gap: 10px; margin-bottom: 14px; }
  .pricing-icon      { width: 56px; height: 56px; }
  .pricing-mult      { font-size: 1.4rem; font-weight: 900; color: var(--primary); line-height: 1; }
  .pricing-tier-name { font-size: .72rem; font-weight: 700; text-transform: uppercase; letter-spacing: .08em; color: var(--muted); margin-bottom: 6px; }
  .pricing-price     { font-size: 2rem; font-weight: 900; color: var(--text); line-height: 1; margin-bottom: 3px; }
  .pricing-per       { font-size: .76rem; color: var(--muted); margin-bottom: 4px; }
  .pricing-disc      { font-size: .7rem; font-weight: 700; color: var(--green); margin-bottom: 14px; }
  .pricing-disc.none { color: var(--muted); }
  .pricing-desc      { font-size: .81rem; color: var(--muted); line-height: 1.55; flex: 1; margin-bottom: 16px; }
  .pricing-cta {
    display: block; text-align: center;
    background: var(--primary); color: #fff;
    border: none; border-radius: 10px; padding: 10px;
    font-weight: 700; font-size: .84rem;
    text-decoration: none; cursor: pointer;
    transition: background .15s, box-shadow .15s;
    box-shadow: 0 2px 10px rgba(91,61,246,.25);
  }
  .pricing-cta:hover { background: var(--primary-dark); box-shadow: 0 4px 16px rgba(91,61,246,.35); }
  .pricing-cta.contact {
    background: var(--bg); color: var(--teal);
    border: 1px solid rgba(20,184,166,.2); box-shadow: none;
  }
  .pricing-cta.contact:hover { background: #CCFBF1; }
  .coming-soon-note {
    text-align: center; margin-top: 28px;
    background: #FFFBEB; border: 1px dashed #FDE68A;
    border-radius: 12px; padding: 16px 20px;
    font-size: .82rem; color: var(--muted); line-height: 1.55;
  }
  .coming-soon-note strong { color: var(--amber); }

  /* ── History ─────────────────────────────────────────────────────────── */
  .history-empty { text-align: center; color: var(--muted); padding: 40px; font-size: .88rem; }
  .history-item {
    background: var(--card-bg); border: 1px solid var(--border);
    border-radius: 12px; padding: 15px 20px; margin-bottom: 10px;
    display: flex; align-items: center; gap: 16px;
    box-shadow: 0 1px 8px rgba(15,23,42,.05);
    transition: border-color .15s;
  }
  .history-item:hover { border-color: rgba(91,61,246,.2); }
  .h-main { flex: 1; min-width: 0; }
  .h-op   { font-weight: 700; font-size: .9rem; color: var(--text); }
  .h-site { font-size: .74rem; color: var(--muted); margin-top: 1px; }
  .h-ts   { font-size: .7rem; color: var(--dim); margin-top: 2px; }
  .h-stats { display: flex; gap: 12px; }
  .h-stat  { font-size: .74rem; font-weight: 700; }
  .h-stat.flags { color: var(--red); }
  .h-stat.vf    { color: var(--green); }
  .h-actions { display: flex; gap: 6px; flex-shrink: 0; }

  /* ── Account ─────────────────────────────────────────────────────────── */
  .account-wrap { max-width: 420px; margin: 0 auto; }
  .coming-soon-badge {
    display: inline-block; font-size: .68rem; font-weight: 700;
    text-transform: uppercase; letter-spacing: .08em;
    padding: 2px 8px; border-radius: 8px;
    background: #FEF3C7; border: 1px solid #FDE68A; color: var(--amber);
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
    background: var(--lavender); border: 1px dashed rgba(91,61,246,.2);
    border-radius: 10px; font-size: .8rem; color: var(--muted); line-height: 1.5;
  }

  /* ── Session stats counter ──────────────────────────────────────────── */
  .stats-counter {
    display: grid; grid-template-columns: repeat(3,1fr);
    gap: 10px; margin-bottom: 22px;
  }
  .sc-box {
    background: var(--card-bg); border: 1px solid var(--border);
    border-radius: 12px; padding: 14px; text-align: center;
    box-shadow: 0 1px 8px rgba(15,23,42,.04);
  }
  .sc-num { font-size: 1.55rem; font-weight: 900; line-height: 1; font-variant-numeric: tabular-nums; }
  .sc-lbl { font-size: .62rem; color: var(--muted); margin-top: 4px; text-transform: uppercase; letter-spacing: .07em; }
  .sc-box.sc-checks .sc-num { color: var(--text); }
  .sc-box.sc-flags  .sc-num { color: var(--red); }
  .sc-box.sc-fines  .sc-num { color: var(--green); }

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
  .ps-active  .ps-num { background: var(--lavender); color: var(--primary); border: 1px solid rgba(91,61,246,.3); animation: pulse 1.4s ease-in-out infinite; }
  .ps-done    .ps-num { background: #DCFCE7; color: #16A34A; border: 1px solid #86EFAC; }

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
    background: linear-gradient(90deg, var(--primary-dark) 0%, var(--primary) 50%, var(--primary-dark) 100%);
    background-size: 200% 100%;
    animation: shimmer 1.6s ease-in-out infinite;
  }
  @keyframes shimmer { 0%,100%{background-position:0% 50%} 50%{background-position:100% 50%} }
  .ps-done .ps-fill { width: 100%; background: var(--green); }

  /* ── About page ──────────────────────────────────────────────────────── */
  .about-hero {
    text-align: center; padding: 40px 28px; position: relative; overflow: hidden;
    background: linear-gradient(135deg, #F8FAFC 0%, #EEF2FF 100%);
  }
  .about-tagline {
    font-size: 1.3rem; font-weight: 900; line-height: 1.35;
    color: var(--primary); margin-bottom: 30px;
  }
  .about-penalty-num {
    font-size: 4rem; font-weight: 900; line-height: 1; font-variant-numeric: tabular-nums;
    color: var(--red);
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
    border-radius: 0 8px 8px 0; color: var(--text);
    display: flex; align-items: flex-start; gap: 9px; line-height: 1.45;
  }
  .feature-list li {
    font-size: .86rem; padding: 8px 14px;
    background: #F0FDF4; border-left: 3px solid #86EFAC;
    border-radius: 0 8px 8px 0; color: var(--text);
    display: flex; align-items: flex-start; gap: 9px; line-height: 1.45;
  }
  .consequence-list li {
    font-size: .86rem; padding: 8px 14px;
    background: #FFFBEB; border-left: 3px solid #FCD34D;
    border-radius: 0 8px 8px 0; color: var(--text);
    display: flex; align-items: flex-start; gap: 9px; line-height: 1.45;
  }
  .audience-grid {
    display: grid; grid-template-columns: repeat(auto-fit, minmax(186px,1fr)); gap: 10px;
  }
  .audience-card {
    font-size: .84rem; padding: 14px 16px;
    background: #F0FDFA; border: 1px solid #CCFBF1;
    border-radius: 12px; color: var(--text); line-height: 1.45;
    display: flex; align-items: flex-start; gap: 10px;
  }
  .certbridge-badge {
    display: inline-block; font-size: .7rem; font-weight: 700; text-transform: uppercase;
    letter-spacing: .08em; padding: 3px 10px; border-radius: 8px; margin-bottom: 12px;
    background: #CCFBF1; border: 1px solid #99F6E4; color: var(--teal);
  }
  .about-bottom-line {
    font-size: 1.02rem; font-weight: 800; color: var(--primary);
    line-height: 1.45; text-align: center; margin-bottom: 12px;
  }
  .about-bottom-sub { font-size: .86rem; color: var(--muted); text-align: center; line-height: 1.6; }
  .cta-btn {
    display: inline-block; margin-top: 22px;
    background: var(--primary); color: #fff;
    border: none; border-radius: 10px;
    padding: 12px 32px; font-size: .92rem; font-weight: 700;
    text-decoration: none;
    transition: background .15s, box-shadow .15s;
    box-shadow: 0 2px 12px rgba(91,61,246,.3);
  }
  .cta-btn:hover { background: var(--primary-dark); box-shadow: 0 4px 20px rgba(91,61,246,.4); }

  /* ── Site footer ─────────────────────────────────────────────────────── */
  .site-footer { background: var(--text); color: rgba(255,255,255,.7); padding: 40px 0 0; }
  .site-footer-inner {
    max-width: 1100px; margin: 0 auto; padding: 0 24px 32px;
    display: grid; grid-template-columns: 2fr 1fr 1fr; gap: 40px;
  }
  .footer-brand-name { font-size: 1rem; font-weight: 800; color: #fff; margin-bottom: 6px; }
  .footer-brand-desc { font-size: .82rem; line-height: 1.6; color: rgba(255,255,255,.5); margin-bottom: 12px; }
  .footer-disclaimer {
    font-size: .73rem; color: rgba(255,255,255,.35); line-height: 1.55;
    padding: 10px 14px; background: rgba(255,255,255,.04);
    border: 1px solid rgba(255,255,255,.07); border-radius: 8px; margin-top: 14px;
  }
  .footer-col h4 { font-size: .72rem; font-weight: 700; text-transform: uppercase; letter-spacing: .1em; color: rgba(255,255,255,.4); margin-bottom: 12px; }
  .footer-col a  { display: block; font-size: .84rem; color: rgba(255,255,255,.6); text-decoration: none; margin-bottom: 8px; transition: color .15s; }
  .footer-col a:hover { color: #fff; }
  .footer-bottom {
    max-width: 1100px; margin: 0 auto; padding: 18px 24px;
    border-top: 1px solid rgba(255,255,255,.07);
    display: flex; align-items: center; justify-content: space-between; gap: 16px; flex-wrap: wrap;
  }
  .footer-bottom-text { font-size: .76rem; color: rgba(255,255,255,.35); }

  /* ── Mobile ──────────────────────────────────────────────────────────── */
  @media (max-width: 768px) {
    header { padding: 10px 16px; gap: 8px; grid-template-columns: 1fr auto; }
    .header-nav { display: none; }
    .header-wordmark { font-size: .88rem; }
    .header-logo-icon { width: 36px; height: 36px; }
    .header-icon { width: 36px; height: 36px; }
    .header-cta-btn { padding: 8px 14px; font-size: .8rem; }
    .nav-user-email { font-size: .75rem; max-width: 110px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .site-footer-inner { grid-template-columns: 1fr; gap: 28px; }
    .page-main { padding: 0 16px; margin-top: 24px; }
    .card { padding: 18px 16px; }
  }

  /* ── Auth modal ──────────────────────────────────────────────────────── */
  .modal-overlay {
    position: fixed; inset: 0; background: rgba(15,23,42,.55); z-index: 9000;
    display: flex; align-items: center; justify-content: center;
    backdrop-filter: blur(4px);
  }
  .modal-card {
    background: var(--surface); border-radius: 18px; padding: 32px 28px;
    width: 100%; max-width: 420px; position: relative;
    box-shadow: 0 20px 60px rgba(0,0,0,.18); border: 1px solid var(--border);
  }
  .modal-close {
    position: absolute; top: 14px; right: 16px; background: none; border: none;
    font-size: 1.4rem; color: var(--muted); cursor: pointer; line-height: 1;
  }
  .modal-close:hover { color: var(--text); }
  .modal-title { font-size: 1.1rem; font-weight: 800; color: var(--text); margin-bottom: 20px; }
  .auth-tabs { display: flex; border-bottom: 2px solid var(--border); margin-bottom: 20px; }
  .auth-tab {
    flex: 1; text-align: center; padding: 9px; font-size: .88rem; font-weight: 600;
    color: var(--muted); cursor: pointer; border-bottom: 3px solid transparent;
    margin-bottom: -2px; transition: color .15s, border-color .15s;
  }
  .auth-tab.active { color: var(--primary); border-color: var(--primary); }
  .auth-msg {
    font-size: .82rem; padding: 10px 14px; border-radius: 8px; margin-bottom: 14px;
  }
  .auth-msg.error   { background: #FEF2F2; border: 1px solid #FCA5A5; color: var(--red); }
  .auth-msg.success { background: #F0FDF4; border: 1px solid #86EFAC; color: #16A34A; }
  .nav-user-email {
    font-size: .82rem; font-weight: 600; color: var(--primary);
    padding: 5px 10px; background: var(--lavender); border-radius: 8px;
  }
  .nav-signout {
    font-size: .78rem; background: none; border: 1px solid var(--border);
    border-radius: 7px; padding: 4px 10px; color: var(--muted); cursor: pointer;
    transition: color .15s, border-color .15s;
  }
  .nav-signout:hover { color: var(--red); border-color: var(--red); }

  /* ── Payment gate / teaser ───────────────────────────────────────────── */
  .gate-teaser {
    display: grid; grid-template-columns: repeat(4, 1fr); gap: 8px; margin-bottom: 28px;
  }
  @media (max-width: 600px) { .gate-teaser { grid-template-columns: repeat(2, 1fr); } }
  .gate-stat {
    padding: 16px 10px; border-radius: 12px; text-align: center;
    background: var(--lavender); border: 1px solid var(--border);
  }
  .gate-stat .g-num { font-size: 2rem; font-weight: 900; color: var(--text); line-height: 1; }
  .gate-stat .g-lbl { font-size: .72rem; color: var(--muted); margin-top: 4px; }
  .gate-stat.red-stat { background: #FEF2F2; border-color: #FCA5A5; }
  .gate-stat.red-stat .g-num { color: var(--red); }
  .gate-divider {
    border: none; border-top: 1px solid var(--border); margin: 0 0 24px;
  }
  .gate-wrap { text-align: center; padding: 8px 0 28px; }
  .gate-lock { font-size: 2.2rem; margin-bottom: 12px; }
  .gate-title { font-size: 1.15rem; font-weight: 800; color: var(--text); margin-bottom: 8px; }
  .gate-sub {
    font-size: .88rem; color: var(--muted); margin-bottom: 24px;
    line-height: 1.6; max-width: 360px; margin-left: auto; margin-right: auto;
  }
  .gate-actions { display: flex; gap: 10px; justify-content: center; flex-wrap: wrap; }
  .gate-btn-primary {
    background: var(--primary); color: #fff; border: none; border-radius: 10px;
    padding: 11px 24px; font-size: .9rem; font-weight: 700; cursor: pointer;
    transition: background .15s;
  }
  .gate-btn-primary:hover { background: var(--primary-dark); }
  .gate-btn-secondary {
    background: none; color: var(--primary); border: 1.5px solid var(--primary);
    border-radius: 10px; padding: 10px 22px; font-size: .9rem; font-weight: 600;
    cursor: pointer; text-decoration: none; transition: background .15s;
  }
  .gate-btn-secondary:hover { background: var(--lavender); }
  .gate-meta-block {
    background: var(--lavender); border-radius: 12px; padding: 16px 20px;
    margin-bottom: 24px; text-align: left; font-size: .84rem;
  }
  .gate-meta-block strong { color: var(--text); }
  .gate-meta-block span   { color: var(--muted); }

  /* ── Glass button (matches icon aesthetic) — global ─────────────────── */
  .btn-glass {
    background: linear-gradient(160deg, #F4EEFF 0%, #D8C4F8 42%, #B898EC 100%);
    color: #4A1D96;
    border: 1.5px solid rgba(144,96,216,0.30);
    border-radius: 12px;
    padding: 12px 28px;
    font-size: .9rem; font-weight: 700;
    cursor: pointer; width: 100%;
    box-shadow: 0 2px 14px rgba(120,70,220,0.20),
                inset 0 1px 0 rgba(255,255,255,0.72);
    transition: transform .15s, box-shadow .15s, background .15s;
    text-decoration: none; display: block; text-align: center;
    letter-spacing: -.01em;
  }
  .btn-glass:hover {
    background: linear-gradient(160deg, #EDE5FF 0%, #CCB6F6 42%, #A888E0 100%);
    box-shadow: 0 4px 22px rgba(120,70,220,0.32),
                inset 0 1px 0 rgba(255,255,255,0.65);
    transform: translateY(-1px);
  }
  .btn-glass:active { transform: translateY(0); }
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
  <link rel="icon" type="image/png" href="/static/favicon.png?v=4">
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800;900&display=swap" rel="stylesheet">
  <style>{{ css | safe }}</style>
</head>
<body>
<header>
  <a href="/" class="header-logo">
    <img src="/static/icon.png" class="header-logo-icon" alt="Organic Web Checker">
    <span class="header-wordmark">Organic Web Checker</span>
  </a>
  <nav class="header-nav">
    <a href="/"        class="nav-link {{ 'active' if active == 'home'    else '' }}">Product</a>
    <a href="/about"   class="nav-link {{ 'active' if active == 'about'   else '' }}">How It Works</a>
    <a href="/pricing" class="nav-link {{ 'active' if active == 'pricing' else '' }}">Pricing</a>
    <a href="/history" class="nav-link {{ 'active' if active == 'history' else '' }}">History</a>
    <a href="/agents"  class="nav-link {{ 'active' if active == 'agents'  else '' }}">API</a>
  </nav>
  <div class="header-right">
    <div id="navUserArea">
      {% if user_email %}
        <span class="nav-user-email">{{ user_email }}</span>
        {% if user_is_admin %}<span style="font-size:.75rem;color:var(--muted)">Admin</span>{% else %}<span style="font-size:.75rem;color:var(--muted)">{{ user_credits }} credit{{ 's' if user_credits != 1 else '' }}</span>{% endif %}
        <button class="nav-signout" onclick="doLogout()">Sign Out</button>
      {% endif %}
    </div>
    <a href="/#run-check" class="header-cta-btn">Run a Check</a>
    <div class="header-icon-wrap">
      <button class="header-icon-btn" id="iconBtn" onclick="toggleDd(event)">
        <img src="/static/icon.png" class="header-icon" alt="">
      </button>
      <div class="header-dropdown" id="hDd">
        <a class="dropdown-item" href="/account">Account</a>
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
  if(email){
    const credTxt=credits>=99999?'Admin':(credits+' credit'+(credits!==1?'s':''));
    el.innerHTML='<span class="nav-user-email">'+email+'</span>&nbsp;<span style="font-size:.75rem;color:var(--muted)">'+credTxt+'</span>&nbsp;<button class="nav-signout" onclick="doLogout()">Sign Out</button>';
  }else{
    el.innerHTML='';
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
{% else %}

  <div class="report-header">
    <div>
      <div class="report-op-name">{{ report.operation }}</div>
      <div class="report-meta-sub">{{ report.certifier }} &middot; {{ report.status }} &middot; {{ report.location }}</div>
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
    <div class="meta-item" style="grid-column:span 2"><label>Website</label><span><a href="{{ report.website_url }}" target="_blank" rel="noopener" style="color:var(--cyan);text-decoration:none;border-bottom:1px solid rgba(0,229,204,.3)">{{ report.website_url }}</a></span></div>
  </div>

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
      <div class="num" style="font-size:1.5rem;color:#ff8c00">{{ report.marketing | length }}</div>
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
      Specific products labeled <em>organic</em> on the website but <strong>NOT found on the current OID certificate</strong>.<br>
      <span style="font-size:.7rem;opacity:.7">Ref: 7 CFR &sect;&nbsp;205.307 (misrepresentation &amp; fraud) &bull; &sect;&nbsp;205.303&ndash;305 (labeling)</span>
    </p>
    <ul class="product-list">
      {% for item in report.flagged | sort(attribute='title') %}
        <li class="flag-item">
          &#9888;
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

  {# ── 🟡 Caution — name variations ─────────────────────────── #}
  {% if report.caution %}
    <div class="section-label amber" style="margin-top:22px">
      &#128993; Caution &mdash; Name Variation
      <span class="badge">{{ report.caution | length }}</span>
    </div>
    <p style="font-size:.76rem;color:var(--muted);margin-bottom:10px;line-height:1.55">
      Products that closely resemble certified items but may have name differences (e.g.&nbsp;<em>Flax Oil</em> vs&nbsp;<em>Flaxseed Oil</em>).
      Not an NOP violation &mdash; requires certifier review for alignment.<br>
      <span style="font-size:.7rem;opacity:.7">Sub-category: name variation &bull; possible mismatch &bull; certifier judgment required</span>
    </p>
    <ul class="product-list scrollable-list">
      {% for item in report.caution | sort(attribute='title') %}
        <li class="caution-item">
          <span class="caution-icon">&#126;</span>
          {% if item.url %}
            <a href="{{ item.url }}" target="_blank" rel="noopener" style="color:var(--amber);text-decoration:none;border-bottom:1px solid rgba(255,208,96,.3)">{{ item.title }}</a>
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
            <a href="{{ item.url }}" target="_blank" rel="noopener" style="color:#ff8c00;text-decoration:none;border-bottom:1px solid rgba(255,140,0,.3)">{{ item.title }}</a>
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
          <a href="{{ item.url }}" target="_blank" rel="noopener" style="color:var(--neon);text-decoration:none">{{ item.title }}</a>
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
  <link rel="icon" type="image/png" href="/static/favicon.png?v=4">
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800;900&display=swap" rel="stylesheet">
  <style>""" + GLOBAL_CSS + """
  /* ── Landing page extras ─────────────────────────────────────────────── */
  .report-title {
    font-size: .73rem; font-weight: 700; text-transform: uppercase;
    letter-spacing: .1em; color: var(--muted); margin-bottom: 22px;
    padding-bottom: 13px; border-bottom: 1px solid var(--border);
  }
  .landing-wrap { max-width: 1100px; margin: 0 auto; padding: 0 24px; }

  /* Hero */
  .hero-section {
    padding: 72px 0 64px;
    background: linear-gradient(180deg, #F8FAFC 0%, #EEF2FF 100%);
    border-bottom: 1px solid var(--border);
  }
  .hero-inner {
    max-width: 1100px; margin: 0 auto; padding: 0 24px;
    display: grid; grid-template-columns: 1fr 1fr; gap: 56px; align-items: center;
  }
  .hero-eyebrow {
    display: inline-flex; align-items: center; gap: 7px;
    background: var(--lavender); color: var(--primary);
    border: 1px solid rgba(91,61,246,.15);
    border-radius: 20px; padding: 5px 14px;
    font-size: .74rem; font-weight: 700; letter-spacing: .02em; margin-bottom: 20px;
  }
  .hero-eyebrow-dot { width: 6px; height: 6px; border-radius: 50%; background: var(--green); flex-shrink: 0; }
  .hero-h1 {
    font-size: 2.55rem; font-weight: 900; line-height: 1.18; letter-spacing: -.03em;
    color: var(--text); margin-bottom: 20px;
  }
  .hero-h1 em { font-style: normal; color: var(--primary); }
  .hero-sub {
    font-size: 1.05rem; color: var(--muted); line-height: 1.7;
    margin-bottom: 32px; max-width: 480px;
  }
  .hero-ctas { display: flex; gap: 12px; flex-wrap: wrap; margin-bottom: 28px; }
  .hero-btn-primary {
    background: var(--primary); color: #fff;
    border: none; border-radius: 10px; padding: 13px 28px;
    font-size: .94rem; font-weight: 700; text-decoration: none; cursor: pointer;
    box-shadow: 0 4px 16px rgba(91,61,246,.35);
    transition: background .15s, box-shadow .15s, transform .1s; display: inline-block;
  }
  .hero-btn-primary:hover { background: var(--primary-dark); box-shadow: 0 6px 24px rgba(91,61,246,.45); transform: translateY(-1px); }
  .hero-btn-secondary {
    background: var(--surface); color: var(--primary);
    border: 1.5px solid rgba(91,61,246,.2); border-radius: 10px; padding: 13px 26px;
    font-size: .94rem; font-weight: 600; text-decoration: none; cursor: pointer;
    transition: background .15s, border-color .15s, transform .1s; display: inline-block;
  }
  .hero-btn-secondary:hover { background: var(--lavender); border-color: rgba(91,61,246,.35); transform: translateY(-1px); }
  .hero-trust { display: flex; flex-wrap: wrap; gap: 16px; font-size: .81rem; color: var(--muted); }
  .hero-trust-item { display: flex; align-items: center; gap: 6px; }
  .hero-trust-check { color: var(--green); font-weight: 700; }

  /* Hero mock card */
  .hero-mock-card {
    background: var(--surface); border: 1px solid var(--border);
    border-radius: 20px; padding: 26px;
    box-shadow: 0 20px 60px rgba(91,61,246,.12), 0 2px 12px rgba(15,23,42,.08);
  }
  .hero-card-header {
    font-size: .72rem; font-weight: 700; text-transform: uppercase;
    letter-spacing: .09em; color: var(--muted); margin-bottom: 18px;
    padding-bottom: 14px; border-bottom: 1px solid var(--border);
    display: flex; align-items: center; gap: 8px;
  }
  .hero-card-dot { width: 8px; height: 8px; border-radius: 50%; background: var(--green); }
  .mock-row {
    display: flex; align-items: center; justify-content: space-between;
    padding: 11px 0; border-bottom: 1px solid var(--border);
  }
  .mock-row:last-of-type { border-bottom: none; }
  .mock-row-label { font-size: .86rem; color: var(--text); font-weight: 500; }
  .chip { font-size: .72rem; font-weight: 700; padding: 4px 11px; border-radius: 20px; border: 1px solid; }
  .chip-green   { background: #DCFCE7; color: #16A34A; border-color: #86EFAC; }
  .chip-amber   { background: #FEF3C7; color: #D97706; border-color: #FDE68A; }
  .chip-red     { background: #FEE2E2; color: #DC2626; border-color: #FCA5A5; }
  .chip-neutral { background: var(--lavender); color: var(--primary); border-color: rgba(91,61,246,.2); }
  .mock-divider { font-size: .7rem; font-weight: 700; text-transform: uppercase; letter-spacing: .08em; color: var(--dim); margin: 14px 0 10px; }
  .mock-flag-item {
    font-size: .82rem; padding: 8px 12px;
    background: #FEF2F2; border-left: 3px solid #FCA5A5;
    border-radius: 0 8px 8px 0; margin-bottom: 5px; color: var(--text);
  }
  .mock-ok-item {
    font-size: .82rem; padding: 8px 12px;
    background: #F0FDF4; border-left: 3px solid #86EFAC;
    border-radius: 0 8px 8px 0; color: var(--text);
  }
  .hero-card-footer {
    margin-top: 16px; padding-top: 14px; border-top: 1px solid var(--border);
    font-size: .72rem; color: var(--dim); text-align: center;
  }

  /* Trust bar */
  .trust-bar { background: var(--surface); border-bottom: 1px solid var(--border); padding: 22px 0; }
  .trust-bar-inner {
    max-width: 1100px; margin: 0 auto; padding: 0 24px;
    display: flex; align-items: center; flex-wrap: wrap; justify-content: center; gap: 8px;
  }
  .trust-bar-label { font-size: .78rem; font-weight: 600; color: var(--muted); margin-right: 24px; white-space: nowrap; }
  .trust-bar-items { display: flex; gap: 28px; flex-wrap: wrap; justify-content: center; }
  .trust-bar-item  { font-size: .82rem; color: var(--muted); display: flex; align-items: center; gap: 8px; font-weight: 500; }
  .trust-bar-icon  {
    width: 28px; height: 28px; border-radius: 8px;
    background: var(--lavender); display: flex; align-items: center; justify-content: center;
    font-size: 13px; flex-shrink: 0;
  }

  /* Features */
  .features-section { padding: 72px 0; background: var(--bg); }
  .section-header   { text-align: center; margin-bottom: 48px; }
  .section-label-sm {
    display: inline-block; font-size: .72rem; font-weight: 700; text-transform: uppercase;
    letter-spacing: .1em; color: var(--primary); margin-bottom: 12px;
  }
  .section-h2 {
    font-size: 1.85rem; font-weight: 900; color: var(--text);
    letter-spacing: -.02em; line-height: 1.2; margin-bottom: 14px;
  }
  .section-sub { font-size: 1rem; color: var(--muted); line-height: 1.6; max-width: 520px; margin: 0 auto; }
  .features-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(230px,1fr)); gap: 20px; }
  .feature-card {
    background: var(--surface); border: 1px solid var(--border);
    border-radius: 16px; padding: 26px 24px;
    transition: transform .15s, box-shadow .15s, border-color .15s;
    box-shadow: 0 1px 8px rgba(15,23,42,.04);
  }
  .feature-card:hover { transform: translateY(-3px); box-shadow: 0 8px 32px rgba(91,61,246,.09); border-color: rgba(91,61,246,.15); }
  .feature-icon {
    width: 42px; height: 42px; border-radius: 12px;
    background: var(--lavender); display: flex; align-items: center; justify-content: center;
    font-size: 20px; margin-bottom: 16px;
  }
  .feature-card-title { font-size: .95rem; font-weight: 800; color: var(--text); margin-bottom: 8px; }
  .feature-card-desc  { font-size: .84rem; color: var(--muted); line-height: 1.6; }

  /* How it works */
  .hiw-section {
    padding: 72px 0;
    background: linear-gradient(180deg, #F8FAFC 0%, #EEF2FF 100%);
    border-top: 1px solid var(--border); border-bottom: 1px solid var(--border);
  }
  .hiw-steps { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px,1fr)); gap: 0; }
  .hiw-step  { padding: 24px 28px 24px 0; }
  .hiw-step-num {
    width: 36px; height: 36px; border-radius: 50%;
    background: var(--primary); color: #fff;
    display: flex; align-items: center; justify-content: center;
    font-size: .84rem; font-weight: 800; margin-bottom: 14px;
  }
  .hiw-step-title { font-size: .95rem; font-weight: 800; color: var(--text); margin-bottom: 6px; }
  .hiw-step-desc  { font-size: .84rem; color: var(--muted); line-height: 1.6; }

  /* Sample output */
  .sample-section { padding: 72px 0; background: var(--bg); }
  .sample-inner {
    max-width: 1100px; margin: 0 auto; padding: 0 24px;
    display: grid; grid-template-columns: 1fr 1.2fr; gap: 56px; align-items: start;
  }
  .sample-left { padding-top: 12px; }
  .sample-left p { font-size: .92rem; color: var(--muted); line-height: 1.7; margin-top: 14px; }
  .sample-report {
    background: var(--surface); border: 1px solid var(--border);
    border-radius: 20px; overflow: hidden; box-shadow: 0 8px 40px rgba(15,23,42,.08);
  }
  .sample-report-header {
    background: var(--bg); padding: 16px 22px; border-bottom: 1px solid var(--border);
    display: flex; align-items: center; justify-content: space-between;
  }
  .sample-op-name { font-size: .92rem; font-weight: 800; color: var(--text); }
  .sample-op-meta { font-size: .72rem; color: var(--muted); margin-top: 2px; }
  .sample-summary { display: grid; grid-template-columns: repeat(3,1fr); border-bottom: 1px solid var(--border); }
  .sample-stat { padding: 16px 18px; text-align: center; border-right: 1px solid var(--border); }
  .sample-stat:last-child { border-right: none; }
  .sample-stat-num { font-size: 1.5rem; font-weight: 900; line-height: 1; font-variant-numeric: tabular-nums; }
  .sample-stat-lbl { font-size: .65rem; color: var(--muted); text-transform: uppercase; letter-spacing: .07em; margin-top: 4px; }
  .sample-stat.red   .sample-stat-num { color: var(--red); }
  .sample-stat.amber .sample-stat-num { color: var(--amber); }
  .sample-stat.green .sample-stat-num { color: var(--green); }
  .sample-items { padding: 16px 22px; }
  .sample-section-head { font-size: .66rem; font-weight: 700; text-transform: uppercase; letter-spacing: .1em; color: var(--muted); margin-bottom: 8px; }
  .sample-flag-item {
    font-size: .84rem; padding: 9px 13px; background: #FEF2F2; border-left: 3px solid #FCA5A5;
    border-radius: 0 8px 8px 0; margin-bottom: 5px; color: var(--text);
  }
  .sample-ok-item {
    font-size: .84rem; padding: 9px 13px; background: #F0FDF4; border-left: 3px solid #86EFAC;
    border-radius: 0 8px 8px 0; margin-bottom: 5px; color: var(--text);
  }
  .sample-report-footer {
    padding: 12px 22px; background: var(--bg); border-top: 1px solid var(--border);
    font-size: .72rem; color: var(--dim); text-align: center;
  }

  /* AI Transparency */
  .ai-section {
    padding: 72px 0; background: var(--surface);
    border-top: 1px solid var(--border); border-bottom: 1px solid var(--border);
  }
  .ai-inner {
    max-width: 1100px; margin: 0 auto; padding: 0 24px;
    display: grid; grid-template-columns: 1fr 1fr; gap: 56px; align-items: center;
  }
  .ai-cards { display: grid; gap: 14px; }
  .ai-card  { background: var(--bg); border: 1px solid var(--border); border-radius: 14px; padding: 20px 22px; }
  .ai-card-icon  { font-size: 22px; margin-bottom: 10px; }
  .ai-card-title { font-size: .9rem; font-weight: 800; color: var(--text); margin-bottom: 6px; }
  .ai-card-desc  { font-size: .83rem; color: var(--muted); line-height: 1.6; }

  /* Run-check section */
  .run-check-section { padding: 72px 0; background: var(--bg); }
  .run-check-inner   { max-width: 640px; margin: 0 auto; padding: 0 24px; }

  /* ── Mobile (landing page) ───────────────────────────────────────────── */
  @media (max-width: 768px) {
    .hero-inner   { grid-template-columns: 1fr; gap: 32px; padding: 0 20px; }
    .hero-h1      { font-size: 2rem; }
    .hero-right   { display: none; }
    .hero-section { padding: 48px 0 40px; }
    .hero-sub     { font-size: .96rem; max-width: 100%; }
    .hero-ctas    { flex-direction: column; }
    .hero-btn-primary, .hero-btn-secondary { text-align: center; }

    .trust-bar-inner { flex-direction: column; align-items: flex-start; gap: 12px; padding: 0 20px; }
    .trust-bar-label { margin-right: 0; }
    .trust-bar-items { gap: 12px; }

    .features-section, .hiw-section, .sample-section,
    .ai-section, .run-check-section { padding: 48px 0; }
    .features-grid { grid-template-columns: 1fr; gap: 14px; }
    .feature-card  { padding: 20px 18px; }

    .hiw-inner  { padding: 0 20px; }
    .hiw-steps  { grid-template-columns: 1fr; gap: 0; }
    .hiw-step   { padding: 20px 0; border-bottom: 1px solid var(--border); }
    .hiw-step:last-child { border-bottom: none; }

    .sample-inner { grid-template-columns: 1fr; gap: 28px; padding: 0 20px; }
    .sample-summary { grid-template-columns: repeat(3,1fr); }

    .ai-inner  { grid-template-columns: 1fr; gap: 28px; padding: 0 20px; }
    .ai-cards  { gap: 10px; }

    .section-inner, .landing-wrap { padding: 0 20px; }
    .section-h2 { font-size: 1.65rem; }

    .run-check-inner { padding: 0 20px; }
    .check-form-card { padding: 20px 16px; }

    .queue-panel { margin: 0 0 16px; }
    .meta-grid   { grid-template-columns: 1fr 1fr; }

    .pricing-grid { grid-template-columns: 1fr; }
  }

  @media (max-width: 480px) {
    .hero-h1     { font-size: 1.75rem; }
    .section-h2  { font-size: 1.4rem; }
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
    <img src="/static/icon.png" class="header-logo-icon" alt="Organic Web Checker">
    <span class="header-wordmark">Organic Web Checker</span>
  </a>
  <nav class="header-nav">
    <a href="/"        class="nav-link active">Product</a>
    <a href="/about"   class="nav-link">How It Works</a>
    <a href="/pricing" class="nav-link">Pricing</a>
    <a href="/history" class="nav-link">History</a>
    <a href="/agents"  class="nav-link">API</a>
  </nav>
  <div class="header-right">
    <div id="navUserArea">
      {% if user_email %}
        <span class="nav-user-email">{{ user_email }}</span>
        {% if user_is_admin %}<span style="font-size:.75rem;color:var(--muted)">Admin</span>{% else %}<span style="font-size:.75rem;color:var(--muted)">{{ user_credits }} credit{{ 's' if user_credits != 1 else '' }}</span>{% endif %}
        <button class="nav-signout" onclick="doLogout()">Sign Out</button>
      {% endif %}
    </div>
    <a href="#run-check" class="header-cta-btn">Run a Check</a>
    <div class="header-icon-wrap">
      <button class="header-icon-btn" id="iconBtn" onclick="toggleDd(event)">
        <img src="/static/icon.png" class="header-icon" alt="">
      </button>
      <div class="header-dropdown" id="hDd">
        <a class="dropdown-item" href="/account">Account</a>
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
        <a href="#run-check" class="hero-btn-primary">Run a Check</a>
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
      <span class="trust-bar-item"><span class="trust-bar-icon">&#127807;</span> Organic handlers &amp; brands</span>
      <span class="trust-bar-item"><span class="trust-bar-icon">&#128269;</span> Certifying agents</span>
      <span class="trust-bar-item"><span class="trust-bar-icon">&#128101;</span> Compliance consultants</span>
      <span class="trust-bar-item"><span class="trust-bar-icon">&#127991;</span> Private label operations</span>
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
        <div class="feature-icon">&#128269;</div>
        <div class="feature-card-title">Website Claim Review</div>
        <div class="feature-card-desc">Scans product pages on Shopify, WooCommerce, BigCommerce, and most product websites for organic claims.</div>
      </div>
      <div class="feature-card">
        <div class="feature-icon">&#128196;</div>
        <div class="feature-card-title">Certificate Comparison</div>
        <div class="feature-card-desc">Pulls the live certificate directly from the USDA Organic Integrity Database and compares it against what&rsquo;s published online.</div>
      </div>
      <div class="feature-card">
        <div class="feature-icon">&#9888;</div>
        <div class="feature-card-title">Compliance Flagging</div>
        <div class="feature-card-desc">Surfaces products that may not appear on the current certificate scope &mdash; flagged, caution, and marketing language categories.</div>
      </div>
      <div class="feature-card">
        <div class="feature-icon">&#128203;</div>
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
        <div class="ai-card-icon">&#129302;</div>
        <div class="ai-card-title">AI assists the review</div>
        <div class="ai-card-desc">Automated scanning and matching identifies potential gaps between what&rsquo;s marketed as organic and what&rsquo;s on the current OID certificate.</div>
      </div>
      <div class="ai-card">
        <div class="ai-card-icon">&#128100;</div>
        <div class="ai-card-title">Humans make the call</div>
        <div class="ai-card-desc">Flagged items require human review. Name variations, reformulations, and context all require certifier judgment &mdash; not automation.</div>
      </div>
      <div class="ai-card">
        <div class="ai-card-icon">&#128203;</div>
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
        <label for="website">Website URL</label>
        <input type="text" id="website" name="website"
               placeholder="e.g. https://greenridgeorganics.com"
               value="{{ prefill_url or '' }}" required>
        <div class="hint">Supports Shopify, WooCommerce, BigCommerce, and most product websites</div>
        <button type="submit" id="submitBtn">Run Check</button>
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
      <div class="footer-brand-name">Organic Web Checker</div>
      <div class="footer-brand-desc">AI-assisted organic compliance review for handlers, certifiers, and compliance teams. Compares website claims against live USDA OID certificate data.</div>
      <div class="footer-disclaimer">
        Not a certifying agent. Results are decision-support only and require human review.<br>
        Not a substitute for a qualified certifier&rsquo;s judgment. Regulatory reference: 7 CFR Part 205 &mdash; USDA National Organic Program.
      </div>
    </div>
    <div class="footer-col">
      <h4>Product</h4>
      <a href="#run-check">Run a Check</a>
      <a href="/pricing">Pricing</a>
      <a href="/history">History</a>
      <a href="/agents">Agents &amp; API</a>
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
    btn.disabled = false; btn.textContent = 'Run Check';
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
      return '<li class="queue-item"><div><div class="op-name">' + j.operation + '</div><div class="site">' + j.website + '</div></div>' + pill + viewBtn + '</li>';
    }).join('');
  }

  async function showJob(jobId) { viewingJobId = jobId; await loadResult(jobId); }

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
  if(email){
    const credTxt=credits>=99999?'Admin':(credits+' credit'+(credits!==1?'s':''));
    el.innerHTML='<span class="nav-user-email">'+email+'</span>&nbsp;<span style="font-size:.75rem;color:var(--muted)">'+credTxt+'</span>&nbsp;<button class="nav-signout" onclick="doLogout()">Sign Out</button>';
  }else{
    el.innerHTML='';
  }
}
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Pricing page
# ---------------------------------------------------------------------------

TIERS = [
    {"count": 1,   "price": 4.99, "per": 4.99, "disc": None,    "name": "Spot Check",     "desc": "Run one check on your operation's website. Great for a first self-review, a quick verification before a certification renewal, or a one-off product claim audit.", "featured": False},
    {"count": 10,  "price": 39,   "per": 3.90, "disc": "22% off","name": "Small Operation","desc": "Ten checks for handlers and brands doing periodic self-audits, reviewing seasonal product updates, or spot-checking specific sections of a product catalog.", "featured": False},
    {"count": 25,  "price": 85,   "per": 3.40, "disc": "32% off","name": "Active Brand",   "desc": "Regular compliance reviews for growing operations. Ideal for brands with ongoing product changes, and for certifiers or consultants doing structured check-ins across a handful of clients.", "featured": True},
    {"count": 50,  "price": 149,  "per": 2.98, "disc": "40% off","name": "Full Audit",     "desc": "Comprehensive coverage for established organic brands. Run full catalog sweeps, pre-certification reviews, or systematic checks across multiple site sections and product lines.", "featured": False},
    {"count": 100, "price": 259,  "per": 2.59, "disc": "48% off","name": "High Volume",    "desc": "For large operations, multi-brand portfolios, and certifiers running systematic reviews across a full client roster. Covers quarterly review cycles at scale.", "featured": False},
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
        <div class="pricing-card">
          <div class="pricing-icon-row">
            <img src="/static/icon.png" class="pricing-icon" alt="">
            <div class="pricing-mult" style="color:var(--cyan);text-shadow:0 0 14px rgba(0,229,204,.4)">&infin;</div>
          </div>
          <div class="pricing-tier-name">Enterprise / Agency</div>
          <div class="pricing-price" style="font-size:1.4rem;color:var(--cyan)">Custom</div>
          <div class="pricing-per">Volume pricing</div>
          <div class="pricing-disc" style="color:var(--cyan)">50%+ off base rate</div>
          <div class="pricing-desc">For agencies and large certifiers running 500+ checks per year. Custom contract, volume discount, and priority support.</div>
          <a href="mailto:hello@organicwebchecker.com" class="pricing-cta contact">Contact Us</a>
        </div>"""

    return f"""
    <div class="pricing-intro">
      <img src="/static/icon.png" class="pricing-big-icon" alt="Organic Web Checker">
      <div class="pricing-intro-text">
        <h2>Checkers are organic web checks</h2>
        <p>Each checker runs a live comparison of a client&rsquo;s website against their current USDA Organic Integrity Database certificate &mdash; flagging products marketed as organic that aren&rsquo;t on the cert. Credits never expire.</p>
      </div>
    </div>
    <div class="pricing-grid">
      {cards}
      {custom}
    </div>
    <script>
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
        alert('Network error — please try again.');
        if (btn) {{ btn.textContent = 'Purchase'; btn.style.opacity = ''; btn.style.pointerEvents = ''; }}
      }}
      return false;
    }}
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
    <div class="card" style="max-width:480px;margin:0 auto">
      <div style="font-size:.82rem;color:var(--muted);margin-bottom:6px">Signed in as</div>
      <div id="acctEmail" style="font-size:1rem;font-weight:700;color:var(--text);margin-bottom:18px"></div>
      <div style="display:flex;gap:12px;align-items:center;padding:16px;background:var(--lavender);border-radius:12px;margin-bottom:20px">
        <div>
          <div style="font-size:.75rem;color:var(--muted)">Checker credits</div>
          <div id="acctCredits" style="font-size:1.5rem;font-weight:900;color:var(--primary)">—</div>
        </div>
        <a href="/pricing" style="margin-left:auto;font-size:.85rem;font-weight:600;color:var(--primary);text-decoration:none;background:#fff;padding:8px 16px;border-radius:8px;border:1px solid var(--border)">Buy more &rarr;</a>
      </div>
      <button onclick="acctDoLogout()" style="background:none;border:1px solid var(--border);border-radius:8px;padding:8px 16px;color:var(--muted);font-size:.84rem;cursor:pointer;transition:color .15s,border-color .15s" onmouseover="this.style.color='var(--red)';this.style.borderColor='var(--red)'" onmouseout="this.style.color='var(--muted)';this.style.borderColor='var(--border)'">Sign Out</button>
    </div>
  </div>
</div>
<script>
async function acctInit(){
  const res=await fetch('/api/user');
  const d=await res.json();
  if(d.logged_in){
    document.getElementById('acctLoggedOut').style.display='none';
    document.getElementById('acctLoggedIn').style.display='';
    document.getElementById('acctEmail').textContent=d.email;
    document.getElementById('acctCredits').textContent=d.is_admin?'Unlimited (Admin)':(d.credits+' credit'+(d.credits!==1?'s':''));
  }
}
acctInit();
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
    <select style="background:rgba(0,0,0,.4);border:1px solid rgba(0,255,127,.14);border-radius:6px;padding:6px 10px;color:var(--text);font-size:.84rem;margin-left:16px">
      <option>.md (Markdown)</option>
      <option>PDF (Print)</option>
    </select>
  </div>
  <div class="setting-row">
    <div class="setting-info">
      <div class="setting-label">API Access</div>
      <div class="setting-desc">Generate an API key to run checkers programmatically. Requires account.</div>
    </div>
    <button style="font-size:.76rem;padding:5px 12px;background:rgba(0,229,204,.06);border:1px solid rgba(0,229,204,.18);border-radius:6px;color:var(--cyan);cursor:pointer;margin-left:16px" onclick="alert('API access coming soon with account launch.')">Generate Key</button>
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
  Have a feature you&rsquo;d like to see? <a href="mailto:hello@organicwebchecker.com" style="color:var(--cyan);text-decoration:none">Email us</a>.
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
    Each <strong style="color:var(--neon)">checker</strong> is one automated web check. Enter an operation name and website URL. The checker pulls the live OID certificate, scans every product page, and instantly flags any organic claim that doesn&rsquo;t match the cert.
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
    if not website.startswith('http'):
        website = 'https://' + website

    job_id = uuid.uuid4().hex[:8]
    with jobs_lock:
        jobs[job_id] = {
            'id': job_id, 'operation': operation, 'website': website,
            'status': 'queued', 'report': None,
            'submitted_at': datetime.now(timezone.utc).isoformat(),
            'finished_at': None,
            'unlocked': False,
        }

    thread = threading.Thread(target=_run_job, args=(job_id, operation, website), daemon=True)
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
    if not job:
        return 'Not found', 404
    if job['status'] not in ('done', 'error'):
        return 'Not ready', 202
    report = job.get('report', {})
    if 'error' in report:
        return render_template_string(REPORT_PARTIAL, report=report, job_id=job_id)

    # ── Gate check ──────────────────────────────────────────────────────────
    email = get_logged_in_email()
    already_unlocked = job.get('unlocked', False)

    if already_unlocked or is_admin(email):
        return render_template_string(REPORT_PARTIAL, report=report, job_id=job_id)

    if email and get_user_credits(email) > 0:
        deduct_user_credit(email)
        with jobs_lock:
            if job_id in jobs:
                jobs[job_id]['unlocked'] = True
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
                            INSERT INTO purchases (token, stripe_session_id, tier_name, credits_purchased, amount_paid_cents)
                            VALUES (%s, %s, %s, %s, %s)
                            ON CONFLICT (stripe_session_id) DO NOTHING
                        """, (ref, stripe_session_id, tier_name, credits, amount_cents))
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
  <div style="font-size:1.7rem;font-weight:900;color:var(--neon);margin-bottom:10px">Payment confirmed</div>
  <div style="color:var(--text);font-size:1.05rem;margin-bottom:6px">
    {f'<strong>{credits} checker{"s" if credits != 1 else ""}</strong> ({tier_name}) added to your account.' if credits else 'Your purchase was successful.'}
  </div>
  <div style="color:var(--muted);font-size:.82rem;margin-bottom:32px">
    Credits are tied to this browser. Sign in with an account (coming soon) to access them anywhere.
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
    with jobs_lock:
        done_jobs = [
            {k: v for k, v in j.items() if k != 'report'}
            for j in jobs.values()
            if j['status'] in ('done', 'error')
        ]
    done_jobs.sort(key=lambda x: x.get('finished_at') or x['submitted_at'], reverse=True)

    if not done_jobs:
        body = '<div class="page-title">Web Check History</div><div class="page-subtitle">Completed checkers appear here. History resets when the server restarts &mdash; <a href="/account" style="color:var(--neon);text-decoration:none">sign in</a> to save permanently.</div><div class="card"><div class="history-empty">No completed checkers yet. <a href="/" style="color:var(--neon);text-decoration:none">Run your first checker &rarr;</a></div></div>'
    else:
        rows = ""
        for j in done_jobs:
            ts = j.get('finished_at', j['submitted_at'])[:19].replace('T', ' ') + ' UTC'
            status_color = 'var(--neon)' if j['status'] == 'done' else 'var(--red)'
            rows += f"""
            <div class="history-item">
              <div class="h-main">
                <div class="h-op">{j['operation']}</div>
                <div class="h-site">{j['website']}</div>
                <div class="h-ts">{ts} &middot; <span style="color:{status_color}">{j['status']}</span></div>
              </div>
              <div class="h-actions">
                <a class="view-btn" href="/job/{j['id']}/download/pdf" target="_blank">PDF</a>
                <a class="view-btn" href="/job/{j['id']}/download/md" style="color:var(--cyan);border-color:rgba(0,229,204,.2)">&#8595; .md</a>
              </div>
            </div>"""
        body = f'<div class="page-title">Web Check History</div><div class="page-subtitle">Completed checkers from this session. <a href="/account" style="color:var(--neon);text-decoration:none">Sign in</a> to save history permanently.</div>{rows}'

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
    padding: 12px 0; border-bottom: 1px solid rgba(0,255,127,.06);
    font-size: .82rem;
  }
  .api-endpoint:last-child { border-bottom: none; }
  .api-method {
    font-size: .65rem; font-weight: 800; text-transform: uppercase;
    letter-spacing: .08em; padding: 3px 7px; border-radius: 4px;
    text-align: center; width: fit-content;
  }
  .api-method.post { background: rgba(255,208,96,.12); color: var(--amber); border: 1px solid rgba(255,208,96,.2); }
  .api-method.get  { background: rgba(0,255,127,.07);  color: var(--neon);  border: 1px solid rgba(0,255,127,.15); }
  .api-path { font-family: monospace; font-size: .82rem; color: var(--cyan); padding-top: 2px; }
  .api-desc { color: var(--muted); line-height: 1.5; }
  .api-desc code { background: rgba(0,255,127,.07); color: var(--neon); padding: 1px 5px; border-radius: 3px; font-size: .78rem; }
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


@app.route('/static/<path:filename>')
def static_files(filename):
    return send_from_directory('static', filename)


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(debug=False, host='0.0.0.0', port=port)
