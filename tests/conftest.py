import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from prism.app import create_app  # noqa: E402
from prism.config import Settings  # noqa: E402
from prism.providers.fake import FakeProvider  # noqa: E402

SEARCH = "prism-sk-search-1a2b3c"
RESEARCH = "prism-sk-research-4d5e6f"
FREE = "prism-sk-free-7g8h9i"
BUDGET = "prism-sk-budget-demo-0j1k2l"
ADMIN = {"X-Admin-Token": "prism-admin-demo"}


def auth(key):
    return {"Authorization": f"Bearer {key}"}


def body(prompt, model="fast", **extra):
    return {"model": model, "messages": [{"role": "user", "content": prompt}], **extra}


@pytest.fixture
def env(tmp_path):
    """A gateway wired to two in-process fake providers and a throwaway database."""
    alpha, beta = FakeProvider("alpha"), FakeProvider("beta")
    app = create_app(Settings(db_path=str(tmp_path / "t.sqlite3")), providers={"alpha": alpha, "beta": beta})
    # no real sleeping during retry tests
    app.state.gateway.executor._sleep = _no_sleep
    with TestClient(app) as client:
        yield type("Env", (), {"client": client, "alpha": alpha, "beta": beta, "app": app, "db": app.state.db})


async def _no_sleep(_):
    return None
