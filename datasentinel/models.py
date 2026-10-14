"""Structures de données partagées.

Le vocabulaire de statut est volontairement strict :
- une *anomalie* est un fait observé et mesuré ;
- une *hypothèse* n'est qu'une explication candidate tant qu'un test ne l'a pas étayée ;
- une cause n'est *confirmée* que si une correction testée sur copie fait disparaître le signal.
Aucun statut n'est jamais attribué par un modèle de langage.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

import pandas as pd

# Statuts d'hypothèse (résultats de tests déterministes)
SUPPORTED = "supported"        # les données mesurées étayent l'hypothèse
REFUTED = "refuted"            # les données mesurées contredisent l'hypothèse
INCONCLUSIVE = "inconclusive"  # test exécuté, résultat non discriminant
UNTESTABLE = "untestable"      # données nécessaires indisponibles
UNLIKELY = "unlikely"          # alternative rendue peu probable par d'autres causes mesurées

# Statuts de cause racine
CONFIRMED = "confirmed"        # étayée + correction vérifiée sur copie
PROBABLE = "probable"          # étayée, correction non vérifiable sans action externe
UNEXPLAINED = "unexplained"

STATUS_LABELS = {
    SUPPORTED: "Étayée par les données",
    REFUTED: "Réfutée",
    INCONCLUSIVE: "Non concluante",
    UNTESTABLE: "Non testable",
    UNLIKELY: "Peu probable",
    CONFIRMED: "Confirmée (correction vérifiée sur copie)",
    PROBABLE: "Probable (vérification complète en attente)",
    UNEXPLAINED: "Cause non déterminée",
}


def records(df: pd.DataFrame, limit: int = 20) -> list[dict]:
    """Convertit un DataFrame en lignes JSON-sérialisables (tronqué pour le rapport)."""
    out = df.head(limit).copy()
    for col in out.columns:
        if pd.api.types.is_datetime64_any_dtype(out[col]):
            out[col] = out[col].dt.strftime("%Y-%m-%d %H:%M").where(out[col].notna(), None)
    out = out.astype(object).where(out.notna(), None)
    rows = out.to_dict(orient="records")
    for row in rows:
        for k, v in row.items():
            if hasattr(v, "item"):
                row[k] = v.item()
            elif hasattr(v, "isoformat"):
                row[k] = v.isoformat()
    return rows


@dataclass
class Evidence:
    description: str
    sql: str
    result: list[dict]
    rows: int

    @classmethod
    def from_query(cls, description: str, sql: str, df: pd.DataFrame, limit: int = 20) -> "Evidence":
        return cls(description, " ".join(sql.split()), records(df, limit), int(len(df)))


@dataclass
class Anomaly:
    id: str
    kind: str
    table: str
    columns: list[str]
    period_start: str
    period_end: str
    metric: str
    observed: float
    expected: float | None
    deviation: float | None
    severity: str
    summary: str
    evidence: Evidence

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Hypothesis:
    cause_type: str
    cause_key: str
    statement: str
    status: str
    tests: list[str]
    evidence: list[Evidence] = field(default_factory=list)
    explained_share: float | None = None
    effect: dict[str, float] = field(default_factory=dict)
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
