"""High-severity regressions: real Hermes scopes and loopback HTTP only.

Scope tests need Hermes installed; missing host APIs are reported as skips, never
emulated as a passing integration. No real home, credential store or provider is used.
"""
import json
import sqlite3
import sys
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from grounded_recall import audit, secret_titles, semantic, sessions
from grounded_recall.core import GroundedRecall


def engine(**settings):
    config = {"sessions_enabled": False, "audit_log": False, **settings}
    return GroundedRecall(lambda k, d=None: config.get(k, d), environ={})


@pytest.fixture
def host():
    constants = pytest.importorskip("hermes_constants")
    scope = pytest.importorskip("agent.secret_scope")
    return constants, scope


@contextmanager
def routed(host, home, *, multiplex=True, secrets=None, bound_home=None):
    constants, scope = host
    ht = constants.set_hermes_home_override(home)
    mt = scope.set_multiplex_context(multiplex)
    st = scope.set_secret_scope(secrets, profile_home=str(bound_home or home))
    try:
        yield
    finally:
        scope.reset_secret_scope(st)
        scope.reset_multiplex_context(mt)
        constants.reset_hermes_home_override(ht)


def make_db(home, label):
    home.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(home / "state.db") as conn:
        conn.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, "
                     "timestamp REAL, role TEXT, content TEXT)")
        conn.execute("INSERT INTO messages VALUES (1,?,?,?,?)",
                     (label, time.time(), "user", "orchidplan " + label))


@contextmanager
def http_server(callback):
    class Handler(BaseHTTPRequestHandler):
        def respond(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            status, headers, response = callback(self, body)
            self.send_response(status)
            for name, value in headers.items():
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)

        do_POST = do_GET = respond

        def log_message(self, *_args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    try:
        yield "http://127.0.0.1:%s" % server.server_port
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)
        assert not thread.is_alive()


def embeddings_receiver(seen):
    def receive(request, body):
        payload = json.loads(body)
        seen.append(request.headers.get("Authorization"))
        data = [{"index": i, "embedding": [1.0, 0.0]} for i in range(len(payload["input"]))]
        return 200, {}, json.dumps({"data": data}).encode()
    return receive


def vault_config(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "a.md").write_text("orchidplan first candidate\n")
    (vault / "b.md").write_text("orchidplan second candidate\n")
    return dict(roots=[str(vault)], use_ripgrep=False, semantic_model="synthetic")


@pytest.mark.parametrize("multiplex", [False, True])
def test_hook_sessions_audit_and_credentials_stay_in_caller_profile(tmp_path, monkeypatch, host, multiplex):
    a, b = tmp_path / "profiles/alpha", tmp_path / "profiles/beta"
    make_db(a, "SYNTHETIC_ALPHA_PRIVATE")
    make_db(b, "SYNTHETIC_BETA_PRIVATE")
    monkeypatch.setenv("HERMES_HOME", str(a))
    monkeypatch.setenv("SYNTHETIC_EMBED_KEY", "alpha-not-a-real-key")
    seen = []
    with http_server(embeddings_receiver(seen)) as url:
        e = engine(**vault_config(tmp_path), sessions_enabled=True, audit_log=True,
                   semantic_enabled=True, semantic_url=url, semantic_api_key_env="SYNTHETIC_EMBED_KEY")
        with routed(host, b, multiplex=multiplex, secrets={"SYNTHETIC_EMBED_KEY": "beta-not-a-real-key"}):
            output = e.recall(session_id="beta-current", user_message="orchidplan")["context"]
    assert "SYNTHETIC_BETA_PRIVATE" in output
    assert "SYNTHETIC_ALPHA_PRIVATE" not in output
    assert seen == ["Bearer beta-not-a-real-key"]
    assert not audit.audit_path(a).exists()
    record = json.loads(audit.audit_path(b).read_text().splitlines()[-1])
    assert record["session"] == "beta-current" and record["semantic"] == "used"


