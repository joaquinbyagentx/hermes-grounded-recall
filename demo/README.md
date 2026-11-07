# Acme Robotics: grounded-recall demo and evaluation kit

Entirely fake data. No company, IP, account, credential, or session from the developer's
machine is used. Documentation addresses are from RFC 5737; names and vendors are
invented. The snapshot is frozen at **2026-11-05**.

## Contents

- `sample-vault/`: 10 small markdown pages, with current, superseded/historical, and
  explicitly unverified material. Start at `sample-vault/README.md`.
- `sample-vault/secret-titles.json`: five fake item **titles only**. No values.
- `questions.yaml`: 10 questions, gold answers, proof filenames, expected fact aliases,
  and stale facts. Gold answers and scoring fields are never sent to a model.
- `run_eval.py`: real plugin-hook dry run, or paired Hermes one-shot model evaluation.
- `hook_adapter.py`: calls the actual `GroundedRecall.recall()` implementation; no replacement search.
- `isolated_entry.py`: child-process isolation shim around the real Hermes CLI.
- `test_run_eval.py`: unit tests for fixtures, scoring, timeouts, and isolation.
- `.eval-home/`: gitignored per-invocation artifacts, isolated homes, and logs.

## Requirements

Use the Python environment of an existing Hermes Agent checkout. The script needs
PyYAML to read the question file (already supplied by Hermes); the rest is standard
library. It does not install dependencies, plugins, services, or credentials.

Commands below are run from the repository root. If your shell's `python3` does not
have PyYAML, substitute your Hermes checkout's `venv/bin/python`.

## 1. Retrieval-only dry run: no model or authentication needed

```sh
python3 -B demo/run_eval.py --dry-run
# One question while developing:
python3 -B demo/run_eval.py --dry-run --limit 1
```

This calls the **real plugin function** for each question in a new subprocess with
an isolated `HOME`, `HERMES_HOME`, and working directory. It prints the returned
injected block verbatim. It also writes `injected.txt` per question and a combined
`dry-run.json`. It never substitutes hand-written or simulated recall results.

A dry run tests retrieval/injection, **not model accuracy**. The report includes
lexical retrieval indicators and whether the expected proof filename and label
appear. These are debugging aids, not proof that a model answered correctly or
that the label belongs to that exact hit.

### Actual plugin interface

The demo adapter exposes:

```python
hook_adapter.build_injection(message: str, config: dict) -> str
```

It instantiates `grounded_recall.core.GroundedRecall(get_config=config.get, environ={})`
and directly calls its `recall(session_id=..., user_message=message,
conversation_history=[])`. The plugin entrypoint registers that same method as
`pre_llm_call`. The adapter returns the real context unchanged; it neither searches
nor renders evidence itself. The standing anti-blank rule is separately registered
by the plugin through `GroundedRecall.section()` in an actual Hermes session.

A hook returning `{"context": "..."}` is also accepted. To use another compatible
function explicitly:

```sh
python3 -B demo/run_eval.py --dry-run --hook grounded_recall.some_module:some_function
```

`plugin_config()` in `run_eval.py` is the single integration point for the plugin's
settings, verified against the implementation's `grounded_recall/config.py`.
It uses `roots`, `secret_titles_file`, `secret_titles_command: []`,
`sessions_enabled: false`, `use_ripgrep: false`, `max_hits`,
`max_context_chars`, `current_max_age_days`, and `time_budget_ms`.
The fake JSON title file must be used instead of any secret-manager command.
Sessions are deliberately **off** in this controlled benchmark: no existing
profile's session history is read, and the historical question is proved by an
explicitly historical markdown page. This kit does not claim session-recall coverage.

If the plugin is not present yet, dry run exits nonzero with the exact missing-hook
error and writes that error to `dry-run.json`. Wait for the implementation or align
the adapter; do not replace it with a fake retrieval implementation.

## 2. Paired model evaluation

Default: a local, zero-API-fee OpenAI-compatible endpoint at
`http://127.0.0.1:11434/v1`, provider `custom`, model `auto`. For Ollama, `auto`
chooses the smallest already-installed **chat-capable** model using the local
`/api/tags` catalog, checking `/api/show` capabilities when available. Embedding and
reranking models are excluded; they cannot answer questions.
For another local endpoint it chooses the lexicographically first advertised
`/v1/models` ID. **It does not download or start a model server.** A model may still
consume local compute; select one explicitly if the server is shared.

```sh
python3 -B demo/run_eval.py
python3 -B demo/run_eval.py --limit 1 --model YOUR_INSTALLED_LOCAL_MODEL
python3 -B demo/run_eval.py \
  --hermes-repo /path/to/hermes-agent \
  --hermes-python /path/to/hermes-agent/venv/bin/python \
  --provider custom --model YOUR_LOCAL_MODEL \
  --base-url http://127.0.0.1:8000/v1
```

