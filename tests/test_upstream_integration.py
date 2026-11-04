"""Exercise the merged API in-process; never connect to a deployed terminal."""
import asyncio
import os
from pathlib import Path
import shlex
import subprocess
import sys
from unittest.mock import AsyncMock

os.environ.setdefault("OPEN_TERMINAL_API_KEY", "test-token")

import httpx
import pytest
from fastapi import HTTPException, Request
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from open_terminal import main
from open_terminal.dependencies import get_filesystem
from open_terminal.utils.fs import UserFS
from open_terminal.utils import events

AUTH = {"Authorization": "Bearer test-token", "X-User-Id": "integration-owner", "X-Session-Id": "integration-chat"}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "_processes", {})
    monkeypatch.setattr(main, "_process_list_changed", asyncio.Event())
    monkeypatch.setattr(main, "_session_cwds", {})
    # Own every filesystem operation and process log created by these tests.
    main.app.dependency_overrides[get_filesystem] = lambda: UserFS(home=str(tmp_path))
    # execute and WS handlers call get_filesystem directly, so supply the same fixture there.
    from unittest.mock import patch
    with patch.object(main, "get_filesystem", return_value=UserFS(home=str(tmp_path))), patch.object(main, "LOG_DIR", str(tmp_path / "logs")):
        with TestClient(main.app, headers=AUTH) as result:
            yield result
    main.app.dependency_overrides.clear()
    main._session_cwds.clear()


def test_feature_discovery_and_action_schemas(client):
    config = client.get("/api/config").json()["features"]
    assert config == {
        "terminal": main.ENABLE_TERMINAL, "notebooks": main.ENABLE_NOTEBOOKS,
        "system": main.ENABLE_SYSTEM_PROMPT, "git": main.shutil.which("git") is not None,
        "events": True, "browser": main.ENABLE_BROWSER,
        "desktop": main.ENABLE_DESKTOP and main.desktop_available(),
    }
    assert all(type(value) is bool for value in config.values())
    from open_terminal.desktop import ActionRequest
    assert "actions" in ActionRequest.model_json_schema()["required"]
    schema = client.get("/openapi.json").json()
    action_endpoints = ["/browser/action"]
    if config["desktop"]:
        action_endpoints.append("/desktop/action")
    for endpoint in action_endpoints:
        ref = schema["paths"][endpoint]["post"]["requestBody"]["content"]["application/json"]["schema"]["$ref"].rsplit("/", 1)[1]
        body = schema["components"]["schemas"][ref]
        assert "actions" in body["required"]
        assert body["properties"]["actions"]["type"] == "array"
    for endpoint in ("/files/cwd", "/files/list", "/git/status", "/api/terminals", "/browser/sessions"):
        assert endpoint not in schema["paths"]


def test_file_paths_ranges_display_and_ui_preview(client, tmp_path):
    folder = tmp_path / "selected"
    folder.mkdir()
    assert client.post("/files/cwd", json={"path": str(folder)}).status_code == 200
    assert client.post("/files/write", json={"path": "note.txt", "content": "first\nsecond\nthird\n"}).status_code == 200
    target = folder / "note.txt"
    assert target.is_file()
    read = client.get("/files/read", params={"path": "note.txt", "start_line": 2, "end_line": 2})
    assert read.json() == {"path": str(target), "total_lines": 3, "content": "second\n"}
    assert client.get("/files/display", params={"path": "note.txt"}).json() == {"path": str(target), "exists": True}
    assert client.get("/files/display", params={"path": "absent.txt"}).json()["exists"] is False
    listing = client.get("/files/list", params={"directory": str(folder)}).json()
    assert listing["dir"] == str(folder)
    assert any(entry["name"] == "note.txt" for entry in listing["entries"])
    assert client.get("/files/view", params={"path": str(target), "preview": True}).content == target.read_bytes()
    assert client.get("/files/cwd", headers={"X-Session-Id": "another-chat"}).json()["cwd"] == str(tmp_path)
    assert client.get("/environment").json()["cwd"] == str(folder)
    assert client.get("/files/search", params={"path": str(folder), "query": "note"}).json()["results"][0]["path"] == str(target)
    matches = client.get("/files/matches", params={"path": str(folder), "query": "second"}).json()
    assert matches["results"][0]["content_matches"][0]["line"] == 2