@pytest.mark.parametrize("multiplex", [False, True])
@pytest.mark.parametrize("state", ["absent", "empty", "mismatch", "reader-error"])
def test_credentials_fail_closed_instead_of_using_launch_environment(tmp_path, monkeypatch, host, multiplex, state):
    a, b = tmp_path / "profiles/alpha", tmp_path / "profiles/beta"
    monkeypatch.setenv("HERMES_HOME", str(a))
    monkeypatch.setenv("SYNTHETIC_EMBED_KEY", "alpha-not-a-real-key")
    secrets = None if state == "absent" else {}
    if state == "mismatch":
        secrets = {"SYNTHETIC_EMBED_KEY": "alpha-not-a-real-key"}
    if state == "reader-error":
        def broken(*_a, **_k):
            raise RuntimeError("synthetic scope unavailable")
        monkeypatch.setattr(host[1], "get_secret", broken)
        monkeypatch.setattr(host[1], "current_secret_scope", broken)
    seen = []
    with http_server(embeddings_receiver(seen)) as url:
        with routed(host, b, multiplex=multiplex, secrets=secrets,
                    bound_home=a if state == "mismatch" else b):
            result = semantic.embed(["orchidplan"], url=url, model="synthetic", timeout_s=0.5,
                                    api_key_env="SYNTHETIC_EMBED_KEY")
    if state == "empty":
        assert seen == [None]  # optional authentication stays optional
        assert result is not None
    else:
        assert seen == [] and result is None


def test_settings_cache_is_not_shared_across_active_homes(tmp_path, monkeypatch, host):
    a, b = tmp_path / "profiles/alpha", tmp_path / "profiles/beta"
    monkeypatch.setenv("HERMES_HOME", str(a))
    config = {a: {"rule_text": "ALPHA_RULE"}, b: {"rule_text": "BETA_RULE"}}
    e = GroundedRecall(lambda k, d=None: config[host[0].get_hermes_home()].get(k, d), environ={})
    assert "ALPHA_RULE" in e.section()
    with routed(host, b, secrets={}):
        assert "BETA_RULE" in e.section() and "ALPHA_RULE" not in e.section()
    assert "ALPHA_RULE" in e.section() and "BETA_RULE" not in e.section()


@pytest.mark.parametrize("exception", [RuntimeError, ImportError])
def test_home_resolution_failure_never_reads_launch_sessions(tmp_path, monkeypatch, host, exception):
    make_db(tmp_path / "launch", "SYNTHETIC_ALPHA_PRIVATE")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "launch"))
    def broken():
        raise exception("synthetic host failure")
    monkeypatch.setattr(host[0], "get_hermes_home", broken)
    assert engine(sessions_enabled=True, audit_log=True).recall(user_message="orchidplan") is None
    assert not audit.audit_path(tmp_path / "launch").exists()


@pytest.mark.parametrize("multiplex", [False, True])
def test_routed_title_command_and_old_cache_are_disabled(tmp_path, monkeypatch, host, multiplex):
    a, b = tmp_path / "profiles/alpha", tmp_path / "profiles/beta"
    monkeypatch.setenv("HERMES_HOME", str(a))
    monkeypatch.setenv("SYNTHETIC_VENDOR_ACCOUNT", "alpha")
    cache = audit.state_dir(b) / "secret-titles.json"
    cache.parent.mkdir(parents=True)
    cache.write_text('["orchidplan OLD_ALPHA_COMMAND_CACHE"]')
    explicit = tmp_path / "titles.json"
    explicit.write_text('["orchidplan EXPLICIT_BETA_TITLE"]')
    launched = tmp_path / "command-ran"
    code = "from pathlib import Path; Path(%r).touch(); print('orchidplan alpha')" % str(launched)
    argv = [sys.executable, "-I", "-B", "-c", code]
    with routed(host, b, multiplex=multiplex, secrets={"SYNTHETIC_VENDOR_ACCOUNT": "beta"}):
        thread = secret_titles.refresh_in_background(argv, cache, 0, 2)
        if thread is not None:  # join even the vulnerable implementation before failing
            thread.join(3)
        assert thread is None
        secret_titles._run_command(argv, cache, 2)  # direct path also fails closed
        output = engine(secret_titles_command=argv, secret_titles_file=str(explicit)).recall(
            user_message="orchidplan")["context"]
    assert not launched.exists()
    assert "OLD_ALPHA_COMMAND_CACHE" not in output and "orchidplan alpha" not in output
    assert "EXPLICIT_BETA_TITLE" in output
    assert json.loads(cache.read_text()) == ["orchidplan OLD_ALPHA_COMMAND_CACHE"]


