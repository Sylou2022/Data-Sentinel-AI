"""Import et normalisation de fichiers CSV de structure inconnue.

Aucun schéma métier n'est imposé : encodage, séparateur, format des nombres et des dates sont
détectés à partir du contenu. Tout ce qui est déduit est consigné dans un rapport d'import
(colonne par colonne) ; toute ambiguïté est signalée plutôt que tranchée en silence, et les
lignes mal formées sont listées avec leur numéro de ligne dans le fichier.

Le fichier d'origine n'est jamais modifié : chaque ligne importée conserve son numéro de ligne
source (colonne technique ``_ligne_source``) pour que tout diagnostic reste traçable.
"""
from __future__ import annotations

import csv
import io
import re
import unicodedata
from dataclasses import asdict, dataclass, field

import numpy as np
import pandas as pd

LINE_COL = "_ligne_source"
ENCODINGS = ("utf-8-sig", "cp1252", "latin-1")
DELIMITERS = [",", ";", "\t", "|"]
DELIMITER_LABELS = {",": "virgule", ";": "point-virgule", "\t": "tabulation", "|": "barre verticale"}
MISSING_TOKENS = {"", "na", "n/a", "nan", "null", "none", "nr", "#n/a", "-", "--", "?"}
# Formats de date essayés (le premier qui couvre toutes les valeurs l'emporte, ISO d'abord).
DATE_FORMATS = ["%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y/%m/%d",
                "%d/%m/%Y", "%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M", "%m/%d/%Y", "%m/%d/%Y %H:%M:%S",
                "%d-%m-%Y", "%d.%m.%Y", "%Y%m%d"]
TYPE_SHARE = 0.95       # part minimale de valeurs conformes pour retenir un type
MIXED_SHARE = 0.5       # au-delà, la colonne est signalée « types mixtes »
MAX_BAD_LINES = 50      # lignes mal formées détaillées dans le rapport (toutes sont comptées)
PREVIEW_ROWS = 10

_INT = re.compile(r"^[+-]?\d+$")
_DEC_POINT = re.compile(r"^[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?$")
_DEC_COMMA = re.compile(r"^[+-]?\d+,\d+$")
_THOUSANDS = re.compile(r"^[+-]?\d{1,3}([   .]\d{3})+(,\d+)?$")
_PERCENT = re.compile(r"^[+-]?\d+([.,]\d+)?\s?%$")
_BOOL = {"true": True, "false": False, "vrai": True, "faux": False, "oui": True, "non": False, "yes": True, "no": False}


class CsvImportError(ValueError):
    """Fichier inexploitable : le message explique la cause et, si possible, comment poursuivre."""


@dataclass
class ColumnReport:
    name: str
    original_name: str
    type: str                     # entier, décimal, pourcentage, date, booléen, texte, vide
    missing: int
    detail: str = ""              # format retenu (ex. « JJ/MM/AAAA », « virgule décimale »)
    nonconforming: int = 0        # valeurs non conformes au type retenu (laissées vides après conversion)
    nonconforming_examples: list[str] = field(default_factory=list)
    nonconforming_lines: list[int] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass
class ImportReport:
    file: str
    table: str
    encoding: str
    delimiter: str
    rows: int
    columns: list[ColumnReport]
    bad_lines: list[dict] = field(default_factory=list)
    bad_line_count: int = 0
    duplicate_rows: int = 0
    warnings: list[str] = field(default_factory=list)
    preview: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["delimiter_label"] = DELIMITER_LABELS.get(self.delimiter, repr(self.delimiter))
        return d


# ---------------------------------------------------------------- lecture brute
def decode(content: bytes) -> tuple[str, str]:
    """Essaie UTF-8 puis les encodages Windows courants. latin-1 ne peut pas échouer : c'est le dernier recours."""
    for enc in ENCODINGS:
        try:
            return content.decode(enc), ("utf-8" if enc == "utf-8-sig" else enc)
        except UnicodeDecodeError:
            continue
    raise AssertionError("inaccessible")  # latin-1 décode tout octet


def _field_counts(sample: str, delim: str) -> list[int]:
    return [len(r) for r in csv.reader(io.StringIO(sample), delimiter=delim) if r]


