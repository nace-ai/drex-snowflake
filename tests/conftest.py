"""Fixtures: the mock Drex API (tests/mock/drex_mock.py, from drex-duckdb) and a way to call a function's handler with a
batch the way Snowflake does, as a DataFrame of its arguments by position."""

import json
import threading
import urllib.request
import uuid

import drex_mock
import pandas
import pngs
import pytest

import drex_snowflake

#: A well-formed test key, never a real one
KEY = "nace_sk_" + "t" * 43
OPEN_ROUTER_KEY = "sk-or-v1-mocktestkey"


@pytest.fixture(scope="session")
def mock_url():
    server = drex_mock.make_server()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


class Drex:
    """The mock, for one test: its own namespace, and the handler's secrets pointed at a scenario."""

    def __init__(self, mock_url, monkeypatch):
        self.mock_url = mock_url
        self.monkeypatch = monkeypatch
        self.ns = uuid.uuid4().hex[:12]
        self.use("ok")

    def use(self, scenario, key=KEY):
        self.monkeypatch.setenv("NACE_API_KEY", key)
        self.monkeypatch.setenv("NACE_BASE_URL", f"{self.mock_url}/ns/{self.ns}/{scenario}")

    def use_open_router(self):
        """OpenRouter's base URL, reached through the mock as an HTTP proxy, which answers it as OpenRouter does."""
        self.ns = "default"
        urllib.request.urlopen(f"{self.mock_url}/__reset/default").read()
        self.monkeypatch.setenv("NACE_API_KEY", OPEN_ROUTER_KEY)
        self.monkeypatch.setenv("NACE_BASE_URL", "http://openrouter.ai/api")
        self.monkeypatch.setenv("HTTP_PROXY", self.mock_url)
        self.monkeypatch.delenv("NO_PROXY", raising=False)
        self.monkeypatch.delenv("no_proxy", raising=False)

    def log(self, endpoint="/v1/systemone"):
        """The namespace's requests to `endpoint`, in the order they arrived."""
        body = urllib.request.urlopen(f"{self.mock_url}/__log/{self.ns}").read().decode()
        records = [json.loads(line) for line in body.splitlines()]
        return [r for r in records if r["endpoint"] == endpoint]


@pytest.fixture
def drex(mock_url, monkeypatch):
    yield Drex(mock_url, monkeypatch)
    with drex_snowflake._clients_lock:
        for client in drex_snowflake._clients.values():
            client.close()
        drex_snowflake._clients.clear()


@pytest.fixture
def staged_images(monkeypatch):
    """Scoped file URLs of the mock's red and blue PNGs, as SnowflakeFile would read them."""
    files = {
        "scoped://red.png": pngs.fixture("red"),
        "scoped://blue.png": pngs.fixture("blue"),
        "scoped://notes.txt": b"not an image",
    }
    monkeypatch.setattr(drex_snowflake, "_read_staged_file", lambda url: files[url])
    return files


def call(function, rows, concurrency=drex_snowflake.DEFAULT_CONCURRENCY):
    """`function`'s results for a batch of argument tuples, through its vectorized handler."""
    run = drex_snowflake.handler(function, concurrency=concurrency)
    frame = pandas.DataFrame([list(row) for row in rows], dtype="object")
    assert run._sf_vectorized_input is pandas.DataFrame
    return run(frame).tolist()