def test_title_command_refuses_unknown_scope(tmp_path, monkeypatch, host):
    def broken():
        raise RuntimeError("synthetic scope failure")
    monkeypatch.setattr(host[1], "serves_routed_profile", broken)
    assert secret_titles.refresh_in_background([sys.executable, "-c", "print('orchidplan')"],
                                               tmp_path / "cache.json", 0, 2) is None
    assert not (tmp_path / "cache.json").exists()


def test_single_profile_title_command_preserves_context_and_environment(tmp_path, monkeypatch, host):
    from contextvars import ContextVar
    marker = ContextVar("title-command-marker", default="lost")
    monkeypatch.setenv("SYNTHETIC_VENDOR_ACCOUNT", "single-profile")
    original = secret_titles._run_command
    observed = []
    def wrapped(*args, **kwargs):
        observed.append(marker.get())
        return original(*args, **kwargs)
    monkeypatch.setattr(secret_titles, "_run_command", wrapped)
    token = marker.set("caller")
    cache = tmp_path / "cache.json"
    try:
        thread = secret_titles.refresh_in_background(
            [sys.executable, "-I", "-B", "-c", "import os,json; print(json.dumps(['orchidplan '+os.environ['SYNTHETIC_VENDOR_ACCOUNT']]))"],
            cache, 24, 2)
        assert thread is not None
        thread.join(3)
        assert not thread.is_alive()
    finally:
        marker.reset(token)
    assert observed == ["caller"]
    assert json.loads(cache.read_text()) == ["orchidplan single-profile"]


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
@pytest.mark.parametrize("same_origin", [False, True])
def test_redirects_never_forward_auth_and_hook_keeps_lexical_result(tmp_path, monkeypatch, status, same_origin):
    monkeypatch.setattr(semantic, "_api_key", lambda _name: "synthetic-redirect-key")
    config = vault_config(tmp_path)
    baseline = engine(**config).recall(user_message="orchidplan")
    reached = []
    origin_auth = []
    def target(request, body):
        reached.append((request.command, request.headers.get("Authorization"), body))
        return 200, {}, b'{"data":[]}'
    with http_server(target) as target_url:
        def redirect(request, body):
            if request.path == "/received":
                return target(request, body)
            origin_auth.append(request.headers.get("Authorization"))
            location = "/received" if same_origin else target_url + "/received"
            return status, {"Location": location}, b""
        with http_server(redirect) as url:
            actual = engine(**config, semantic_enabled=True, semantic_url=url,
                            semantic_api_key_env="SYNTHETIC_EMBED_KEY").recall(user_message="orchidplan")
    assert origin_auth == ["Bearer synthetic-redirect-key"]
    assert reached == []
    assert actual == baseline


@pytest.mark.parametrize("multiplex", [False, True])
@pytest.mark.parametrize("path_style", ["environment", "dollar-home", "braced-home"])
def test_source_paths_cannot_inherit_launch_profile(tmp_path, monkeypatch, host, multiplex, path_style):
    a, b = tmp_path / "profiles/alpha", tmp_path / "profiles/beta"
    for home, marker in ((a, "ALPHA_PRIVATE"), (b, "BETA_PUBLIC")):
        (home / "notes").mkdir(parents=True)
        (home / "notes/note.md").write_text("orchidplan NOTE_" + marker)
        (home / "facts.md").write_text("orchidplan FACT_" + marker)
        (home / "titles.json").write_text(json.dumps(["orchidplan TITLE_" + marker]))
    monkeypatch.setenv("HERMES_HOME", str(a))
    launch = {"roots": str(a / "notes"), "key_facts_file": str(a / "facts.md"),
              "secret_titles_file": str(a / "titles.json")}
    for key, value in launch.items():
        monkeypatch.setenv("GROUNDED_RECALL_" + key.upper(), value)
    scope_values = {}
    config = {"sessions_enabled": False, "audit_log": False, "use_ripgrep": False}
    if path_style == "environment":
        scope_values = {"GROUNDED_RECALL_" + k.upper(): v.replace(str(a), str(b))
                        for k, v in launch.items()}
    else:
        prefix = "$HERMES_HOME" if path_style == "dollar-home" else "${HERMES_HOME}"
        config.update({k: v.replace(str(a), prefix) for k, v in launch.items()})
    e = GroundedRecall(lambda k, d=None: config.get(k, d))  # real env resolution, not environ={}
    with routed(host, b, multiplex=multiplex, secrets=scope_values):
        output = e.recall(user_message="orchidplan")["context"] + e.section()
    assert "ALPHA_PRIVATE" not in output
    assert "NOTE_BETA_PUBLIC" in output and "FACT_BETA_PUBLIC" in output and "TITLE_BETA_PUBLIC" in output


