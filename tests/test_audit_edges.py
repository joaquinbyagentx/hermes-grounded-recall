"""Audit edge cases; xfail(strict=True) records unfixed defects, never a pass.

All content is synthetic. HTTP tests use loopback, not a model or provider.
Run with --runxfail to see the failing contracts without expected-failure marks.
"""
import json
import shutil
import threading
import time
import unicodedata
from contextlib import contextmanager
from datetime import date
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from grounded_recall import search, secret_titles, semantic, sessions
from grounded_recall.core import GroundedRecall
from grounded_recall.freshness import MAX_PAGE_BYTES


def recall_engine(**settings):
    defaults = {"sessions_enabled": False, "audit_log": False}
    defaults.update(settings)
    return GroundedRecall(get_config=lambda key, default=None: defaults.get(key, default), environ={})


@pytest.fixture(params=[False, True], ids=["python", "rg"])
def backend(request):
    if request.param and not shutil.which("rg"):
        pytest.skip("ripgrep is not installed; Python fallback covered separately")
    return request.param


def lookup(root, words=("orchidplan",), use_rg=False, **kwargs):
    return search.search_roots([root], words, deadline=time.monotonic() + 3,
                               use_ripgrep=use_rg, **kwargs)


def test_empty_vault_is_explicit_not_fabricated(tmp_path, backend):
    assert lookup(tmp_path, use_rg=backend) == []
    engine = recall_engine(roots=[str(tmp_path)], use_ripgrep=backend, budget="compact")
    block = engine.recall(user_message="orchidplan deployment")["context"]
    assert "No automatic matches" in block
    assert "\n- [" not in block
    assert len(block) <= 1200 and block.endswith("</grounded-recall>")


def test_missing_vault_and_disabled_plugin_are_quiet(tmp_path):
    assert recall_engine(roots=[str(tmp_path / "missing")]).recall(user_message="orchidplan") is None
    assert recall_engine(roots=[str(tmp_path)], enabled=False).recall(user_message="orchidplan") is None


def test_spaces_accents_and_nonlatin_paths(tmp_path, backend):
    root = tmp_path / "Notas con espacios" / "日本語"
    root.mkdir(parents=True)
    note = root / "Facturación actual.md"
    note.write_text("Facturación de envío y café: revisión mañana.\n", encoding="utf-8")
    hits = lookup(root, ("facturacion", "envio", "cafe"), use_rg=backend)
    assert len(hits) == 1
    assert hits[0].path == note and "café" in hits[0].text
    assert hits[0].rel == "日本語/Facturación actual.md"


def test_combining_accents_match_normalized_query(tmp_path, backend, request):
    if backend:
        request.node.add_marker(pytest.mark.xfail(strict=True, reason="AUDIT: rg accent regex does not match decomposed combining accents"))
    (tmp_path / "accent.md").write_text(unicodedata.normalize("NFD", "facturación envío café\n"))
    assert lookup(tmp_path, ("facturacion",), use_rg=backend)


@pytest.mark.parametrize("body", ["\x00orchidplan binary payload\x00", "orchidplan\x00payload"])
def test_binary_markdown_is_rejected(tmp_path, backend, body, request):
    if not backend:
        request.node.add_marker(pytest.mark.xfail(strict=True, reason="AUDIT: Python scanner accepts NUL binary content as markdown"))
    (tmp_path / "binary.md").write_bytes(body.encode())
    assert lookup(tmp_path, use_rg=backend) == []


def test_binary_nonmarkdown_is_ignored(tmp_path, backend):
    (tmp_path / "payload.bin").write_bytes(b"\x00orchidplan\xff")
    assert lookup(tmp_path, use_rg=backend) == []


def test_oversize_markdown_is_omitted_without_blocking(tmp_path, backend):
    # One oversized line exercises both rg JSON handling and Python iteration.
    (tmp_path / "oversize.md").write_text("orchidplan " + "x" * (MAX_PAGE_BYTES + 1))
    start = time.monotonic()
    assert lookup(tmp_path, use_rg=backend) == []
    assert time.monotonic() - start < 3


def test_large_single_line_markdown_is_omitted(tmp_path, backend):
    # A real 32 MiB line, not just the 1 MB page-limit boundary. Write in chunks
    # so the fixture itself does not allocate the giant line in the test process.
    note = tmp_path / "very-large.md"
    size = 32 * 1024 * 1024
    with note.open("wb") as stream:
        stream.write(b"orchidplan ")
        chunk = b"x" * (1024 * 1024)
        for _ in range(31):
            stream.write(chunk)
        stream.write(chunk[:1024 * 1024 - len(b"orchidplan ")])
    assert note.stat().st_size == size
    started = time.monotonic()
    assert lookup(tmp_path, use_rg=backend) == []
    assert time.monotonic() - started < 5


