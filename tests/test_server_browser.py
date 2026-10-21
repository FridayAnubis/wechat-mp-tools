from unittest.mock import Mock

import pytest

from backend import runtime


@pytest.fixture(autouse=True)
def browser_environment(monkeypatch):
    for key in ("WECHAT_MP_HEADLESS", "CHROME_PATH", "CHROMIUM_PATH"):
        monkeypatch.delenv(key, raising=False)


def test_desktop_browser_options_are_preserved(monkeypatch):
    monkeypatch.setattr(runtime, "bundled_browsers_available", lambda: False)
    chromium = Mock()
    runtime.launch_chromium(chromium, headless=False, args=["--original"])
    chromium.launch.assert_called_once_with(headless=False, args=["--original"], channel="chrome")


@pytest.mark.parametrize("persistent", [False, True])
def test_server_browser_uses_configured_executable_and_headless(monkeypatch, tmp_path, persistent):
    monkeypatch.setenv("WECHAT_MP_HEADLESS", "1")
    monkeypatch.setenv("CHROME_PATH", "/usr/bin/chromium")
    chromium = Mock()
    options = {"headless": False, "args": ["--original"]}
    if persistent:
        runtime.launch_persistent_context(chromium, tmp_path, **options)
        args, kwargs = chromium.launch_persistent_context.call_args
        assert args == (str(tmp_path),)
    else:
        runtime.launch_chromium(chromium, **options)
        _, kwargs = chromium.launch.call_args
    assert kwargs["headless"] is True
    assert kwargs["executable_path"] == "/usr/bin/chromium"
    assert "channel" not in kwargs
    assert kwargs["args"] == ["--original", "--no-sandbox", "--disable-dev-shm-usage"]
    assert options == {"headless": False, "args": ["--original"]}


def test_explicit_browser_path_is_not_replaced(monkeypatch):
    monkeypatch.setenv("CHROME_PATH", "/usr/bin/chromium")
    chromium = Mock()
    runtime.launch_chromium(chromium, executable_path="/custom/chrome", headless=True)
    chromium.launch.assert_called_once_with(executable_path="/custom/chrome", headless=True)
