"""End-to-end API flow with the offline mock provider (no API key needed)."""

import json


def load(client):
    r = client.post("/api/samples/load").json()
    doc = client.get(f"/api/documents/{r['document_id']}").json()
    return doc


def code_all(client, doc):
    for p in doc["passages"]:
        r = client.post(f"/api/passages/{p['id']}/code", json={})
        assert r.status_code == 200, r.text
        assert r.json()["error"] is None
    return client.get(f"/api/documents/{doc['id']}").json()


def all_apps(doc):
    return [a for p in doc["passages"] for a in p["applications"]]


def test_samples_load_once(client):
    first = client.post("/api/samples/load").json()
    second = client.post("/api/samples/load").json()
    assert first["codes_imported"] == 5 and second["codes_imported"] == 0
    assert second["document_id"] is None
    assert len(client.get("/api/prompts").json()) == 2


def test_coding_produces_cited_applications(client):
    doc = code_all(client, load(client))
    apps = all_apps(doc)
    assert apps, "mock should apply at least some codes to the sample transcript"
    names = {a["code_name"] for a in apps}
    assert {"Financial stress", "AI tool use"} & names
    for a in apps:
        assert a["cited_refs"] and a["cited_refs"][0].startswith("CODE:")
        assert a["flags"] == []  # mock quotes verbatim and only cites refs it was shown
        ref = client.get(f"/api/refs/{a['cited_refs'][0]}").json()
        assert ref["title"] == a["code_name"]


def test_run_logs_exact_prompt(client):
    doc = load(client)
    p = doc["passages"][3]
    run_id = client.post(f"/api/passages/{p['id']}/code", json={}).json()["run_id"]
    run = client.get(f"/api/runs/{run_id}").json()
    assert "## Codebook" in run["system_prompt"]
    assert "[PROMPT:" in run["system_prompt"]
    assert p["text"] in run["user_prompt"]
    assert any(r.startswith("CODE:") for r in run["context_refs"])


def test_reject_teaches_a_negative_example_and_recode_moves_it(client):
    doc = code_all(client, load(client))
    target = all_apps(doc)[0]
    other = next(c for c in client.get("/api/codes").json() if c["id"] != target["code_id"])

    r = client.post(f"/api/applications/{target['id']}/review",
                    json={"action": "recode", "new_code_id": other["id"], "note": "wrong code"})
    assert r.status_code == 200

    codes = {c["id"]: c for c in client.get("/api/codes").json()}
    neg = [e for e in codes[target["code_id"]]["examples"] if e["polarity"] == "negative" and e["source"] == "correction"]
    pos = [e for e in codes[other["id"]]["examples"] if e["source"] == "correction"]
    assert neg and neg[-1]["note"] == "wrong code"
    assert pos

    # re-coding the same passage must not resurrect the rejected code
    client.post(f"/api/passages/{target['passage_id']}/code", json={})
    doc = client.get(f"/api/documents/{doc['id']}").json()
    passage = next(p for p in doc["passages"] if p["id"] == target["passage_id"])
    statuses = {(a["code_id"], a["status"]) for a in passage["applications"]}
    assert (target["code_id"], "pending") not in statuses
    assert (other["id"], "accepted") in statuses


def test_accept_and_undo(client):
    doc = code_all(client, load(client))
    a = all_apps(doc)[0]
    before = sum(len(c["examples"]) for c in client.get("/api/codes").json())
    client.post(f"/api/applications/{a['id']}/review", json={"action": "accept"})
    assert sum(len(c["examples"]) for c in client.get("/api/codes").json()) == before + 1
    assert client.post(f"/api/applications/{a['id']}/review", json={"action": "reject"}).status_code == 409
    client.post(f"/api/applications/{a['id']}/review", json={"action": "undo"})
    assert sum(len(c["examples"]) for c in client.get("/api/codes").json()) == before
    doc = client.get(f"/api/documents/{doc['id']}").json()
    assert next(x for x in all_apps(doc) if x["id"] == a["id"])["status"] == "pending"


def test_human_added_code_and_exports(client):
    doc = load(client)
    code = client.get("/api/codes").json()[0]
    p = doc["passages"][2]
    assert client.post(f"/api/passages/{p['id']}/applications", json={"code_id": code["id"]}).status_code == 200

    csv_text = client.get("/api/export/segments.csv").text
    assert "evidence_quote" in csv_text.splitlines()[0]
    assert code["name"] in csv_text

    lines = [json.loads(l) for l in client.get("/api/export/training.jsonl").text.splitlines()]
    assert len(lines) == 1
    roles = [m["role"] for m in lines[0]["messages"]]
    assert roles == ["system", "user", "assistant"]
    target = json.loads(lines[0]["messages"][2]["content"])
    assert target["applications"][0]["code_id"] == code["id"]


def test_upload_txt_and_reject_unknown_type(client):
    r = client.post("/api/documents/upload", files={"file": ("notes.txt", b"P1: hello there.\n\nP1: bye.", "text/plain")})
    assert r.status_code == 200
    assert len(client.get(f"/api/documents/{r.json()['id']}").json()["passages"]) == 2
    bad = client.post("/api/documents/upload", files={"file": ("x.xlsx", b"zz", "application/octet-stream")})
    assert bad.status_code == 400


def test_unconfigured_provider_is_refused(tmp_path):
    from fastapi.testclient import TestClient

    from qualcode.main import create_app
    from qualcode.providers import ClaudeProvider

    class NoKey(ClaudeProvider):
        def is_configured(self):
            return False

    c = TestClient(create_app(str(tmp_path / "x.db"), providers={"claude": NoKey()}))
    doc = c.get(f"/api/documents/{c.post('/api/samples/load').json()['document_id']}").json()
    r = c.post(f"/api/passages/{doc['passages'][0]['id']}/code", json={})
    assert r.status_code == 400 and "not configured" in r.json()["detail"]
