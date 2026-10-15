"""Contrôles de qualité génériques : s'appliquent à toute table, sans schéma métier imposé.

Cinq familles de constats (spécification §3) :
- structurels : lignes mal formées, valeurs non conformes au type de la colonne ;
- qualité : valeurs manquantes, doublons de lignes ou d'identifiants, formats invalides ;
- incohérences logiques : valeurs négatives ou hors bornes là où elles sont impossibles ;
- anomalies statistiques : valeurs extrêmes (méthode robuste médiane / écart absolu médian) ;
- incohérences entre colonnes : relation observée sur la quasi-totalité des lignes (ex. CA = quantité × prix)
  et violée sur quelques-unes.

Chaque constat distingue une *erreur confirmée* (règle certaine) d'une *anomalie potentielle*
(valeur inhabituelle, pas forcément fausse), cite la ligne, la colonne, la valeur observée, la règle,
la gravité, la confiance, la justification et les limites. Une correction n'est proposée que si la
valeur peut être déduite des données ; sinon la recommandation le dit. Rien n'est appliqué à la source.
"""
from __future__ import annotations

import re
from datetime import date
from itertools import combinations, permutations

import numpy as np
import pandas as pd

from .ingestion import LINE_COL
from .models import Anomaly, Evidence

ERROR, POTENTIAL = "Erreur confirmée", "Anomalie potentielle"
CATEGORIES = {"structure": "Erreur structurelle", "quality": "Erreur de qualité", "logic": "Incohérence logique",
              "stats": "Anomalie statistique", "business": "Incohérence entre colonnes"}
MAX_ROWS = 50            # lignes détaillées par constat (toutes sont comptées)
OUTLIER_Z = 6.0          # écart robuste (en écarts absolus médians normalisés) au-delà duquel une valeur est extrême
RELATION_SHARE = 0.9     # part minimale de lignes respectant une relation pour la considérer établie

_ID = re.compile(r"(^id$|_id$|^id_|^code|^sku$|^ref$|^reference|^numero|^num_|^no_)", re.I)
_NONNEG = re.compile(r"(quant|qty|qte|stock|prix|price|cost|cout|coût|amount|montant|revenue|revenu|chiffre|^ca$|_ca$|"
                     r"sales|vente|orders|commandes|^nb_|nombre|^count|_count|^age$|_age$|poids|weight|seuil|threshold|duree|"
                     r"duration|lifetime_value|ltv|salaire|salary)", re.I)
_RATE = re.compile(r"(rate|taux|ratio|pct|percent|pourcent|discount|remise)", re.I)
_UNIT_PRICE = re.compile(r"(unit_price|prix_unitaire|unit_cost|cout_unitaire|^price$|^prix$)", re.I)
_AGE = re.compile(r"(^age$|_age$|^age_)", re.I)
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_EMAIL_SQL = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"


def _q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _num(v):
    if v is None or (isinstance(v, float) and np.isnan(v)) or v is pd.NA or v is pd.NaT:
        return None
    if isinstance(v, (pd.Timestamp, date)):
        return v.isoformat()[:19]
    return v.item() if hasattr(v, "item") else v


