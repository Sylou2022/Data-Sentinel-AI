"""Rapport de diagnostic : JSON (machine) et HTML (lecture humaine)."""
from __future__ import annotations

import json
from datetime import datetime, timezone

from jinja2 import Environment, select_autoescape

from .models import CONFIRMED, PROBABLE, STATUS_LABELS

LIMITATIONS = [
    "Le lignage KPI est déclaré dans un catalogue, pas découvert automatiquement.",
    "Les seuils de détection sont statistiques : une variation métier réelle de grande ampleur serait signalée "
    "comme anomalie jusqu'à preuve du contraire.",
    "Les causes de type chargement partiel ou retard ne peuvent être confirmées qu'après rechargement du fichier source.",
    "Aucune correction n'est appliquée à la source : les scripts approuvés sont exportés pour exécution par l'équipe data.",
]


def build_report(state: dict) -> dict:
    rcs = state.get("root_causes", [])
    counts = {s: sum(1 for r in rcs if r["assessment"]["status"] == s) for s in (CONFIRMED, PROBABLE)}
    detection = state.get("detection", {})
    return {
        "run_id": state["run_id"],
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "dataset": {"db_path": state["db_path"], "as_of": detection.get("as_of"),
                    "window_start": detection.get("window_start")},
        "summary": {
            "anomalies": len(detection.get("anomalies", [])),
            "root_causes": len(rcs),
            "confirmed": counts[CONFIRMED],
            "probable": counts[PROBABLE],
            "unexplained_cases": len(state.get("unexplained", [])),
        },
        "root_causes": rcs,
        "unexplained_cases": state.get("unexplained", []),
        "anomalies": detection.get("anomalies", []),
        "skipped_detectors": detection.get("skipped", []),
        "plans": state.get("plans", []),
        "residual_after_corrections": state.get("residual", {}),
        "review": state.get("review_log", []),
        "agent_trace": state.get("trace", []),
        "queries": state.get("queries", []),
        "limitations": LIMITATIONS,
    }


def to_json(report: dict) -> str:
    return json.dumps(report, ensure_ascii=False, indent=2, default=str)


