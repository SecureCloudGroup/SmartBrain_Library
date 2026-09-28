"""The Library API: happy paths, refusals, rate limits, admin auth and the body cap."""
from __future__ import annotations

import copy
import json

import pytest
from fastapi.testclient import TestClient

from service.app import MAX_BODY, Settings, create_app

TOKEN = "t" * 40
V = "/library/v1"


def a_record() -> dict:
    """What the app's Add-a-source form builds (tier local, the user's own id), values removed."""
    return {
        "id": "local-3f2a9c1b7e44", "name": "Harbor water temperature", "description": "Water temperature by station.",
        "provider": {"id": "local", "name": "api.harbordata.org", "url": "https://api.harbordata.org",
                     "authority": "official"},
        "tier": "local", "categories": ["weather/current"], "kinds": ["current_value"],
        "coverage": {"geo": "local", "entity": ""},
        "access": {"kind": "http_json", "url_template": "https://api.harbordata.org/v1/temp?station={station}",
                   "params": [{"name": "station", "kind": "station", "example": None, "required": True}],
                   "auth": "none", "headers": {}, "docs_url": "https://api.harbordata.org/docs"},
        "terms": {"status": "public_domain", "note": "added by you", "terms_url": ""},
        "freshness": {"cadence": "hourly"}, "examples": [], "notes": "",
        "origin": {"by": "user", "at": "2026-09-28T10:00:00Z"}, "validation": {"status": "ok"},
        "votes": {"yes": 999, "no": 0},
    }


@pytest.fixture
def make(tmp_path):
    def _make(**kw):
        manifest = tmp_path / "manifest.json"
        s = Settings(db=tmp_path / "q.sqlite3", manifest=manifest, admin_token=TOKEN, **kw)
        return TestClient(create_app(s)), manifest
    return _make


@pytest.fixture
def client(make):
    return make()[0]


def admin(c, path="/admin/queue", **kw):
    return c.get(V + path, headers={"Authorization": f"Bearer {TOKEN}"}, **kw)


# ------------------------------------------------------------------------------------------ happy paths
def test_healthz_and_security_headers(client):
    r = client.get(V + "/healthz")
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["cache-control"] == "no-store"
    assert "default-src 'none'" in r.headers["content-security-policy"]
    assert "access-control-allow-origin" not in r.headers
    assert "set-cookie" not in r.headers


def test_no_cors_and_no_docs(client):
    r = client.options(V + "/votes", headers={"Origin": "https://evil.example",
                                              "Access-Control-Request-Method": "POST"})
    assert "access-control-allow-origin" not in r.headers
    for p in ("/docs", "/openapi.json", "/redoc"):
        assert client.get(p).status_code == 404


def test_vote_is_queued_without_identifiers(client):
    r = client.post(V + "/votes", json={"source_id": "noaa-tides-predictions", "verdict": "yes",
                                        "app_version": "0.24.0"})
    assert r.status_code == 202, r.text
    items = admin(client).json()["items"]
    assert len(items) == 1
    item = items[0]
    assert item["kind"] == "vote"
    assert item["payload"] == {"source_id": "noaa-tides-predictions", "verdict": "yes", "app_version": "0.24.0"}
    assert set(item) == {"id", "kind", "day", "payload"} and len(item["day"]) == 10  # the day, no time


def test_suggestion_is_cleaned_server_side(client):
    r = client.post(V + "/suggestions", json={"record": a_record(), "via": "form", "app_version": "0.24.0"})
    assert r.status_code == 202, r.text
    rec = admin(client).json()["items"][0]["payload"]["record"]
    assert rec["tier"] == "harvested" and rec["origin"] == {"by": "user-suggestion"}
    assert rec["votes"] == {"yes": 0, "no": 0} and rec["validation"] == {"status": "unvalidated"}
    assert rec["terms"]["status"] == "unverified" and rec["provider"]["authority"] == "community"
    assert rec["id"] == r.json()["id"] == "api-harbordata-org-harbor-water-temperature"  # never the client's id


