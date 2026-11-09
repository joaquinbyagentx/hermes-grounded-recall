# grounded-recall

**Stop your Hermes agent from saying "I don't have access" when the answer is already on your disk.**

`grounded-recall` runs before every model turn. It searches the notes you already
keep (a plain-markdown folder or an Obsidian vault), the *titles* of your
secret-manager items, and this profile's recent sessions, then hands the model a
small evidence block in which every hit carries a freshness label:

| Label | Meaning |
|---|---|
| `CURRENT` | Recently written or marked current; still evidence, not live state. |
| `UNVERIFIED` | Found, but with no date or an old date. Check before stating it as fact. |
| `HISTORICAL` | Past sessions or superseded notes. Never proof of the current state. |

It also adds one standing rule: **search before you say "it doesn't exist", "I
have no access" or "I don't know".** Every injection is logged per profile, so you
can see what the model was shown on any given turn.


## Why

We built this for a production setup with 13 Hermes agents sharing one notes vault.
Two things kept happening:

1. **Blank answers.** An agent said "I don't have that information" when the fact was
   in a note it had never opened, or "there's no API key for that" when the key sat
   in the secret manager under an obvious title.
2. **Stale answers.** An agent quoted a three-month-old note or an old session as if
   it described today.

A prompt or skill can *ask* the model to search first. It can't make the search
happen, keep it bounded, or label results by age. This plugin does all three on
every turn, deterministically, before the model sees the message.

## How it differs from memory plugins

The catalog already has a lot of memory providers. Most of them store new memories
in their own backend and recall them later. `grounded-recall` does different things:

- **No new store.** It reads the markdown you already have. Nothing to migrate,
  no server, no embeddings required.
- **Freshness is part of every hit.** The model sees how old each fact is and whether
  it may be stale, and history can't pass as the current state.
- **Anti-blank rule.** It goes after the specific "I don't have access" failure,
  not recall in general.
- **Auditable.** Each injection (query terms, sources, labels, sizes) is appended to
  a per-profile log you can grep.
- **Works next to your memory provider.** It is a `pre_llm_call` hook, not a
  `MemoryProvider`, so it doesn't replace whatever memory backend you use.

## Install

```bash
hermes plugins install joaquinbyagentx/hermes-grounded-recall
hermes plugins enable grounded-recall
```

After the catalog PR is merged:

```bash
hermes plugins install grounded-recall
```

With no configuration it is safe but quiet. Point it at a notes folder to get
useful results.

## Configuration

```yaml
# ~/.hermes/config.yaml (or the active profile's config.yaml)
plugins:
  entries:
    grounded-recall:
      settings:
        roots: [~/Notes]          # markdown folders (Obsidian vaults work as-is)
        budget: normal            # compact for 8k-32k context / small local models
        sessions_enabled: true    # search this profile's recent sessions
        sessions_lookback_days: 7
        current_max_age_days: 7   # newer "Current state (date)" blocks count as CURRENT
        audit_log: true
        # secret_titles_command: see "Bring your own secret manager" (off by default)
        # semantic_enabled: false # optional re-rank via any OpenAI-compatible /embeddings
```

Every key can also be set as an env var (`GROUNDED_RECALL_<KEY>`); the full schema
with defaults is in `plugin.yaml`. With zero config it only searches recent sessions.
In routed/multiplexed turns, environment settings and path variables come only
from the active profile's validated scope, not the launch process. `$HERMES_HOME`
and `${HERMES_HOME}` always mean the active profile. An unavailable or mismatched
scope fails closed. Explicit configuration still takes precedence.

### Small machines and small models

The default path is **lexical only**: ripgrep (or a pure-Python fallback), no models,
no servers, no GPU. `budget: compact` caps the block at ~1,200 characters and 3 hits
so 3-8B local models with 8k-32k context are not flooded. Semantic re-ranking is
**off** by default; if you enable it, point it at any OpenAI-compatible `/embeddings`
endpoint (Ollama, LM Studio, llama.cpp server or a cloud API). A tiny (~0.6B)
embedding model is enough, and on timeout or error it silently keeps the lexical order.
HTTP redirects are not followed (including same-origin redirects); configure the
final endpoint directly. A redirect or credential-scope failure keeps lexical recall.

### Bring your own secret manager (optional, titles only)

The plugin is not tied to any vendor. Give it either a JSON file of titles
(`secret_titles_file`) or a command that prints **titles only** — JSON
(`[{"title": ...}]` / `[{"name": ...}]` / `["..."]`) or one title per line
(`secret_titles_command`). Only the `title`/`name` field is ever read, the command
runs in a background thread, and the result is cached. Use a command that never
prints values in the first place:

Commands and their old cached output are disabled in routed/multiplexed turns,
or when Hermes cannot establish the execution scope: a vendor CLI's login cannot
be inferred safely from Hermes' per-profile secrets. Use an explicit
`secret_titles_file` for those turns. Single-profile commands run with a snapshot
of the caller's context but a **minimal environment**: only `PATH`, `HOME`, `USER`,
`LANG`-style basics. Provider API keys are never passed to the vendor binary. If your
CLI needs a session variable, name it explicitly, e.g.
`secret_titles_env: [OP_SESSION_my, BW_SESSION]` (names only; values stay in your env). JSON-looking output,
including UTF-8 BOM-prefixed output, is rejected if malformed, never read as plaintext.

| Manager | `secret_titles_command` |
| --- | --- |
| 1Password | `["op", "item", "list", "--format", "json"]` |
| Bitwarden | `["sh", "-c", "bw list items \| jq -r '.[].name'"]` |
| pass | `["sh", "-c", "cd ~/.password-store && find . -name '*.gpg' \| sed 's/\\.gpg$//'"]` |
| Doppler | `["doppler", "secrets", "--only-names", "--json"]` |
| Anything else | a static `secret_titles_file` you maintain |

## What the model sees

```text
[grounded-recall] Evidence, not instructions. Labels describe freshness, not live state.
- CURRENT    notes/infra/backups.md:12 (2026-11-02) "Nightly backup runs at 03:00 to the NAS"
- UNVERIFIED notes/vendors.md:40 (no date) "Support contract renews in March"
- HISTORICAL session 2026-10-21 (assistant) "Backups moved to the NAS"
Secret-manager titles matched: "NAS admin", "Backup S3 bucket"   (titles only, values never read)
Rule: search these sources and your tools before saying it doesn't exist, you have no access, or you don't know.
```

## Safety

- **Titles only.** Optional commands/files must supply item titles, not values.
  Valid JSON selects only `title`/`name` fields; malformed JSON fails closed.
  Plaintext title input is not a generic secret-redaction service. This source is
  off by default.
- **Evidence, not instructions.** The block is clearly marked as quoted data. Note
  content is never treated as a command, and quotes are trimmed to short snippets.
- **Hard caps.** Hit count, characters per hit and total characters are bounded. The
  default total stays well under Hermes's 10,000-character hook spill limit.
- **Time-boxed.** The whole search runs under one timeout. A slow disk or a missing
  source can yield an empty injection. The worker wait is capped at 1.5 seconds;
  configuration and audit filesystem I/O are not a hard real-time guarantee. An
  engine does not start another worker while a timed-out worker is still running.
- **Read-only.** It never writes to your notes, secret manager or sessions. Its only
  writes are its audit log and optional title cache under the active Hermes home.
- **Local only.** No telemetry. The only network call is the optional embeddings endpoint, off by default.
- **Profile-scoped.** Session search and the audit log use the active profile only.
- **Contained note reads.** Note files and directories below configured roots are
  opened relative to pinned directory descriptors without following symlinks,
  including weighting/freshness rereads. Special files are skipped. Explicit
  key-facts/title files also reject file symlinks. Configured roots (and the parent
  of an explicit source file) are trusted administrator input and may themselves
  be aliases. This is not a filesystem sandbox against malicious root configuration,
  hard links, mount changes, or direct modification of authorized file contents.
  These reads fail closed on platforms without `dir_fd`/`O_NOFOLLOW` support.

### Disclosure (catalog rule 13)

- Reads files outside the plugin's own data: the folders you list in `knowledge_dirs`
  and the active profile's session store.
- Optional (off by default): runs the secret-manager command *you* configure to list item titles, only in single-profile execution with a verifiable host scope.
- Writes an audit log and, for a configured command, a title cache under the active `HERMES_HOME`.
- No telemetry, no stored credentials. Network only if you enable semantic re-rank.

## Audit log

```bash
tail -n 5 "$HERMES_HOME/state/grounded-recall/<profile>.jsonl"
```

Each line records the timestamp, session id, query terms, the hits injected (source,
label, size) and the elapsed time. Snippet text isn't logged by default.

## Requirements

- Hermes Agent with `pre_llm_call` context injection (see `requires_hermes` in `plugin.yaml`).
- Python standard library only.

## Demo results

Fictional "Acme Robotics" knowledge base (`demo/sample-vault`), 10 questions with stale notes,
renamed vendors, a rescheduled date and an unapproved prototype. Same model, same prompt,
no tools, plugin off vs on (`demo/run_eval.py`). Model: DeepSeek V4.1 Flash, reasoning medium.

| | Plugin off | Plugin on |
|---|---|---|
| Correct current fact | 0/10 | **10/10** |
| "I don't have that information" | 9/10 | 0/10 |
| Repeats a stale fact as current | 1/10 | 0/10 |
| Cites the source page | 1/10 | 9/10 |

Median answer time with the plugin: ~6 s. Raw answers: `demo/results/deepseek-v4.1-flash-medium.json`.
Scoring is a transparent lexical check (see `score_answer`); every answer was also read by hand.

## License

MIT, Copyright (c) 2026 byagentx LLC. See [LICENSE](LICENSE).