class TableQuality:
    """Constats de qualité d'une table."""

    def __init__(self, wh, table: str, import_report: dict | None = None):
        self.wh, self.table, self.rep = wh, table, import_report
        self.df = wh.query(f"SELECT * FROM {_q(table)}")
        self.has_line = LINE_COL in self.df.columns
        self.cols = [c for c in self.df.columns if c != LINE_COL]
        self.n = len(self.df)
        self.key = LINE_COL if self.has_line else self._business_key()
        self.out: list[Anomaly] = []
        self.flagged: set[tuple] = set()   # (ligne, colonne) signalée par une règle certaine
        self.suspect: set[tuple] = set()   # (ligne, colonne) signalée par une règle quelconque

    # ------------------------------------------------------------ rôles des colonnes
    def _business_key(self) -> str | None:
        for c in self.df.columns:
            s = self.df[c]
            if _ID.search(c) and s.notna().all() and s.is_unique:
                return c
        return None

    def numeric(self, c: str) -> bool:
        s = self.df[c]
        return pd.api.types.is_numeric_dtype(s) and not pd.api.types.is_bool_dtype(s) and not _ID.search(c)

    def is_id(self, c: str) -> bool:
        s = self.df[c].dropna()
        if len(s) < 5 or self.is_email(c):
            return False
        return bool(_ID.search(c)) or (s.dtype == object and s.nunique() / len(s) >= 0.98 and len(s) >= 20)

    def is_email(self, c: str) -> bool:
        s = self.df[c].dropna()
        if s.empty or s.dtype != object:
            return False
        return "mail" in c.lower() or s.astype(str).str.contains("@").mean() >= 0.8

    # ------------------------------------------------------------ construction d'un constat
    def ident(self, idx) -> object:
        if self.key:
            return _num(self.df.at[idx, self.key])
        return int(idx) + 1

    @property
    def ident_label(self) -> str:
        return "Ligne du fichier" if self.key == LINE_COL else (self.key or "Rang")

    def where_ids(self, idxs) -> str:
        """Condition SQL ciblant exactement ces lignes (numéro de ligne source ou identifiant)."""
        if not self.key:
            return "FALSE"
        vals = [self.df.at[i, self.key] for i in idxs]
        lits = ", ".join(str(int(v)) if isinstance(v, (int, np.integer)) else "'" + str(v).replace("'", "''") + "'"
                         for v in vals)
        return f"{_q(self.key)} IN ({lits})"

    def add(self, rule: str, label: str, category: str, status: str, severity: str, confidence: str,
            columns: list[str], idxs, where: str, justification: str, limits: str, recommendation: str,
            expected: dict | None = None, correction: dict | None = None, method: str = "", chart: dict | None = None,
            focus: dict | None = None):
        """focus : colonne à afficher pour chaque ligne (par défaut toutes les colonnes du constat)."""
        idxs = list(idxs)
        if not idxs:
            return
        rows = []
        for i in idxs[:MAX_ROWS]:
            for c in ([focus[i]] if focus and i in focus else columns):
                row = {"ident": self.ident(i), "column": c, "value": _num(self.df.at[i, c]) if c in self.df else None}
                if expected and i in expected:
                    row["expected"] = expected[i]
                rows.append(row)
        for i in idxs:
            for c in columns:
                self.suspect.add((i, c))
                if status == ERROR:
                    self.flagged.add((i, c))
        shown = ", ".join([LINE_COL] * self.has_line + ([self.key] if self.key and self.key != LINE_COL else []) +
                          [c for c in columns if c != self.key])
        sql = f"SELECT {', '.join(_q(c) for c in shown.split(', '))} FROM {_q(self.table)} WHERE {where}"
        try:
            ev_df = self.wh.query(sql + " LIMIT 50")  # requête rejouable : la preuve vient de la source
        except Exception:
            ev_df = self.df.loc[idxs[:50], [c for c in shown.split(", ")]]
        share = len(idxs) / self.n if self.n else 0
        col_txt = ", ".join(columns)
        summary = f"{label} : {len(idxs)} ligne(s) de {self.table} ({share:.1%}) — colonne(s) {col_txt}."
        self.out.append(Anomaly(
            id=f"quality_{rule}_{self.table}_{'_'.join(columns)}", kind=f"quality_{rule}", table=self.table,
            columns=columns, period_start="", period_end="", metric=rule, observed=float(len(idxs)), expected=0.0,
            deviation=round(share, 6), severity=severity, summary=summary,
            evidence=Evidence.from_query(f"Lignes concernées : {label.lower()} ({col_txt}).", sql, ev_df, limit=50),
            details={"category": CATEGORIES[category], "status": status, "confidence": confidence, "rule": label,
                     "method": method, "justification": justification, "limits": limits,
                     "recommendation": recommendation, "count": len(idxs), "total_rows": self.n,
                     "ident_label": self.ident_label, "rows": rows, "correction": correction, "chart": chart}))

    def null_fix(self, c: str, idxs, why: str) -> dict | None:
        if not self.key:
            return None
        return {"sql": f"UPDATE {_q(self.table)} SET {_q(c)} = NULL WHERE {self.where_ids(idxs)};",
                "description": f"Mettre ces valeurs à vide pour les exclure des calculs, en attendant la valeur correcte "
                               f"du système source. {why}",
                "derived": False,
                "consequences": "Les totaux et moyennes de la colonne ignoreront ces lignes ; la valeur reste à compléter."}

    # ------------------------------------------------------------ règles
    def run(self) -> list[Anomaly]:
        if self.n == 0:
            return []
        self.structure()
        self.duplicates()
        self.formats()
        self.logic()
        self.relations()     # avant les valeurs extrêmes : une valeur expliquée par une relation est une erreur
        self.outliers()
        self.missing()
        return self.out

    def structure(self):
        if not self.rep:
            return
        for c in self.rep["columns"]:
            if c["nonconforming"] and c["name"] in self.df:
                lines = set(c["nonconforming_lines"])
                idxs = self.df.index[self.df[LINE_COL].isin(lines)] if self.has_line else []
                if not len(idxs):
                    continue
                self.add("type", f"Valeur non conforme au type « {c['type']} »", "structure", ERROR, "high", "élevée",
                         [c["name"]], idxs, self.where_ids(idxs),
                         f"La colonne {c['name']} contient {c['nonconforming']} valeur(s) qui ne sont pas des {c['type']}s "
                         f"(ex. {', '.join(map(repr, c['nonconforming_examples'][:3]))}) alors que le reste de la colonne l'est. "
                         "Ces valeurs sont laissées vides dans la table analysée.",
                         "Le type de la colonne est déduit de la majorité de ses valeurs.",
                         f"Corriger ces valeurs dans le fichier source (format attendu : {c['type']}{', ' + c['detail'] if c['detail'] else ''}).",
                         method="Inférence de type à l'import (≥ 95 % des valeurs conformes)")

    def duplicates(self):
        data = self.df[self.cols]
        dup = data.duplicated(keep="first")
        if dup.any():
            idxs = self.df.index[dup]
            fix = {"sql": f"DELETE FROM {_q(self.table)} WHERE {self.where_ids(idxs)};",
                   "description": "Supprimer les copies en conservant la première occurrence de chaque ligne.",
                   "derived": True, "consequences": "Les totaux reviennent à leur valeur sans double comptage."} if self.key else None
            self.add("duplicate_rows", "Ligne entièrement dupliquée", "quality", ERROR, "high", "élevée",
                     self.cols[:1], idxs, self.where_ids(idxs),
                     "Ces lignes sont identiques, colonne par colonne, à une ligne précédente du fichier.",
                     "Si le fichier liste légitimement des événements identiques (deux achats identiques), ce ne sont pas des doublons.",
                     "Supprimer les copies et vérifier pourquoi l'extraction a produit deux fois les mêmes lignes.",
                     correction=fix, method="Comparaison de toutes les colonnes")
        for c in self.cols:
            if not self.is_id(c):
                continue
            s = self.df[c]
            dmask = s.notna() & s.duplicated(keep=False) & ~dup
            if not dmask.any() or s.dropna().nunique() / max(len(s.dropna()), 1) < 0.9:
                continue
            idxs = self.df.index[dmask]
            vals = sorted(set(map(str, s[dmask])))
            self.add("duplicate_key", "Identifiant en double", "quality", ERROR, "high", "élevée", [c], idxs,
                     self.where_ids(idxs) if self.key and self.key != c else f"{_q(c)} IN ({', '.join(repr(v) for v in vals[:200])})",
                     f"La colonne {c} identifie chaque ligne ({s.nunique()} valeurs distinctes sur {s.notna().sum()}), "
                     f"mais {len(vals)} identifiant(s) apparaissent sur plusieurs lignes différentes : {', '.join(vals[:5])}.",
                     "Le rôle d'identifiant est déduit du nom de la colonne et de l'unicité de ses valeurs.",
                     "Déterminer laquelle des lignes est correcte avec le métier, puis attribuer un nouvel identifiant "
                     "à l'autre ou la supprimer. Aucune correction automatique : les données ne disent pas quelle ligne garder.",
                     method="Unicité de la colonne identifiant")

    def formats(self):
        for c in self.cols:
            if not self.is_email(c):
                continue
            s = self.df[c]
            bad = s.notna() & ~s.astype(str).str.match(_EMAIL)
            if bad.any():
                idxs = self.df.index[bad]
                self.add("email", "Adresse e-mail invalide", "quality", ERROR, "medium", "élevée", [c], idxs,
                         f"{_q(c)} IS NOT NULL AND NOT regexp_matches({_q(c)}, '{_EMAIL_SQL}')",
                         "Une adresse e-mail doit avoir la forme nom@domaine.extension.",
                         "Le format est vérifié, pas l'existence de l'adresse.",
                         "Corriger l'adresse dans le système source (aucune valeur ne peut être déduite).",
                         method="Expression régulière nom@domaine.ext")

    def logic(self):
        for c in self.cols:
            s = self.df[c]
            if pd.api.types.is_datetime64_any_dtype(s):
                self._dates(c, s)
                continue
            if not self.numeric(c):
                continue
            v = s.astype(float)
            name_nonneg = bool(_NONNEG.search(c))
            if _RATE.search(c) and v.dropna().between(0, 1).mean() >= 0.9:
                bad = v.notna() & ~v.between(0, 1)
                idxs = self.df.index[bad]
                self.add("rate_range", "Taux hors de l'intervalle 0–100 %", "logic", ERROR, "high", "élevée", [c], idxs,
                         f"{_q(c)} < 0 OR {_q(c)} > 1",
                         f"La colonne {c} est un taux exprimé en fraction (≥ 90 % des valeurs entre 0 et 1) : "
                         "une valeur hors de [0 ; 1] correspond à moins de 0 % ou plus de 100 %.",
                         "Si certains taux sont saisis en pourcentage (15 au lieu de 0,15), la règle les signale aussi.",
                         "Vérifier la valeur dans le système source ; la correction proposée la met à vide en attendant.",
                         correction=self.null_fix(c, idxs, "Un taux ne peut pas dépasser 100 %."),
                         method="Bornes logiques d'un taux")
                continue
            if _AGE.search(c):
                bad = v.notna() & ~v.between(0, 120)
                idxs = self.df.index[bad]
                self.add("age_range", "Âge impossible", "logic", ERROR, "high", "élevée", [c], idxs,
                         f"{_q(c)} < 0 OR {_q(c)} > 120",
                         "Un âge humain est compris entre 0 et 120 ans.",
                         "La règle suppose que la colonne décrit l'âge d'une personne.",
                         "Corriger l'âge dans le système source (souvent une erreur de saisie) ; la correction proposée le met à vide.",
                         correction=self.null_fix(c, idxs, "Un âge hors de 0–120 ans ne peut pas être exact."),
                         method="Bornes logiques d'un âge")
                continue
            neg = v < 0
            if neg.any() and (name_nonneg or v.dropna().ge(0).mean() >= 0.98):
                idxs = self.df.index[neg]
                status, sev, conf = (ERROR, "high", "élevée") if name_nonneg else (POTENTIAL, "medium", "moyenne")
                self.add("negative", "Valeur négative impossible" if name_nonneg else "Valeur négative inhabituelle",
                         "logic", status, sev, conf, [c], idxs, f"{_q(c)} < 0",
                         (f"Une valeur de type « {c} » (quantité, prix, montant, stock…) ne peut pas être négative."
                          if name_nonneg else
                          f"{v.dropna().ge(0).mean():.0%} des valeurs de {c} sont positives : ces valeurs négatives sont isolées."),
                         "Règle déduite du nom de la colonne : si des valeurs négatives sont légitimes (avoirs, retours), "
                         "écartez cette alerte." if name_nonneg else
                         "Une valeur négative isolée n'est pas forcément fausse : à confirmer avec le métier.",
                         "Vérifier la valeur dans le système source (signe inversé, retour mal codé…).",
                         correction=self.null_fix(c, idxs, "Une valeur négative fausserait les totaux."),
                         method="Règle de signe" + (" (nom de colonne)" if name_nonneg else " (distribution)"))
            if _UNIT_PRICE.search(c):
                zero = v == 0
                if zero.any() and zero.mean() < 0.05:
                    idxs = self.df.index[zero]
                    self.add("zero_price", "Prix ou coût unitaire nul", "logic", POTENTIAL, "medium", "moyenne", [c], idxs,
                             f"{_q(c)} = 0",
                             f"Un prix ou coût unitaire nul est rare ({zero.sum()} ligne(s) sur {self.n}) : souvent une valeur non renseignée.",
                             "Un article gratuit ou offert peut légitimement avoir un prix nul.",
                             "Vérifier s'il s'agit d'un article offert ; sinon renseigner le prix réel.",
                             correction=self.null_fix(c, idxs, "Un prix nul non justifié fausse le chiffre d'affaires et la marge."),
                             method="Valeur nulle sur un prix unitaire")

    def _dates(self, c: str, s: pd.Series):
        today = pd.Timestamp(date.today())
        old = s.notna() & (s < pd.Timestamp("1900-01-01"))
        if old.any():
            idxs = self.df.index[old]
            self.add("date_old", "Date antérieure à 1900", "logic", ERROR, "medium", "élevée", [c], idxs,
                     f"{_q(c)} < TIMESTAMP '1900-01-01'", "Une date antérieure à 1900 est presque toujours une valeur par défaut ou une erreur de saisie.",
                     "Les données historiques anciennes peuvent être légitimes.", "Corriger la date dans le système source.",
                     method="Bornes de date")
        fut = s.notna() & (s > today)
        if fut.any() and fut.mean() < 0.5:
            idxs = self.df.index[fut]
            self.add("date_future", "Date dans le futur", "logic", POTENTIAL, "low", "faible", [c], idxs,
                     f"{_q(c)} > TIMESTAMP '{today:%Y-%m-%d}'",
                     f"Ces dates sont postérieures à aujourd'hui ({today:%d/%m/%Y}).",
                     "Une échéance ou une date prévisionnelle peut légitimement être future.",
                     "Vérifier s'il s'agit d'une date prévue ; sinon corriger la saisie.", method="Comparaison à la date du jour")

    def missing(self):
        for c in self.cols:
            miss = self.df[c].isna()
            if not miss.any() or miss.all():
                continue
            idxs = [i for i in self.df.index[miss] if (i, c) not in self.flagged]
            if not idxs:
                continue
            share = len(idxs) / self.n
            ident = self.is_id(c)
            self.add("missing", "Valeur manquante", "quality", ERROR if ident else POTENTIAL,
                     "high" if ident else ("medium" if share >= 0.05 else "low"), "élevée", [c], idxs, f"{_q(c)} IS NULL",
                     f"{len(idxs)} ligne(s) sur {self.n} n'ont pas de valeur pour {c}"
                     + (" alors que cette colonne identifie chaque ligne." if ident else "."),
                     "Une absence peut être légitime (information non applicable) : seul le métier peut le dire.",
                     "Compléter la valeur depuis le système source. Aucune valeur n'est inventée : "
                     "la donnée manquante ne peut pas être déduite des autres colonnes.",
                     method="Comptage des valeurs vides (y compris NA, N/A, null, -)")

    def outliers(self):
        for c in self.cols:
            if not self.numeric(c) or _RATE.search(c):
                continue
            v = self.df[c].astype(float)
            x = v.dropna()
            if len(x) < 20 or x.nunique() < 10:
                continue
            med = float(x.median())
            mad = float((x - med).abs().median()) * 1.4826
            if mad == 0:
                continue
            z = (v - med) / mad
            q1, q3 = x.quantile(0.25), x.quantile(0.75)
            fence_lo, fence_hi = q1 - 3 * (q3 - q1), q3 + 3 * (q3 - q1)
            bad = v.notna() & (z.abs() > OUTLIER_Z) & ((v > fence_hi) | (v < fence_lo))
            bad &= ~pd.Series([(i, c) in self.flagged for i in self.df.index], index=self.df.index)
            if not bad.any():
                continue
            idxs = self.df.index[bad]
            ratio = float(v[bad].abs().max() / abs(med)) if med else None
            lo, hi = float(max(fence_lo, x.min())), float(min(fence_hi, x.max()))
            counts, edges = np.histogram(x, bins=20)
            self.add("outlier", "Valeur extrême", "stats", POTENTIAL, "high" if ratio and ratio >= 10 else "medium",
                     "moyenne", [c], idxs, f"{_q(c)} > {fence_hi:.6g} OR {_q(c)} < {fence_lo:.6g}",
                     f"Ces valeurs s'écartent de plus de {OUTLIER_Z:g} écarts robustes de la médiane ({med:,.2f}) et sortent "
                     f"de l'intervalle [{fence_lo:,.2f} ; {fence_hi:,.2f}]"
                     + (f" ; la plus forte vaut {ratio:.1f} fois la médiane." if ratio else ".")
                     + " Une valeur extrême n'est pas forcément fausse.",
                     "Méthode statistique : une grosse commande ou un pic réel produisent le même signal. "
                     "À confirmer avec le métier avant toute correction.",
                     "Vérifier ces valeurs dans le système source ; les exclure des moyennes tant qu'elles ne sont pas confirmées.",
                     method="Écart robuste à la médiane (MAD) et clôtures à 3 × l'écart interquartile",
                     chart={"type": "histogram", "edges": [round(float(e), 4) for e in edges],
                            "counts": [int(n) for n in counts], "median": med, "low": lo, "high": hi,
                            "flagged": [round(float(val), 4) for val in v[bad].head(MAX_ROWS)]})

    def relations(self):
        """Cherche C = A × B (ou A × B × (1 − D)) vérifiée sur ≥ 90 % des lignes ; signale les lignes qui la violent."""
        nums = [c for c in self.cols if self.numeric(c)][:10]
        rates = [c for c in nums if _RATE.search(c)]
        best = None
        for c in nums:
            for a, b in combinations([x for x in nums if x != c and x not in rates], 2):
                for d in [None, *[r for r in rates if r != c]]:
                    cols = [a, b, c] + ([d] if d else [])
                    sub = self.df[cols].astype(float).dropna()
                    if len(sub) < 10:
                        continue
                    pred = sub[a] * sub[b] * ((1 - sub[d]) if d else 1)
                    ok = (sub[c] - pred).abs() <= np.maximum(0.011, 0.005 * sub[c].abs())
                    if ok.mean() >= RELATION_SHARE and (best is None or ok.mean() > best[0]):
                        best = (ok.mean(), a, b, c, d)
        if not best:
            return
        share, a, b, c, d = best
        cols = [a, b, c] + ([d] if d else [])
        sub = self.df[cols].astype(float)
        pred = sub[a] * sub[b] * ((1 - sub[d]) if d else 1)
        viol = sub.notna().all(axis=1) & ((sub[c] - pred).abs() > np.maximum(0.011, 0.005 * sub[c].abs()))
        if not viol.any():
            return
        formula = f"{c} = {a} × {b}" + (f" × (1 − {d})" if d else "")
        expected, culprit_of = {}, {}
        for i in self.df.index[viol]:
            # colonne fautive : celle déjà signalée par une règle certaine, sinon le résultat de la formule
            suspects = [x for x in cols if (i, x) in self.flagged] or [x for x in cols if (i, x) in self.suspect]
            culprit = suspects[0] if suspects else c
            culprit_of[i] = culprit
            r = sub.loc[i]
            k = (1 - r[d]) if d else 1.0
            try:
                if culprit == c:
                    val = r[a] * r[b] * k
                elif culprit in (a, b):
                    other = r[b] if culprit == a else r[a]
                    val = r[c] / (other * k) if other * k else None
                else:
                    val = 1 - r[c] / (r[a] * r[b]) if r[a] * r[b] else None
            except ZeroDivisionError:
                val = None
            if val is None or not np.isfinite(val):
                expected[i] = None
            elif pd.api.types.is_integer_dtype(self.df[culprit]):
                expected[i] = int(round(val))
            else:
                expected[i] = round(float(val), 4)
        idxs = list(self.df.index[viol])
        fixes = [f"UPDATE {_q(self.table)} SET {_q(culprit_of[i])} = {expected[i]} WHERE {self.where_ids([i])};"
                 for i in idxs if expected[i] is not None and self.key and
                 (culprit_of[i] == c or not (expected[i] < 0 and _NONNEG.search(culprit_of[i])))]
        rows_expected = {i: expected[i] if expected[i] is not None else "non déductible" for i in idxs}
        self.add("relation", "Incohérence entre colonnes", "business", ERROR, "high", "élevée" if share >= 0.98 else "moyenne",
                 cols, idxs, f"abs({_q(c)} - {_q(a)} * {_q(b)}{f' * (1 - {_q(d)})' if d else ''}) > greatest(0.011, 0.005 * abs({_q(c)}))",
                 f"La relation {formula} est respectée par {share:.1%} des lignes complètes du fichier ; ces lignes la violent. "
                 "Quand une des colonnes est déjà signalée (valeur négative, taux impossible…), c'est elle qui est désignée "
                 "comme fautive ; sinon c'est le résultat du calcul.",
                 "La relation est déduite des données, pas d'une règle métier déclarée. La colonne fautive est une hypothèse "
                 "à valider : la valeur proposée est celle qui rétablit la relation.",
                 f"Recalculer la colonne fautive pour rétablir {formula}, après vérification dans le système source.",
                 expected=rows_expected, focus=culprit_of,
                 correction={"sql": "\n".join(fixes), "derived": True,
                             "description": f"Rétablir la relation {formula} en recalculant la colonne fautive de chaque ligne.",
                             "consequences": "Les totaux de la colonne corrigée changent ; à valider ligne par ligne."} if fixes else None,
                 method=f"Relation entre colonnes découverte automatiquement ({formula})")


def detect_quality(wh, tables: list[str], import_reports: dict[str, dict] | None = None) -> tuple[list[Anomaly], list[str]]:
    out, skipped = [], []
    for t in tables:
        try:
            out += TableQuality(wh, t, (import_reports or {}).get(t)).run()
        except Exception as exc:  # une table illisible ne bloque pas les autres
            skipped.append(f"Contrôles qualité de {t} : {type(exc).__name__} : {exc}")
    return out, skipped
