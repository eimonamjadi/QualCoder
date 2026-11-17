import os
import sys
from pathlib import Path

import pytest

os.environ["QUALCODE_NO_AUTOAPP"] = "1"  # don't build the module-level app on import
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from qualcode.main import create_app  # noqa: E402
from qualcode.providers import MockProvider  # noqa: E402


@pytest.fixture
def client(tmp_path):
    app = create_app(str(tmp_path / "test.db"), providers={"mock": MockProvider()})
    c = TestClient(app)
    c.put("/api/settings", json={"provider": "mock"})
    return c
