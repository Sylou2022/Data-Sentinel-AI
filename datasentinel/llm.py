"""Rédaction de l'explication métier.

Le modèle de langage n'intervient qu'en *rédaction* : il reformule des faits déjà établis
par des tests déterministes. Il ne fixe aucun statut, aucun chiffre, aucune cause.
Par défaut, la rédaction est locale (gabarit) ; Claude n'est appelé qu'avec une
configuration explicite (DATASENTINEL_LLM_PROVIDER=anthropic).
"""
from __future__ import annotations

import hashlib
import json
import re

from .config import Settings
from .models import CONFIRMED, STATUS_LABELS

CERTAINTY = re.compile(r"\b(certaine?s?|certainement|sans aucun doute|indiscutable\w*|avérée?s?|prouvée?s?|confirmée?s?)\b",
                       re.IGNORECASE)


def fmt_eur(x: float | None) -> str:
    if x is None:
        return "n.d."
    return f"{x:,.0f}".replace(",", " ") + " €"


def fmt_num(x: float | None) -> str:
    return "n.d." if x is None else f"{x:,.0f}".replace(",", " ")


def facts_for(rc: dict) -> dict:
    """Sous-ensemble factuel transmis au rédacteur (aucune donnée ligne à ligne)."""
    impact = rc.get("impact", {})
    return {
        "cause": rc["statement"],
        "status": STATUS_LABELS[rc["assessment"]["status"]],
        "confidence": rc["assessment"]["confidence"],
        "measurements": [{k: m.get(k) for k in ("label", "period", "as_reported", "reference", "delta", "delta_pct", "basis")}
                         for m in impact.get("measurements", [])],
        "month_to_date_revenue_error_share": impact.get("month_to_date", {}).get("revenue_error_share"),
        "dashboards": impact.get("dashboards", []),
        "decisions": impact.get("decisions", []),
        "uncertainties": rc["assessment"]["uncertainties"],
    }


class TemplateNarrator:
    provider, model = "local-template", "datasentinel-template-v1"

    def explain(self, rc: dict) -> dict:
        f = facts_for(rc)
        parts = [f"{f['status']} — confiance {f['confidence']}. {f['cause']}"]
        for m in f["measurements"]:
            unit = fmt_eur if m["label"] in ("Chiffre d'affaires", "Marge brute", "Panier moyen", "CA par catégorie") else fmt_num
            if m["as_reported"] is not None and m["reference"] is not None:
                pct = f" ({m['delta_pct']:+.0%})" if m["delta_pct"] is not None else ""
                parts.append(f"{m['label']} ({m['period']}) : {unit(m['as_reported'])} affiché contre "
                             f"{unit(m['reference'])} attendu{pct} — {m['basis']}.")
            elif m["delta"] is not None:
                parts.append(f"{m['label']} ({m['period']}) : {unit(abs(m['delta']))} non rattachés — {m['basis']}.")
        if f["month_to_date_revenue_error_share"] is not None:
            parts.append(f"Sur le mois en cours, l'erreur représente {f['month_to_date_revenue_error_share']:+.1%} du CA affiché.")
        if f["dashboards"]:
            parts.append("Tableaux de bord concernés : " + ", ".join(f["dashboards"]) + ".")
        if f["decisions"]:
            parts.append("Décisions à suspendre ou à revoir : " + ", ".join(f["decisions"]) + ".")
        if f["uncertainties"]:
            parts.append("Points encore incertains : " + " ".join(f["uncertainties"]))
        return {"text": " ".join(parts), "provider": self.provider, "model": self.model, "guard": "n/a"}


SYSTEM_PROMPT = """Tu es le rédacteur de DataSentinel AI. Tu reformules, pour un public métier \
(direction commerciale, contrôle de gestion), un diagnostic de qualité de données dont les faits, \
les chiffres et le statut ont été établis par des tests déterministes.

Règles :
- N'ajoute aucun fait, chiffre, cause ou recommandation absent des données fournies.
- Reprends le statut tel quel. Si le statut n'est pas « Confirmée », n'emploie aucune formulation \
de certitude (certain, prouvé, avéré, confirmé…).
- Mentionne les points encore incertains.
- Réponds en français, en texte brut, en 120 mots au plus."""


class ClaudeNarrator:
    provider = "anthropic"

    def __init__(self, model: str):
        self.model = model
        self._client = None
        self.fallback = TemplateNarrator()

    @property
    def client(self):
        if self._client is None:
            import anthropic  # import paresseux : dépendance optionnelle

            self._client = anthropic.Anthropic()  # peut lever une erreur si aucune clé n'est configurée
        return self._client

    def explain(self, rc: dict) -> dict:
        facts = facts_for(rc)
        payload = json.dumps(facts, ensure_ascii=False, sort_keys=True)
        base = {"provider": self.provider, "model": self.model,
                "prompt_sha256": hashlib.sha256((SYSTEM_PROMPT + payload).encode()).hexdigest()[:16]}
        try:
            response = self.client.beta.messages.create(
                model=self.model,
                max_tokens=2000,
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
                output_config={"effort": "low"},
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": f"Diagnostic à reformuler :\n{payload}"}],
            )
        except Exception as exc:  # réseau, quota, clé absente : on retombe sur le gabarit
            return {**self.fallback.explain(rc), "guard": f"llm_error: {type(exc).__name__}"}
        if response.stop_reason == "refusal":
            return {**self.fallback.explain(rc), "guard": "llm_refusal"}
        text = "".join(b.text for b in response.content if b.type == "text").strip()
        if rc["assessment"]["status"] != CONFIRMED and CERTAINTY.search(text):
            # Garde-fou : le texte affirmerait une certitude non démontrée → rejeté.
            return {**self.fallback.explain(rc), "guard": "rejected_overclaim", "rejected_text": text}
        return {**base, "text": text, "model": response.model, "guard": "passed"}


def get_narrator(settings: Settings):
    if settings.llm_provider == "anthropic":
        return ClaudeNarrator(settings.llm_model)
    return TemplateNarrator()
