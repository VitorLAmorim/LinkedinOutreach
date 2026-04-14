# tests/browser/test_proxy_config.py
import pytest

from linkedin.browser.login import _build_proxy_config


def test_empty_returns_none():
    assert _build_proxy_config("") is None
    assert _build_proxy_config(None) is None


def test_unauthenticated_proxy():
    cfg = _build_proxy_config("http://proxy.example.com:3128/")
    assert cfg == {"server": "http://proxy.example.com:3128"}


def test_authenticated_proxy_splits_credentials():
    cfg = _build_proxy_config("http://vqbwknhn:648wgd7jcrzo@31.59.20.176:6754/")
    assert cfg == {
        "server": "http://31.59.20.176:6754",
        "username": "vqbwknhn",
        "password": "648wgd7jcrzo",
    }


def test_https_scheme_preserved():
    cfg = _build_proxy_config("https://user:pw@proxy.example.com:443/")
    assert cfg["server"] == "https://proxy.example.com:443"
    assert cfg["username"] == "user"
    assert cfg["password"] == "pw"


def test_missing_hostname_raises():
    with pytest.raises(ValueError, match="no hostname"):
        _build_proxy_config("not-a-url")