A local catalog/authentication failure is a real error, not an empty model response
or a scored wrong answer. Use `--dry-run` when no isolated model can authenticate.
No logged-in subscription, existing profile `.env`, OAuth tokens, or parent-process
provider credentials are imported automatically.

For an explicitly selected remote OpenAI-compatible provider, supply the model,
endpoint, and the **name** of a credential environment variable. Never put a key
literal in the command or question file:

```sh
# Set DEMO_MODEL_KEY through your own secure shell workflow first.
python3 -B demo/run_eval.py \
  --provider custom --model YOUR_PROVIDER_MODEL_ID \
  --base-url https://YOUR_PROVIDER.example/v1 \
  --api-key-env DEMO_MODEL_KEY
```

Only the explicitly selected variable is forwarded, also as `OPENAI_API_KEY` for
Hermes' custom-provider route. No key is placed in arguments, manifests, or config.
A real remote evaluation may make billable requests: all 10 questions × 2 modes,
plus any provider/runtime retries. The fake evidence and prompts go to that model.
There is no default paid provider or assumed free remote model.

### Actual CLI path and fairness

The local Hermes source was inspected for `chat --oneshot`, `-q`, `-Q`,
`--ignore-rules`, `--toolsets`, `--base-url`, and profile-local plugin discovery.
The harness executes the real CLI module through `isolated_entry.py`, equivalent to:

```sh
hermes --profile default chat --oneshot -Q --ignore-rules \
  --toolsets '' --max-turns 1 --provider custom \
  --model MODEL --base-url ENDPOINT -q 'QUESTION WITHOUT GOLD ANSWER'
```

The real plugin must pass a hook preflight before any model/runtime is started.
Each question is run **disabled then enabled**, with the same question, model,
prompt, temperature setting, and no tools. Each mode/question gets a fresh home:
there is no conversation carry-over, shared cache, or answer leakage. This is a
controlled evidence-injection ablation, **not** a benchmark of how well a tool-using
baseline can search the same vault. The baseline has no pre-injected vault context.
No particular score improvement is promised.

The studied checkout does **not** support `HERMES_PLUGIN_PATH`. The runner instead
copies only the local `plugin.yaml`, `__init__.py`, and `grounded_recall/` package
into the **throwaway** `$HERMES_HOME/plugins/grounded-recall/`, identically for both
arms. This is below `demo/.eval-home/`, never the real `~/.hermes/plugins`.
It writes only its own temporary `plugins.entries.grounded-recall.settings` config.
The disabled entry is explicitly false; the enabled entry is true. The current CLI
accepts `--toolsets ''` as an empty list; the name `none` is invalid. The config
also explicitly sets `platform_toolsets.cli: []`.

An enabled answer is not scored unless
`$HERMES_HOME/state/grounded-recall/default.jsonl` contains a real injection record
with a nonzero character count, context digest, and no timeout. A disabled answer
is rejected if such an injection record exists, preventing cross-arm contamination.

Recent Hermes one-shot uses a gateway transport. This runner requires support for
`gateway run --no-supervise`, starts **its own foreground child** for the fresh home,
waits for read-only readiness discovery, and terminates only the process group it
created. It does not install, restart, stop, replace, or connect to an existing
profile's service. Older checkouts without that foreground mode fail explicitly;
retrieval-only dry run still works.

## Isolation details

- All generated files are under gitignored `demo/.eval-home/`. Reports and dry-run
  homes are under a unique run directory. Live homes use short sibling names
  `hXXXXXX`; `homes.json` maps them to the run, question, and arm. This keeps macOS
  Unix sockets within its path limit **without writing a fallback socket to /tmp**.
- `HOME`, `USERPROFILE`, `HERMES_HOME`, XDG dirs, temp/cache dirs, and cwd are fresh.
- The environment is allowlisted; existing profile selection, provider credentials,
  secret-manager tokens, proxies, and gateway URLs are not inherited.
- The CLI shim disables dotenv hydration **in its own process**, because the studied
  Hermes checkout otherwise loads its repository `.env` even with another home.
- The shim reuses the explicitly selected, already-prepared interpreter. It disables
  automatic dependency activation/bootstrap and update-recovery/bytecode-sweep
  maintenance in that child only; it also refuses package-manager subprocesses and
  interpreter re-exec. This is not a Hermes installation or source/config edit.
- A Python audit-hook tripwire rejects file mutations outside that child home and
  reads of external `.env`, auth/credential JSON, or `config.yaml` files.
