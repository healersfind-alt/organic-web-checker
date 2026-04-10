"""
Organic Web Checker — Flask web app
"""

import os
import uuid
import threading
from datetime import datetime, timezone
from flask import Flask, request, render_template_string, send_from_directory, jsonify, Response
from checker import run_check

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Job queue (in-memory, single-server)
# ---------------------------------------------------------------------------

jobs = {}
jobs_lock = threading.Lock()
_check_semaphore = threading.Semaphore(1)


def _run_job(job_id: str, operation: str, website: str):
    with _check_semaphore:
        with jobs_lock:
            jobs[job_id]['status'] = 'running'
        try:
            report = run_check(operation, website)
            with jobs_lock:
                jobs[job_id]['status'] = 'done'
                jobs[job_id]['report'] = report
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
    now = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    flagged  = sorted(report.get('flagged', []),  key=lambda x: x['title'])
    verified = sorted(report.get('verified', []), key=lambda x: x['title'])
    certs    = sorted(report.get('cert_products', []))

    lines = [
        "# ORGANIC WEB CHECKER — COMPLIANCE REPORT",
        f"Generated: {now}",
        "",
        "## Operation Details",
        f"- **Name:** {report.get('operation', '')}",
        f"- **Certifier:** {report.get('certifier', '')}",
        f"- **Status:** {report.get('status', '')}",
        f"- **Location:** {report.get('location', '')}",
        f"- **Website:** {report.get('website_url', '')}",
        "",
        "## Summary",
        "| Metric | Count |",
        "|--------|-------|",
        f"| OID Certified Products | {report.get('cert_product_count', 0)} |",
        f"| Website Organic Products | {report.get('website_organic_count', 0)} |",
        f"| Verified (on certificate) | {len(verified)} |",
        f"| **FLAGGED (not on certificate)** | **{len(flagged)}** |",
        "",
    ]

    if flagged:
        lines += [
            f"## ⚠ NON-COMPLIANCE FLAGS ({len(flagged)} items)",
            "Products marketed as organic on the website but NOT found on the OID certificate:",
            "",
        ]
        for i, item in enumerate(flagged, 1):
            url = f" → {item['url']}" if item.get('url') else ""
            lines.append(f"{i}. **{item['title']}**{url}")
        lines.append("")
    else:
        lines += ["## ✓ NO FLAGS", "All organic-labeled products match the OID certificate.", ""]

    lines += [f"## ✓ VERIFIED PRODUCTS ({len(verified)} items)",
              "Products on the website that match the OID certificate:", ""]
    for i, item in enumerate(verified, 1):
        url = f" → {item['url']}" if item.get('url') else ""
        lines.append(f"{i}. {item['title']}{url}")
    lines.append("")

    lines += [f"## CERTIFICATE PRODUCTS — OID ({len(certs)} items)",
              "All products on the current USDA Organic Integrity Database certificate:", ""]
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
    --muted:     #4d7a5a;
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
  .red   .badge { background: rgba(255,68,85,.08); border-color: rgba(255,68,85,.2); color: var(--red); }
  .green .badge { background: rgba(0,255,127,.07); border-color: rgba(0,255,127,.15); color: var(--neon); }
  .cyan  .badge { background: rgba(0,229,204,.07); border-color: rgba(0,229,204,.15); color: var(--cyan); }

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
      <a href="/history" class="nav-link {{ 'active' if active == 'history' else '' }}">History</a>
      <a href="/pricing" class="nav-link {{ 'active' if active == 'pricing' else '' }}">Pricing</a>
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

  <div class="stats">
    <div class="stat">
      <div class="num">{{ report.cert_product_count }}</div>
      <div class="lbl">OID Certified</div>
    </div>
    <div class="stat">
      <div class="num">{{ report.website_organic_count }}</div>
      <div class="lbl">Website Organic</div>
    </div>
    <div class="stat verified">
      <div class="num">{{ report.verified | length }}</div>
      <div class="lbl">Verified</div>
    </div>
    <div class="stat flagged">
      <div class="num">{{ report.flagged | length }}</div>
      <div class="lbl">Flagged</div>
    </div>
  </div>

  {# ── Flags ─────────────────────────────────────────────────── #}
  {% if report.flagged %}
    <div class="section-label red">
      &#9888; Non-Compliance Flags
      <span class="badge">{{ report.flagged | length }}</span>
    </div>
    <p style="font-size:.78rem;color:var(--muted);margin-bottom:10px">
      Marketed as organic on the website — NOT found on the OID certificate
    </p>
    <ul class="product-list" style="margin-bottom:0">
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
    <div class="clean">&#10003; No flags — all organic-labeled products match the OID certificate.</div>
  {% endif %}

  {# ── All website organic products ──────────────────────────── #}
  <div class="section-label green" style="margin-top:28px">
    Website Organic Products
    <span class="badge">{{ report.website_organic_count }}</span>
  </div>
  <p style="font-size:.78rem;color:var(--muted);margin-bottom:10px">
    All products found on the website labeled organic. &#10003; = on certificate &nbsp; &#9888; = not on certificate
  </p>
  <ul class="product-list scrollable-list">
    {% set all_site = (report.verified + report.flagged) | sort(attribute='title') %}
    {% for item in all_site %}
      {% set is_flag = item in report.flagged %}
      <li class="{{ 'flag-item' if is_flag else 'ok-item' }}">
        {{ '&#9888;' if is_flag else '&#10003;' }}
        {% if item.url %}
          <a href="{{ item.url }}" target="_blank" rel="noopener" style="{{ 'color:var(--red)' if is_flag else 'color:var(--neon)' }}">{{ item.title }}</a>
        {% else %}
          <span class="no-link">{{ item.title }}</span>
        {% endif %}
      </li>
    {% endfor %}
  </ul>

  {# ── OID certificate products ──────────────────────────────── #}
  <div class="section-label cyan" style="margin-top:28px">
    OID Certificate Products
    <span class="badge">{{ report.cert_product_count }}</span>
  </div>
  <p style="font-size:.78rem;color:var(--muted);margin-bottom:10px">
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
  <title>Organic Web Checker</title>
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
      <a href="/history" class="nav-link">History</a>
      <a href="/pricing" class="nav-link">Pricing</a>
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
        <a class="dropdown-item" href="/settings">Settings</a>
      </div>
    </div>
  </div>
</header>

<main class="page-main">
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

  // ── Poll active job ──────────────────────────────────────────────────────
  function startPolling(jobId) {
    clearInterval(pollTimer);
    pollTimer = setInterval(async () => {
      const res = await fetch('/job/' + jobId);
      const job = await res.json();
      if (job.status === 'done' || job.status === 'error') {
        clearInterval(pollTimer);
        loadResult(jobId);
        refreshQueue();
      }
    }, 3000);
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
  setInterval(refreshQueue, 5000);
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
    for t in TIERS:
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
          <a href="#" class="pricing-cta" onclick="return comingSoon()">Purchase</a>
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
          <a href="mailto:hello@organicwebcheck.com" class="pricing-cta contact">Contact Us</a>
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
    <div class="coming-soon-note">
      <strong>Payments launching soon.</strong> Stripe integration is in progress. To get early access or be notified at launch, email <a href="mailto:hello@organicwebcheck.com" style="color:var(--amber);text-decoration:none">hello@organicwebcheck.com</a>
    </div>
    <script>function comingSoon(){{alert('Payments launching soon. Email hello@organicwebcheck.com for early access.');return false;}}</script>"""


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
  Have a feature you&rsquo;d like to see? <a href="mailto:hello@organicwebcheck.com" style="color:var(--cyan);text-decoration:none">Email us</a>.
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
<div class="ts">Generated {now} by Organic Web Checker &mdash; organicwebcheck.up.railway.app</div>
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


@app.route('/static/<path:filename>')
def static_files(filename):
    return send_from_directory('static', filename)


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(debug=False, host='0.0.0.0', port=port)
