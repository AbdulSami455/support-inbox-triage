import json

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import SQLiteDB
from app.main import create_app

SECRET = "hook-secret"


class FakeLLM:
    """Stands in for the OpenRouter call; returns canned text and counts calls."""

    def __init__(self, content):
        self.content, self.calls = content, 0

    async def __call__(self, messages):
        self.calls += 1
        return self.content


def settings():
    return Settings(
        session_secret="x" * 32, webhook_secret=SECRET, demo_password="pw", cookie_secure=False,
        openrouter_api_key="", openrouter_model="m", openrouter_base_url="", db_path=":memory:", pbkdf2_iterations=1000,
    )


def make_client(llm_content='{"category":"billing","urgency":"high","draft_reply":"Hi","article_id":"KB-102"}'):
    llm = FakeLLM(llm_content)
    client = TestClient(create_app(settings(), db=SQLiteDB(":memory:"), llm=llm))
    client.__enter__()  # run lifespan (schema + seed)
    return client, llm


def login(c, email):
    assert c.post("/api/login", json={"email": email, "password": "pw"}).status_code == 200


def webhook(c, **kw):
    payload = {"message_id": "m1", "from": "Jane <jane@example.com>", "subject": "Charged twice", "text": "My invoice was charged twice"}
    return c.post("/api/webhook/email", json={**payload, **kw}, headers={"X-Webhook-Secret": SECRET})


def test_webhook_requires_secret_and_dedupes():
    c, _ = make_client()
    assert c.post("/api/webhook/email", json={}, headers={"X-Webhook-Secret": "nope"}).status_code == 401
    first, again = webhook(c), webhook(c)
    assert first.status_code == 201 and again.status_code == 200
    assert again.json() == {"id": first.json()["id"], "duplicate": True}


def test_only_admin_can_reassign():
    c, _ = make_client()
    tid = webhook(c).json()["id"]
    login(c, "sam@support.test")
    assert c.patch(f"/api/tickets/{tid}/assign", json={"assignee_id": 2}).status_code == 403
    login(c, "admin@support.test")
    assert c.patch(f"/api/tickets/{tid}/assign", json={"assignee_id": 2}).status_code == 204
    assert c.get(f"/api/tickets/{tid}").json()["assignee_name"] == "Sam"


def test_list_filters_and_pages():
    c, _ = make_client()
    for i in range(12):
        webhook(c, message_id=f"m{i}")
    login(c, "sam@support.test")
    p1 = c.get("/api/tickets?status=open&page=1").json()
    p2 = c.get("/api/tickets?status=open&page=2").json()
    assert p1["total"] == 12 and len(p1["items"]) == 10 and len(p2["items"]) == 2
    assert c.get("/api/tickets?status=closed").json()["total"] == 0


def test_triage_valid_is_cached_and_reply_is_recorded():
    c, llm = make_client()
    tid = webhook(c).json()["id"]
    login(c, "jordan@support.test")
    r = c.post(f"/api/tickets/{tid}/triage").json()
    assert r["triage"]["category"] == "billing" and r["triage"]["article"]["id"] == "KB-102"
    assert c.post(f"/api/tickets/{tid}/triage").json()["cached"] is True
    assert llm.calls == 1  # one AI call per ticket

    assert c.post(f"/api/tickets/{tid}/replies", json={"body": "Hi Jane"}).status_code == 201
    t = c.get(f"/api/tickets/{tid}").json()
    assert t["status"] == "replied"
    assert t["replies"][0]["sent_by"] == "Jordan" and t["replies"][0]["sent_at"] > 0


@pytest.mark.parametrize("bad", [
    "not json",
    '{"category":"billing","urgency":"high"}',                                # missing draft
    '{"category":"refunds","urgency":"high","draft_reply":"x"}',              # unknown category
    '{"category":"billing","urgency":"high","draft_reply":"x","extra":1}',    # extra key
    '{"category":"billing","urgency":5,"draft_reply":"x"}',                   # wrong type
    '{"category":"billing","urgency":"low","draft_reply":"x","article_id":"KB-999"}',  # made-up article
])
def test_invalid_ai_json_shows_error_not_draft(bad):
    c, _ = make_client(bad)
    tid = webhook(c).json()["id"]
    login(c, "riley@support.test")
    r = c.post(f"/api/tickets/{tid}/triage")
    assert r.status_code == 502
    t = c.get(f"/api/tickets/{tid}").json()
    assert t["triage"] is None and t["triage_error"] and t["triage_attempts_left"] == 2