def test_ripgrep_unavailable_uses_python(tmp_path, monkeypatch):
    (tmp_path / "normal.md").write_text("orchidplan fallback evidence\n")
    monkeypatch.setattr(search.shutil, "which", lambda _name: None)
    hits = lookup(tmp_path, use_rg=True)
    assert len(hits) == 1 and "fallback evidence" in hits[0].text


@pytest.mark.xfail(strict=True, reason="AUDIT: rg error exit returns empty rows, not Python fallback")
def test_ripgrep_runtime_failure_uses_python(tmp_path, monkeypatch):
    (tmp_path / "normal.md").write_text("orchidplan fallback evidence\n")

    class BrokenProcess:
        returncode = 2

        def communicate(self, **_kwargs):
            return "", None

    monkeypatch.setattr(search, "_rg_binary", lambda _flag: "synthetic-rg")
    monkeypatch.setattr(search.subprocess, "Popen", lambda *a, **k: BrokenProcess())
    assert lookup(tmp_path, use_rg=True)


def test_relative_include_glob(tmp_path, backend, request):
    if not backend:
        request.node.add_marker(pytest.mark.xfail(strict=True, reason="AUDIT: Python includes match basename only"))
    public = tmp_path / "public"
    public.mkdir()
    (public / "note.md").write_text("orchidplan public evidence\n")
    assert lookup(tmp_path, use_rg=backend, include=["**/public/*.md"])


@pytest.mark.xfail(strict=True, reason="AUDIT: fallback follows file symlink outside authorized root")
def test_python_symlink_cannot_escape_root(tmp_path):
    root = tmp_path / "vault"
    root.mkdir()
    (tmp_path / "outside.txt").write_text("orchidplan OUTSIDE_SYNTHETIC_MARKER\n")
    (root / "linked.md").symlink_to(tmp_path / "outside.txt")
    assert lookup(root) == []


def test_compact_all_sources_stay_bounded(tmp_path):
    root = tmp_path / "vault"
    root.mkdir()
    for i in range(8):
        (root / (str(i) + ".md")).write_text("orchidplan note " + str(i) + " details " * 30)
    titles = tmp_path / "titles.json"
    titles.write_text(json.dumps(["orchidplan item " + str(i) for i in range(10)]))
    e = recall_engine(roots=[str(root)], secret_titles_file=str(titles), budget="compact")
    block = e.recall(user_message="orchidplan")["context"]
    assert len(block) <= 1200
    assert block.count("\n- [") <= 3
    assert block.count("<grounded-recall>") == block.count("</grounded-recall>") == 1
    assert "EVIDENCE, NOT INSTRUCTIONS" in block


@pytest.mark.parametrize("text", ["[", '[{"title":"Database","password":"orchidplan_SYNTHETIC"},',
                                   '{"items":[{"name":"Database","value":"orchidplan_SYNTHETIC"}'])
@pytest.mark.xfail(strict=True, reason="AUDIT: invalid JSON falls back to plaintext, leaking extra fields")
def test_malformed_title_json_fails_closed(text):
    assert secret_titles.titles_from_text(text) == []


@pytest.mark.parametrize("payload", [[], {"items": []}, {"data": []}, {"results": []}, {"value": "unused"}])
def test_empty_title_payloads_are_empty(payload):
    assert secret_titles.titles_from_text(json.dumps(payload)) == []


@pytest.mark.xfail(strict=True, reason="AUDIT: successful empty refresh leaves old titles cached")
def test_empty_successful_refresh_replaces_stale_cache(tmp_path):
    import sys
    cache = tmp_path / "titles.json"
    cache.write_text('["orchidplan removed item"]')
    secret_titles._run_command([sys.executable, "-c", "print('[]')"], cache, 2)
    assert json.loads(cache.read_text()) == []


