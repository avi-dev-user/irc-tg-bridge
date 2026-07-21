"""Tests for the pure gofile response parsers (the HTTP call is live glue)."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from tgbridge.gofile import parse_server, parse_link  # noqa: E402


def test_parse_server_from_data_server():
    assert parse_server({"status": "ok", "data": {"server": "store1"}}) == "store1"


def test_parse_server_from_servers_list():
    assert parse_server({"data": {"servers": [{"name": "store5"}]}}) == "store5"
    assert parse_server({"data": {"servers": ["store7"]}}) == "store7"


def test_parse_server_missing():
    assert parse_server({}) is None
    assert parse_server({"data": {}}) is None
    assert parse_server({"data": {"servers": []}}) is None


def test_parse_link_ok():
    payload = {"status": "ok", "data": {"downloadPage": "https://gofile.io/d/abc123"}}
    assert parse_link(payload) == "https://gofile.io/d/abc123"


def test_parse_link_failure_or_missing():
    assert parse_link({"status": "error"}) is None
    assert parse_link({"status": "ok", "data": {}}) is None
    assert parse_link({}) is None


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all tests passed")