def test_process_cwd_output_input_and_owner_boundaries(client, tmp_path):
    program = "import os,sys; print(os.getcwd(), flush=True); print('server-key-in-env=' + str('OPEN_TERMINAL_API_KEY' in os.environ), flush=True); print(sys.stdin.readline().strip(), flush=True)"
    command = f"{shlex.quote(sys.executable)} -c {shlex.quote(program)}"
    started = client.post("/execute", params={"wait": 0}, json={"command": command, "cwd": str(tmp_path)})
    assert started.status_code == 200
    data = started.json()
    assert data["cwd"] == str(tmp_path)  # response_model must not discard the upstream field
    pid = data["id"]
    for foreign in ({"X-User-Id": "other-owner"}, {"X-Session-Id": "other-chat"}):
        assert client.get(f"/execute/{pid}/status", headers=foreign).status_code == 404
        assert client.post(f"/execute/{pid}/input", headers=foreign, json={"input": "bad\n"}).status_code == 404
        assert client.delete(f"/execute/{pid}", headers=foreign).status_code == 404
        assert client.get("/execute", headers=foreign).json() == []
    assert client.post(f"/execute/{pid}/input", json={"input": "integration-answer\\n"}).status_code == 200
    finished = client.get(f"/execute/{pid}/status", params={"wait": 10}).json()
    assert finished["status"] == "done" and finished["exit_code"] == 0
    assert "cwd" not in finished  # preserve the upstream status shape
    output = "".join(entry["data"] for entry in finished["output"])
    assert "integration-answer" in output
    assert "server-key-in-env=False" in output
    with client.websocket_connect(f"/execute/{pid}/status") as ws:
        ws.send_json({"type": "auth", "token": "test-token"})
        streamed = ws.receive_json()
        assert streamed["id"] == pid and streamed["status"] == "done"
    assert client.post(f"/execute/{pid}/input", json={"input": "later\n"}).status_code == 400


def test_kill_process_signature_and_behavior(client, tmp_path):
    program = "import time; time.sleep(60)"
    started = client.post("/execute", params={"wait": 0}, json={"command": f"{shlex.quote(sys.executable)} -c {shlex.quote(program)}", "cwd": str(tmp_path)}).json()
    assert client.delete(f"/execute/{started['id']}").json() == {"status": "killed"}
    assert client.get(f"/execute/{started['id']}/status").json()["status"] == "killed"


@pytest.mark.parametrize("path", ["/events", "/execute", "/execute/missing/status", "/desktop/stream", "/desktop/audio"])
def test_streams_reject_invalid_first_message_auth(client, path):
    with client.websocket_connect(path) as ws:
        ws.send_json({"type": "auth", "token": "wrong-token"})
        with pytest.raises(WebSocketDisconnect) as error:
            ws.receive_json()
        assert error.value.code == 4001


def test_events_auth_preserves_session_cwd(client, tmp_path, monkeypatch):
    async def fake_events(ws, fs, cwd, ports):
        await ws.send_json({"cwd": cwd, "home": fs.home})
    monkeypatch.setattr(main, "system_events", fake_events)
    folder = tmp_path / "events-folder"
    folder.mkdir()
    client.post("/files/cwd", json={"path": str(folder)})
    with client.websocket_connect("/events") as ws:
        ws.send_json({"type": "auth", "token": "test-token"})
        assert ws.receive_json() == {"cwd": str(folder), "home": str(tmp_path)}


def test_desktop_stream_auth_with_mock_backend(client, monkeypatch):
    import open_terminal.desktop as desktop
    class FakeDesktop:
        audio_available = True
        async def stream(self, ws):
            await ws.send_bytes(b"mock-desktop")
        async def stream_audio(self, ws):
            await ws.send_bytes(b"mock-audio")
    monkeypatch.setattr(desktop, "get_desktop", AsyncMock(return_value=FakeDesktop()))
    for path, expected in (("/desktop/stream", b"mock-desktop"), ("/desktop/audio", b"mock-audio")):
        with client.websocket_connect(path) as ws:
            ws.send_json({"type": "auth", "token": "test-token"})
            assert ws.receive_bytes() == expected


def test_browser_session_owner_chat_and_capability(client):
    # Proxy mode allocates metadata only; never launches Chrome or fetches a URL.
    created = client.post("/browser/sessions", json={})
    assert created.status_code == 200
    session = created.json()
    sid = session["id"]
    assert client.get(f"/browser/sessions/{sid}").status_code == 200
    assert client.get(f"/browser/sessions/{sid}", headers={"X-User-Id": "foreign"}).status_code == 404
    assert client.get("/browser/tabs", headers={"X-Session-Id": "foreign"}).json()["tabs"] == []
    runtime = client.get(f"/browser/view/{sid}/{session['token']}/runtime.js")
    assert runtime.status_code == 200 and "javascript" in runtime.headers["content-type"]
    assert client.get(f"/browser/view/{sid}/wrong/runtime.js").status_code == 404
    assert client.delete(f"/browser/sessions/{sid}").status_code == 200