def detect_delimiter(text: str) -> tuple[str, list[str]]:
    """Retient le séparateur qui donne un nombre de colonnes > 1 et le plus régulier sur l'échantillon."""
    sample = "\n".join(text.splitlines()[:200])
    scores = []
    for d in DELIMITERS:
        counts = _field_counts(sample, d)
        if not counts or counts[0] < 2:
            continue
        regular = sum(c == counts[0] for c in counts) / len(counts)
        scores.append((regular, counts[0], d))
    if not scores:
        return ",", ["Une seule colonne détectée : aucun séparateur courant (virgule, point-virgule, tabulation, "
                     "barre verticale) ne découpe l'en-tête. Vérifiez le séparateur si le fichier en contient plusieurs."]
    scores.sort(reverse=True)
    best = scores[0]
    warnings = []
    if best[0] < 0.9:
        warnings.append(f"Séparateur « {DELIMITER_LABELS[best[2]]} » retenu, mais seules {best[0]:.0%} des lignes de "
                        "l'échantillon ont le même nombre de colonnes que l'en-tête.")
    rivals = [s for s in scores[1:] if s[0] >= 0.99 and best[0] >= 0.99]
    if rivals:
        alt = ", ".join(DELIMITER_LABELS[s[2]] for s in rivals)
        warnings.append(f"Séparateur ambigu : « {DELIMITER_LABELS[best[2]]} » retenu ({best[1]} colonnes), mais "
                        f"{alt} découpe aussi le fichier de façon régulière. Forcez le séparateur si l'aperçu est incorrect.")
    return best[2], warnings


def safe_table_name(stem: str) -> str:
    s = unicodedata.normalize("NFKD", stem).encode("ascii", "ignore").decode()
    s = re.sub(r"[^A-Za-z0-9_]+", "_", s).strip("_").lower() or "table"
    return "t_" + s if s[0].isdigit() else s


def _clean_headers(header: list[str]) -> tuple[list[str], list[str]]:
    names, warnings, seen = [], [], {}
    for i, raw in enumerate(header, start=1):
        name = raw.strip().lstrip("﻿")
        if not name:
            name = f"colonne_{i}"
            warnings.append(f"La colonne n°{i} n'a pas de nom : elle est nommée « {name} ».")
        base = name
        if name in seen:
            seen[base] += 1
            name = f"{base}_{seen[base]}"
            warnings.append(f"Nom de colonne en double « {base} » : la seconde occurrence est renommée « {name} ».")
        else:
            seen[name] = 1
        if name == LINE_COL:
            raise CsvImportError(f"La colonne « {LINE_COL} » est réservée par DataSentinel AI : renommez-la dans le fichier.")
        names.append(name)
    return names, warnings


# ---------------------------------------------------------------- inférence des types
def _missing_mask(s: pd.Series) -> pd.Series:
    return s.str.strip().str.lower().isin(MISSING_TOKENS)


def _to_number(values: pd.Series, decimal_comma: bool) -> pd.Series:
    v = values.str.strip().str.replace(r"[   ]", "", regex=True)
    if decimal_comma:
        v = v.str.replace(".", "", regex=False).str.replace(",", ".", regex=False)
    return pd.to_numeric(v, errors="coerce")


def _infer_numeric(v: pd.Series, delimiter: str) -> tuple[str, str, pd.Series] | None:
    stripped = v.str.strip()
    if stripped.str.match(r"^0\d").any() and stripped.str.match(_INT).mean() >= TYPE_SHARE:
        return None  # codes à zéro initial (codes postaux, références) : à conserver en texte
    pct = stripped.str.match(_PERCENT)
    if pct.mean() >= TYPE_SHARE:
        num = _to_number(stripped.str.rstrip("%").str.strip(), stripped.str.contains(",").any()) / 100
        return "pourcentage", "valeurs en %, converties en fraction (50 % → 0,5)", num
    is_int = stripped.str.match(_INT)
    point = stripped.str.match(_DEC_POINT)
    comma = stripped.str.match(_DEC_COMMA)
    thousands = stripped.str.match(_THOUSANDS)
    if is_int.mean() >= TYPE_SHARE:
        return "entier", "", pd.to_numeric(stripped.where(is_int), errors="coerce")
    if delimiter != "," and (comma | is_int | thousands).mean() >= TYPE_SHARE and comma.any():
        return "décimal", "virgule décimale", _to_number(stripped.where(comma | is_int | thousands), True)
    if (point | thousands).mean() >= TYPE_SHARE:
        detail = "séparateur de milliers retiré" if thousands.any() else ""
        return "décimal", detail, _to_number(stripped.where(point | thousands), False)
    return None


