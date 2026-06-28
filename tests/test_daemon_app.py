"""relic daemon loopback surface: inject (recall) and capture (write-back).

The Claude Code hooks call this on every turn, so the contract has to be exact and
forgiving: an empty prompt or an empty session is a clean no-op, a malformed body is a
400, and the optional bearer guards the writes but never liveness.
"""

from starlette.testclient import TestClient

from relic.serve import build_daemon_app


def _client(*, recall=None, capture=None, token=None):
    async def _recall(query, num_results, cwd):
        return f"ctx({query},{num_results})"

    async def _capture(payload):
        return {"status": "captured", "session_id": payload["session_id"], "loaded": 1}

    return TestClient(
        build_daemon_app(recall=recall or _recall, capture=capture or _capture, token=token)
    )


def test_status_reports_zeroed_counters():
    body = _client().get("/v1/daemon/status").json()
    assert body["status"] == "ok"
    assert body["injects"] == 0 and body["captures"] == 0
    assert body["last_inject_at"] is None


def test_inject_returns_recall_context_and_counts():
    seen = {}

    async def recall(query, num_results, cwd):
        seen["query"], seen["n"] = query, num_results
        return "REMEMBERED"

    client = _client(recall=recall)
    resp = client.post(
        "/v1/daemon/inject", json={"prompt": "how do tokens expire", "num_results": 5}
    )
    assert resp.status_code == 200
    assert resp.json()["context"] == "REMEMBERED"
    assert seen == {"query": "how do tokens expire", "n": 5}
    status = client.get("/v1/daemon/status").json()
    assert status["injects"] == 1
    assert status["last_inject_at"] is not None


def test_inject_empty_prompt_is_noop():
    called = False

    async def recall(query, num_results, cwd):
        nonlocal called
        called = True
        return "x"

    resp = _client(recall=recall).post("/v1/daemon/inject", json={"prompt": "   "})
    assert resp.status_code == 200
    assert resp.json()["context"] == ""
    assert not called


def test_inject_clamps_num_results():
    seen = {}

    async def recall(query, num_results, cwd):
        seen["n"] = num_results
        return ""

    client = _client(recall=recall)
    client.post("/v1/daemon/inject", json={"prompt": "q", "num_results": 9999})
    assert seen["n"] == 25  # bounded to _MAX_RESULTS
    client.post("/v1/daemon/inject", json={"prompt": "q", "num_results": "bad"})
    assert seen["n"] == 10  # falls back to the default on garbage


def test_inject_forwards_cwd_to_recall():
    seen = {}

    async def recall(query, num_results, cwd):
        seen["cwd"] = cwd
        return ""

    _client(recall=recall).post("/v1/daemon/inject", json={"prompt": "q", "cwd": "/work/some-repo"})
    assert seen["cwd"] == "/work/some-repo"


def test_inject_bad_json_is_400():
    resp = _client().post("/v1/daemon/inject", content=b"not json")
    assert resp.status_code == 400


def test_capture_accepts_and_writes_back_in_background():
    seen = {}

    async def capture(payload):
        seen.update(payload)
        return {"status": "captured", "session_id": payload["session_id"], "loaded": 2}

    client = _client(capture=capture)
    resp = client.post(
        "/v1/daemon/capture",
        json={"session_id": "abc", "transcript": "did things", "summary": "a summary"},
    )
    # accepted immediately so the SessionEnd hook never blocks on LLM extraction
    assert resp.status_code == 202
    assert resp.json()["status"] == "accepted"
    # the background write-back ran: capture got the payload, the counter advanced
    assert seen["session_id"] == "abc"
    assert seen["transcript"] == "did things"
    assert seen["summary"] == "a summary"
    assert client.get("/v1/daemon/status").json()["captures"] == 1


