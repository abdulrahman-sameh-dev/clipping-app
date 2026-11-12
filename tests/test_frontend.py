from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from clipping_ai.api.app import create_app
from tests.helpers import make_manager, make_registry

FRONTEND_DIR = Path(__file__).resolve().parents[1] / "clipping_ai" / "frontend"


def _client(tmp_path: Path) -> TestClient:
    registry = make_registry(providers={}, models=[], routing={})
    manager = make_manager(registry, {}, tmp_path)
    return TestClient(create_app(manager, start_worker=False))


def test_root_redirects_to_studio(tmp_path):
    client = _client(tmp_path)
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 307
    assert response.headers["location"] == "/ui/"


def test_studio_index_serves_app_shell(tmp_path):
    client = _client(tmp_path)
    response = client.get("/ui/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    body = response.text
    assert 'class="app-shell"' in body
    assert 'src="./js/app.js"' in body
    assert 'id="studio-dialog"' in body
    assert "{{" not in body


def test_studio_assets_are_served(tmp_path):
    client = _client(tmp_path)
    for path, content_type in [
        ("/ui/styles.css", "text/css"),
        ("/ui/js/app.js", "text/javascript"),
        ("/ui/js/api.js", "text/javascript"),
        ("/ui/js/i18n.js", "text/javascript"),
        ("/ui/js/ui.js", "text/javascript"),
    ]:
        response = client.get(path)
        assert response.status_code == 200, path
        assert response.headers["content-type"].startswith(content_type), path
        assert response.text.strip(), path


def test_studio_unknown_asset_is_not_found(tmp_path):
    client = _client(tmp_path)
    assert client.get("/ui/does-not-exist.js").status_code == 404


def test_frontend_files_are_present():
    for path in [
        "index.html",
        "styles.css",
        "js/app.js",
        "js/api.js",
        "js/i18n.js",
        "js/ui.js",
    ]:
        assert (FRONTEND_DIR / path).is_file(), path
