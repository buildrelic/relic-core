"""relic daemon loopback surface: inject (recall) and capture (write-back).

The Claude Code hooks call this on every turn, so the contract has to be exact and
forgiving: an empty prompt or an empty session is a clean no-op, a malformed body is a
400, and the optional bearer guards the writes but never liveness.
"""

from starlette.testclient import TestClient

from relic.serve import build_daemon_app


def _client(*, recall=None, capture=None, token=None):
    async def _recall(query, num_results):
        return f"ctx({query},{num_results})"

    async def _capture(session_id, transcript, summary):
        return {"status": "captured", "session_id": session_id, "loaded": 1}

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

    async def recall(query, num_results):
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

    async def recall(query, num_results):
        nonlocal called
        called = True
        return "x"

    resp = _client(recall=recall).post("/v1/daemon/inject", json={"prompt": "   "})
    assert resp.status_code == 200
    assert resp.json()["context"] == ""
    assert not called


def test_inject_clamps_num_results():
    seen = {}

    async def recall(query, num_results):
        seen["n"] = num_results
        return ""

    client = _client(recall=recall)
    client.post("/v1/daemon/inject", json={"prompt": "q", "num_results": 9999})
    assert seen["n"] == 25  # bounded to _MAX_RESULTS
    client.post("/v1/daemon/inject", json={"prompt": "q", "num_results": "bad"})
    assert seen["n"] == 10  # falls back to the default on garbage


def test_inject_bad_json_is_400():
    resp = _client().post("/v1/daemon/inject", content=b"not json")
    assert resp.status_code == 400


def test_capture_passes_fields_and_counts():
    seen = {}

    async def capture(session_id, transcript, summary):
        seen.update(session_id=session_id, transcript=transcript, summary=summary)
        return {"status": "captured", "session_id": session_id, "loaded": 2}

    client = _client(capture=capture)
    resp = client.post(
        "/v1/daemon/capture",
        json={"session_id": "abc", "transcript": "did things", "summary": "a summary"},
    )
    assert resp.status_code == 200
    assert resp.json()["loaded"] == 2
    assert seen == {"session_id": "abc", "transcript": "did things", "summary": "a summary"}
    assert client.get("/v1/daemon/status").json()["captures"] == 1


def test_inject_degrades_when_recall_fails():
    async def recall(query, num_results):
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
    async def capture(session_id, transcript, summary):
        raise RuntimeError("falkordb down")

    client = _client(capture=capture)
    resp = client.post("/v1/daemon/capture", json={"session_id": "a", "transcript": "t"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "error"
    assert client.get("/v1/daemon/status").json()["capture_errors"] == 1


def test_capture_requires_session_id():
    resp = _client().post("/v1/daemon/capture", json={"transcript": "x"})
    assert resp.status_code == 400


def test_capture_empty_session_is_noop():
    called = False

    async def capture(session_id, transcript, summary):
        nonlocal called
        called = True
        return {}

    resp = _client(capture=capture).post("/v1/daemon/capture", json={"session_id": "abc"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "empty"
    assert not called


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
        client.post(
            "/v1/daemon/capture", json={"session_id": "a", "transcript": "t"}
        ).status_code
        == 401
    )
    ok = client.post(
        "/v1/daemon/inject", json={"prompt": "q"}, headers={"Authorization": "Bearer secret"}
    )
    assert ok.status_code == 200