def _infer_date(v: pd.Series) -> tuple[str, list[str], pd.Series] | None:
    stripped = v.str.strip()
    if not stripped.str.match(r"^\d").all():
        return None
    best = None
    for fmt in DATE_FORMATS:
        parsed = pd.to_datetime(stripped, format=fmt, errors="coerce")
        share = parsed.notna().mean()
        if share >= TYPE_SHARE and (best is None or share > best[1]):
            best = (fmt, share, parsed)
            if share == 1.0:
                break
    if best is None:
        return None
    fmt, _, parsed = best
    warnings = []
    swapped = {"%d/%m/%Y": "%m/%d/%Y", "%m/%d/%Y": "%d/%m/%Y"}.get(fmt.split(" ")[0])
    if swapped and pd.to_datetime(stripped.str.split(" ").str[0], format=swapped, errors="coerce").notna().all():
        warnings.append("Format de date ambigu : jour et mois ne dépassent jamais 12, JJ/MM et MM/JJ sont tous deux possibles. "
                        f"Interprétation retenue : {_fmt_label(fmt)}. Vérifiez sur l'aperçu.")
    return fmt, warnings, parsed


def _fmt_label(fmt: str) -> str:
    return (fmt.replace("%d", "JJ").replace("%m", "MM").replace("%Y", "AAAA")
               .replace("%H", "hh").replace("%M", "mm").replace("%S", "ss").replace("T", " "))


def infer_column(raw: pd.Series, name: str, original: str, lines: pd.Series, delimiter: str) -> tuple[pd.Series, ColumnReport]:
    missing = _missing_mask(raw)
    values = raw[~missing]
    rep = ColumnReport(name=name, original_name=original, type="texte", missing=int(missing.sum()))
    if values.empty:
        rep.type = "vide"
        rep.warnings.append("Colonne entièrement vide.")
        return pd.Series([None] * len(raw), index=raw.index, dtype=object), rep

    converted: pd.Series | None = None
    if (b := values.str.strip().str.lower()).isin(_BOOL).all() and not values.str.strip().isin(["0", "1"]).all():
        rep.type, converted = "booléen", b.map(_BOOL).astype("boolean")
    elif (num := _infer_numeric(values, delimiter)) is not None:
        rep.type, rep.detail, converted = num
    elif (dt := _infer_date(values)) is not None:
        fmt, warns, converted = dt
        rep.type, rep.detail = "date", _fmt_label(fmt)
        rep.warnings += warns
    else:
        # Types mixtes : majoritairement numérique sans atteindre le seuil, la colonne reste texte.
        numeric_like = values.str.strip().str.match(r"^[+-]?\d+([.,]\d+)?$")
        if MIXED_SHARE <= numeric_like.mean() < TYPE_SHARE:
            odd = values[~numeric_like]
            rep.warnings.append(f"Types mixtes : {numeric_like.mean():.0%} de valeurs numériques, la colonne est conservée "
                                f"en texte. Valeurs non numériques : {', '.join(map(repr, odd.head(5)))}.")
            rep.nonconforming = int(len(odd))
            rep.nonconforming_examples = odd.head(5).tolist()
            rep.nonconforming_lines = lines[odd.index].head(20).astype(int).tolist()
        return raw.str.strip().astype(object).where(~missing, None), rep

    bad = converted.isna()
    if bad.any():
        rep.nonconforming = int(bad.sum())
        rep.nonconforming_examples = values[bad].head(5).tolist()
        rep.nonconforming_lines = lines[values[bad].index].head(20).astype(int).tolist()
        rep.warnings.append(f"{rep.nonconforming} valeur(s) non conforme(s) au type {rep.type} "
                            f"({', '.join(map(repr, rep.nonconforming_examples))}) : laissées vides dans la table analysée, "
                            "intactes dans le fichier d'origine.")
    out = pd.Series(pd.NA if rep.type in ("entier", "booléen") else np.nan, index=raw.index,
                    dtype={"entier": "Int64", "booléen": "boolean", "date": "datetime64[ns]"}.get(rep.type, "float64"))
    out[values.index] = converted.astype(out.dtype) if rep.type != "entier" else converted.round().astype("Int64")
    return out, rep