def test_capture_accepts_a_transcript_path_only():
    seen = {}

    async def capture(payload):
        seen.update(payload)
        return {"status": "captured", "session_id": payload["session_id"], "loaded": 1}

    client = _client(capture=capture)
    resp = client.post(
        "/v1/daemon/capture", json={"session_id": "s1", "transcript_path": "/tmp/x.jsonl"}
    )
    assert resp.status_code == 202
    assert seen["transcript_path"] == "/tmp/x.jsonl"


def test_recall_returns_context_without_bumping_loop_counters():
    async def recall(query, num_results, cwd):
        return f"hit:{query}"

    client = _client(recall=recall)
    resp = client.post("/v1/daemon/recall", json={"query": "auth flow", "num_results": 3})
    assert resp.status_code == 200
    assert resp.json()["context"] == "hit:auth flow"
    # a manual search is not a loop turn: inject counter stays at zero
    assert client.get("/v1/daemon/status").json()["injects"] == 0


def test_inject_degrades_when_recall_fails():
    async def recall(query, num_results, cwd):
        raise RuntimeError("engram down")

    client = _client(recall=recall)
    resp = client.post("/v1/daemon/inject", json={"prompt": "q"})
    # never break the turn: empty context, 200, and the failure is counted
    assert resp.status_code == 200
    assert resp.json()["context"] == ""
    status = client.get("/v1/daemon/status").json()
    assert status["inject_errors"] == 1
    assert status["injects"] == 0


def test_capture_degrades_when_writeback_fails():
    async def capture(payload):
        raise RuntimeError("falkordb down")

    client = _client(capture=capture)
    resp = client.post("/v1/daemon/capture", json={"session_id": "a", "transcript": "t"})
    # accepted up front; the async write-back failure is counted, never surfaced
    assert resp.status_code == 202
    assert client.get("/v1/daemon/status").json()["capture_errors"] == 1


def test_capture_requires_session_id():
    resp = _client().post("/v1/daemon/capture", json={"transcript": "x"})
    assert resp.status_code == 400


def test_capture_empty_session_is_noop():
    called = False

    async def capture(payload):
        nonlocal called
        called = True
        return {}

    # no transcript, no transcript_path, no summary -> nothing to remember
    resp = _client(capture=capture).post("/v1/daemon/capture", json={"session_id": "abc"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "empty"
    assert not called


def test_activity_feed_records_inject_and_capture():
    client = _client()
    client.post("/v1/daemon/inject", json={"prompt": "how do tokens work", "cwd": "/work/myrepo"})
    client.post(
        "/v1/daemon/capture",
        json={"session_id": "sess-abcdef12", "transcript": "t", "cwd": "/work/myrepo"},
    )
    events = client.get("/v1/daemon/activity").json()["events"]
    kinds = [e["kind"] for e in events]
    assert "inject" in kinds and "capture" in kinds
    inj = next(e for e in events if e["kind"] == "inject")
    assert inj["repo"] == "myrepo"  # leaf dir, not the full path
    assert inj["query"] == "how do tokens work"


def test_activity_requires_auth_when_token_set():
    client = _client(token="secret")
    assert client.get("/v1/daemon/activity").status_code == 401
    ok = client.get("/v1/daemon/activity", headers={"Authorization": "Bearer secret"})
    assert ok.status_code == 200


def test_empty_token_means_no_auth():
    # a blank token must not lock everyone out by requiring the literal "Bearer ".
    for blank in ("", "   ", None):
        client = _client(token=blank)
        assert client.post("/v1/daemon/inject", json={"prompt": "q"}).status_code == 200


def test_token_guards_writes_not_status():
    client = _client(token="secret")
    # liveness stays open so a supervisor can poll without the secret
    assert client.get("/v1/daemon/status").status_code == 200
    # writes need the bearer
    assert client.post("/v1/daemon/inject", json={"prompt": "q"}).status_code == 401
    assert (
        client.post("/v1/daemon/capture", json={"session_id": "a", "transcript": "t"}).status_code
        == 401
    )
    ok = client.post(
        "/v1/daemon/inject", json={"prompt": "q"}, headers={"Authorization": "Bearer secret"}
    )
    assert ok.status_code == 200