def test_manifest(make):
    c, manifest = make()
    assert c.get(V + "/manifest").status_code == 503
    m = {"tag": "v1.0.1", "url": "https://github.com/SecureCloudGroup/SmartBrain_Library/releases/download/v1.0.1/"
                                "library.duckdb.gz", "sha256": "a" * 64, "bytes": 8123456, "extra": "not served"}
    manifest.write_text(json.dumps(m))
    r = c.get(V + "/manifest")
    assert r.status_code == 200
    assert r.json() == {k: m[k] for k in ("tag", "url", "sha256", "bytes")}
    manifest.write_text(json.dumps({**m, "url": "http://insecure.example/pack"}))
    assert c.get(V + "/manifest").status_code == 503


# ------------------------------------------------------------------------------------------ refusals
def _suggest(c, record):
    return c.post(V + "/suggestions", json={"record": record, "via": "yes", "app_version": "0.24.0"})


@pytest.mark.parametrize("edit, why", [
    (lambda r: r["access"].update(url_template="https://api.harbordata.org/v1/temp?station={station}&api_key=abc123"),
     "credential"),
    (lambda r: r["access"].update(url_template="https://bob:hunter2@api.harbordata.org/v1/temp?station={station}"),
     "credential"),
    (lambda r: r["access"].update(url_template="http://api.harbordata.org/v1/temp?station={station}"), "https only"),
    (lambda r: r["access"].update(url_template="https://192.168.1.20/v1/temp?station={station}"), "public host"),
    (lambda r: r["access"].update(url_template="https://127.0.0.1/v1/temp?station={station}"), "public host"),
    (lambda r: r["access"].update(url_template="https://nas.local/v1/temp?station={station}"), "public host"),
    (lambda r: r["access"].update(url_template="https://localhost/v1/temp?station={station}"), "public host"),
    (lambda r: r["access"].update(docs_url="https://10.0.0.5/docs"), "public host"),
    (lambda r: r["access"].update(url_template="https://[::1/v1/temp?station={station}"), "not a valid address"),
    (lambda r: r["provider"].update(url="https://[fe80::1]/"), "public host"),
    (lambda r: r["access"]["params"][0].update(example="8723214"), "carries a value"),
    (lambda r: r["access"].update(headers={"Authorization": "Bearer sk-live-123"}), "credential"),
    (lambda r: r.update(categories=["weather/not-a-subcategory"]), "unknown categories"),
    (lambda r: r.update(favourite_color="blue"), "unknown fields"),
    (lambda r: r["access"].update(secret_sauce=True), "unknown access fields"),
    (lambda r: r.update(description="x" * 1500), "longer than"),
    (lambda r: r.update(examples=["x"] * 40), "longer than"),
    (lambda r: r.update(coverage="US"), "must be an object"),
    (lambda r: r.pop("kinds"), "missing kinds"),
])
def test_suggestion_refusals(client, edit, why):
    rec = copy.deepcopy(a_record())
    edit(rec)
    r = _suggest(client, rec)
    assert r.status_code == 422, r.text
    assert any(why in p for p in r.json()["problems"]), r.json()
    assert admin(client).json()["items"] == []


@pytest.mark.parametrize("body", [
    {"source_id": "noaa-tides", "verdict": "maybe", "app_version": "0.24.0"},
    {"source_id": "NOAA Tides!", "verdict": "yes", "app_version": "0.24.0"},
    {"source_id": "x" * 121, "verdict": "yes", "app_version": "0.24.0"},
    {"source_id": "noaa-tides", "verdict": "yes", "app_version": "0.24.0", "install_id": "abc"},
    {"source_id": "noaa-tides", "verdict": "yes"},
    {"source_id": "noaa-tides", "verdict": "yes", "app_version": "0.24.0; rm -rf /"},
    {"source_id": 12, "verdict": "yes", "app_version": "0.24.0"},
])
def test_vote_refusals(client, body):
    r = client.post(V + "/votes", json=body)
    assert r.status_code == 422, r.text
    assert "rm -rf" not in r.text  # problems name the field, never echo the value


def test_suggestion_extra_top_level_field(client):
    r = client.post(V + "/suggestions", json={"record": a_record(), "via": "form", "app_version": "1", "ask": "tides"})
    assert r.status_code == 422


