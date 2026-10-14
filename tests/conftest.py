import sys
from pathlib import Path

import pytest
from langgraph.checkpoint.memory import MemorySaver

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from datasentinel.config import Settings  # noqa: E402
from datasentinel.generator import build_lab  # noqa: E402


@pytest.fixture(scope="session")
def settings(tmp_path_factory) -> Settings:
    s = Settings(home=tmp_path_factory.mktemp("home"), llm_provider="none")
    s.ensure_dirs()
    return s


@pytest.fixture(scope="session")
def lab(settings):
    cache: dict[str, Path] = {}

    def _get(scenario: str) -> Path:
        if scenario not in cache:
            cache[scenario] = build_lab(scenario, settings.labs_dir, write_csv=False)
        return cache[scenario]

    return _get


@pytest.fixture
def service(settings):
    from datasentinel.service import DataSentinel

    return DataSentinel(settings, checkpointer=MemorySaver())
