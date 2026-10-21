import csv
import json
from datetime import datetime
from html import escape
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
CSV_FILE = BASE_DIR / "part-merged.csv"
JSON_FILE = BASE_DIR / "ai_quality_report.json"
HTML_FILE = BASE_DIR / "final_quality_report.html"


def load_orders():
    with CSV_FILE.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def load_ai_report():
    with JSON_FILE.open("r", encoding="utf-8") as f:
        return json.load(f)


def safe(value):
    return escape(str(value if value is not None else ""))


def to_int(value):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def norm_severity(item):
    sev = str(item.get("severity", "Low")).strip().capitalize()
    return sev if sev in ("High", "Medium", "Low") else "Low"


def order_cell(column, value):
    """Render one order field, calling out the values that matter."""
    if column == "customer_id" and not str(value or "").strip():
        return '<span class="gap">missing</span>'
    if column == "quantity":
        n = to_int(value)
        if n is not None and n <= 0:
            return f'<span class="neg">{safe(value)}</span>'
    if column == "duplicate_rank":
        n = to_int(value)
        if n is not None and n > 1:
            return f'<span class="dup">occurrence #{n}</span>'
    return safe(value)


def finding_html(item, kind, orders_by_id):
    sev = norm_severity(item)
    oid = str(item.get("order_id", "N/A"))
    order = orders_by_id.get(oid, {})
    rank = to_int(order.get("duplicate_rank"))

    snapshot = ""
    if order:
        parts = []
        for key in ("customer_id", "product", "quantity", "unit_price"):
            if key in order:
                label = key.replace("_", " ")
                parts.append(
                    f"<div><dt>{safe(label)}</dt>"
                    f"<dd>{order_cell(key, order[key])}</dd></div>"
                )
        snapshot = f'<dl class="snapshot">{"".join(parts)}</dl>'

    note = ""
    if rank is not None and rank > 1:
        note = (
            f'<p class="note">The pipeline ranked this order #{rank} in its '
            "group of identical orders. The earlier matching order was not "
            "written to this file, so confirm it in the source data before "
            "treating this as a duplicate.</p>"
        )

    kind_key = "confirmed" if kind == "Confirmed error" else "potential"
    return f"""
    <details class="finding" data-type="{kind_key}" data-sev="{sev.lower()}">
      <summary>
        <span class="oid">#{safe(oid)}</span>
        <span class="issue">{safe(item.get('issue_type', 'Unspecified'))}</span>
        <span class="tag {kind_key}">{safe(kind)}</span>
        <span class="sev {sev.lower()}">{safe(sev)}</span>
      </summary>
      <div class="body">
        {snapshot}
        <h4>What the AI found</h4>
        <p>{safe(item.get('explanation', 'No explanation provided.'))}</p>
        <h4>Recommended action</h4>
        <p>{safe(item.get('recommended_action', 'No action provided.'))}</p>
        {note}
      </div>
    </details>"""


TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Order quality report</title>
<style>
:root {
  --bg:#f3f5f8; --panel:#ffffff; --ink:#16222e; --muted:#5b6978;
  --line:#e2e7ee; --side:#10272e; --side-ink:#d3e3e6; --accent:#0e7c86;
  --good:#1f9d63; --track:#e4e9ef;
  --high-bg:#fde4e1; --high-ink:#9b2318;
  --med-bg:#fdf0d2;  --med-ink:#7a5200;
  --low-bg:#e2f1e8;  --low-ink:#1d6b3f;
  --conf-bg:#e7ecfb; --conf-ink:#2e44a0;
  --pot-bg:#eef0f3;  --pot-ink:#47525f;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg:#0c1418; --panel:#141f26; --ink:#e5edf2; --muted:#93a4b2;
    --line:#233540; --side:#091a20; --side-ink:#b9cfd4; --accent:#3fc1cc;
    --good:#43c98a; --track:#26373f;
    --high-bg:#4a1f1b; --high-ink:#ffb3aa;
    --med-bg:#463614;  --med-ink:#f6cf86;
    --low-bg:#1b3b2a;  --low-ink:#9fdcb7;
    --conf-bg:#222e5c; --conf-ink:#b5c2ff;
    --pot-bg:#25323b;  --pot-ink:#b6c3ce;
  }
}
* { box-sizing: border-box; }
html { scroll-behavior: smooth; }
body {
  margin: 0; background: var(--bg); color: var(--ink);
  font: 15px/1.5 "Manrope", "Segoe UI", system-ui, -apple-system, sans-serif;
}
a { color: inherit; }
:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }

