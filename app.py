"""
Organic Web Checker — Flask web app
"""

import os
import uuid
import threading
from datetime import datetime, timezone
from flask import Flask, request, render_template_string, send_from_directory, jsonify
from checker import run_check

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Job queue (in-memory, single-server)
# ---------------------------------------------------------------------------

jobs = {}
jobs_lock = threading.Lock()
# Playwright is not thread-safe — one browser check at a time
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
# Report partial — rendered server-side so Jinja handles the flag template
# ---------------------------------------------------------------------------

REPORT_PARTIAL = """
{% if report.get('error') %}
  <div class="error">{{ report['error'] }}</div>
{% else %}
  <div class="meta-grid">
    <div class="meta-item"><label>Operation</label><span>{{ report.operation }}</span></div>
    <div class="meta-item"><label>Certifier</label><span>{{ report.certifier }}</span></div>
    <div class="meta-item"><label>Status</label><span>{{ report.status }}</span></div>
    <div class="meta-item"><label>Location</label><span>{{ report.location }}</span></div>
    <div class="meta-item"><label>Website</label><span>{{ report.website_url }}</span></div>
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
  {% if report.flagged %}
    <div class="section-title">Non-Compliance Flags — On website as organic, not on OID certificate</div>
    <ul class="flag-list">
      {% for item in report.flagged | sort(attribute='title') %}
        <li>
          {% if item.url %}
            <a href="{{ item.url }}" target="_blank" rel="noopener">{{ item.title }}</a>
            <a href="{{ item.url }}" target="_blank" rel="noopener" class="verify-btn">Verify →</a>
          {% else %}
            <span class="no-link">{{ item.title }}</span>
          {% endif %}
        </li>
      {% endfor %}
    </ul>
  {% else %}
    <div class="clean">✓ No flags — all organic-labeled products match the OID certificate.</div>
  {% endif %}
{% endif %}
"""


# ---------------------------------------------------------------------------
# Main HTML
# ---------------------------------------------------------------------------

HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Organic Web Checker</title>
  <link rel="icon" type="image/svg+xml" href="/static/favicon.svg">
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { font-family: system-ui, sans-serif; background: #f5f5f0; color: #1a1a1a; }

    header {
      background: #2d5a27; color: white;
      padding: 24px 32px;
      display: flex; align-items: center; justify-content: space-between; gap: 24px;
    }
    header h1 { font-size: 1.4rem; font-weight: 600; letter-spacing: 0.02em; }
    header p { font-size: 0.85rem; opacity: 0.75; margin-top: 4px; }
    .header-icon-wrap { position: relative; flex-shrink: 0; }
    .header-icon-btn { background: none; border: none; cursor: pointer; padding: 0; display: block; border-radius: 6px; }
    .header-icon-btn:focus-visible { outline: 2px solid rgba(255,255,255,0.6); outline-offset: 2px; }
    .header-icon { width: 52px; height: 52px; opacity: 0.92; display: block; }
    .header-dropdown {
      position: absolute; top: calc(100% + 8px); right: 0;
      background: white; border-radius: 8px;
      box-shadow: 0 4px 20px rgba(0,0,0,0.18);
      min-width: 188px; z-index: 100;
      display: none; overflow: hidden;
    }
    .header-dropdown.open { display: block; }
    .dropdown-item {
      display: block; padding: 12px 18px;
      font-size: 0.875rem; color: #1a1a1a;
      text-decoration: none; border-bottom: 1px solid #f0f0ee;
      cursor: pointer;
    }
    .dropdown-item:last-child { border-bottom: none; }
    .dropdown-item:hover { background: #f5f5f0; color: #2d5a27; }

    main { max-width: 860px; margin: 40px auto; padding: 0 24px; }

    .card {
      background: white; border-radius: 8px;
      padding: 28px 32px;
      box-shadow: 0 1px 4px rgba(0,0,0,0.08);
      margin-bottom: 24px;
    }

    label { display: block; font-size: 0.85rem; font-weight: 600; margin-bottom: 6px; color: #444; }
    input[type=text] {
      width: 100%; padding: 10px 14px;
      border: 1px solid #ccc; border-radius: 6px;
      font-size: 0.95rem; margin-bottom: 16px;
    }
    input[type=text]:focus { outline: none; border-color: #2d5a27; box-shadow: 0 0 0 2px #2d5a2720; }

    button[type=submit] {
      background: #2d5a27; color: white;
      border: none; border-radius: 6px;
      padding: 11px 28px; font-size: 0.95rem;
      cursor: pointer; font-weight: 600;
    }
    button[type=submit]:hover { background: #244a20; }
    button[type=submit]:disabled { background: #999; cursor: default; }

    .hint { font-size: 0.78rem; color: #888; margin-top: -10px; margin-bottom: 16px; }

    /* Queue panel */
    .queue-panel {
      background: white; border-radius: 8px;
      box-shadow: 0 1px 4px rgba(0,0,0,0.08);
      margin-bottom: 24px; overflow: hidden;
    }
    .queue-header {
      padding: 14px 20px;
      background: #f9f9f7; border-bottom: 1px solid #eee;
      font-size: 0.8rem; font-weight: 700; text-transform: uppercase;
      letter-spacing: 0.06em; color: #555;
      display: flex; align-items: center; gap: 8px;
    }
    .queue-count {
      background: #2d5a27; color: white;
      border-radius: 10px; padding: 1px 7px;
      font-size: 0.72rem;
    }
    .queue-list { list-style: none; }
    .queue-item {
      padding: 12px 20px;
      border-bottom: 1px solid #f0f0ee;
      display: flex; align-items: center; gap: 12px;
      font-size: 0.875rem;
    }
    .queue-item:last-child { border-bottom: none; }
    .queue-item .op-name { font-weight: 600; flex: 1; min-width: 0; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .queue-item .site { font-size: 0.78rem; color: #888; }
    .status-pill {
      font-size: 0.72rem; font-weight: 700; padding: 3px 9px;
      border-radius: 10px; white-space: nowrap; flex-shrink: 0;
    }
    .status-queued  { background: #f0f0ee; color: #666; }
    .status-running { background: #fff8e1; color: #b8860b; animation: pulse 1.4s ease-in-out infinite; }
    .status-done    { background: #eaf4ea; color: #2d5a27; }
    .status-error   { background: #fdf3f2; color: #c0392b; }
    @keyframes pulse { 0%,100% { opacity:1; } 50% { opacity:.55; } }

    /* Animated checker loader — queued/running state */
    .checker-loader {
      position: relative; width: 40px; height: 40px;
      flex-shrink: 0; border-radius: 3px; overflow: hidden;
      background: repeating-conic-gradient(#2d5a27 0% 25%, #8bc78b 0% 50%) 0 0 / 8px 8px;
    }
    .cb-p {
      position: absolute; width: 6px; height: 6px;
      border-radius: 50%; background: #7b1f00;
      box-shadow: inset 0 -1px 1px rgba(0,0,0,0.4), 0 1px 2px rgba(0,0,0,0.5);
    }
    .cb-p.p1 { animation: cb1 2s ease-in-out infinite; }
    .cb-p.p2 { animation: cb2 2s ease-in-out infinite 0.67s; }
    .cb-p.p3 { animation: cb3 2s ease-in-out infinite 1.33s; }
    @keyframes cb1 { 0%,100% { top:1px;  left:9px;  } 50% { top:17px; left:25px; } }
    @keyframes cb2 { 0%,100% { top:9px;  left:33px; } 50% { top:25px; left:17px; } }
    @keyframes cb3 { 0%,100% { top:25px; left:1px;  } 50% { top:9px;  left:17px; } }

    .view-btn {
      font-size: 0.75rem; padding: 3px 10px;
      border: 1px solid #2d5a2760; border-radius: 4px;
      color: #2d5a27; text-decoration: none; cursor: pointer;
      background: none; flex-shrink: 0;
    }
    .view-btn:hover { background: #2d5a2710; }

    /* Report */
    .meta-grid {
      display: grid; grid-template-columns: 1fr 1fr;
      gap: 12px 32px; margin-bottom: 20px;
    }
    .meta-item label { color: #666; font-size: 0.78rem; text-transform: uppercase; letter-spacing: 0.05em; }
    .meta-item span { font-size: 0.95rem; font-weight: 500; }

    .stats {
      display: grid; grid-template-columns: repeat(4, 1fr);
      gap: 12px; margin-bottom: 24px;
    }
    .stat { background: #f9f9f7; border-radius: 6px; padding: 14px 16px; text-align: center; }
    .stat .num { font-size: 1.8rem; font-weight: 700; line-height: 1; }
    .stat .lbl { font-size: 0.72rem; color: #666; margin-top: 4px; text-transform: uppercase; letter-spacing: 0.04em; }
    .stat.flagged .num { color: #c0392b; }
    .stat.verified .num { color: #2d5a27; }

    .section-title {
      font-size: 0.85rem; font-weight: 700;
      text-transform: uppercase; letter-spacing: 0.06em;
      color: #555; margin-bottom: 10px;
    }
    .flag-list { list-style: none; }
    .flag-list li {
      padding: 9px 14px;
      background: #fdf3f2; border-left: 3px solid #c0392b;
      border-radius: 0 4px 4px 0;
      margin-bottom: 6px; font-size: 0.9rem;
      display: flex; align-items: center; gap: 10px;
    }
    .flag-list li::before { content: "⚠"; color: #c0392b; flex-shrink: 0; }
    .flag-list a { color: #c0392b; font-weight: 600; text-decoration: none; border-bottom: 1px solid #c0392b40; }
    .flag-list a:hover { border-bottom-color: #c0392b; }
    .flag-list .no-link { color: #1a1a1a; }
    .verify-btn {
      margin-left: auto; flex-shrink: 0;
      font-size: 0.75rem; color: #c0392b;
      border: 1px solid #c0392b50; border-radius: 4px;
      padding: 2px 8px; text-decoration: none; white-space: nowrap;
    }
    .verify-btn:hover { background: #c0392b10; }

    .report-title {
      font-size: 0.8rem; font-weight: 700; text-transform: uppercase;
      letter-spacing: 0.06em; color: #555; margin-bottom: 20px;
      padding-bottom: 12px; border-bottom: 1px solid #eee;
    }

    .clean { color: #2d5a27; font-weight: 600; padding: 12px 0; }
    .error { background: #fdf3f2; border-left: 3px solid #c0392b; padding: 12px 16px; border-radius: 0 6px 6px 0; color: #c0392b; }
    .empty-queue { padding: 20px; text-align: center; color: #aaa; font-size: 0.85rem; }
  </style>
</head>
<body>

<header>
  <div>
    <h1>Organic Web Checker</h1>
    <p>Checkers are web checks — compare organic product claims against the USDA OID certificate</p>
  </div>
  <div class="header-icon-wrap">
    <button class="header-icon-btn" id="iconBtn" onclick="toggleDropdown(event)">
      <img src="/static/favicon.svg" class="header-icon" alt="Organic Web Checker">
    </button>
    <div class="header-dropdown" id="headerDropdown">
      <a class="dropdown-item" href="#">Account</a>
      <a class="dropdown-item" href="#" onclick="scrollToHistory(event)">Web Check History</a>
      <a class="dropdown-item" href="#">Pricing Plan</a>
      <a class="dropdown-item" href="#">Settings</a>
    </div>
  </div>
</header>

<main>
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
      <button type="submit" id="submitBtn">Run Check</button>
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
  let queueTimer = null;
  let viewingJobId = ACTIVE_JOB;

  // ── Form submit ──────────────────────────────────────────────────────────
  document.getElementById('checkForm').addEventListener('submit', async (e) => {
    e.preventDefault();
    const btn = document.getElementById('submitBtn');
    btn.disabled = true;
    btn.textContent = 'Submitting...';

    const res = await fetch('/check', { method: 'POST', body: new URLSearchParams(new FormData(e.target)) });
    const data = await res.json();
    viewingJobId = data.job_id;

    btn.disabled = false;
    btn.textContent = 'Run Check';
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
    const res = await fetch('/job/' + jobId + '/result');
    const html = await res.text();
    const card = document.getElementById('reportCard');
    const body = document.getElementById('reportBody');
    const title = document.getElementById('reportTitle');
    const job = await (await fetch('/job/' + jobId)).json();
    title.textContent = job.operation || 'Results';
    body.innerHTML = html;
    card.style.display = 'block';
    card.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }

  // ── Queue panel ──────────────────────────────────────────────────────────
  async function refreshQueue() {
    const res = await fetch('/jobs');
    const jobs = await res.json();
    const panel = document.getElementById('queuePanel');
    const list = document.getElementById('queueList');
    const count = document.getElementById('queueCount');

    if (jobs.length === 0) { panel.style.display = 'none'; return; }
    panel.style.display = 'block';
    count.textContent = jobs.length;

    list.innerHTML = jobs.map(j => {
      let pill;
      if (j.status === 'queued' || j.status === 'running') {
        pill = `<div class="checker-loader"><div class="cb-p p1"></div><div class="cb-p p2"></div><div class="cb-p p3"></div></div>`;
      } else {
        pill = `<span class="status-pill status-${j.status}">${j.status}</span>`;
      }
      const viewBtn = (j.status === 'done' || j.status === 'error')
        ? `<button class="view-btn" onclick="showJob('${j.id}')">View</button>`
        : '';
      return `<li class="queue-item">
        <div>
          <div class="op-name">${j.operation}</div>
          <div class="site">${j.website}</div>
        </div>
        ${pill}
        ${viewBtn}
      </li>`;
    }).join('');
  }

  async function showJob(jobId) {
    viewingJobId = jobId;
    await loadResult(jobId);
  }

  // ── Header dropdown ──────────────────────────────────────────────────────
  function toggleDropdown(e) {
    e.stopPropagation();
    document.getElementById('headerDropdown').classList.toggle('open');
  }
  document.addEventListener('click', () => {
    document.getElementById('headerDropdown').classList.remove('open');
  });
  function scrollToHistory(e) {
    e.preventDefault();
    document.getElementById('headerDropdown').classList.remove('open');
    const panel = document.getElementById('queuePanel');
    if (panel.style.display !== 'none') {
      panel.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }
  }

  // ── Init ─────────────────────────────────────────────────────────────────
  if (ACTIVE_JOB) {
    startPolling(ACTIVE_JOB);
  }
  refreshQueue();
  queueTimer = setInterval(refreshQueue, 5000);
</script>

</body>
</html>
"""


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route('/', methods=['GET'])
def index():
    return render_template_string(HTML, active_job=None, prefill_op='', prefill_url='')


@app.route('/check', methods=['POST'])
def check():
    operation = request.form.get('operation', '').strip()
    website = request.form.get('website', '').strip()
    if not website.startswith('http'):
        website = 'https://' + website

    job_id = uuid.uuid4().hex[:8]
    with jobs_lock:
        jobs[job_id] = {
            'id': job_id,
            'operation': operation,
            'website': website,
            'status': 'queued',
            'report': None,
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
    return render_template_string(REPORT_PARTIAL, report=job.get('report', {}))


@app.route('/jobs')
def jobs_list():
    with jobs_lock:
        result = [
            {k: v for k, v in j.items() if k != 'report'}
            for j in jobs.values()
        ]
    return jsonify(sorted(result, key=lambda x: x['submitted_at'], reverse=True))


@app.route('/static/<path:filename>')
def static_files(filename):
    return send_from_directory('static', filename)


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(debug=False, host='0.0.0.0', port=port)
