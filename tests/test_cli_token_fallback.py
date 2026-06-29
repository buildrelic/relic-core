"""A forwarded user token falls back to the server token only when it is truly dead.

The serve handler only format-validates a forwarded user token; an expired or revoked one
is well-formed but github rejects it at use time with a 401. The composition root probes it
with a cheap authenticated call before the heavy fetch. The probe is best-effort: only a
401 falls back to the server token. A 403 (rate limit, sso, scope) or any other probe error
keeps the user token, so a throttled or restricted user is never silently re-run as the
server. These tests are hermetic: a fake GitHub client stands in, so there is no network and
no FalkorDB. The token value is never logged.
"""

import logging
import types

import pytest
from githubkit.exception import RequestFailed

from relic import cli

_USER_TOKEN = "gho_user_secret"
_SERVER_TOKEN = "gho_server_secret"


class _FakeResponse:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


class _FakeUsers:
    def __init__(self, status: int | None) -> None:
        self._status = status

    async def async_get_authenticated(self) -> object:
        if self._status is not None:
            # mimic githubkit: a failed status raises RequestFailed carrying the response
            exc = RequestFailed.__new__(RequestFailed)
            exc.response = _FakeResponse(self._status)  # type: ignore[attr-defined]
            raise exc
        return object()


class _FakeRest:
    def __init__(self, status: int | None) -> None:
        self.users = _FakeUsers(status)


class _FakeGH:
    """Stands in for githubkit.GitHub: an async context manager with a rest.users probe."""

    def __init__(self, token: str, status_for: dict[str, int | None]) -> None:
        self.token = token
        self.rest = _FakeRest(status_for.get(token))

    async def __aenter__(self) -> "_FakeGH":
        return self

    async def __aexit__(self, *_a: object) -> bool:
        return False


def _make_github_factory(status_for: dict[str, int | None]):
    """Build a make_github stub; status_for maps a token to the status its probe raises."""

    def _factory(token: str) -> _FakeGH:
        return _FakeGH(token, status_for)

    return _factory