.shell { display: grid; grid-template-columns: 240px 1fr; min-height: 100vh; }
aside {
  background: var(--side); color: var(--side-ink);
  padding: 28px 20px; position: sticky; top: 0; height: 100vh;
  display: flex; flex-direction: column; gap: 28px;
}
.brand { font-size: 18px; font-weight: 700; color: #fff; }
.brand small { display: block; font-size: 13px; font-weight: 400; color: var(--side-ink); }
nav { display: flex; flex-direction: column; gap: 4px; }
nav a {
  text-decoration: none; padding: 9px 12px; border-radius: 8px;
  color: var(--side-ink);
}
nav a:hover { background: #ffffff14; color: #fff; }
.flow { margin-top: auto; font-size: 13px; }
.flow h3 { font-size: 13px; margin: 0 0 10px; color: #fff; }
.flow ol { list-style: none; margin: 0; padding: 0; border-left: 2px solid #ffffff2e; }
.flow li { padding: 0 0 12px 14px; position: relative; }
.flow li::before {
  content: ""; position: absolute; left: -6px; top: 6px;
  width: 10px; height: 10px; border-radius: 50%; background: var(--accent);
}

main { padding: 32px clamp(18px, 4vw, 48px) 48px; max-width: 1280px; }
header.top { margin-bottom: 24px; }
h1 { margin: 0 0 4px; font-size: 28px; letter-spacing: -0.01em; }
.meta { color: var(--muted); margin: 0; }

.panel {
  background: var(--panel); border: 1px solid var(--line);
  border-radius: 14px; padding: 22px; margin-bottom: 22px;
}
.panel h2 { margin: 0 0 14px; font-size: 18px; }

.summary { display: grid; grid-template-columns: auto 1fr; gap: 28px; align-items: center; }
.ring {
  --p: 0; width: 132px; height: 132px; border-radius: 50%;
  background: conic-gradient(var(--good) calc(var(--p) * 1%), var(--track) 0);
  display: grid; place-items: center;
}
.ring div {
  width: 100px; height: 100px; border-radius: 50%; background: var(--panel);
  display: grid; place-items: center; text-align: center;
  font-size: 26px; font-weight: 700; line-height: 1.1;
}
.ring span { display: block; font-size: 12px; font-weight: 500; color: var(--muted); }
.headline { font-size: 22px; font-weight: 600; margin: 0 0 6px; }
.stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(120px, 1fr)); gap: 14px; margin-top: 16px; }
.stat { border-left: 3px solid var(--line); padding-left: 12px; }
.stat b { display: block; font-size: 22px; font-variant-numeric: tabular-nums; }
.stat span { color: var(--muted); font-size: 13px; }
.stat.high { border-color: var(--high-ink); }
.stat.med { border-color: var(--med-ink); }

.tools { display: flex; flex-wrap: wrap; gap: 10px; align-items: center; margin-bottom: 14px; }
.chip {
  border: 1px solid var(--line); background: transparent; color: var(--ink);
  padding: 6px 14px; border-radius: 999px; font: inherit; cursor: pointer;
}
.chip[aria-pressed="true"] { background: var(--accent); border-color: var(--accent); color: #fff; }
.search {
  margin-left: auto; padding: 8px 12px; border-radius: 8px; min-width: 200px;
  border: 1px solid var(--line); background: var(--bg); color: var(--ink); font: inherit;
}

.finding { border: 1px solid var(--line); border-radius: 10px; margin-bottom: 10px; }
.finding[hidden] { display: none; }
.finding summary {
  display: grid; grid-template-columns: 90px 1fr auto auto; gap: 12px;
  align-items: center; padding: 12px 16px; cursor: pointer; list-style: none;
}
.finding summary::-webkit-details-marker { display: none; }
.finding[open] summary { border-bottom: 1px solid var(--line); }
.oid { font-weight: 700; font-variant-numeric: tabular-nums; }
.issue { overflow-wrap: anywhere; }
.tag, .sev, .badge {
  padding: 3px 10px; border-radius: 999px; font-size: 13px;
  font-weight: 600; white-space: nowrap;
}
.tag.confirmed { background: var(--conf-bg); color: var(--conf-ink); }
.tag.potential { background: var(--pot-bg); color: var(--pot-ink); }
.sev.high { background: var(--high-bg); color: var(--high-ink); }
.sev.medium { background: var(--med-bg); color: var(--med-ink); }
.sev.low { background: var(--low-bg); color: var(--low-ink); }
.body { padding: 14px 16px 16px; }
.body h4 { margin: 14px 0 2px; font-size: 14px; color: var(--muted); font-weight: 600; }
.body p { margin: 0; max-width: 72ch; }
.snapshot { display: flex; flex-wrap: wrap; gap: 8px 28px; margin: 0 0 4px; }
.snapshot dt { font-size: 13px; color: var(--muted); }
.snapshot dd { margin: 0; font-weight: 600; font-variant-numeric: tabular-nums; }
.note {
  margin-top: 14px !important; padding: 10px 12px; border-radius: 8px;
  background: var(--med-bg); color: var(--med-ink); font-size: 14px;
}
.empty { color: var(--muted); padding: 12px 0; }

.wrap { overflow-x: auto; }
table { width: 100%; border-collapse: collapse; }
th, td { padding: 11px 12px; text-align: left; border-bottom: 1px solid var(--line); white-space: nowrap; }
th { font-size: 13px; color: var(--muted); font-weight: 600; background: var(--bg); }
td { font-variant-numeric: tabular-nums; }
tr[hidden] { display: none; }
.badge.flagged { background: var(--high-bg); color: var(--high-ink); }
.badge.clean { background: var(--low-bg); color: var(--low-ink); }
.neg { color: var(--high-ink); font-weight: 700; }
.gap { color: var(--high-ink); font-style: italic; }
.dup { background: var(--med-bg); color: var(--med-ink); padding: 2px 8px; border-radius: 6px; font-weight: 600; }
.toggle { display: flex; align-items: center; gap: 8px; color: var(--muted); }

footer { color: var(--muted); font-size: 13px; margin-top: 8px; max-width: 80ch; }

@media (max-width: 860px) {
  .shell { grid-template-columns: 1fr; }
  aside { position: static; height: auto; flex-direction: row; flex-wrap: wrap; align-items: center; padding: 14px 18px; gap: 12px; }
  nav { flex-direction: row; }
  .flow { display: none; }
  .summary { grid-template-columns: 1fr; justify-items: start; }
  .finding summary { grid-template-columns: 1fr auto; }
  .finding summary .issue { grid-column: 1 / -1; order: 3; }
  .search { margin-left: 0; width: 100%; }
}
@media (prefers-reduced-motion: reduce) { html { scroll-behavior: auto; } }
</style>
</head>
<body>
<div class="shell">
  <aside>
    <div class="brand">Order quality<small>Data review</small></div>
    <nav aria-label="Sections">
      <a href="#overview">Overview</a>
      <a href="#findings">Findings</a>
      <a href="#orders">Orders</a>
    </nav>
    <div class="flow">
      <h3>How this report was made</h3>
      <ol>
        <li>Data Factory checks each order</li>
        <li>Flagged rows saved to Blob Storage</li>
        <li>Foundry agent reviews the rows</li>
      </ol>
    </div>
  </aside>

  <main>
    <header class="top">
      <h1>Order quality report</h1>
      <p class="meta">Generated %%GENERATED%% from %%CSV_NAME%% and %%JSON_NAME%%</p>
    </header>

    <section class="panel" id="overview" aria-label="Overview">
      <div class="summary">
        <div class="ring" style="--p:%%SCORE%%"><div>%%SCORE%%%<span>clean</span></div></div>
        <div>
          <p class="headline">%%HEADLINE%%</p>
          <p class="meta">Findings marked as potential need a check against the source data. Nothing in the source records was changed.</p>
          <div class="stats">
            <div class="stat"><b>%%N_ORDERS%%</b><span>Orders analyzed</span></div>
            <div class="stat"><b>%%N_CONFIRMED%%</b><span>Confirmed errors</span></div>
            <div class="stat"><b>%%N_POTENTIAL%%</b><span>Potential anomalies</span></div>
            <div class="stat high"><b>%%N_HIGH%%</b><span>High severity</span></div>
            <div class="stat med"><b>%%N_MEDIUM%%</b><span>Medium severity</span></div>
          </div>
        </div>
      </div>
    </section>

    <section class="panel" id="findings" aria-label="Findings">
      <h2>Findings</h2>
      <div class="tools">
        <button class="chip" data-f="all" aria-pressed="true">All</button>
        <button class="chip" data-f="confirmed" aria-pressed="false">Confirmed</button>
        <button class="chip" data-f="potential" aria-pressed="false">Potential</button>
        <button class="chip" data-f="high" aria-pressed="false">High</button>
        <button class="chip" data-f="medium" aria-pressed="false">Medium</button>
        <button class="chip" data-f="low" aria-pressed="false">Low</button>
        <input class="search" id="q" type="search" placeholder="Search order or issue" aria-label="Search findings">
      </div>
      <div id="list">%%FINDINGS%%</div>
      <p class="empty" id="none" hidden>No findings match these filters.</p>
    </section>

    <section class="panel" id="orders" aria-label="Orders">
      <h2>Order records</h2>
      <label class="toggle"><input type="checkbox" id="only"> Show only orders with a finding</label>
      <div class="wrap">
        <table>
          <thead><tr>%%HEADERS%%<th>Status</th></tr></thead>
          <tbody>%%ORDER_ROWS%%</tbody>
        </table>
      </div>
    </section>

    <footer>
      Occurrence numbers come from the Data Factory duplicate check. Occurrence #2 means an earlier order
      with the same customer, product, quantity and price exists, and that earlier order may not appear in this file.
    </footer>
  </main>
</div>

<script>
(function () {
  var chips = document.querySelectorAll(".chip");
  var q = document.getElementById("q");
  var items = document.querySelectorAll(".finding");
  var none = document.getElementById("none");
  var only = document.getElementById("only");
  var filter = "all";

  function applyFindings() {
    var t = q.value.trim().toLowerCase(), shown = 0;
    items.forEach(function (el) {
      var okF = filter === "all" || el.dataset.type === filter || el.dataset.sev === filter;
      var okQ = !t || el.textContent.toLowerCase().indexOf(t) !== -1;
      el.hidden = !(okF && okQ);
      if (!el.hidden) shown++;
    });
    none.hidden = shown !== 0;
  }
  chips.forEach(function (c) {
    c.addEventListener("click", function () {
      filter = c.dataset.f;
      chips.forEach(function (x) { x.setAttribute("aria-pressed", x === c ? "true" : "false"); });
      applyFindings();
    });
  });
  q.addEventListener("input", applyFindings);
  only.addEventListener("change", function () {
    document.querySelectorAll("tbody tr").forEach(function (tr) {
      tr.hidden = only.checked && tr.dataset.flagged !== "1";
    });
  });
})();
</script>
</body>
</html>
"""


def main():
    try:
        orders = load_orders()
        report = load_ai_report()
    except FileNotFoundError as err:
        raise SystemExit(f"Missing input file: {err.filename}")

    confirmed = report.get("confirmed_errors", [])
    anomalies = report.get("potential_anomalies", [])
    findings = [(i, "Confirmed error") for i in confirmed] + [
        (i, "Potential anomaly") for i in anomalies
    ]

    sev_counts = {"High": 0, "Medium": 0, "Low": 0}
    for item, _ in findings:
        sev_counts[norm_severity(item)] += 1

    orders_by_id = {str(o.get("order_id", "")): o for o in orders}
    flagged_ids = {str(i.get("order_id", "")) for i, _ in findings}
    flagged_in_file = [oid for oid in orders_by_id if oid in flagged_ids]

    total = len(orders)
    score = round(100 * (total - len(flagged_in_file)) / total) if total else 100

    if not total:
        headline = "No orders were found in the CSV."
    elif flagged_in_file:
        headline = (
            f"{len(flagged_in_file)} of {total} orders need review."
        )
    else:
        headline = f"All {total} orders passed review."

    findings_html = "".join(
        finding_html(item, kind, orders_by_id) for item, kind in findings
    ) or '<p class="empty">The AI report contains no findings.</p>'

    headers = list(orders[0].keys()) if orders else []
    header_html = "".join(f"<th>{safe(h.replace('_', ' '))}</th>" for h in headers)

    row_html = []
    for order in orders:
        oid = str(order.get("order_id", ""))
        flagged = oid in flagged_ids
        cells = "".join(
            f"<td>{order_cell(col, order.get(col))}</td>" for col in headers
        )
        badge = (
            '<span class="badge flagged">Flagged</span>'
            if flagged
            else '<span class="badge clean">No finding</span>'
        )
        row_html.append(
            f'<tr data-flagged="{1 if flagged else 0}">{cells}<td>{badge}</td></tr>'
        )

    values = {
        "GENERATED": safe(datetime.now().astimezone().strftime("%d %b %Y, %H:%M")),
        "CSV_NAME": safe(CSV_FILE.name),
        "JSON_NAME": safe(JSON_FILE.name),
        "SCORE": str(score),
        "HEADLINE": safe(headline),
        "N_ORDERS": str(total),
        "N_CONFIRMED": str(len(confirmed)),
        "N_POTENTIAL": str(len(anomalies)),
        "N_HIGH": str(sev_counts["High"]),
        "N_MEDIUM": str(sev_counts["Medium"]),
        "FINDINGS": findings_html,
        "HEADERS": header_html,
        "ORDER_ROWS": "".join(row_html)
        or '<tr><td colspan="99">No source records found.</td></tr>',
    }

    html = TEMPLATE
    for key, val in values.items():
        html = html.replace(f"%%{key}%%", val)

    HTML_FILE.write_text(html, encoding="utf-8")

    print(f"Orders analyzed: {total}")
    print(f"Confirmed findings: {len(confirmed)}")
    print(f"Potential anomalies: {len(anomalies)}")
    print(f"Dashboard saved to: {HTML_FILE}")
    print("Open final_quality_report.html in your browser.")


if __name__ == "__main__":
    main()