"""Import de CSV hétérogènes : aucun schéma imposé, ambiguïtés signalées, diagnostics précis."""
import pandas as pd
import pytest

from datasentinel.ingestion import LINE_COL, CsvImportError, read_csv, safe_table_name
from datasentinel.warehouse import Warehouse, import_report, load_csv_workspace


def _types(rep):
    return {c.name: c.type for c in rep.columns}


@pytest.mark.parametrize("sep", [",", ";", "\t", "|"])
def test_delimiter_is_detected(sep):
    content = sep.join(["code", "qte", "prix"]) + "\n" + "\n".join(sep.join([f"A{i}", str(i), f"{i}.5"]) for i in range(20))
    df, rep = read_csv(content.encode(), "f.csv")
    assert rep.delimiter == sep and rep.rows == 20
    assert _types(rep) == {"code": "texte", "qte": "entier", "prix": "décimal"}


def test_windows_encoding_decimal_comma_and_french_dates():
    content = "nom;montant;date\nCafé;1 234,50;31/01/2024\nThé;12,5;15/02/2024\n".encode("cp1252")
    df, rep = read_csv(content, "ventes.csv")
    assert rep.encoding == "cp1252" and any("cp1252" in w for w in rep.warnings)
    assert df["nom"].tolist() == ["Café", "Thé"]
    assert df["montant"].tolist() == [1234.5, 12.5]
    assert df["date"].tolist() == [pd.Timestamp("2024-01-31"), pd.Timestamp("2024-02-15")]
    assert _types(rep)["date"] == "date" and rep.columns[2].detail == "JJ/MM/AAAA"


def test_ambiguous_dates_are_flagged_not_silently_chosen():
    df, rep = read_csv(b"d,v\n01/02/2024,1\n03/04/2024,2\n", "f.csv")
    assert _types(rep)["d"] == "date"
    assert any("ambigu" in w for w in rep.columns[0].warnings)


def test_ambiguous_delimiter_is_flagged():
    _, rep = read_csv(b"a;b,c\n1;2,5\n3;4,5\n", "f.csv")
    assert any("Séparateur ambigu" in w for w in rep.warnings)


def test_malformed_lines_are_reported_with_line_numbers_and_excluded():
    df, rep = read_csv(b"a,b,c\n1,2,3\n\n4,5\n6,7,8,9\n10,11,12\n", "f.csv")
    assert rep.bad_line_count == 2
    assert [(b["line"], b["found"]) for b in rep.bad_lines] == [(4, 2), (5, 4)]
    assert df[LINE_COL].tolist() == [2, 6]  # numéros de ligne du fichier d'origine


def test_unclosed_quote_is_flagged():
    _, rep = read_csv(b'a,b\n1,"ok"\n2,"jamais referme\n3,x\n', "f.csv")
    assert any("plusieurs lignes" in w for w in rep.warnings)


def test_mixed_types_and_nonconforming_values_are_traced():
    rows = "\n".join(f"{i},{i * 10}" for i in range(40))
    df, rep = read_csv(f"id,val\n{rows}\n40,abc\n".encode(), "f.csv")
    val = rep.columns[1]
    assert val.type == "entier" and val.nonconforming == 1
    assert val.nonconforming_examples == ["abc"] and val.nonconforming_lines == [42]
    _, rep = read_csv(b"id,val\n1,10\n2,abc\n3,30\n4,x\n", "f.csv")
    assert rep.columns[1].type == "texte" and any("Types mixtes" in w for w in rep.columns[1].warnings)


def test_leading_zero_codes_stay_text_and_missing_tokens_are_null():
    df, rep = read_csv(b"cp,v\n01000,NA\n75001,\n13001,3\n", "f.csv")
    assert df["cp"].tolist() == ["01000", "75001", "13001"]
    assert rep.columns[1].missing == 2 and df["v"].isna().sum() == 2


def test_header_problems_are_repaired_and_reported():
    df, rep = read_csv(b"a,a,\n1,2,3\n", "f.csv")
    assert list(df.columns) == [LINE_COL, "a", "a_2", "colonne_3"]
    assert len(rep.warnings) == 2


def test_percent_and_boolean_columns():
    df, rep = read_csv(b"taux,actif\n50%,oui\n12.5%,non\n", "f.csv")
    assert df["taux"].tolist() == [0.5, 0.125] and df["actif"].tolist() == [True, False]


@pytest.mark.parametrize("content, message", [
    (b"", "vide"),
    (b"   \n\n", "vide"),
    (b"PK\x03\x04\x00\x00", "Excel"),
    (b"a,b\n1;2;3\n4;5;6\n", "séparateur"),
])
def test_unusable_files_get_a_precise_diagnostic(content, message):
    with pytest.raises(CsvImportError, match=message):
        read_csv(content, "f.csv", sep="," if b";" in content else None)


def test_header_only_file_is_accepted_with_warning():
    df, rep = read_csv(b"a;b\n", "f.csv")
    assert rep.rows == 0 and any("qu'un en-tête" in w for w in rep.warnings)


def test_table_names_from_any_file_name():
    assert safe_table_name("Ventes 2024 (été)") == "ventes_2024_ete"
    assert safe_table_name("2024-stocks") == "t_2024_stocks"


def test_workspace_accepts_unknown_schemas_and_keeps_report(tmp_path):
    files = {"Clients.csv": b"customer_id;age\nC1;30\nC2;240\n",
             "kpi mensuels.csv": b"month,revenue\n2024-01-01,100.5\n2024-02-01,99\n"}
    db = load_csv_workspace(files, tmp_path / "w" / "warehouse.duckdb")
    with Warehouse(db) as wh:
        assert set(wh.tables()) == {"clients", "kpi_mensuels"}
        assert wh.query("SELECT max(age) AS m FROM clients")["m"].iloc[0] == 240
        assert str(wh.query("SELECT * FROM kpi_mensuels")["month"].dtype).startswith("datetime64")
    rep = import_report(db)
    assert [r["table"] for r in rep] == ["clients", "kpi_mensuels"] and rep[0]["delimiter_label"] == "point-virgule"


def test_upload_shows_import_diagnostic_in_profile(service):
    from fastapi.testclient import TestClient

    import api.main as api_main

    api_main.app.dependency_overrides[api_main.get_service] = lambda: service
    try:
        client = TestClient(api_main.app)
        content = "produit;prix\nCafé;2,5\nThé;3\nlignecassée\n".encode("cp1252")
        db = client.post("/ui/api/sources/upload", files={"files": ("Stock été.csv", content)},
                         data={"sep": "auto"}).json()["db_path"]
        prof = client.get("/ui/api/profile", params={"db_path": db}).json()
    finally:
        api_main.app.dependency_overrides.clear()
    t = prof[0]
    assert t["table"] == "stock_ete" and [c["name"] for c in t["columns"]] == ["produit", "prix"]
    assert t["import"]["encoding"] == "cp1252" and t["import"]["bad_lines"][0]["line"] == 4
    assert t["columns"][1]["type"] == "Montant (virgule décimale)"