def test_routed_scope_without_settings_does_not_use_process_settings(tmp_path, monkeypatch, host):
    a, b = tmp_path / "profiles/alpha", tmp_path / "profiles/beta"
    a.mkdir(parents=True)
    (a / "note.md").write_text("orchidplan ALPHA_PRIVATE")
    monkeypatch.setenv("HERMES_HOME", str(a))
    monkeypatch.setenv("GROUNDED_RECALL_ROOTS", str(a))
    with routed(host, b, secrets={}):
        e = GroundedRecall(lambda k, d=None: False if k == "sessions_enabled" else d)
        assert e.recall(user_message="orchidplan") is None


@pytest.mark.parametrize("key", ["TERMINAL_EMBEDDING_API_KEY", "HERMES_TELEGRAM_EMBED_KEY", "SYNTHETIC_KEY"])
def test_arbitrary_key_names_use_only_validated_scope(tmp_path, monkeypatch, host, key):
    monkeypatch.setenv(key, "alpha-not-a-real-key")
    seen = []
    with http_server(embeddings_receiver(seen)) as url:
        with routed(host, tmp_path / "profiles/beta", secrets={key: "beta-not-a-real-key"}):
            assert semantic.embed(["orchidplan"], url=url, model="synthetic", timeout_s=0.5,
                                  api_key_env=key) is not None
    assert seen == ["Bearer beta-not-a-real-key"]


def test_single_profile_secret_reader_failure_does_not_fall_back_to_environment(tmp_path, monkeypatch, host):
    monkeypatch.setenv("SYNTHETIC_EMBED_KEY", "alpha-not-a-real-key")
    def broken(*_args, **_kwargs):
        raise RuntimeError("synthetic credential reader failure")
    monkeypatch.setattr(host[1], "get_secret", broken)
    seen = []
    with http_server(embeddings_receiver(seen)) as url:
        assert semantic.embed(["orchidplan"], url=url, model="synthetic", timeout_s=0.5,
                              api_key_env="SYNTHETIC_EMBED_KEY") is None
    assert seen == []


@pytest.mark.parametrize("suffix", ["]", ","])
def test_json_titles_with_bom_never_inject_other_fields(tmp_path, suffix):
    path = tmp_path / "titles.json"
    path.write_text('\ufeff[{"title":"orchidplan","password":"SYNTHETIC_PRIVATE_VALUE"}' + suffix)
    result = engine(secret_titles_file=str(path)).recall(user_message="orchidplan")
    assert "SYNTHETIC_PRIVATE_VALUE" not in str(result)
    if suffix == "]":
        assert secret_titles.read_titles_file(path) == ["orchidplan"]
    else:
        assert result is None


def test_timed_out_worker_keeps_its_scope_and_does_not_accumulate(tmp_path, monkeypatch, host):
    import grounded_recall.core as core
    entered = threading.Event()
    release = threading.Event()
    calls = []
    homes = []
    def slow_search(*_args, **_kwargs):
        calls.append(True)
        entered.set()
        assert release.wait(3)
        homes.append(sessions.hermes_home())
        return []
    monkeypatch.setattr(core, "search_roots", slow_search)
    a, b = tmp_path / "profiles/alpha", tmp_path / "profiles/beta"
    monkeypatch.setenv("HERMES_HOME", str(a))
    e = engine(**vault_config(tmp_path), time_budget_ms=150)
    original_threads = set(threading.enumerate())
    try:
        with routed(host, b, secrets={}):
            assert e.recall(user_message="orchidplan") is None
            assert entered.is_set() and len(calls) == 1
        assert e.recall(user_message="orchidplan") is None
        assert len(calls) == 1
    finally:
        release.set()
        for worker in set(threading.enumerate()) - original_threads:
            if worker.name == "grounded-recall":
                worker.join(2)
                assert not worker.is_alive()
    assert homes == [b]  # original context survives even after the caller resets it
    result = e.recall(user_message="orchidplan")
    assert result is not None and "No automatic matches" in result["context"]
    assert calls == [True, True] and homes == [b, a]
