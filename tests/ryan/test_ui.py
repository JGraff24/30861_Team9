from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import SystemSettings
from app.contracts.ports import SystemServices
from app.ui.routes import create_router


def test_root_and_mount():
    app = FastAPI()
    app.include_router(create_router(SystemServices(settings=SystemSettings())))
    parent = FastAPI()
    parent.mount("/system", app)
    for application, base in ((app, ""), (parent, "/system")):
        client = TestClient(application)
        response = client.get(base + "/")
        assert response.status_code == 200
        assert f'src="{base}/ui/app.js"' in response.text
        assert 'for="file"' in response.text
        assert 'role="status"' in response.text
        assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
        assert client.get(base + "/ui/app.js").status_code == 200
        assert client.get(base + "/ui/style.css").status_code == 200
