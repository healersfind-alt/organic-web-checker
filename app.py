"""
Organic Web Checker — Flask web app
"""

import os
from flask import Flask, request, render_template_string, send_from_directory
from checker import run_check

app = Flask(__name__)

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
      background: #2d5a27;
      color: white;
      padding: 24px 32px;
    }
    header h1 { font-size: 1.4rem; font-weight: 600; letter-spacing: 0.02em; }
    header p { font-size: 0.85rem; opacity: 0.75; margin-top: 4px; }

    main { max-width: 860px; margin: 40px auto; padding: 0 24px; }

    .card {
      background: white;
      border-radius: 8px;
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
    .stat {
      background: #f9f9f7; border-radius: 6px;
      padding: 14px 16px; text-align: center;
    }
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
    }
    .flag-list li::before { content: "⚠ "; color: #c0392b; }

    .clean { color: #2d5a27; font-weight: 600; padding: 12px 0; }

    .spinner {
      display: none; text-align: center;
      padding: 32px; color: #666; font-size: 0.9rem;
    }
    .spinner.active { display: block; }

    .error { background: #fdf3f2; border-left: 3px solid #c0392b; padding: 12px 16px; border-radius: 0 6px 6px 0; color: #c0392b; }
  </style>
</head>
<body>

<header>
  <h1>Organic Web Checker</h1>
  <p>Compare products marketed as organic on a website against the USDA Organic Integrity Database certificate</p>
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

  <div class="spinner" id="spinner">
    Connecting to USDA OID and scraping website — this takes about 20 seconds...
  </div>

  {% if report %}
  <div class="card" id="report">
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
          {% for item in report.flagged | sort %}
            <li>{{ item }}</li>
          {% endfor %}
        </ul>
      {% else %}
        <div class="clean">✓ No flags — all organic-labeled products match the OID certificate.</div>
      {% endif %}
    {% endif %}
  </div>
  {% endif %}
</main>

<script>
  document.getElementById('checkForm').addEventListener('submit', async (e) => {
    e.preventDefault();
    const btn = document.getElementById('submitBtn');
    const spinner = document.getElementById('spinner');
    btn.disabled = true;
    btn.textContent = 'Checking...';
    spinner.classList.add('active');

    const formData = new FormData(e.target);
    const res = await fetch('/check', {
      method: 'POST',
      body: new URLSearchParams(formData)
    });
    const html = await res.text();
    document.open();
    document.write(html);
    document.close();
  });
</script>

</body>
</html>
"""


@app.route('/', methods=['GET'])
def index():
    return render_template_string(HTML)


@app.route('/check', methods=['POST'])
def check():
    operation = request.form.get('operation', '').strip()
    website = request.form.get('website', '').strip()

    if not website.startswith('http'):
        website = 'https://' + website

    report = run_check(operation, website)

    return render_template_string(
        HTML,
        report=report,
        prefill_op=operation,
        prefill_url=website,
    )


@app.route('/static/<path:filename>')
def static_files(filename):
    return send_from_directory('static', filename)


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(debug=False, host='0.0.0.0', port=port)