_TEMPLATE = """<!doctype html>
<html lang="fr"><head><meta charset="utf-8"><title>DataSentinel AI — Rapport {{ r.run_id }}</title>
<style>
 body{font-family:system-ui,Segoe UI,sans-serif;max-width:1100px;margin:2rem auto;color:#1d2433;line-height:1.45;padding:0 1rem}
 h1{color:#0b3d6e} h2{border-bottom:2px solid #e3e8ef;padding-bottom:.3rem;margin-top:2rem}
 .badge{display:inline-block;padding:.15rem .55rem;border-radius:1rem;font-size:.8rem;font-weight:600}
 .confirmed{background:#dff5e3;color:#11632a}.probable{background:#fff1d6;color:#7a4b00}.unexplained{background:#fde2e2;color:#8a1c1c}
 .card{border:1px solid #e3e8ef;border-radius:.6rem;padding:1rem 1.2rem;margin:1rem 0}
 table{border-collapse:collapse;width:100%;font-size:.85rem;margin:.5rem 0}
 th,td{border:1px solid #e3e8ef;padding:.3rem .5rem;text-align:left;vertical-align:top}
 th{background:#f5f7fa} code,pre{font-size:.78rem;background:#f5f7fa;border-radius:.3rem}
 pre{padding:.6rem;white-space:pre-wrap} details{margin:.4rem 0} .muted{color:#667085;font-size:.85rem}
 .kpis{display:flex;gap:1rem;flex-wrap:wrap}.kpi{background:#f5f7fa;border-radius:.6rem;padding:.6rem 1rem;min-width:140px}
 .kpi b{font-size:1.4rem;display:block}
</style></head><body>
<h1>DataSentinel AI — Rapport de diagnostic</h1>
<p class="muted">Exécution {{ r.run_id }} · générée le {{ r.generated_at[:19] }} UTC · données au {{ r.dataset.as_of }}
 · fenêtre analysée depuis le {{ r.dataset.window_start }}</p>
<div class="kpis">
 <div class="kpi"><b>{{ r.summary.anomalies }}</b>anomalies observées</div>
 <div class="kpi"><b>{{ r.summary.root_causes }}</b>causes identifiées</div>
 <div class="kpi"><b>{{ r.summary.confirmed }}</b>confirmées sur copie</div>
 <div class="kpi"><b>{{ r.summary.probable }}</b>probables</div>
 <div class="kpi"><b>{{ r.summary.unexplained_cases }}</b>dossiers sans cause</div>
</div>
<p class="muted">Vocabulaire : <b>observé</b> = mesuré dans les données · <b>probable</b> = étayé par des tests,
vérification complète en attente · <b>confirmé</b> = la correction testée sur une copie fait disparaître le signal.
Les statuts sont attribués par des règles déterministes, jamais par un modèle de langage.</p>

<h2>Causes identifiées</h2>
{% for rc in r.root_causes %}
<div class="card">
 <span class="badge {{ rc.assessment.status }}">{{ labels[rc.assessment.status] }}</span>
 <span class="badge" style="background:#eef2f7">confiance {{ rc.assessment.confidence }}</span>
 <h3>{{ rc.statement }}</h3>
 <p>{{ rc.narrative.text }}</p>
 <p class="muted">Rédaction : {{ rc.narrative.provider }} / {{ rc.narrative.model }} (garde-fou : {{ rc.narrative.guard }})</p>
 <b>Justification</b><ul>{% for j in rc.assessment.justification %}<li>{{ j }}</li>{% endfor %}</ul>
 {% if rc.assessment.uncertainties %}<b>Incertitudes</b><ul>{% for u in rc.assessment.uncertainties %}<li>{{ u }}</li>{% endfor %}</ul>{% endif %}
 <b>Impact sur les KPI</b>
 <table><tr><th>KPI</th><th>Période</th><th>Affiché</th><th>Référence</th><th>Écart</th><th>Base</th></tr>
 {% for m in rc.impact.measurements %}<tr><td>{{ m.label }}</td><td>{{ m.period }}</td><td>{{ m.as_reported }}</td>
 <td>{{ m.reference }}</td><td>{{ m.delta }}{% if m.delta_pct is not none %} ({{ '%+.0f' % (m.delta_pct*100) }} %){% endif %}</td><td>{{ m.basis }}</td></tr>{% endfor %}
 </table>
 <p class="muted">Lignage : {{ rc.impact.lineage_source }}</p>
 {% set p = plans.get(rc.id) %}{% if p %}
 <b>Plan de correction : {{ p.title }}</b> — risque {{ p.risk }}{% if p.decision %} · décision : <b>{{ p.decision }}</b>{% endif %}
 <ol>{% for s in p.steps %}<li>{{ s }}</li>{% endfor %}</ol>
 {% if p.sql %}<pre>{{ p.sql }}</pre>{% endif %}
 {% set v = rc.verification %}
 {% if v.executed %}<table><tr><th>Contrôle sur copie</th><th>Résultat</th><th>Observé</th></tr>
 {% for c in v.checks %}<tr><td>{{ c.name }}</td><td>{{ '✅' if c.passed else '❌' }}</td><td>{{ c.observed }}</td></tr>{% endfor %}</table>
 {% else %}<p class="muted">Non exécuté sur copie : {{ v.reason }}</p>{% endif %}
 {% endif %}
 <details><summary>Preuves ({{ rc.hypotheses|length }} test(s))</summary>
 {% for h in rc.hypotheses %}<p><b>{{ h.case_id }}</b> — {{ h.statement }} [{{ h.status }}]</p>
  {% for e in h.evidence %}<p class="muted">{{ e.description }} ({{ e.rows }} ligne(s))</p><pre>{{ e.sql }}</pre>
   {% if e.result %}<table><tr>{% for k in e.result[0].keys() %}<th>{{ k }}</th>{% endfor %}</tr>
   {% for row in e.result[:8] %}<tr>{% for v in row.values() %}<td>{{ v }}</td>{% endfor %}</tr>{% endfor %}</table>{% endif %}
  {% endfor %}{% endfor %}
 </details>
</div>
{% else %}<p>Aucune cause identifiée.</p>{% endfor %}

{% if r.unexplained_cases %}<h2>Dossiers sans cause déterminée</h2>
{% for c in r.unexplained_cases %}<div class="card"><span class="badge unexplained">Cause non déterminée</span>
<ul>{% for a in c.anomalies %}<li>{{ a.summary }}</li>{% endfor %}</ul>
<ul>{% for h in c.hypotheses %}<li>{{ h.statement }} — {{ h.status }} {{ h.details.reason or '' }}</li>{% endfor %}</ul></div>{% endfor %}{% endif %}

<h2>Anomalies observées</h2>
<table><tr><th>Signal</th><th>Période</th><th>Constat</th></tr>
{% for a in r.anomalies %}<tr><td>{{ a.kind }}</td><td>{{ a.period_start }}{% if a.period_end != a.period_start %} → {{ a.period_end }}{% endif %}</td><td>{{ a.summary }}</td></tr>{% endfor %}
</table>

{% if r.residual_after_corrections %}<h2>Contrôle global après corrections (copie)</h2>
<p>Corrections appliquées ensemble : {{ r.residual_after_corrections.applied_plans|join(', ') or 'aucune' }}</p>
<ul>{% for a in r.residual_after_corrections.residual_anomalies %}<li>{{ a.summary }}</li>{% else %}<li>Aucune anomalie résiduelle.</li>{% endfor %}</ul>{% endif %}

{% if r.review %}<h2>Validation humaine</h2><table><tr><th>Date</th><th>Relecteur</th><th>Plan</th><th>Décision</th><th>Commentaire</th></tr>
{% for e in r.review %}<tr><td>{{ e.ts[:19] }}</td><td>{{ e.reviewer }}</td><td>{{ e.plan_id }}</td><td>{{ e.decision }}</td><td>{{ e.comment or '' }}</td></tr>{% endfor %}</table>{% endif %}

<h2>Traçabilité</h2>
<ul>{% for t in r.agent_trace %}<li><b>{{ t.agent }}</b> — {{ t.summary }}</li>{% endfor %}</ul>
<p class="muted">{{ r.queries|length }} requêtes exécutées (journal complet dans le JSON et le journal d'audit).</p>
<h2>Limites</h2><ul>{% for l in r.limitations %}<li>{{ l }}</li>{% endfor %}</ul>
</body></html>
"""


def to_html(report: dict) -> str:
    env = Environment(autoescape=select_autoescape(default_for_string=True, default=True))
    plans = {p["root_cause_id"]: p for p in report.get("plans", [])}
    return env.from_string(_TEMPLATE).render(r=report, labels=STATUS_LABELS, plans=plans)
