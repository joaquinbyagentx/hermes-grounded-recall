#!/usr/bin/env python3
"""Isolated, fake-data-only grounded-recall demo. No existing Hermes profiles.

Dry runs call the real plugin in a scrubbed subprocess, never a fixture renderer.
The hook adapter is deliberately explicit; see invoke_hook() and --hook.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import time
import unicodedata
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
import uuid

sys.dont_write_bytecode = True
DEMO = Path(__file__).resolve().parent
REPO = DEMO.parent
EVAL_ROOT = DEMO / ".eval-home"
VAULT = DEMO / "sample-vault"
ISOLATED_ENTRY = DEMO / "isolated_entry.py"

NO_ACCESS = re.compile(
    r"\b(?:i (?:do not|don't|don’t|cannot|can't|can’t) (?:have |currently have |directly )?"
    r"access|i have no access|i (?:do not|don't|don’t) know|"
    r"i (?:do not|don't|don’t) have (?:that|this|the requested) information|"
    r"(?:cannot|can't|can’t|unable to) access)\b", re.I,
)
HISTORICAL_CUE = re.compile(
    r"\b(?:historical|superseded|legacy|obsolete|previous(?:ly)?|formerly|former|"
    r"old|before|no longer|not current|not .*?anymore|used to|replaced|"
    r"was (?:the |used )|migrated (?:away )?from|rather than)\b", re.I,
)


def write_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def load_questions(path: Path) -> list[dict]:
    try:
        import yaml
    except ImportError as exc:
        raise RuntimeError("PyYAML is needed to read questions.yaml; use Hermes' own Python.") from exc
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    questions = data.get("questions", [])
    if data.get("schema_version") != 1 or len(questions) != 10:
        raise ValueError("Expected schema_version 1 and exactly 10 questions")
    ids = [q["id"] for q in questions]
    if len(set(ids)) != len(ids):
        raise ValueError("Question IDs must be unique")
    for q in questions:
        if not re.fullmatch(r"[a-z0-9-]+", q["id"]):
            raise ValueError("Unsafe question ID")
        if not q.get("expected_facts") or not q.get("expected_answer"):
            raise ValueError(f"Missing gold fields: {q['id']}")
        for name in q["proof_pages"]:
            proof = (VAULT / name).resolve()
            if proof.parent != VAULT.resolve() or not proof.is_file():
                raise ValueError(f"Invalid proof page: {name}")
    return questions


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", text).casefold()).strip()


def contains_literal(text: str, fact: str) -> bool:
    return re.search(r"(?<!\w)" + re.escape(normalize(fact)) + r"(?!\w)", normalize(text)) is not None


def score_answer(answer: str, question: dict) -> dict:
    """Transparent lexical indicators, NOT a semantic or factuality judge."""
    normalized = normalize(answer)
    groups = question["expected_facts"]
    matches = [any(contains_literal(normalized, str(alias)) for alias in group) for group in groups]
    stale_mentions, stale_assertions = [], []
    # A clause labeled old/historical is not scored as an affirmative stale assertion.
    # Sentence splitting intentionally preserves periods inside documentation IPs.
    clauses = re.split(r"(?<=[.!?;])\s+|\n+", answer)
    for stale in question.get("stale_facts", []):
        if not contains_literal(normalized, stale):
            continue
        stale_mentions.append(stale)
        if any(contains_literal(clause, stale) and not HISTORICAL_CUE.search(clause)
               and not re.search(r"\b(?:not|no|isn't|isn’t|is not)\b", clause, re.I)
               for clause in clauses):
            stale_assertions.append(stale)
    no_access_phrases = NO_ACCESS.findall(answer)
    return {
        "contains_expected_fact": all(matches),
        "matched_fact_groups": sum(matches),
        "expected_fact_groups": len(groups),
        "says_no_access": bool(no_access_phrases),
        "no_access_phrases": no_access_phrases,
        "mentions_stale_fact": bool(stale_mentions),
        "cites_stale_fact": bool(stale_assertions),
        "stale_mentions": stale_mentions,
        "stale_assertions": stale_assertions,
        "cites_proof_page": any(name.casefold() in answer.casefold()
                                for name in question["proof_pages"]),
    }


def new_run() -> Path:
    # Refuse symlink escapes instead of following a pre-existing .eval-home elsewhere.
    if EVAL_ROOT.is_symlink():
        raise ValueError("demo/.eval-home must not be a symlink")
    EVAL_ROOT.mkdir(mode=0o700, exist_ok=True)
    if EVAL_ROOT.resolve().parent != DEMO.resolve():
        raise ValueError("Evaluation home escaped demo/")
    run = EVAL_ROOT / (time.strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8])
    run.mkdir(mode=0o700)
    return run


def new_live_home() -> Path:
    # macOS AF_UNIX is limited to 104 bytes. Deep run/mode/question paths make
    # Hermes fall back to /tmp, which this demo deliberately does not permit.
    for _attempt in range(20):
        home = EVAL_ROOT / ("h" + uuid.uuid4().hex[:6])
        if os.name == "posix" and len(str(home / "gateway.sock").encode()) > 100:
            raise RuntimeError("Checkout path is too long for an in-demo Unix socket. "
                               "Use a shorter checkout path or --dry-run; /tmp is not used.")
        try:
            home.mkdir(mode=0o700)
            return home
        except FileExistsError:
            continue
    raise RuntimeError("Could not allocate a unique short evaluation home")


def clean_environment(home: Path, hermes_repo: Path, key_env: str | None = None) -> dict:
    """Allowlist, not an inherited environment with a few credentials removed."""
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    user_home = home / "user-home"
    work = home / "work"
    for directory in (user_home, work, home / "tmp", home / "cache", home / "config", home / "data"):
        directory.mkdir(mode=0o700, exist_ok=True)
    env = {
        "HOME": str(user_home), "USERPROFILE": str(user_home),
        "HERMES_HOME": str(home),
        "XDG_CONFIG_HOME": str(home / "config"), "XDG_CACHE_HOME": str(home / "cache"),
        "XDG_DATA_HOME": str(home / "data"), "TMPDIR": str(home / "tmp"),
        "PATH": os.pathsep.join([str(Path(sys.executable).parent), "/usr/bin", "/bin"]),
        "PYTHONPATH": os.pathsep.join([str(DEMO), str(hermes_repo), str(REPO)]),
        "PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONIOENCODING": "utf-8", "LANG": "en_US.UTF-8", "TERM": "dumb",
        "NO_COLOR": "1",
        "TERMINAL_CWD": str(work),
    }
    if key_env:
        if key_env in env or key_env.startswith("HERMES_"):
            raise ValueError("Credential variable must not overwrite isolation settings")
        value = os.environ.get(key_env, "")
        if not value:
            raise ValueError(f"The explicitly requested credential environment variable {key_env} is empty")
        env[key_env] = value
        # The explicit custom endpoint route reads OPENAI_API_KEY, never an existing .env.
        env["OPENAI_API_KEY"] = value
    return env


def plugin_config(home: Path) -> dict:
    # Settings verified against grounded_recall/config.py. Explicitly no external command
    # and no session source, so the fake dataset is the only recall input.
    return {
        "roots": [str(VAULT)],
        "use_ripgrep": False,
        "secret_titles_file": str(VAULT / "secret-titles.json"),
        "secret_titles_command": [],
        "sessions_enabled": False,
        "semantic_enabled": False,
        "max_hits": 16,
        "max_hits_per_file": 4,
        "excerpt_chars": 500,
        "max_context_chars": 8500,
        "current_max_age_days": 30,
        "time_budget_ms": 5000,
        "audit_log": True,
    }


def invoke_hook(message: str, config: dict, hook: str) -> str:
    """An explicit (message, config) -> injected text interface, overrideable with --hook."""
    module_name, separator, function_name = hook.partition(":")
    if not separator or not function_name:
        raise ValueError("--hook must be module:function")
    try:
        module = importlib.import_module(module_name)
        function = getattr(module, function_name)
    except (ImportError, AttributeError) as exc:
        raise RuntimeError(
            f"Real plugin hook {hook} is not available: {exc}. "
            "Expected a function (message, config) -> injected text; no evidence was fabricated. "
            "Wait for the plugin worker or pass its actual --hook module:function."
        ) from exc
    result = function(message, config)
    if isinstance(result, dict):
        result = result.get("context", "")
    if not isinstance(result, str) or not result.strip():
        raise RuntimeError("Plugin hook returned no injected text")
    return result


def stop_owned_process(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    # Only the new process group we started; never a discovered PID or service name.
    if os.name == "posix":
        os.killpg(process.pid, signal.SIGTERM)
    else:
        process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
        process.wait(timeout=5)


def run_process(command: list[str], env: dict, cwd: Path, timeout: float) -> dict:
    start = time.monotonic()
    process = subprocess.Popen(command, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, start_new_session=(os.name == "posix"))
    timed_out = False
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        stop_owned_process(process)
        stdout, stderr = process.communicate()
    finally:
        stop_owned_process(process)
    return {"returncode": process.returncode, "stdout": stdout, "stderr": stderr,
            "timed_out": timed_out, "elapsed_seconds": round(time.monotonic() - start, 3)}


def redact(text: str, secret: str) -> str:
    return text.replace(secret, "[REDACTED]") if secret else text


def local_endpoint(base_url: str) -> bool:
    hostname = urlsplit(base_url).hostname or ""
    if hostname == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def looks_like_chat_model(name: str) -> bool:
    return not re.search(r"embed|rerank|\bbge\b|arctic-embed", name, re.I)


def choose_model(base_url: str, requested: str) -> str:
    if requested != "auto":
        return requested
    if not local_endpoint(base_url):
        raise ValueError("Remote endpoints require an explicit --model; no assumed free model")
    # Ollama's local catalog gives sizes: choose the smallest installed model. Zero API fee.
    parts = urlsplit(base_url)
    tags_url = f"{parts.scheme}://{parts.netloc}/api/tags"
    try:
        with urlopen(Request(tags_url), timeout=4) as response:
            models = json.load(response).get("models", [])
        candidates = sorted((m for m in models if looks_like_chat_model(m["name"])),
                            key=lambda m: (m.get("size", float("inf")), m["name"]))
        for candidate in candidates:
            # This is metadata only, not inference or a model download/load.
            request = Request(f"{parts.scheme}://{parts.netloc}/api/show",
                              data=json.dumps({"model": candidate["name"]}).encode(),
                              headers={"Content-Type": "application/json"})
            try:
                with urlopen(request, timeout=4) as response:
                    capabilities = json.load(response).get("capabilities")
                if isinstance(capabilities, list) and "completion" not in capabilities:
                    continue
            except (URLError, OSError, ValueError):
                pass  # Older local catalogs lack capabilities; use the name filter.
            return candidate["name"]
    except (URLError, OSError, ValueError, KeyError):
        pass
    try:
        with urlopen(Request(base_url.rstrip("/") + "/models"), timeout=4) as response:
            models = json.load(response).get("data", [])
        names = sorted(m["id"] for m in models if isinstance(m.get("id"), str)
                       and looks_like_chat_model(m["id"]))
        if names:
            return names[0]
    except (URLError, OSError, ValueError, KeyError):
        pass
    raise RuntimeError("No unauthenticated local model catalog at the endpoint. Use --dry-run, "
                       "or supply --model/--base-url and --api-key-env explicitly.")


def configure_home(home: Path, enabled: bool, model: str, args) -> dict:
    plugin_dir = home / "plugins" / "grounded-recall"
    plugin_dir.mkdir(parents=True, exist_ok=True)
    for name in ("plugin.yaml", "__init__.py"):
        source = REPO / name
        if not source.is_file():
            raise RuntimeError("Plugin entrypoint not ready: " + str(source))
        shutil.copyfile(source, plugin_dir / name)
    shutil.copytree(REPO / "grounded_recall", plugin_dir / "grounded_recall",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    settings = plugin_config(home)
    config = {
        "model": {"provider": args.provider, "default": model, "base_url": args.base_url,
                  "api_mode": "chat_completions"},
        "agent": {"max_turns": 1, "temperature": 0, "tool_use_enforcement": False,
                  **({"reasoning_effort": args.reasoning_effort} if args.reasoning_effort else {})},
        "toolsets": [],
        "platform_toolsets": {"cli": []},
        "memory": {"memory_enabled": False, "user_profile_enabled": False},
        "checkpoints": {"enabled": False},
        "auxiliary": {"title_generation": {"enabled": False, "model_upgrade_enabled": False},
                      "background_review": {"enabled": False}},
        "plugins": {"auto_enable": False,
                    "enabled": ["grounded-recall"] if enabled else [],
                    "entries": {"grounded-recall": {"enabled": enabled,
                               "allow_gateway_injection": True, "settings": settings}}},
    }
    # JSON is a YAML subset. No secret values are written to this file.
    write_json(home / "config.yaml", config)
    return settings


def run_hook(question: dict, home: Path, args) -> dict:
    env = clean_environment(home, args.hermes_repo)
    payload = home / "hook-input.json"
    write_json(payload, {"message": question["question"], "config": plugin_config(home),
                         "hook": args.hook})
    result = run_process([args.hermes_python, str(ISOLATED_ENTRY), "hook", str(payload)],
                         env, home / "work", args.timeout)
    if result["returncode"] != 0:
        raise RuntimeError(result["stderr"].strip() or "Plugin hook subprocess failed")
    output = json.loads(result["stdout"])
    block = output["injected_text"]
    (home / "injected.txt").write_text(block + "\n", encoding="utf-8")
    return {"id": question["id"], "question": question["question"],
            "injected_text": block, "elapsed_seconds": result["elapsed_seconds"],
            "expected_label_present": question["expected_evidence_label"] in block,
            "proof_page_present": any(p in block for p in question["proof_pages"]),
            "retrieval_score": score_answer(block, question)}


# Identical in both modes: the demo agent has no tools, so ask for a direct answer.
ANSWER_SUFFIX = ("\n\nNo tools are available. Answer directly in plain text in 1-3 sentences. "
                 "If you do not know, say so.")


def run_live(question: dict, enabled: bool, home: Path, model: str, args) -> dict:
    env = clean_environment(home, args.hermes_repo, args.api_key_env)
    configure_home(home, enabled, model, args)
    if not local_endpoint(args.base_url) and not args.api_key_env:
        raise ValueError("Remote inference requires an explicit --api-key-env; no profile authentication")
    prefix = [args.hermes_python, str(ISOLATED_ENTRY), "cli", "--profile", "default"]
    cli = prefix + ["chat", "--oneshot", "-Q", "--ignore-rules", "--toolsets", "",
                    "--max-turns", "1", "--provider", args.provider,
                    "--model", model, "--base-url", args.base_url,
                    "-q", question["question"] + ANSWER_SUFFIX]
    secret = os.environ.get(args.api_key_env, "") if args.api_key_env else ""
    # This checkout's one-shot transport uses a gateway. Start our own foreground,
    # unsupervised child first to avoid auto-start via launchd/systemd. Never install,
    # restart, replace, stop, or attach to an existing profile's service.
    gateway_command = prefix + ["gateway", "run", "--no-supervise", "--quiet"]
    help_result = run_process(prefix + ["gateway", "run", "--help"], env,
                              home / "work", min(args.timeout, 30))
    if help_result["returncode"] or "--no-supervise" not in help_result["stdout"]:
        raise RuntimeError("Hermes must support gateway run --no-supervise; dry-run remains available")
    gateway_log = home / "foreground-gateway.log"
    with gateway_log.open("w", encoding="utf-8") as output:
        gateway = subprocess.Popen(gateway_command, env=env, cwd=home / "work",
                                   stdin=subprocess.DEVNULL, stdout=output, stderr=output,
                                   start_new_session=(os.name == "posix"))
        try:
            ready = run_process([args.hermes_python, str(ISOLATED_ENTRY), "probe"],
                                env, home / "work", 50)
            write_json(home / "readiness.json", ready)
            if ready["returncode"] != 0 or gateway.poll() is not None:
                raise RuntimeError("Isolated foreground gateway did not become ready; see " + str(gateway_log))
            result = run_process(cli, env, home / "work", args.timeout)
        finally:
            stop_owned_process(gateway)
            output.flush()
            # Redact even if readiness/CLI execution failed; do not expose a supplied
            # credential in a retained child log on an exception path.
            gateway_log.write_text(redact(gateway_log.read_text(encoding="utf-8", errors="replace"), secret), encoding="utf-8")
    for field in ("stdout", "stderr"):
        result[field] = redact(result[field], secret)

    audit = home / "state" / "grounded-recall" / "default.jsonl"
    audit_records = []
    if audit.is_file():
        for line in audit.read_text(encoding="utf-8").splitlines():
            try:
                audit_records.append(json.loads(line))
            except ValueError:
                continue
    audit_present = any(record.get("chars", 0) > 0 and record.get("sha")
                        and not record.get("timeout") for record in audit_records)
    answer = result["stdout"].strip()
    valid = (not result["timed_out"] and result["returncode"] == 0 and bool(answer)
             and audit_present == enabled)
    row = {"id": question["id"], "mode": "enabled" if enabled else "disabled",
           "question": question["question"], "answer": answer,
           "valid": valid, "audit_present": audit_present, "audit_records": audit_records,
           "command": cli, **result}
    row["score"] = score_answer(answer, question) if valid else None
    if not valid:
        if result["timed_out"]:
            row["error"] = f"CLI timed out after {args.timeout:g}s; no completed answer was scored"
        elif result["returncode"] != 0:
            row["error"] = f"CLI exited {result['returncode']}; not scored (see stdout/stderr)"
        else:
            row["error"] = "Empty answer or plugin audit does not match the selected arm; not scored"
    write_json(home / "result.json", row)
    return row


def summarize(rows: list[dict]) -> dict:
    summary = {}
    for mode in ("disabled", "enabled"):
        subset = [r for r in rows if r["mode"] == mode]
        valid = [r for r in subset if r["valid"]]
        summary[mode] = {"attempted": len(subset), "scored": len(valid),
                         "errors": len(subset) - len(valid)}
        for metric in ("contains_expected_fact", "says_no_access", "cites_stale_fact",
                       "mentions_stale_fact", "cites_proof_page"):
            summary[mode][metric] = sum(r["score"][metric] for r in valid)
    return summary


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Call plugin hook only; no model/CLI calls")
    parser.add_argument("--questions", type=Path, default=DEMO / "questions.yaml")
    parser.add_argument("--limit", type=int, default=10, help="First N questions, default all 10")
    parser.add_argument("--hook", default="hook_adapter:build_injection",
                        help="Explicit real plugin function, module:function")
    parser.add_argument("--hermes-repo", type=Path, default=Path.home() / ".hermes/hermes-agent",
                        help="Read-only Hermes code checkout (not a profile/config)")
    parser.add_argument("--hermes-python", help="Python with Hermes dependencies; defaults to its venv")
    parser.add_argument("--provider", default="custom")
    parser.add_argument("--reasoning-effort", default=None,
                        help="Optional agent.reasoning_effort for reasoning models (e.g. medium)")
    parser.add_argument("--model", default="auto", help="Default: smallest local Ollama model, zero API fee")
    parser.add_argument("--base-url", default="http://127.0.0.1:11434/v1")
    parser.add_argument("--api-key-env", help="Only this explicitly named environment credential is forwarded")
    parser.add_argument("--timeout", type=float, default=120, help="Per CLI/hook invocation seconds")
    parser.add_argument("--_hook-worker", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    args.hermes_repo = args.hermes_repo.expanduser().resolve()
    candidate = args.hermes_repo / "venv/bin/python"
    args.hermes_python = args.hermes_python or (str(candidate) if candidate.is_file() else sys.executable)
    if not 1 <= args.limit <= 10 or args.timeout <= 0:
        parser.error("--limit must be 1..10 and --timeout must be positive")
    url = urlsplit(args.base_url)
    if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password or url.query or url.fragment:
        parser.error("--base-url must be http(s), with no credentials, query, or fragment")
    if args.api_key_env and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", args.api_key_env):
        parser.error("--api-key-env must be an exact environment-variable name")
    return args


def source_hashes() -> dict:
    paths = list(VAULT.glob("*.md")) + [VAULT / "secret-titles.json", DEMO / "questions.yaml",
                                       DEMO / "run_eval.py", DEMO / "hook_adapter.py", ISOLATED_ENTRY]
    paths += [p for p in (REPO / "plugin.yaml", REPO / "__init__.py") if p.is_file()]
    if (REPO / "grounded_recall").is_dir():
        paths += list((REPO / "grounded_recall").glob("**/*.py"))
    return {str(p.relative_to(REPO)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(paths)}


def main(argv=None) -> int:
    args = parse_args(argv)
    if args._hook_worker:
        payload = json.loads(args._hook_worker.read_text(encoding="utf-8"))
        text = invoke_hook(payload["message"], payload["config"], payload["hook"])
        print(json.dumps({"injected_text": text}))
        return 0
    questions = load_questions(args.questions)[:args.limit]
    run = new_run()
    print("Artifacts: " + str(run), flush=True)
    write_json(run / "manifest.json", {
        "dry_run": args.dry_run, "question_ids": [q["id"] for q in questions],
        "hook": args.hook, "provider": args.provider, "requested_model": args.model,
        "base_url": args.base_url, "hermes_repo": str(args.hermes_repo),
        "source_sha256": source_hashes(), "python": sys.version,
        "isolation": "fresh HOME/HERMES_HOME/XDG dirs per question and mode; no inherited credentials",
    })
    if args.dry_run:
        rows, errors = [], []
        for q in questions:
            home = run / "dry-run" / q["id"]
            try:
                row = run_hook(q, home, args)
                rows.append(row)
                print(f"\n=== {q['id']}: {q['question']} ===\n{row['injected_text']}", flush=True)
            except (OSError, RuntimeError, ValueError) as exc:
                errors.append({"id": q["id"], "error": str(exc)})
                print(f"ERROR {q['id']}: {exc}", file=sys.stderr)
        report = {"kind": "retrieval-only; NOT model evaluation", "questions": rows, "errors": errors}
        write_json(run / "dry-run.json", report)
        print(f"\nDry-run: {len(rows)}/{len(questions)} real plugin injections, {len(errors)} errors")
        print("Report: " + str(run / "dry-run.json"))
        return 1 if errors else 0
    # Fail before inference or runtime startup if the real plugin cannot inject.
    run_hook(questions[0], run / "preflight", args)
    model = choose_model(args.base_url, args.model)
    print(f"Model: {model} | provider: {args.provider}", flush=True)
    rows = []
    homes = []
    for q in questions:
        for enabled in (False, True):
            mode = "enabled" if enabled else "disabled"
            home = new_live_home()
            homes.append({"id": q["id"], "mode": mode, "hermes_home": str(home)})
            write_json(run / "homes.json", homes)
            try:
                row = run_live(q, enabled, home, model, args)
            except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
                row = {"id": q["id"], "mode": mode, "valid": False, "score": None, "error": str(exc)}
            row["hermes_home"] = str(home)
            rows.append(row)
            write_json(run / "results.json", {"model": model, "rows": rows, "summary": summarize(rows)})
            print(f"{mode:8} {q['id']:20} " + (json.dumps(row["score"]) if row["valid"]
                                                   else "ERROR: " + row.get("error", "unknown")), flush=True)
    summary = summarize(rows)
    print(json.dumps(summary, indent=2))
    print("Report: " + str(run / "results.json"))
    return 0 if all(r["valid"] for r in rows) else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, ValueError) as exc:
        print("ERROR: " + str(exc), file=sys.stderr)
        raise SystemExit(1)
