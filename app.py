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
from checker import run_check
import stripe
import psycopg2

app = Flask(__name__)
app.secret_key = os.environ.get('FLASK_SECRET_KEY', 'dev-secret-change-in-prod')

# ---------------------------------------------------------------------------
# Stripe config
# ---------------------------------------------------------------------------

stripe.api_key         = os.environ.get('STRIPE_SECRET_KEY', '')
STRIPE_PK              = os.environ.get('STRIPE_PUBLISHABLE_KEY', '')
STRIPE_WH_SECRET       = os.environ.get('STRIPE_WEBHOOK_SECRET', '')
DATABASE_URL           = os.environ.get('DATABASE_URL', '')
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
        conn.commit()

try:
    init_db()
except Exception as _db_err:
    print(f'[WARN] DB init skipped: {_db_err}')

def get_session_token():
    if 'token' not in session:
        session['token'] = uuid.uuid4().hex
    return session['token']

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
  :root {
    --bg:        #060e0a;
    --surface:   #0c1e12;
    --card-bg:   rgba(10, 28, 16, 0.92);
    --border:    rgba(0, 255, 127, 0.10);
    --glow:      rgba(0, 255, 127, 0.07);
    --neon:      #00ff7f;
    --neon-dim:  #00c860;
    --neon-dark: #006830;
    --cyan:      #00e5cc;
    --amber:     #ffd060;
    --red:       #ff4455;
    --red-glow:  rgba(255, 68, 85, 0.12);
    --text:      #cce8d0;
    --muted:     #8ab89a;
    --dim:       #2a4a32;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  html { scroll-behavior: smooth; }
  body {
    font-family: system-ui, -apple-system, sans-serif;
    background: var(--bg); color: var(--text);
    min-height: 100vh;
  }
  body::before {
    content: '';
    position: fixed; inset: 0; pointer-events: none; z-index: 0;
    background-image:
      linear-gradient(rgba(0,255,127,.018) 1px, transparent 1px),
      linear-gradient(90deg, rgba(0,255,127,.018) 1px, transparent 1px);
    background-size: 48px 48px;
  }

  /* ── Header ─────────────────────────────────────────────────────────── */
  header {
    background: linear-gradient(135deg, #030a06 0%, #0a1f12 60%, #060e08 100%);
    border-bottom: 1px solid rgba(0,255,127,.10);
    box-shadow: 0 2px 60px rgba(0,255,127,.06);
    padding: 18px 32px;
    display: flex; align-items: center; justify-content: space-between; gap: 20px;
    position: relative; z-index: 10;
  }
  header h1 {
    font-size: 1.25rem; font-weight: 800; letter-spacing: .04em;
    color: var(--neon);
    text-shadow: 0 0 24px rgba(0,255,127,.55), 0 0 60px rgba(0,255,127,.18);
  }
  header p { font-size: .8rem; color: var(--muted); margin-top: 3px; }

  .header-right { display: flex; align-items: center; gap: 10px; }

  .header-nav { display: flex; gap: 2px; }
  .nav-link {
    color: var(--muted); font-size: .78rem; text-decoration: none;
    padding: 6px 11px; border-radius: 6px;
    transition: color .18s, background .18s;
  }
  .nav-link:hover, .nav-link.active { color: var(--neon); background: rgba(0,255,127,.06); }

  .header-icon-wrap { position: relative; flex-shrink: 0; }
  .header-icon-btn {
    background: none; border: 1px solid transparent; cursor: pointer;
    padding: 5px; border-radius: 9px; display: block;
    transition: border-color .2s, box-shadow .2s;
  }
  .header-icon-btn:hover {
    border-color: rgba(0,255,127,.28);
    box-shadow: 0 0 14px rgba(0,255,127,.15);
  }
  .header-icon { width: 46px; height: 46px; display: block; }
  .header-dropdown {
    position: absolute; top: calc(100% + 8px); right: 0;
    background: #0c2018; border: 1px solid rgba(0,255,127,.14);
    border-radius: 10px;
    box-shadow: 0 8px 40px rgba(0,0,0,.55), 0 0 24px rgba(0,255,127,.07);
    min-width: 190px; z-index: 100; display: none; overflow: hidden;
  }
  .header-dropdown.open { display: block; }
  .dropdown-item {
    display: block; padding: 11px 17px;
    font-size: .84rem; color: var(--text);
    text-decoration: none; border-bottom: 1px solid rgba(0,255,127,.06);
  }
  .dropdown-item:last-child { border-bottom: none; }
  .dropdown-item:hover { background: rgba(0,255,127,.07); color: var(--neon); }

  /* ── Layout ──────────────────────────────────────────────────────────── */
  .page-main { max-width: 920px; margin: 36px auto; padding: 0 24px; position: relative; z-index: 1; }

  /* ── Cards ───────────────────────────────────────────────────────────── */
  .card {
    background: var(--card-bg);
    border: 1px solid var(--border);
    border-radius: 12px; padding: 26px 30px;
    box-shadow: 0 0 40px var(--glow), inset 0 1px 0 rgba(0,255,127,.06);
    margin-bottom: 22px;
  }

  /* ── Page headers ────────────────────────────────────────────────────── */
  .page-title {
    font-size: 1.35rem; font-weight: 800; color: var(--neon);
    text-shadow: 0 0 20px rgba(0,255,127,.35); margin-bottom: 6px;
  }
  .page-subtitle { font-size: .85rem; color: var(--muted); margin-bottom: 28px; }

  /* ── Form ────────────────────────────────────────────────────────────── */
  label {
    display: block; font-size: .78rem; font-weight: 700; margin-bottom: 6px;
    color: var(--muted); text-transform: uppercase; letter-spacing: .07em;
  }
  input[type=text], input[type=email], input[type=password] {
    width: 100%; padding: 11px 14px;
    background: rgba(0,0,0,.45); border: 1px solid rgba(0,255,127,.14);
    border-radius: 8px; font-size: .94rem; margin-bottom: 16px; color: var(--text);
    transition: border-color .2s, box-shadow .2s;
  }
  input[type=text]::placeholder,
  input[type=email]::placeholder,
  input[type=password]::placeholder { color: var(--dim); }
  input:focus {
    outline: none; border-color: rgba(0,255,127,.38);
    box-shadow: 0 0 0 3px rgba(0,255,127,.07), 0 0 18px rgba(0,255,127,.09);
  }
  .hint { font-size: .76rem; color: var(--muted); margin-top: -10px; margin-bottom: 16px; }

  button[type=submit], .btn-primary {
    background: var(--neon-dark); color: #c8ffd8;
    border: 1px solid rgba(0,255,127,.28); border-radius: 8px;
    padding: 11px 28px; font-size: .9rem;
    cursor: pointer; font-weight: 800; letter-spacing: .04em;
    transition: background .2s, box-shadow .2s, color .2s;
    box-shadow: 0 0 20px rgba(0,255,127,.14);
  }
  button[type=submit]:hover, .btn-primary:hover {
    background: var(--neon-dim); color: #030a06;
    box-shadow: 0 0 30px rgba(0,255,127,.35);
  }
  button[type=submit]:disabled { background: #0e2218; color: var(--dim); box-shadow: none; cursor: default; }

  /* ── Queue panel ─────────────────────────────────────────────────────── */
  .queue-panel {
    background: var(--card-bg); border: 1px solid var(--border);
    border-radius: 12px; margin-bottom: 22px; overflow: hidden;
    box-shadow: 0 0 40px var(--glow);
  }
  .queue-header {
    padding: 12px 20px;
    background: rgba(0,0,0,.3); border-bottom: 1px solid rgba(0,255,127,.07);
    font-size: .72rem; font-weight: 800; text-transform: uppercase;
    letter-spacing: .09em; color: var(--muted);
    display: flex; align-items: center; gap: 8px;
  }
  .queue-count {
    background: rgba(0,255,127,.12); color: var(--neon);
    border: 1px solid rgba(0,255,127,.2); border-radius: 10px;
    padding: 1px 8px; font-size: .7rem;
  }
  .queue-list { list-style: none; }
  .queue-item {
    padding: 12px 20px; border-bottom: 1px solid rgba(0,255,127,.04);
    display: flex; align-items: center; gap: 12px; font-size: .875rem;
  }
  .queue-item:last-child { border-bottom: none; }
  .op-name { font-weight: 700; flex: 1; min-width: 0; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .site { font-size: .76rem; color: var(--muted); }

  .status-pill {
    font-size: .68rem; font-weight: 800; padding: 3px 9px;
    border-radius: 10px; white-space: nowrap; flex-shrink: 0; border: 1px solid;
  }
  .status-queued  { background: rgba(255,255,255,.03); color: var(--muted); border-color: rgba(255,255,255,.08); }
  .status-running { background: rgba(255,208,96,.08); color: var(--amber); border-color: rgba(255,208,96,.18); animation: pulse 1.4s ease-in-out infinite; }
  .status-done    { background: rgba(0,255,127,.07); color: var(--neon); border-color: rgba(0,255,127,.18); }
  .status-error   { background: rgba(255,68,85,.07); color: var(--red); border-color: rgba(255,68,85,.18); }
  @keyframes pulse { 0%,100% { opacity:1; } 50% { opacity:.45; } }

  /* Animated checker loader */
  .checker-loader {
    position: relative; width: 40px; height: 40px; flex-shrink: 0;
    border-radius: 4px; overflow: hidden;
    background: repeating-conic-gradient(#001a0a 0% 25%, rgba(0,255,127,.1) 0% 50%) 0 0 / 8px 8px;
    border: 1px solid rgba(0,255,127,.18);
    box-shadow: 0 0 10px rgba(0,255,127,.15);
  }
  .cb-p {
    position: absolute; width: 6px; height: 6px; border-radius: 50%;
    background: #ff4455; box-shadow: 0 0 7px rgba(255,68,85,.9);
  }
  .cb-p.p1 { animation: cb1 2s ease-in-out infinite; }
  .cb-p.p2 { animation: cb2 2s ease-in-out infinite .67s; }
  .cb-p.p3 { animation: cb3 2s ease-in-out infinite 1.33s; }
  @keyframes cb1 { 0%,100%{top:1px;left:9px}  50%{top:17px;left:25px} }
  @keyframes cb2 { 0%,100%{top:9px;left:33px} 50%{top:25px;left:17px} }
  @keyframes cb3 { 0%,100%{top:25px;left:1px} 50%{top:9px;left:17px} }

  .view-btn {
    font-size: .73rem; padding: 4px 11px;
    border: 1px solid rgba(0,255,127,.18); border-radius: 6px;
    color: var(--neon); background: rgba(0,255,127,.05);
    cursor: pointer; flex-shrink: 0; text-decoration: none;
    transition: background .18s, box-shadow .18s;
  }
  .view-btn:hover { background: rgba(0,255,127,.12); box-shadow: 0 0 10px rgba(0,255,127,.18); }

  .empty-queue { padding: 22px; text-align: center; color: var(--muted); font-size: .84rem; }

  /* ── Report ──────────────────────────────────────────────────────────── */
  .report-header {
    display: flex; align-items: flex-start; justify-content: space-between;
    gap: 16px; margin-bottom: 22px;
    padding-bottom: 14px; border-bottom: 1px solid rgba(0,255,127,.08);
  }
  .report-op-name { font-size: 1rem; font-weight: 800; color: var(--neon); }
  .report-meta-sub { font-size: .74rem; color: var(--muted); margin-top: 2px; }
  .download-btns { display: flex; gap: 7px; flex-shrink: 0; }
  .dl-btn {
    font-size: .7rem; padding: 5px 12px; border-radius: 6px;
    text-decoration: none; font-weight: 800; border: 1px solid;
    transition: background .18s, box-shadow .18s; white-space: nowrap;
  }
  .dl-btn.md  { color: var(--cyan); border-color: rgba(0,229,204,.22); background: rgba(0,229,204,.05); }
  .dl-btn.md:hover  { background: rgba(0,229,204,.12); box-shadow: 0 0 10px rgba(0,229,204,.18); }
  .dl-btn.pdf { color: var(--amber); border-color: rgba(255,208,96,.22); background: rgba(255,208,96,.05); }
  .dl-btn.pdf:hover { background: rgba(255,208,96,.12); box-shadow: 0 0 10px rgba(255,208,96,.18); }

  .meta-grid {
    display: grid; grid-template-columns: 1fr 1fr;
    gap: 10px 28px; margin-bottom: 22px;
  }
  .meta-item label { color: var(--muted); font-size: .7rem; text-transform: uppercase; letter-spacing: .06em; margin-bottom: 2px; }
  .meta-item span { font-size: .9rem; font-weight: 600; color: var(--text); }

  .stats {
    display: grid; grid-template-columns: repeat(4,1fr);
    gap: 10px; margin-bottom: 26px;
  }
  .stat {
    background: rgba(0,0,0,.35); border: 1px solid rgba(0,255,127,.07);
    border-radius: 10px; padding: 14px; text-align: center;
  }
  .stat .num { font-size: 1.9rem; font-weight: 900; line-height: 1; font-variant-numeric: tabular-nums; }
  .stat .lbl { font-size: .66rem; color: var(--muted); margin-top: 5px; text-transform: uppercase; letter-spacing: .06em; }
  .stat.flagged  { border-color: rgba(255,68,85,.18); background: rgba(255,68,85,.04); }
  .stat.flagged  .num { color: var(--red); text-shadow: 0 0 20px rgba(255,68,85,.5); }
  .stat.verified .num { color: var(--neon); text-shadow: 0 0 18px rgba(0,255,127,.4); }

  .section-label {
    font-size: .7rem; font-weight: 800; text-transform: uppercase; letter-spacing: .1em;
    margin-bottom: 10px; margin-top: 24px;
    display: flex; align-items: center; gap: 8px;
  }
  .section-label:first-of-type { margin-top: 0; }
  .section-label.red   { color: var(--red); }
  .section-label.green { color: var(--neon); }
  .section-label.cyan  { color: var(--cyan); }
  .badge {
    font-size: .66rem; padding: 1px 7px; border-radius: 8px; font-weight: 700; border: 1px solid;
  }
  .red    .badge { background: rgba(255,68,85,.08); border-color: rgba(255,68,85,.2); color: var(--red); }
  .green  .badge { background: rgba(0,255,127,.07); border-color: rgba(0,255,127,.15); color: var(--neon); }
  .cyan   .badge { background: rgba(0,229,204,.07); border-color: rgba(0,229,204,.15); color: var(--cyan); }
  .amber  .badge { background: rgba(255,208,96,.07); border-color: rgba(255,208,96,.18); color: var(--amber); }
  .orange .badge { background: rgba(255,140,0,.07); border-color: rgba(255,140,0,.18); color: #ff8c00; }
  .section-label.amber  { color: var(--amber); }
  .section-label.orange { color: #ff8c00; }

  .product-list { list-style: none; display: grid; gap: 4px; }
  .product-list li {
    padding: 8px 13px; border-radius: 0 6px 6px 0;
    font-size: .86rem; display: flex; align-items: center; gap: 9px;
  }
  .product-list li.flag-item {
    background: rgba(255,68,85,.05); border-left: 3px solid var(--red);
    box-shadow: inset 0 0 14px rgba(255,68,85,.06);
  }
  .product-list li.ok-item {
    background: rgba(0,255,127,.025); border-left: 3px solid rgba(0,255,127,.2);
  }
  .product-list li.cert-item {
    background: rgba(0,229,204,.025); border-left: 3px solid rgba(0,229,204,.18);
    color: var(--muted);
  }
  .product-list li.caution-item {
    background: rgba(255,208,96,.04); border-left: 3px solid rgba(255,208,96,.3);
    box-shadow: inset 0 0 12px rgba(255,208,96,.05);
  }
  .product-list li.marketing-item {
    background: rgba(255,140,0,.04); border-left: 3px solid rgba(255,140,0,.28);
  }
  .caution-icon  { color: var(--amber); flex-shrink: 0; }
  .marketing-icon { color: #ff8c00; flex-shrink: 0; }
  .product-list a { color: var(--red); font-weight: 600; text-decoration: none; border-bottom: 1px solid rgba(255,68,85,.3); }
  .product-list a:hover { border-bottom-color: var(--red); }
  .product-list .no-link { color: var(--text); }
  .verify-btn {
    margin-left: auto; flex-shrink: 0; font-size: .7rem;
    border: 1px solid rgba(255,68,85,.28); border-radius: 4px;
    padding: 2px 7px; color: var(--red); text-decoration: none;
    background: none; white-space: nowrap;
  }
  .verify-btn:hover { background: rgba(255,68,85,.08); }

  .scrollable-list { max-height: 340px; overflow-y: auto; padding-right: 4px; }
  .scrollable-list::-webkit-scrollbar { width: 4px; }
  .scrollable-list::-webkit-scrollbar-track { background: rgba(0,255,127,.04); }
  .scrollable-list::-webkit-scrollbar-thumb { background: rgba(0,255,127,.18); border-radius: 2px; }

  .clean { color: var(--neon); font-weight: 700; padding: 14px 0; text-shadow: 0 0 14px rgba(0,255,127,.35); }
  .error-msg { background: rgba(255,68,85,.07); border-left: 3px solid var(--red); padding: 13px 17px; border-radius: 0 8px 8px 0; color: var(--red); }

  /* ── Pricing ─────────────────────────────────────────────────────────── */
  .pricing-intro {
    background: var(--card-bg); border: 1px solid var(--border);
    border-radius: 12px; padding: 24px 28px; margin-bottom: 24px;
    display: flex; align-items: center; gap: 20px;
    box-shadow: 0 0 40px var(--glow);
  }
  .pricing-intro-text h2 { font-size: 1.05rem; font-weight: 800; color: var(--neon); margin-bottom: 6px; }
  .pricing-intro-text p  { font-size: .84rem; color: var(--muted); line-height: 1.55; }
  .pricing-big-icon { width: 72px; height: 72px; flex-shrink: 0; filter: drop-shadow(0 0 10px rgba(0,255,127,.35)); }

  .pricing-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(230px,1fr)); gap: 14px; }
  .pricing-card {
    background: var(--card-bg); border: 1px solid var(--border);
    border-radius: 12px; padding: 22px;
    display: flex; flex-direction: column;
    transition: transform .18s, box-shadow .18s, border-color .18s;
    box-shadow: 0 0 30px rgba(0,0,0,.3);
  }
  .pricing-card:hover {
    transform: translateY(-3px);
    box-shadow: 0 8px 40px rgba(0,255,127,.09);
    border-color: rgba(0,255,127,.22);
  }
  .pricing-card.featured {
    border-color: rgba(0,255,127,.28);
    box-shadow: 0 0 35px rgba(0,255,127,.09);
  }
  .pricing-icon-row { display: flex; align-items: center; gap: 10px; margin-bottom: 14px; }
  .pricing-icon { width: 34px; height: 34px; }
  .pricing-mult {
    font-size: 1.5rem; font-weight: 900; color: var(--neon);
    text-shadow: 0 0 14px rgba(0,255,127,.4); line-height: 1;
  }
  .pricing-tier-name {
    font-size: .72rem; font-weight: 800; text-transform: uppercase;
    letter-spacing: .08em; color: var(--muted); margin-bottom: 6px;
  }
  .pricing-price { font-size: 2rem; font-weight: 900; color: var(--text); line-height: 1; margin-bottom: 3px; }
  .pricing-per   { font-size: .76rem; color: var(--muted); margin-bottom: 4px; }
  .pricing-disc  {
    font-size: .7rem; font-weight: 800; color: var(--neon);
    text-shadow: 0 0 8px rgba(0,255,127,.3); margin-bottom: 14px;
  }
  .pricing-disc.none { color: var(--muted); }
  .pricing-desc  { font-size: .81rem; color: var(--muted); line-height: 1.5; flex: 1; margin-bottom: 16px; }
  .pricing-cta {
    display: block; text-align: center;
    background: rgba(0,255,127,.07); border: 1px solid rgba(0,255,127,.18);
    border-radius: 8px; padding: 9px;
    color: var(--neon); font-weight: 800; font-size: .84rem;
    text-decoration: none; cursor: pointer;
    transition: background .18s, box-shadow .18s;
  }
  .pricing-cta:hover { background: rgba(0,255,127,.14); box-shadow: 0 0 18px rgba(0,255,127,.18); }
  .pricing-cta.contact { color: var(--cyan); border-color: rgba(0,229,204,.2); background: rgba(0,229,204,.05); }
  .pricing-cta.contact:hover { background: rgba(0,229,204,.12); box-shadow: 0 0 18px rgba(0,229,204,.15); }
  .coming-soon-note {
    text-align: center; margin-top: 28px;
    background: rgba(255,208,96,.05); border: 1px dashed rgba(255,208,96,.2);
    border-radius: 10px; padding: 16px 20px;
    font-size: .82rem; color: var(--muted); line-height: 1.55;
  }
  .coming-soon-note strong { color: var(--amber); }

  /* ── History ─────────────────────────────────────────────────────────── */
  .history-empty { text-align: center; color: var(--muted); padding: 40px; font-size: .88rem; }
  .history-item {
    background: var(--card-bg); border: 1px solid var(--border);
    border-radius: 10px; padding: 15px 20px; margin-bottom: 10px;
    display: flex; align-items: center; gap: 16px;
    box-shadow: 0 0 20px rgba(0,0,0,.3);
    transition: border-color .18s;
  }
  .history-item:hover { border-color: rgba(0,255,127,.2); }
  .h-main { flex: 1; min-width: 0; }
  .h-op   { font-weight: 700; font-size: .9rem; color: var(--text); }
  .h-site { font-size: .74rem; color: var(--muted); margin-top: 1px; }
  .h-ts   { font-size: .7rem; color: var(--dim); margin-top: 2px; }
  .h-stats { display: flex; gap: 12px; }
  .h-stat { font-size: .74rem; font-weight: 700; }
  .h-stat.flags { color: var(--red); }
  .h-stat.vf    { color: var(--neon); }
  .h-actions { display: flex; gap: 6px; flex-shrink: 0; }

  /* ── Account ─────────────────────────────────────────────────────────── */
  .account-wrap { max-width: 420px; margin: 0 auto; }
  .coming-soon-badge {
    display: inline-block; font-size: .68rem; font-weight: 800;
    text-transform: uppercase; letter-spacing: .08em;
    padding: 2px 8px; border-radius: 8px;
    background: rgba(255,208,96,.1); border: 1px solid rgba(255,208,96,.2); color: var(--amber);
  }
  .divider {
    text-align: center; color: var(--muted); font-size: .76rem;
    margin: 14px 0; display: flex; align-items: center; gap: 12px;
  }
  .divider::before, .divider::after { content:''; flex:1; height:1px; background: rgba(0,255,127,.07); }

  /* ── Settings ────────────────────────────────────────────────────────── */
  .setting-row {
    display: flex; align-items: center; justify-content: space-between;
    padding: 16px 0; border-bottom: 1px solid rgba(0,255,127,.05);
  }
  .setting-row:last-child { border-bottom: none; }
  .setting-info { flex: 1; }
  .setting-label { font-size: .88rem; font-weight: 700; color: var(--text); }
  .setting-desc  { font-size: .75rem; color: var(--muted); margin-top: 2px; }
  .toggle {
    width: 40px; height: 22px; background: rgba(0,0,0,.5);
    border: 1px solid rgba(0,255,127,.14); border-radius: 11px;
    cursor: pointer; position: relative; flex-shrink: 0; margin-left: 16px;
    transition: background .25s, border-color .25s;
  }
  .toggle.on { background: rgba(0,200,96,.22); border-color: rgba(0,255,127,.3); }
  .toggle::after {
    content:''; position: absolute; top: 2px; left: 2px;
    width: 16px; height: 16px; border-radius: 50%;
    background: var(--muted); transition: transform .25s, background .25s;
  }
  .toggle.on::after { transform: translateX(18px); background: var(--neon); }
  .settings-note {
    margin-top: 24px; padding: 14px 18px;
    background: rgba(0,229,204,.04); border: 1px dashed rgba(0,229,204,.15);
    border-radius: 8px; font-size: .8rem; color: var(--muted); line-height: 1.5;
  }

  /* ── Session stats counter ──────────────────────────────────────────── */
  .stats-counter {
    display: grid; grid-template-columns: repeat(3,1fr);
    gap: 10px; margin-bottom: 22px;
  }
  .sc-box {
    background: rgba(0,0,0,.38); border: 1px solid rgba(0,255,127,.07);
    border-radius: 10px; padding: 13px 14px; text-align: center;
  }
  .sc-num { font-size: 1.55rem; font-weight: 900; line-height: 1; font-variant-numeric: tabular-nums; }
  .sc-lbl { font-size: .62rem; color: var(--muted); margin-top: 4px; text-transform: uppercase; letter-spacing: .07em; }
  .sc-box.sc-checks .sc-num { color: var(--text); }
  .sc-box.sc-flags  .sc-num { color: var(--red);  text-shadow: 0 0 14px rgba(255,68,85,.4); }
  .sc-box.sc-fines  .sc-num { color: var(--neon); text-shadow: 0 0 16px rgba(0,255,127,.4); font-size: 1.15rem; }

  /* ── Below-form session totals strip ────────────────────────────────── */
  .form-stats {
    display: flex; justify-content: center; gap: 28px;
    margin-top: 10px; margin-bottom: 4px;
    font-size: .73rem; color: var(--muted);
    padding: 9px 14px;
  }
  .form-stat-item { display: flex; align-items: center; gap: 7px; }
  .form-stat-num  { font-variant-numeric: tabular-nums; font-weight: 700; color: var(--text); }
  .form-stat-num.red { color: var(--red); }

  /* ── Progress panel ──────────────────────────────────────────────────── */
  .ps-header {
    font-size: .72rem; font-weight: 800; text-transform: uppercase;
    letter-spacing: .09em; color: var(--muted); margin-bottom: 14px;
    display: flex; align-items: center; gap: 8px;
  }
  .ps-spin { display: inline-block; animation: spin 1.1s linear infinite; }
  @keyframes spin { to { transform: rotate(360deg); } }

  .progress-step {
    display: flex; align-items: center; gap: 14px;
    padding: 9px 0; border-bottom: 1px solid rgba(0,255,127,.05);
    transition: opacity .3s;
  }
  .progress-step:last-child { border-bottom: none; }

  .ps-num {
    width: 26px; height: 26px; border-radius: 50%; flex-shrink: 0;
    display: flex; align-items: center; justify-content: center;
    font-size: .72rem; font-weight: 800; transition: all .3s;
  }
  .ps-pending .ps-num { background: rgba(255,255,255,.04); color: var(--dim); border: 1px solid rgba(255,255,255,.07); }
  .ps-active  .ps-num { background: rgba(0,255,127,.1); color: var(--neon); border: 1px solid rgba(0,255,127,.3); box-shadow: 0 0 12px rgba(0,255,127,.25); animation: pulse 1.4s ease-in-out infinite; }
  .ps-done    .ps-num { background: rgba(0,255,127,.14); color: var(--neon); border: 1px solid rgba(0,255,127,.22); }

  .ps-info  { flex: 1; }
  .ps-name  { font-size: .83rem; font-weight: 600; transition: color .3s; }
  .ps-msg   { font-size: .72rem; color: var(--muted); margin-top: 2px; min-height: 14px; }
  .ps-pending .ps-name { color: var(--dim); }
  .ps-active  .ps-name { color: var(--neon); }
  .ps-done    .ps-name { color: var(--muted); }

  .ps-bar { width: 90px; height: 3px; background: rgba(255,255,255,.06); border-radius: 2px; overflow: hidden; flex-shrink: 0; }
  .ps-fill { height: 100%; border-radius: 2px; transition: width .6s ease, background .4s; }
  .ps-pending .ps-fill { width: 0; background: transparent; }
  .ps-active  .ps-fill {
    width: 65%;
    background: linear-gradient(90deg, var(--neon-dark) 0%, var(--neon) 50%, var(--neon-dark) 100%);
    background-size: 200% 100%;
    animation: shimmer 1.6s ease-in-out infinite;
  }
  @keyframes shimmer { 0%,100%{background-position:0% 50%} 50%{background-position:100% 50%} }
  .ps-done .ps-fill { width: 100%; background: var(--neon-dim); }

  /* ── Hero banner (main page) ─────────────────────────────────────────── */
  .hero-banner {
    background: rgba(255,68,85,.04); border: 1px solid rgba(255,68,85,.1);
    border-radius: 10px; padding: 14px 20px; margin-bottom: 22px;
    display: flex; align-items: center; gap: 16px; flex-wrap: wrap;
  }
  .hero-penalty {
    font-size: 1.1rem; font-weight: 900; color: var(--red);
    text-shadow: 0 0 16px rgba(255,68,85,.5); white-space: nowrap; flex-shrink: 0;
  }
  .hero-penalty span { font-size: .68rem; font-weight: 700; vertical-align: middle; margin-left: 2px; }
  .hero-text { font-size: .8rem; color: var(--muted); flex: 1; min-width: 180px; line-height: 1.5; }
  .hero-learn { font-size: .76rem; color: var(--neon); text-decoration: none; font-weight: 800; white-space: nowrap; flex-shrink: 0; }
  .hero-learn:hover { text-shadow: 0 0 10px rgba(0,255,127,.4); }

  /* ── About page ──────────────────────────────────────────────────────── */
  .about-hero {
    text-align: center; padding: 40px 28px; position: relative; overflow: hidden;
  }
  .about-hero::before {
    content:''; position: absolute; inset: 0; pointer-events: none;
    background: radial-gradient(ellipse at 50% 10%, rgba(255,68,85,.08) 0%, transparent 65%);
  }
  .about-tagline {
    font-size: 1.3rem; font-weight: 900; line-height: 1.35;
    color: var(--neon); text-shadow: 0 0 24px rgba(0,255,127,.4); margin-bottom: 30px;
  }
  .about-penalty-num {
    font-size: 4rem; font-weight: 900; line-height: 1; font-variant-numeric: tabular-nums;
    color: var(--red); text-shadow: 0 0 50px rgba(255,68,85,.65), 0 0 100px rgba(255,68,85,.2);
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
    background: rgba(255,68,85,.04); border-left: 3px solid rgba(255,68,85,.28);
    border-radius: 0 6px 6px 0; color: var(--text);
    display: flex; align-items: flex-start; gap: 9px; line-height: 1.45;
  }
  .feature-list li {
    font-size: .86rem; padding: 8px 14px;
    background: rgba(0,255,127,.03); border-left: 3px solid rgba(0,255,127,.2);
    border-radius: 0 6px 6px 0; color: var(--text);
    display: flex; align-items: flex-start; gap: 9px; line-height: 1.45;
  }
  .consequence-list li {
    font-size: .86rem; padding: 8px 14px;
    background: rgba(255,208,96,.03); border-left: 3px solid rgba(255,208,96,.2);
    border-radius: 0 6px 6px 0; color: var(--text);
    display: flex; align-items: flex-start; gap: 9px; line-height: 1.45;
  }
  .audience-grid {
    display: grid; grid-template-columns: repeat(auto-fit, minmax(186px,1fr)); gap: 10px;
  }
  .audience-card {
    font-size: .84rem; padding: 14px 16px;
    background: rgba(0,229,204,.04); border: 1px solid rgba(0,229,204,.1);
    border-radius: 9px; color: var(--text); line-height: 1.45;
    display: flex; align-items: flex-start; gap: 10px;
  }
  .certbridge-badge {
    display: inline-block; font-size: .7rem; font-weight: 800; text-transform: uppercase;
    letter-spacing: .08em; padding: 3px 10px; border-radius: 8px; margin-bottom: 12px;
    background: rgba(0,229,204,.08); border: 1px solid rgba(0,229,204,.2); color: var(--cyan);
  }
  .about-bottom-line {
    font-size: 1.02rem; font-weight: 800; color: var(--neon);
    text-shadow: 0 0 20px rgba(0,255,127,.35); line-height: 1.45;
    text-align: center; margin-bottom: 12px;
  }
  .about-bottom-sub { font-size: .86rem; color: var(--muted); text-align: center; line-height: 1.6; }
  .cta-btn {
    display: inline-block; margin-top: 22px;
    background: var(--neon-dark); color: #c8ffd8;
    border: 1px solid rgba(0,255,127,.28); border-radius: 8px;
    padding: 12px 32px; font-size: .92rem; font-weight: 800; letter-spacing: .04em;
    text-decoration: none;
    transition: background .2s, box-shadow .2s;
    box-shadow: 0 0 20px rgba(0,255,127,.14);
  }
  .cta-btn:hover { background: var(--neon-dim); color: #030a06; box-shadow: 0 0 30px rgba(0,255,127,.35); }
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
  <link rel="icon" type="image/svg+xml" href="/static/favicon.svg">
  <style>{{ css | safe }}</style>
</head>
<body>
<header>
  <div>
    <h1>Organic Web Checker</h1>
    <p>Compare products marketed as organic on a website against the USDA Organic Integrity Database certificate</p>
  </div>
  <div class="header-right">
    <nav class="header-nav">
      <a href="/"        class="nav-link {{ 'active' if active == 'home'    else '' }}">Run Checker</a>
      <a href="/about"   class="nav-link {{ 'active' if active == 'about'   else '' }}">About</a>
      <a href="/history" class="nav-link {{ 'active' if active == 'history' else '' }}">History</a>
      <a href="/pricing" class="nav-link {{ 'active' if active == 'pricing' else '' }}">Pricing</a>
      <a href="/agents"  class="nav-link {{ 'active' if active == 'agents'  else '' }}">Agents</a>
      <a href="/account" class="nav-link {{ 'active' if active == 'account' else '' }}">Account</a>
    </nav>
    <div class="header-icon-wrap">
      <button class="header-icon-btn" id="iconBtn" onclick="toggleDd(event)">
        <img src="/static/favicon.svg" class="header-icon" alt="">
      </button>
      <div class="header-dropdown" id="hDd">
        <a class="dropdown-item" href="/account">Account</a>
        <a class="dropdown-item" href="/history">Web Check History</a>
        <a class="dropdown-item" href="/pricing">Pricing Plan</a>
        <a class="dropdown-item" href="/agents">Agents &amp; API</a>
        <a class="dropdown-item" href="/settings">Settings</a>
      </div>
    </div>
  </div>
</header>
<main class="page-main">
  {{ body | safe }}
</main>
<script>
function toggleDd(e){e.stopPropagation();document.getElementById('hDd').classList.toggle('open');}
document.addEventListener('click',()=>{const d=document.getElementById('hDd');if(d)d.classList.remove('open');});
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


# ---------------------------------------------------------------------------
# Main page HTML (Jinja template — includes dynamic ACTIVE_JOB injection)
# ---------------------------------------------------------------------------

MAIN_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Organic Web Checker — Automatic Organic Compliance Audits</title>
  <meta name="description" content="Identify potential non-compliant organic claims on your website. Organic Web Checker compares your organic product listings against your live USDA Organic Integrity Database certificate for review.">
  <meta property="og:title" content="Organic Web Checker — Organic Compliance Review Tool">
  <meta property="og:description" content="Surface potential organic compliance gaps for review. Compares your website's organic product claims against your live USDA OID certificate.
  <link rel="icon" type="image/svg+xml" href="/static/favicon.svg">
  <style>""" + GLOBAL_CSS + """
    /* main-page report card title */
    .report-title {
      font-size: .73rem; font-weight: 800; text-transform: uppercase;
      letter-spacing: .1em; color: var(--muted); margin-bottom: 22px;
      padding-bottom: 13px; border-bottom: 1px solid rgba(0,255,127,.08);
    }
  </style>
</head>
<body>
<header>
  <div>
    <h1>Organic Web Checker</h1>
    <p>Compare products marketed as organic on a website against the USDA Organic Integrity Database certificate</p>
  </div>
  <div class="header-right">
    <nav class="header-nav">
      <a href="/"        class="nav-link active">Run Checker</a>
      <a href="/about"   class="nav-link">About</a>
      <a href="/history" class="nav-link">History</a>
      <a href="/pricing" class="nav-link">Pricing</a>
      <a href="/agents"  class="nav-link">Agents</a>
      <a href="/account" class="nav-link">Account</a>
    </nav>
    <div class="header-icon-wrap">
      <button class="header-icon-btn" id="iconBtn" onclick="toggleDd(event)">
        <img src="/static/favicon.svg" class="header-icon" alt="">
      </button>
      <div class="header-dropdown" id="hDd">
        <a class="dropdown-item" href="/account">Account</a>
        <a class="dropdown-item" href="/history">Web Check History</a>
        <a class="dropdown-item" href="/pricing">Pricing Plan</a>
        <a class="dropdown-item" href="/agents">Agents &amp; API</a>
        <a class="dropdown-item" href="/settings">Settings</a>
      </div>
    </div>
  </div>
</header>

<main class="page-main">
  <div class="hero-banner">
    <div class="hero-penalty">$22,974<span>&thinsp;/ violation</span></div>
    <div class="hero-text">Identify potential non-compliant organic claims for review &mdash; compares your website product listings against your live USDA Organic Integrity Database certificate.</div>
    <a href="/about" class="hero-learn">Learn more &rarr;</a>
  </div>

  <div class="stats-counter" id="statsCounter">
    <div class="sc-box sc-checks">
      <div class="sc-num" id="scChecks">0</div>
      <div class="sc-lbl">Checks This Session</div>
    </div>
    <div class="sc-box sc-flags">
      <div class="sc-num" id="scFlags">0</div>
      <div class="sc-lbl">Flags Detected</div>
    </div>
    <div class="sc-box sc-fines">
      <div class="sc-num" id="scFines">0</div>
      <div class="sc-lbl">Items for Review</div>
    </div>
  </div>

  <div class="card" id="progressPanel" style="display:none">
    <div class="ps-header">Running Checker <span class="ps-spin">&#8635;</span></div>
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
             placeholder="e.g. GREEN RIDGE ORGANICS LLC"
             value="{{ prefill_op or '' }}" required>
      <label for="website">Website URL</label>
      <input type="text" id="website" name="website"
             placeholder="e.g. https://greenridgeorganics.com"
             value="{{ prefill_url or '' }}" required>
      <div class="hint">Supports Shopify, WooCommerce, BigCommerce, and most product websites</div>
      <button type="submit" id="submitBtn">Run Checker</button>
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
      Checkers <span class="queue-count" id="queueCount">0</span>
    </div>
    <ul class="queue-list" id="queueList">
      <li class="empty-queue">No checkers yet</li>
    </ul>
  </div>

  <div class="card" id="reportCard" style="display:none">
    <div class="report-title" id="reportTitle">Results</div>
    <div id="reportBody"></div>
  </div>
</main>

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
    '', // 1-indexed
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
</body>
</html>"""


# ---------------------------------------------------------------------------
# Pricing page
# ---------------------------------------------------------------------------

TIERS = [
    {"count": 1,   "price": 4.99, "per": 4.99, "disc": None,    "name": "Spot Check",     "desc": "One operation, one verification. Perfect for a first-time check or quick one-off audit.", "featured": False},
    {"count": 10,  "price": 39,   "per": 3.90, "disc": "22% off","name": "Small Certifier","desc": "Occasional compliance spot checks for up to 10 client operations. Great for smaller certifiers doing periodic reviews.", "featured": False},
    {"count": 25,  "price": 85,   "per": 3.40, "disc": "32% off","name": "Growing Program","desc": "Regular compliance reviews for an active client roster. Ideal for certifiers building a systematic review program.", "featured": True},
    {"count": 50,  "price": 149,  "per": 2.98, "disc": "40% off","name": "Active Certifier","desc": "Monthly compliance cycles for mid-size operations. Covers a full seasonal review or pre-audit sweep.", "featured": False},
    {"count": 100, "price": 259,  "per": 2.59, "disc": "48% off","name": "High Volume",     "desc": "Systematic quarterly reviews across a full client base. Built for large certifiers with 100+ operations.", "featured": False},
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
            <img src="/static/favicon.svg" class="pricing-icon" alt="">
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
            <img src="/static/favicon.svg" class="pricing-icon" alt="">
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
      <img src="/static/favicon.svg" class="pricing-big-icon" alt="Organic Web Checker">
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
  <div class="page-title">Account <span class="coming-soon-badge">Coming Soon</span></div>
  <div class="page-subtitle">Sign in to access your check history, saved reports, and purchased checker credits.</div>
  <div class="card">
    <form onsubmit="return false">
      <label for="email">Email</label>
      <input type="email" id="email" placeholder="you@example.com">
      <label for="pw">Password</label>
      <input type="password" id="pw" placeholder="&bull;&bull;&bull;&bull;&bull;&bull;&bull;&bull;">
      <button type="submit" style="width:100%;margin-top:4px" onclick="alert('Account sign-in is coming soon. Your check history is currently session-based.')">Sign In</button>
    </form>
    <div class="divider">or</div>
    <div style="text-align:center;font-size:.82rem;color:var(--muted)">
      Don&rsquo;t have an account? &nbsp;
      <a href="#" style="color:var(--neon);text-decoration:none" onclick="alert('Registration coming soon.')">Create one &rarr;</a>
    </div>
  </div>
  <div style="text-align:center;font-size:.78rem;color:var(--muted);margin-top:12px">
    Until accounts are live, all check history is stored in your browser session only.
  </div>
</div>
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
    return render_template_string(REPORT_PARTIAL, report=job.get('report', {}), job_id=job_id)


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
        token = get_session_token()
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
        token             = obj.get('client_reference_id', '')
        credits           = int(obj.get('metadata', {}).get('credits', 0))
        tier_name         = obj.get('metadata', {}).get('tier_name', '')
        stripe_session_id = obj['id']
        amount_cents      = obj.get('amount_total', 0)
        if token and credits > 0 and DATABASE_URL:
            try:
                with db_conn() as conn:
                    with conn.cursor() as cur:
                        cur.execute("""
                            INSERT INTO credit_accounts (token, credits_remaining, total_purchased)
                            VALUES (%s, %s, %s)
                            ON CONFLICT (token) DO UPDATE SET
                                credits_remaining = credit_accounts.credits_remaining + EXCLUDED.credits_remaining,
                                total_purchased   = credit_accounts.total_purchased   + EXCLUDED.total_purchased
                        """, (token, credits, credits))
                        cur.execute("""
                            INSERT INTO purchases
                                (token, stripe_session_id, tier_name, credits_purchased, amount_paid_cents)
                            VALUES (%s, %s, %s, %s, %s)
                            ON CONFLICT (stripe_session_id) DO NOTHING
                        """, (token, stripe_session_id, tier_name, credits, amount_cents))
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
  <img src="/static/favicon.svg" style="width:72px;height:72px;margin-bottom:24px">
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
