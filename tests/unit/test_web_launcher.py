from __future__ import annotations

import asyncio

import pytest

from teams_connector.surfaces.web import main as web_main


def test_ui_opens_browser_after_server_is_ready(monkeypatch, capsys) -> None:
    instances = []

    class Server:
        def __init__(self, _config) -> None:
            self.started = False
            self.should_exit = False
            instances.append(self)

        async def serve(self) -> None:
            self.started = True
            while not self.should_exit:
                await asyncio.sleep(0)

    monkeypatch.setattr(web_main, "_free_port", lambda _port: 45678)
    monkeypatch.setattr(web_main, "AppContext", lambda **_kwargs: _Context())
    monkeypatch.setattr(web_main, "create_app", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(web_main.uvicorn, "Server", Server)

    def open_browser(url: str) -> bool:
        assert instances[0].started
        assert url == "http://127.0.0.1:45678/"
        instances[0].should_exit = True
        return True

    monkeypatch.setattr(web_main.webbrowser, "open", open_browser)
    web_main.run(unsecure=False, open_browser=True)

    output = capsys.readouterr().out
    assert "http://127.0.0.1:45678/" in output
    assert "did not start" not in output


def test_ui_reports_chosen_address_when_startup_fails(monkeypatch, capsys) -> None:
    class Server:
        def __init__(self, _config) -> None:
            self.started = False
            self.should_exit = False

        async def serve(self) -> None:
            self.should_exit = True

    monkeypatch.setattr(web_main, "_free_port", lambda _port: 45679)
    monkeypatch.setattr(web_main, "AppContext", lambda **_kwargs: _Context())
    monkeypatch.setattr(web_main, "create_app", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(web_main.uvicorn, "Server", Server)
    monkeypatch.setattr(web_main.webbrowser, "open", lambda _url: pytest.fail("browser opened"))

    web_main.run(unsecure=False, open_browser=True)

    output = capsys.readouterr().out
    assert "http://127.0.0.1:45679/" in output
    assert "did not start" in output


class _Context:
    async def aclose(self) -> None:
        return None