def test_json_only(client):
    r = client.post(V + "/votes", content=b"source_id=x", headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert r.status_code == 415
    r = client.post(V + "/votes", content=b'{"source_id": "noaa-tides", "verdict": "yes", "app_version": "1"}',
                    headers={"Content-Type": "text/plain"})
    assert r.status_code == 415


def test_body_cap(client):
    rec = a_record()
    rec["notes"] = "x" * (MAX_BODY + 10)
    r = _suggest(client, rec)
    assert r.status_code == 413
    # a lying Content-Length is caught while reading, too
    body = json.dumps({"record": rec, "via": "yes", "app_version": "1"}).encode()
    r = client.post(V + "/suggestions", content=body, headers={"Content-Type": "application/json",
                                                              "Content-Length": "100"})
    assert r.status_code == 413


# ------------------------------------------------------------------------------------------ rate limits
def test_vote_rate_limit(make):
    c, _ = make(votes_per_hour=3)
    body = {"source_id": "noaa-tides", "verdict": "yes", "app_version": "1"}
    assert [c.post(V + "/votes", json=body).status_code for _ in range(3)] == [202] * 3
    r = c.post(V + "/votes", json=body)
    assert r.status_code == 429 and int(r.headers["retry-after"]) >= 1


def test_suggestion_rate_limit_is_per_client_behind_the_proxy(make):
    c, _ = make(suggestions_per_hour=2, trusted_proxies=frozenset({"172.16.0.0/12"}))
    c = TestClient(c.app, client=("172.18.0.3", 40000))  # the peer is the Caddy container
    body = {"record": a_record(), "via": "form", "app_version": "1"}
    one = {"X-Forwarded-For": "203.0.113.7"}
    assert [c.post(V + "/suggestions", json=body, headers=one).status_code for _ in range(2)] == [202, 202]
    assert c.post(V + "/suggestions", json=body, headers=one).status_code == 429
    # another user behind the same proxy is not affected; a spoofed left-hand entry does not help
    assert c.post(V + "/suggestions", json=body, headers={"X-Forwarded-For": "198.51.100.9"}).status_code == 202
    spoof = {"X-Forwarded-For": "1.2.3.4, 203.0.113.7"}
    assert c.post(V + "/suggestions", json=body, headers=spoof).status_code == 429


def test_untrusted_peer_cannot_pick_its_address(make):
    c, _ = make(votes_per_hour=1)  # the test client's peer is not a trusted proxy
    body = {"source_id": "noaa-tides", "verdict": "yes", "app_version": "1"}
    assert c.post(V + "/votes", json=body, headers={"X-Forwarded-For": "10.0.0.1"}).status_code == 202
    assert c.post(V + "/votes", json=body, headers={"X-Forwarded-For": "10.0.0.2"}).status_code == 429


def test_queue_cap(make):
    c, _ = make(max_queue=1)
    body = {"source_id": "noaa-tides", "verdict": "yes", "app_version": "1"}
    assert c.post(V + "/votes", json=body).status_code == 202
    assert c.post(V + "/votes", json=body).status_code == 503


# ------------------------------------------------------------------------------------------ admin
@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer wrong"}, {"Authorization": TOKEN},
                                     {"Authorization": f"Bearer {TOKEN}x"}, {"Authorization": f"Basic {TOKEN}"}])
def test_admin_auth(client, headers):
    assert client.get(V + "/admin/queue", headers=headers).status_code == 401
    assert client.post(V + "/admin/ack", json={"through": 5}, headers=headers).status_code == 401


@pytest.mark.parametrize("token", ["", "short"])
def test_admin_disabled_without_a_strong_token(tmp_path, token):
    c = TestClient(create_app(Settings(db=tmp_path / "q.sqlite3", admin_token=token)))
    assert c.get(V + "/admin/queue", headers={"Authorization": f"Bearer {token}"}).status_code == 401


def test_admin_paging_and_ack(client):
    for i in range(5):
        client.post(V + "/votes", json={"source_id": f"source-{i}", "verdict": "no", "app_version": "1"})
    page = admin(client, params={"since": 0, "limit": 2}).json()
    assert [x["payload"]["source_id"] for x in page["items"]] == ["source-0", "source-1"]
    page2 = admin(client, params={"since": page["next"], "limit": 10}).json()
    assert len(page2["items"]) == 3
    r = client.post(V + "/admin/ack", json={"through": page["next"]}, headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.json() == {"deleted": 2}
    assert len(admin(client).json()["items"]) == 3
