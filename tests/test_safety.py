import hashlib

import duckdb
import pytest

from datasentinel.llm import CERTAINTY, TemplateNarrator
from datasentinel.remediation import validate_sandbox_sql
from datasentinel.warehouse import Warehouse


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_source_is_read_only(lab):
    with Warehouse(lab("duplicate_orders")) as wh:
        with pytest.raises(PermissionError):
            wh.query("DELETE FROM orders")
        with pytest.raises(duckdb.Error):
            wh.con.execute("DELETE FROM orders")


def test_sandbox_changes_do_not_touch_source(lab):
    db = lab("duplicate_orders")
    before = _sha(db)
    with Warehouse(db) as wh:
        sb = wh.sandbox()
        sb.execute("DELETE FROM orders")
        assert int(sb.query("SELECT count(*) AS n FROM orders")["n"].iloc[0]) == 0
        assert int(wh.query("SELECT count(*) AS n FROM orders")["n"].iloc[0]) > 0
        sb.close()
    assert _sha(db) == before


def test_sandbox_has_no_file_access(lab, tmp_path):
    with Warehouse(lab("baseline")) as wh:
        sb = wh.sandbox()
        with pytest.raises(duckdb.Error):
            sb.execute(f"COPY orders TO '{(tmp_path / 'leak.csv').as_posix()}'")
        sb.close()


@pytest.mark.parametrize("sql", ["ATTACH 'x.db' AS x", "COPY orders TO 'out.csv'", "SELECT * FROM read_csv('a.csv')",
                                 "INSTALL httpfs"])
def test_reviewer_sql_is_filtered(sql):
    with pytest.raises(PermissionError):
        validate_sandbox_sql(sql)


def test_certainty_guard_detects_overclaims():
    assert CERTAINTY.search("La cause est certaine.")
    assert CERTAINTY.search("C'est confirmé par les données")
    assert not CERTAINTY.search("La cause est probable, à vérifier après rechargement.")


def test_template_narrator_keeps_status_and_uncertainty():
    rc = {"statement": "Lot partiel.", "assessment": {"status": "probable", "confidence": "élevée",
                                                      "uncertainties": ["Effet estimé."]},
          "impact": {"measurements": [], "dashboards": ["Ventes"], "decisions": ["Objectifs"],
                     "month_to_date": {"revenue_error_share": None}}}
    text = TemplateNarrator().explain(rc)["text"]
    assert text.startswith("Probable")
    assert "Effet estimé." in text


def test_claude_without_key_falls_back_to_template(monkeypatch):
    from datasentinel.llm import ClaudeNarrator

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    rc = {"statement": "Lot partiel.", "assessment": {"status": "probable", "confidence": "élevée", "uncertainties": []},
          "impact": {"measurements": [], "dashboards": [], "decisions": [], "month_to_date": {"revenue_error_share": None}}}
    out = ClaudeNarrator("claude-opus-5-5").explain(rc)
    assert out["provider"] == "local-template" and out["guard"].startswith("llm_error")
