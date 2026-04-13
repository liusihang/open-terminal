import pytest
from fastapi.testclient import TestClient

import open_terminal.main as main
from open_terminal.main import OfficeCliRequest, build_officecli_args
from open_terminal.utils.fs import UserFS


def _fs() -> UserFS:
    return UserFS(home="/home/user")


def test_build_view_args_resolves_file_path_and_mode():
    request = OfficeCliRequest(command="view", file="docs/manual.docx", mode="text")

    args = build_officecli_args(request, _fs())

    assert args == [
        "officecli",
        "view",
        "/home/user/docs/manual.docx",
        "text",
        "--json",
    ]


def test_build_set_args_maps_props_to_repeatable_prop_flags():
    request = OfficeCliRequest(
        command="set",
        file="/home/user/manual.docx",
        path="/body/p[1]",
        props=["text=Hello", "bold=true"],
        force="true",
    )

    args = build_officecli_args(request, _fs())

    assert args[:4] == ["officecli", "set", "/home/user/manual.docx", "/body/p[1]"]
    assert args.count("--prop") == 2
    assert "text=Hello" in args
    assert "bold=true" in args
    assert "--force" in args
    assert args[-1] == "--json"


@pytest.mark.parametrize("command", ["view", "get", "query", "set", "add", "remove", "move", "swap", "raw", "validate", "check", "batch", "create", "new"])
def test_build_args_rejects_commands_requiring_file_without_file(command: str):
    request = OfficeCliRequest(command=command)

    with pytest.raises(ValueError, match="requires 'file'"):
        build_officecli_args(request, _fs())


def test_build_help_command_uses_format_selector_when_provided():
    request = OfficeCliRequest(command="help", format="docx")

    args = build_officecli_args(request, _fs())

    assert args == ["officecli", "docx", "--json"]


def test_officecli_endpoint_executes_in_terminal_runtime_and_parses_json(monkeypatch):
    captured = {}
    fs = UserFS()

    async def _fake_run(args, cwd, run_as_user, timeout):
        captured["args"] = args
        captured["cwd"] = cwd
        captured["run_as_user"] = run_as_user
        captured["timeout"] = timeout
        return 0, '{"status":"ok"}'

    monkeypatch.setattr(main, "_run_command_args", _fake_run)
    client = TestClient(main.app)

    response = client.post(
        "/officecli",
        json={
            "command": "view",
            "file": "manual.docx",
            "mode": "text",
        },
    )

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert captured["args"] == [
        "officecli",
        "view",
        fs.resolve_path("manual.docx"),
        "text",
        "--json",
    ]
    assert captured["cwd"] == fs.home


def test_officecli_endpoint_returns_400_for_missing_required_file():
    client = TestClient(main.app)

    response = client.post(
        "/officecli",
        json={
            "command": "view",
            "mode": "text",
        },
    )

    assert response.status_code == 400
    assert "requires 'file'" in response.json()["detail"]