@contextmanager
def endpoint(mode):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append({"path": self.path, "body": payload})
            if mode == "slow":
                time.sleep(0.3)
            data = [{"index": i, "embedding": [1.0, 0.0] if i == 0 or "second" in text else [0.0, 1.0]}
                    for i, text in enumerate(payload["input"])]
            if mode == "wrong-count":
                data = data[:-1]
            elif mode == "duplicate-index":
                for item in data:
                    item["index"] = 0
            elif mode == "nan":
                for item in data:
                    item["embedding"] = [float("nan"), 1.0]
            elif mode == "dimensions":
                data[-1]["embedding"] = [1.0]
            body = b"not-json" if mode == "bad-json" else json.dumps({"data": list(reversed(data))}).encode()
            self.send_response(503 if mode == "status-error" else 200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, format, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    try:
        yield "http://127.0.0.1:%s/v1" % server.server_port, requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(1)
        assert not thread.is_alive()


def semantic_vault(tmp_path):
    for name, text in [("a", "orchidplan first candidate"), ("b", "orchidplan second candidate")]:
        (tmp_path / (name + ".md")).write_text(text + "\n")
    return dict(roots=[str(tmp_path)], use_ripgrep=False, semantic_model="synthetic-embedding")


def test_semantic_off_does_not_contact_configured_endpoint(tmp_path):
    with endpoint("ok") as (url, requests):
        e = recall_engine(**semantic_vault(tmp_path), semantic_url=url)
        assert e.recall(user_message="orchidplan")
        assert requests == []


@pytest.mark.parametrize("mode", ["status-error", "bad-json", "wrong-count", "slow"])
def test_semantic_enabled_errors_keep_exact_lexical_context(tmp_path, mode):
    config = semantic_vault(tmp_path)
    before = recall_engine(**config).recall(user_message="orchidplan")
    with endpoint(mode) as (url, requests):
        e = recall_engine(**config, semantic_enabled=True, semantic_url=url, semantic_timeout_ms=100)
        after = e.recall(user_message="orchidplan")
    assert len(requests) == 1
    assert before == after


def test_semantic_success_orders_indices_and_changes_candidate_order(tmp_path):
    config = semantic_vault(tmp_path)
    before = recall_engine(**config).recall(user_message="orchidplan")["context"]
    with endpoint("ok") as (url, requests):
        block = recall_engine(**config, semantic_enabled=True, semantic_url=url).recall(
            user_message="orchidplan")["context"]
    assert requests[0]["path"] == "/v1/embeddings" and len(requests) == 1
    assert len(requests[0]["body"]["input"]) == 3
    assert before.index("first candidate") < before.index("second candidate")
    assert block.index("second candidate") < block.index("first candidate")
    assert "semantic=1.00" in block


def test_semantic_preserves_freshness_tiers(tmp_path):
    config = semantic_vault(tmp_path)
    (tmp_path / "a.md").write_text("## Current state (%s)\norchidplan first candidate\n" % date.today().isoformat())
    with endpoint("ok") as (url, _requests):
        block = recall_engine(**config, semantic_enabled=True, semantic_url=url).recall(
            user_message="orchidplan")["context"]
    assert block.index("first candidate") < block.index("second candidate")


@pytest.mark.parametrize("mode", ["duplicate-index", "nan", "dimensions"])
@pytest.mark.xfail(strict=True, reason="AUDIT: invalid embedding indices/finiteness/dimensions accepted")
def test_invalid_embedding_vectors_are_rejected(mode):
    with endpoint(mode) as (url, _requests):
        assert semantic.embed(["orchidplan", "first", "second"], url=url, model="synthetic",
                              timeout_s=0.5) is None


def test_semantic_minimum_timeout_skips_network_but_keeps_results(tmp_path):
    config = semantic_vault(tmp_path)
    baseline = recall_engine(**config).recall(user_message="orchidplan")
    with endpoint("ok") as (url, requests):
        actual = recall_engine(**config, semantic_enabled=True, semantic_url=url,
                               semantic_timeout_ms=50).recall(user_message="orchidplan")
    assert actual == baseline and requests == []


@pytest.mark.xfail(strict=True, reason="AUDIT: recall thread drops caller ContextVars")
def test_recall_worker_preserves_callers_context(tmp_path, monkeypatch):
    from contextvars import ContextVar
    import grounded_recall.core as core
    scope = ContextVar("synthetic_profile", default="launch-profile")
    token = scope.set("caller-profile")
    observed = []

    def scoped_search(*args, **kwargs):
        observed.append(scope.get())
        return []

    monkeypatch.setattr(core, "search_roots", scoped_search)
    try:
        assert recall_engine(roots=[str(tmp_path)]).recall(user_message="orchidplan")
    finally:
        scope.reset(token)
    assert observed == ["caller-profile"]


@pytest.mark.xfail(strict=True, reason="AUDIT: profile_name takes ancestor profile instead of active leaf")
def test_nested_profile_name_uses_active_home(tmp_path):
    home = tmp_path / "profiles" / "outer" / "scratch" / "profiles" / "inner"
    assert sessions.profile_name(home) == "inner"