# ---------------------------------------------------------------- point d'entrée
def read_csv(content: bytes | str, file_name: str = "fichier.csv", sep: str | None = None) -> tuple[pd.DataFrame, ImportReport]:
    """Lit un CSV quelconque. sep=None : détection automatique.

    Retourne la table typée (avec ``_ligne_source``) et le rapport d'import. Lève CsvImportError
    avec un diagnostic précis si le fichier est inexploitable."""
    if isinstance(content, bytes):
        if not content.strip():
            raise CsvImportError(f"{file_name} : fichier vide (0 octet utile).")
        if b"\x00" in content[:4096]:
            raise CsvImportError(f"{file_name} : contenu binaire détecté (octets nuls). S'agit-il d'un fichier Excel "
                                 "(.xlsx) renommé en .csv ? Réenregistrez-le au format « CSV » depuis le tableur.")
        text, encoding = decode(content)
    else:
        text, encoding = content, "utf-8"
    if not text.strip():
        raise CsvImportError(f"{file_name} : fichier vide.")

    warnings: list[str] = []
    if encoding != "utf-8":
        warnings.append(f"Encodage détecté : {encoding} (fichier Windows/Excel) ; les accents ont été convertis en UTF-8.")
    if sep is None:
        sep, w = detect_delimiter(text)
        warnings += w

    reader = csv.reader(io.StringIO(text, newline=""), delimiter=sep)
    try:
        header = next(r for r in reader if any(c.strip() for c in r))
    except StopIteration:
        raise CsvImportError(f"{file_name} : aucune ligne d'en-tête.") from None
    names, w = _clean_headers(header)
    warnings += w
    width = len(names)

    rows, lines, bad, bad_count, multiline = [], [], [], 0, []
    prev_line = reader.line_num
    try:
        for row in reader:
            start, prev_line = prev_line + 1, reader.line_num
            if reader.line_num > start:
                multiline.append(start)
            if not row or not any(c.strip() for c in row):
                continue  # lignes vides ignorées
            if len(row) != width:
                bad_count += 1
                if len(bad) < MAX_BAD_LINES:
                    bad.append({"line": start, "expected": width, "found": len(row),
                                "excerpt": sep.join(row)[:160]})
                continue
            rows.append(row)
            lines.append(start)
    except csv.Error as exc:
        raise CsvImportError(f"{file_name}, ligne {reader.line_num} : structure CSV illisible ({exc}). "
                             "Cause fréquente : guillemet non refermé. Corrigez cette ligne puis réimportez.") from None

    if multiline:
        warnings.append(f"{len(multiline)} enregistrement(s) s'étendent sur plusieurs lignes du fichier (à partir des lignes "
                        f"{', '.join(map(str, multiline[:10]))}). C'est normal pour un texte entre guillemets contenant un "
                        "retour à la ligne, mais peut aussi signaler un guillemet non refermé : vérifiez ces lignes.")
    if bad_count:
        warnings.append(f"{bad_count} ligne(s) mal formée(s) (nombre de champs différent des {width} colonnes de l'en-tête) "
                        "écartée(s) de l'analyse ; elles sont listées dans le rapport d'import.")
    if not rows and bad_count:
        raise CsvImportError(f"{file_name} : aucune ligne n'a le même nombre de champs que l'en-tête ({width}). "
                             f"Le séparateur « {DELIMITER_LABELS.get(sep, sep)} » est probablement incorrect : forcez-en un autre.")
    if not rows:
        warnings.append("Le fichier ne contient qu'un en-tête : aucune ligne de données à analyser.")

    raw = pd.DataFrame(rows, columns=names, dtype=object).astype(str) if rows else pd.DataFrame(columns=names, dtype=str)
    line_s = pd.Series(lines, dtype="int64")
    out = pd.DataFrame({LINE_COL: line_s})
    col_reports = []
    for orig, name in zip(header, names):
        series, rep = infer_column(raw[name], name, orig.strip(), line_s, sep)
        out[name] = series
        col_reports.append(rep)
    out = out[[LINE_COL, *names]]

    dup = int(raw.duplicated().sum()) if rows else 0
    preview = raw.head(PREVIEW_ROWS).assign(**{LINE_COL: line_s.head(PREVIEW_ROWS)})[[LINE_COL, *names]]
    report = ImportReport(
        file=file_name, table=safe_table_name(file_name.rsplit(".", 1)[0]), encoding=encoding, delimiter=sep,
        rows=len(rows), columns=col_reports, bad_lines=bad, bad_line_count=bad_count, duplicate_rows=dup,
        warnings=warnings, preview=preview.to_dict(orient="records"),
    )
    return out, report