- `PYTHONDONTWRITEBYTECODE` avoids creating caches in another worker's plugin paths.
- There are no model tools, filesystem checkpoints, memory provider, title-generation
  model calls, or background-review model calls in the generated config.
- Python audit hooks are **not an OS sandbox for an untrusted plugin or native child
  process**. This evaluates the repository's trusted code, not arbitrary downloaded
  code. The real Hermes source/dependency environment remains read-only input.

## Scores and artifacts

Every paired result contains the final stdout answer, stderr, return code, duration,
exact non-secret command, audit-presence flag, and score. Incremental `results.json`
is written after every attempt so partial work survives an interrupted evaluation.
CLI failures, timeouts, missing audit logs, and blank answers are reported as
**unscored errors**, not improvements or ordinary wrong answers.

Scoring is intentionally transparent and lexical, not an LLM judge:

- `contains_expected_fact`: every expected alias group has a case-insensitive match.
- `says_no_access`: matches listed English denial/unknown phrases, even if a nuanced
  answer legitimately qualifies secret-value access. Review these flags manually.
- `mentions_stale_fact`: raw mention of an obsolete value.
- `cites_stale_fact`: a stale mention in a clause without a historical/negative cue.
  This is heuristic: it may miss an incorrectly asserted value with a cue elsewhere
  in the same clause, or flag a quotation. Inspect the saved answers.
- `cites_proof_page`: at least one expected proof filename is present.

The uncertainty question requires an unverified proposal, no approval, and no
approved launch date. The historical question asks for the former IP on purpose;
using history appropriately is not penalized. None of these checks establish live
infrastructure access or authorization to retrieve a credential value.

### Model-results table — fill only after a real paired run

| Mode | Scored / attempted | Expected facts | No-access phrase | Stale asserted | Proof filename | Model / report |
|---|---:|---:|---:|---:|---:|---|
| Disabled | NOT RUN | — | — | — | — | Record exact model and results.json |
| Enabled | NOT RUN | — | — | — | — | Record exact model and results.json |

Do not put dry-run retrieval scores or unit-test outcomes in this model table.

## Verify the kit itself

```sh
python3 -B -m unittest discover -s demo -p 'test_run_eval.py' -v
```

### Real verification checkpoint

- Unit tests: **23 passed** (`Ran 23 tests ... OK`).
- Actual hook dry run: **10/10 real plugin injections, 0 errors**, exit 0.
  Report: `.eval-home/20261107T152225-64038f0d/dry-run.json`.
- Earlier actual dry-run report `.eval-home/20261107T150735-28fe73d2/dry-run.json`
  had 10 injection audit files and literal expected facts in 8/10 blocks. Missing:
  `backup-policy` (35-day retention) and `historical-ip` (the old IP). Merely quoting
  a proof filename did not guarantee the needed line survived the hit cap. These
  are retrieval limitations to investigate, not fabricated successful answers.
- A real isolated CLI paired smoke was attempted with local `qwen3.8:latest`.
  Both arms timed out without a completed final answer; **0 answers scored**.
  Final smoke report: `.eval-home/20261107T152136-43c9ab10/results.json`.
  Another smoke with a 45-second budget and a 60-second `gemma4:e2b` attempt also
  failed to produce a final answer. This is not a successful model benchmark.
- The CLI path, isolated gateway startup, and actual plugin audit toggle have been
  exercised. All owned child processes were terminated; no existing service was
  restarted. The full 10-question model-results table remains deliberately blank.

During development, the first cold CLI attempt unexpectedly tried to prepare
dependencies **inside its throwaway home** and hit a `pilk`/`string.h` build error.
Those temporary cache/install trees were removed; diagnostic logs were retained.
The final shim now suppresses this bootstrap instead of installing dependencies.
The later socket-length and invalid `none` toolset errors were also corrected in
the harness, without modifying Hermes or the plugin source.

Remaining limits: validate a responsive model/endpoint before publishing a paired
accuracy claim; inspect the two retrieval misses; and remember that a 30-day
freshness window will age this frozen snapshot. Expected labels are dated-document
labels, never a timeless live-state guarantee. No live secret-manager or session-source
coverage is claimed by this fake-only benchmark.

To clean up, remove only a completed run directory printed by the script and its
short live homes listed in `homes.json` (after its children have exited). Do not
point a cleanup command at an existing Hermes profile. No cleanup is needed for
Git: `.eval-home/` is ignored by `demo/.gitignore`.

## Handoff boundary

This worker owns only `demo/`; no changes to root plugin code, shared README,
catalog, other workers' tests, real Hermes config, secret managers, or services.
Project-wide wiki/release updates belong to the coordinating worker. This page is
the local reproducibility checkpoint, not evidence of a finished model benchmark.