def test_multi_user_requires_identity(monkeypatch, tmp_path):
    import open_terminal.dependencies as dependencies
    monkeypatch.setattr(dependencies, "MULTI_USER", True)
    request = Request({"type": "http", "headers": []})
    with pytest.raises(HTTPException) as error:
        dependencies.get_filesystem(request)
    assert error.value.status_code == 400
    # Verify the merged cwd map separates equal chat IDs for different user homes.
    first = UserFS(home=str(tmp_path / "first"))
    second = UserFS(home=str(tmp_path / "second"))
    main._set_session_cwd("same-chat", str(tmp_path / "first" / "selected"), first)
    assert main._get_session_cwd("same-chat", first).endswith("/first/selected")
    assert main._get_session_cwd("same-chat", second) == second.home


def test_non_repository_watcher_does_not_recurse(tmp_path):
    paths, recursive = asyncio.run(events.watch_paths(UserFS(home=str(tmp_path)), str(tmp_path), str(tmp_path)))
    assert paths == [str(tmp_path)] and recursive is False


def test_dockerfile_keeps_custom_installation_and_upstream_runtime():
    dockerfile = Path("Dockerfile").read_text()
    assert "FROM python:3.12.13" in dockerfile
    for required in ("r-base r-base-dev", "https://mirrors.tuna.tsinghua.edu.cn/CRAN/", "/etc/R/Rprofile.site", "PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple", "npm install -g @officecli/officecli@1.0.132", "officecli --version", "chromium chromium-sandbox", "fonts-noto-color-emoji", "tigervnc-standalone-server xfce4", "pulseaudio pulseaudio-utils", "ENV OPEN_TERMINAL_BROWSER_NO_SANDBOX=true"):
        assert required in dockerfile


def test_existing_terminal_session_auth_without_opening_shell(client, monkeypatch):
    session = {"user_id": AUTH["X-User-Id"], "chat_id": AUTH["X-Session-Id"], "connected": True, "backend": "pty", "created_at": "test", "pid": 123}
    monkeypatch.setattr(main, "_terminal_sessions", {"fixture-shell": session})
    monkeypatch.setattr(main, "_session_is_alive", lambda _: True)
    try:
        assert client.get("/api/terminals/fixture-shell").json()["id"] == "fixture-shell"
        assert client.get("/api/terminals/fixture-shell", headers={"X-User-Id": "foreign"}).status_code == 404
        for headers, token, code in (({}, "wrong-token", 4001), ({"X-User-Id": "foreign"}, "test-token", 4004), ({}, "test-token", 4009)):
            with client.websocket_connect("/api/terminals/fixture-shell", headers=headers) as ws:
                ws.send_json({"type": "auth", "token": token})
                with pytest.raises(WebSocketDisconnect) as error:
                    ws.receive_json()
                assert error.value.code == code
    finally:
        main._terminal_sessions.clear()  # fixture metadata has no real process to clean up


def test_browser_page_proxy_rewrites_resources_and_strips_credentials(client):
    from importlib import import_module
    browser = import_module("open_terminal.browser.router")
    session = client.post("/browser/sessions", json={}).json()
    sid, token = session["id"], session["token"]
    old_profile = browser.profiles[AUTH["X-User-Id"]]
    client.portal.call(old_profile.aclose)
    requests = []
    def page(request):
        requests.append(request)
        return httpx.Response(200, headers={"content-type": "text/html"}, text='<html><head></head><body><script src="/assets/app.js"></script><a href="/next">Next</a></body></html>')
    browser.profiles[AUTH["X-User-Id"]] = httpx.AsyncClient(transport=httpx.MockTransport(page))
    response = client.get(f"/browser/view/{sid}/{token}/frame/http/example.test/")
    assert response.status_code == 200
    assert "/frame/http/example.test/assets/app.js" in response.text
    assert "/frame/http/example.test/next" in response.text
    assert "sandbox" in response.headers["content-security-policy"]
    assert str(requests[0].url) == "http://example.test/"
    assert "authorization" not in requests[0].headers
    assert not any(key.startswith("x-") for key in requests[0].headers)


def test_git_panel_status_and_repository_watch(client, tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, capture_output=True)
    (tmp_path / "new.txt").write_text("new content\n")
    status = client.get("/git/status", params={"root": str(tmp_path)})
    assert status.status_code == 200
    data = status.json()
    assert data["is_repo"] is True and data["root"] == str(tmp_path.resolve())
    assert data["files"][0]["path"] == "new.txt"
    assert data["files"][0]["status"] == "untracked"
    paths, recursive = asyncio.run(events.watch_paths(UserFS(home=str(tmp_path)), str(tmp_path), str(tmp_path)))
    assert recursive is True and str(tmp_path.resolve()) in paths