def test_json_wrapped_in_prose_is_extracted_then_validated():
    c, _ = make_client('Sure! Here it is:\n{"category":"billing","urgency":"low","draft_reply":"Hi","article_id":null}\nThanks')
    tid = webhook(c).json()["id"]
    login(c, "sam@support.test")
    r = c.post(f"/api/tickets/{tid}/triage")
    assert r.status_code == 200 and r.json()["triage"]["urgency"] == "low"


def test_triage_attempts_are_capped():
    c, llm = make_client("garbage")
    tid = webhook(c).json()["id"]
    login(c, "sam@support.test")
    codes = [c.post(f"/api/tickets/{tid}/triage").status_code for _ in range(4)]
    assert codes == [502, 502, 502, 429] and llm.calls == 3


def test_form_ticket_requires_login():
    c, _ = make_client()
    body = {"from": "bob@example.com", "subject": "Hi", "body": "Hello"}
    assert c.post("/api/tickets", json=body).status_code == 401
    login(c, "sam@support.test")
    assert c.post("/api/tickets", json=body).status_code == 201


def test_pure_python_pbkdf2_matches_hashlib(monkeypatch):
    import hashlib

    from app import auth

    expected = hashlib.pbkdf2_hmac("sha256", b"secret", b"salty", 1000)
    monkeypatch.delattr(hashlib, "pbkdf2_hmac")  # simulate Cloudflare's runtime
    assert auth._pbkdf2_sha256(b"secret", b"salty", 1000) == expected
    assert auth.check_password(auth.hash_password("pw", 1000), "pw")


def test_overdue_flag_and_filter():
    import asyncio
    import time

    c, _ = make_client()
    old = webhook(c, message_id="old").json()["id"]
    replied = webhook(c, message_id="old-replied").json()["id"]
    fresh = webhook(c, message_id="fresh").json()["id"]
    db = c.app.state.db
    two_days_ago = int(time.time()) - 2 * 86400
    asyncio.run(db.execute("UPDATE tickets SET created_at = ? WHERE id IN (?, ?)", (two_days_ago, old, replied)))
    login(c, "sam@support.test")
    c.post(f"/api/tickets/{replied}/replies", json={"body": "On it"})

    listing = {t["id"]: t["overdue"] for t in c.get("/api/tickets").json()["items"]}
    assert listing == {old: True, replied: False, fresh: False}
    overdue = c.get("/api/tickets?status=overdue").json()
    assert overdue["total"] == 1 and overdue["overdue_total"] == 1 and overdue["items"][0]["id"] == old
    assert c.get(f"/api/tickets/{old}").json()["overdue"] is True
    assert c.get(f"/api/tickets/{fresh}").json()["overdue"] is False


def test_live_updates_report_new_tickets():
    c, _ = make_client()
    login(c, "sam@support.test")
    first = webhook(c, message_id="a").json()["id"]
    seen = c.get("/api/updates").json()
    assert seen == {"latest_id": first, "new": []}
    second = webhook(c, message_id="b", subject="Second one").json()["id"]
    upd = c.get(f"/api/updates?after_id={first}").json()
    assert upd["latest_id"] == second and [t["subject"] for t in upd["new"]] == ["Second one"]


def test_login_tab_must_match_role():
    c, _ = make_client()
    ok = c.post("/api/login", json={"email": "admin@support.test", "password": "pw", "role": "admin"})
    assert ok.status_code == 200
    wrong = c.post("/api/login", json={"email": "sam@support.test", "password": "pw", "role": "admin"})
    assert wrong.status_code == 403 and "Agent sign-in" in wrong.json()["detail"]


def test_regenerate_replaces_draft_within_the_cap():
    c, llm = make_client()
    tid = webhook(c).json()["id"]
    login(c, "sam@support.test")
    first = c.post(f"/api/tickets/{tid}/triage").json()
    assert first["attempts_left"] == 2
    llm.content = '{"category":"billing","urgency":"low","draft_reply":"Second draft","article_id":"KB-102"}'
    second = c.post(f"/api/tickets/{tid}/triage?regenerate=true").json()
    assert second["cached"] is False and second["triage"]["draft_reply"] == "Second draft"
    assert c.get(f"/api/tickets/{tid}").json()["triage"]["draft_reply"] == "Second draft"

    llm.content = "garbage"  # a failed regenerate keeps the previous draft
    assert c.post(f"/api/tickets/{tid}/triage?regenerate=true").status_code == 502
    assert c.get(f"/api/tickets/{tid}").json()["triage"]["draft_reply"] == "Second draft"
    assert c.post(f"/api/tickets/{tid}/triage?regenerate=true").status_code == 429
    assert llm.calls == 3
