from datasentinel.generator import INCIDENTS, generate


def test_generation_is_reproducible():
    a, _ = generate("all")
    b, _ = generate("all")
    for name in a:
        assert a[name].equals(b[name]), name


def test_baseline_has_no_ground_truth_and_no_duplicates():
    tables, truth = generate("baseline")
    assert truth == []
    assert tables["orders"]["order_id"].is_unique
    assert tables["products"]["category"].notna().all()


def test_each_incident_is_documented_in_ground_truth():
    for incident in INCIDENTS:
        _, truth = generate(incident)
        assert [t.incident for t in truth] == [incident]
    _, truth = generate("all")
    assert sorted(t.incident for t in truth) == sorted(INCIDENTS)


def test_no_personal_data():
    tables, _ = generate("baseline")
    assert set(tables["customers"].columns) == {"customer_id", "segment", "region"}