async def test_user_token_401_falls_back_to_server_token(
    monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    monkeypatch.setenv(cli._SERVER_TOKEN_ENV, _SERVER_TOKEN)
    # the user token is dead (401); the server token is healthy
    factory = _make_github_factory({_USER_TOKEN: 401, _SERVER_TOKEN: None})
    log = logging.getLogger("test-fallback")

    with caplog.at_level(logging.WARNING):
        used = await cli._github_token_with_fallback(_USER_TOKEN, factory, log)

    assert used == _SERVER_TOKEN  # fell back
    # no token value ever lands in a log line
    assert _USER_TOKEN not in caplog.text
    assert _SERVER_TOKEN not in caplog.text
    assert "401" in caplog.text  # but the status is reported


@pytest.mark.parametrize("status", [403, 404, 500])
async def test_non_401_keeps_the_user_token(
    monkeypatch: pytest.MonkeyPatch, caplog, status: int
) -> None:
    # a 403 (rate limit / sso / ip allow-list / missing scope) or any other http error is
    # not a dead token: keep the user's identity rather than silently running as the server.
    monkeypatch.setenv(cli._SERVER_TOKEN_ENV, _SERVER_TOKEN)
    factory = _make_github_factory({_USER_TOKEN: status, _SERVER_TOKEN: None})

    with caplog.at_level(logging.WARNING):
        used = await cli._github_token_with_fallback(_USER_TOKEN, factory, logging.getLogger("t"))

    assert used == _USER_TOKEN  # kept the user token, no fallback
    assert caplog.text == ""  # nothing logged: this is not a rejection


async def test_probe_transient_error_keeps_the_user_token(
    monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    # a non-RequestFailed error (network, timeout) during the probe must not fail an ingest
    # that would otherwise run: the probe only ever adds a fallback, it never blocks a run.
    # and the token value must never reach a log line on this branch either.
    monkeypatch.setenv(cli._SERVER_TOKEN_ENV, _SERVER_TOKEN)

    class _Boom:
        async def __aenter__(self):
            raise RuntimeError("network down")

        async def __aexit__(self, *_a: object) -> bool:
            return False

    def _factory(_token: str) -> _Boom:
        return _Boom()

    with caplog.at_level(logging.DEBUG):
        used = await cli._github_token_with_fallback(_USER_TOKEN, _factory, logging.getLogger("t"))

    assert used == _USER_TOKEN
    assert _USER_TOKEN not in caplog.text  # the token is never logged, even on the transient path
    assert _SERVER_TOKEN not in caplog.text


async def test_request_failed_with_no_response_keeps_the_user_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # a malformed RequestFailed carrying no response (status resolves to None) is not a 401,
    # so the guard must keep the user token rather than silently swapping identity.
    monkeypatch.setenv(cli._SERVER_TOKEN_ENV, _SERVER_TOKEN)

    class _NoRespUsers:
        async def async_get_authenticated(self) -> object:
            exc = RequestFailed.__new__(RequestFailed)
            exc.response = None  # type: ignore[attr-defined]
            raise exc

    class _NoRespGH:
        def __init__(self) -> None:
            self.rest = types.SimpleNamespace(users=_NoRespUsers())

        async def __aenter__(self) -> "_NoRespGH":
            return self

        async def __aexit__(self, *_a: object) -> bool:
            return False

    used = await cli._github_token_with_fallback(
        _USER_TOKEN, lambda _t: _NoRespGH(), logging.getLogger("t")
    )
    assert used == _USER_TOKEN


async def test_valid_user_token_used_as_is(monkeypatch: pytest.MonkeyPatch, caplog) -> None:
    monkeypatch.setenv(cli._SERVER_TOKEN_ENV, _SERVER_TOKEN)
    factory = _make_github_factory({_USER_TOKEN: None, _SERVER_TOKEN: None})
    log = logging.getLogger("test-fallback")

    with caplog.at_level(logging.WARNING):
        used = await cli._github_token_with_fallback(_USER_TOKEN, factory, log)

    assert used == _USER_TOKEN  # healthy user token kept
    assert caplog.text == ""  # nothing logged on the happy path
    assert _USER_TOKEN not in caplog.text


async def test_no_server_fallback_skips_the_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    # without a stashed server token there is nothing to fall back to: skip the round trip
    # entirely (the real fetch will surface any auth error itself).
    monkeypatch.delenv(cli._SERVER_TOKEN_ENV, raising=False)
    probed = False

    def _factory(_token: str) -> _FakeGH:
        nonlocal probed
        probed = True
        return _FakeGH(_token, {})

    used = await cli._github_token_with_fallback(_USER_TOKEN, _factory, logging.getLogger("t"))
    assert used == _USER_TOKEN
    assert not probed  # no authenticated call made


async def test_token_equal_to_server_skips_the_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    # the resolved token already is the server token (no user token forwarded): nothing to
    # verify, so skip the extra call.
    monkeypatch.setenv(cli._SERVER_TOKEN_ENV, _SERVER_TOKEN)
    probed = False

    def _factory(_token: str) -> _FakeGH:
        nonlocal probed
        probed = True
        return _FakeGH(_token, {})

    used = await cli._github_token_with_fallback(_SERVER_TOKEN, _factory, logging.getLogger("t"))
    assert used == _SERVER_TOKEN
    assert not probed


def test_resolve_server_token_reads_settings_not_raw_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # the server token must come through settings (env or .env), not a raw os.environ read,
    # so the fallback still works when GITHUB_TOKEN lives only in .env.
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)  # not exported in the process env
    monkeypatch.setattr(
        "relic.config.get_settings", lambda: types.SimpleNamespace(github_token=_SERVER_TOKEN)
    )

    assert cli._resolve_server_token() == _SERVER_TOKEN


def test_resolve_server_token_none_when_unconfigured(monkeypatch: pytest.MonkeyPatch) -> None:
    # no server token of its own: nothing to stash, so return None. crucially this never
    # shells out to `gh auth token`, so it stays deterministic on the ingest trigger path.
    monkeypatch.setattr(
        "relic.config.get_settings", lambda: types.SimpleNamespace(github_token=None)
    )
    assert cli._resolve_server_token() is None


def test_trigger_ingest_stashes_server_token_for_child(monkeypatch: pytest.MonkeyPatch) -> None:
    import subprocess

    cli._INGEST_PROCS.clear()
    captured = {}

    class _FakeProc:
        def poll(self):
            return None

    def _fake_popen(args, env=None):
        captured["args"], captured["env"] = list(args), env
        return _FakeProc()

    monkeypatch.setattr(subprocess, "Popen", _fake_popen)
    # the server resolves its own identity the normal way
    monkeypatch.setattr(cli, "_resolve_server_token", lambda: _SERVER_TOKEN)

    cli._trigger_ingest("github", "owner/repo", _USER_TOKEN)
    env = captured["env"]
    assert env["GITHUB_TOKEN"] == _USER_TOKEN  # child runs as the user
    assert env[cli._SERVER_TOKEN_ENV] == _SERVER_TOKEN  # server token rides along for fallback
    assert _USER_TOKEN not in captured["args"]  # never on the command line


def test_trigger_ingest_drops_stale_server_token(monkeypatch: pytest.MonkeyPatch) -> None:
    import subprocess

    cli._INGEST_PROCS.clear()
    captured = {}

    def _fake_popen(args, env=None):
        captured["env"] = env
        return type("P", (), {"poll": lambda self: None})()

    monkeypatch.setattr(subprocess, "Popen", _fake_popen)
    # a stale value is already in the parent env, and the server has no token of its own
    monkeypatch.setenv(cli._SERVER_TOKEN_ENV, "gho_stale_leftover")
    monkeypatch.setattr(cli, "_resolve_server_token", lambda: None)

    cli._trigger_ingest("github", "owner/repo", _USER_TOKEN)
    # the stale value must not survive into the child posing as a fallback identity
    assert cli._SERVER_TOKEN_ENV not in captured["env"]
    assert captured["env"]["GITHUB_TOKEN"] == _USER_TOKEN


def test_trigger_ingest_no_user_token_inherits_env(monkeypatch: pytest.MonkeyPatch) -> None:
    import subprocess

    cli._INGEST_PROCS.clear()
    captured = {}

    def _fake_popen(args, env=None):
        captured["env"] = env
        return type("P", (), {"poll": lambda self: None})()

    monkeypatch.setattr(subprocess, "Popen", _fake_popen)
    cli._trigger_ingest("github", "owner/repo", None)
    assert captured["env"] is None  # no override, no stash; child inherits the parent env
