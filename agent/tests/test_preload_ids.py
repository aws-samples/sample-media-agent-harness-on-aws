from preload.ids import project_id_for_name
from projects.ids import is_project_id


def test_project_id_is_well_formed_and_stable():
    a = project_id_for_name("Demo Catalog")
    b = project_id_for_name("Demo Catalog")
    assert a == b                      # deterministic: a re-deploy derives the same id
    assert is_project_id(a)            # matches ^p[0-9a-f]{32}$
    assert a.startswith("p") and len(a) == 33


def test_distinct_names_distinct_ids():
    assert project_id_for_name("One") != project_id_for_name("Two")


def test_empty_name_rejected():
    import pytest
    with pytest.raises(ValueError):
        project_id_for_name("")
