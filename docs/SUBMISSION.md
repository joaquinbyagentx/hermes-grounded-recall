# Hackathon submission: grounded-recall

## Official rules (verified 2026-11-07)

Source: the Nous Research announcement on X,
https://x.com/NousResearch/status/2118094256438936062 (posted 2026-11-04). We read its
full text through mirrors (r.jina.ai, nitter) and it matched.

- **Event:** Hermes Agent Plugin Hackathon, 7 days, $20,000 in cash prizes. Any kind of plugin is eligible.
- **Prizes:** 1st $10,000 + neon sign + merch + $1,000 credits · 2nd $5,000 + neon + merch + $1,000 credits ·
  3rd $2,500 + neon + merch + $1,000 credits · 4th $1,500 + merch + $500 credits · 5th $1,000 + merch + $500 credits ·
  runners-up (x5) merch + $250 credits.
- **Deadline:** 5 PM EST, Nov 11 2026 = **16:00 America/Mexico_City** (22:00 UTC).
- **To enter:**
  1. Open a PR that adds the plugin to the Hermes plugin catalog:
     https://hermes-agent.nousresearch.com/docs/developer-guide/plugins/catalog-submission
  2. Post a demo video on X and tag **@NousResearch**. Keep it **under 90 seconds** and show the plugin working.
  3. Submit both links through the form: **https://form.typeform.com/to/FwJLvOln**
     (the page title reads "Hermes Agent Plugin Hackathon Submission Form").
- **Judging:** usefulness, reliability, polish and creativity. Judges favour plugins that do
  more than a prompt or a skill could.
- The form can be submitted as soon as the PR is open. A merge isn't required to enter.
  Catalog checks run once a maintainer starts them, and entries are reviewed every few days.
- After close, Nous opens a Discord thread for each entry (discord.gg/nousresearch).
- Accepted entries are listed in the Hermes Marketplace.

**Not verified:**
- The exact form fields. The Typeform is JS-rendered and only its welcome screen was
  readable. A third-party playbook lists title, short description, email, Discord,
  X post link and catalog PR link. Have all six ready.
- Any limit on open PRs per author.
- A "public GitHub repo" requirement is not in the announcement, but catalog admission
  requires a public https repo (GitHub or GitLab), so it applies anyway.

## Pre-flight checklist

- [ ] Plugin code, tests and `plugin.yaml` done (other worker). `plugin.yaml` declares `requires_hermes`.
- [ ] `README.md` placeholders filled: owner/repo, config keys match `config_schema`, audit-log path, secret-title mechanism.
- [ ] `docs/demo.gif` recorded and committed (or the image line removed).
- [ ] Every example is free of real names, hosts, paths and secrets. Use a throwaway demo vault.
- [ ] Tests pass: `python -m pytest tests -q`
- [ ] Validation passes on a clean checkout at the commit you will pin:
      `hermes plugins validate /path/to/hermes-grounded-recall --install-deps`
      Fix every failure and read every warning. Check `capabilities` match (rule 6), run the security scan (rule 7),
      check for core overrides (rule 9) and confirm credentials are handled under rule 11 (see risk below).
- [ ] **Rule 11 risk:** listing secret-manager titles through its CLI may count as touching "another tool's login".
      Keep it opt-in and off by default, disclose it, and call it out in the PR description so the reviewer
      decides. If the reviewer objects, drop the feature in the catalog build.
- [ ] Public repo created, code pushed, release tagged `v1.0.0`. Record the 40-hex SHA: `git rev-parse HEAD`.
- [ ] `catalog/grounded-recall.yaml`: set `repo`, `sha`, `maintainer` (GitHub handle of the repo owner, rule 5),
      `docs_url`, `requires_hermes` (same floor as `plugin.yaml`, not newer than the current release). `version` must stay quoted.
- [ ] Validate the entry:
      `python3 ~/.hermes/hermes-agent/scripts/validate_plugin_catalog.py catalog/grounded-recall.yaml`

## Catalog PR steps (owner approval needed; agents don't open PRs)

```bash
gh repo fork NousResearch/hermes-agent --clone --remote
cd hermes-agent
git checkout -b plugin-catalog/grounded-recall
cp ../hermes-grounded-recall/catalog/grounded-recall.yaml plugin-catalog/grounded-recall.yaml
# drop the leading TODO comment lines first
python3 scripts/validate_plugin_catalog.py plugin-catalog/grounded-recall.yaml
git add plugin-catalog/grounded-recall.yaml
git commit -m "plugin-catalog: add grounded-recall"
git push -u origin plugin-catalog/grounded-recall
gh pr create --repo NousResearch/hermes-agent \
  --title "plugin-catalog: add grounded-recall" --body "<paste PR body below>"
```

The PR adds **exactly one file**. The PR body must cover:
- What it does, in one paragraph (reuse the README intro).
- The Hermes surface it uses: one `pre_llm_call` hook that returns `{"context": ...}`. No tools, no middleware,
  no core patching, stdlib only.
- Disclosure (rule 13): reads the configured note folders and the active profile's session store. Opt-in
  secret-title listing via the CLI (titles only). Writes an audit log under `HERMES_HOME`. No network, no telemetry,
  no background process.
- Hackathon note: "Entry for the Hermes Agent Plugin Hackathon."

Then wait for a maintainer to start the catalog CI and answer review comments. Record the PR URL.

## Demo video and X post (owner posts)

- Script: `docs/demo-script.md`. Target 80–88 s, H.264 1080p30, AAC.
  Check the length with `ffprobe -v error -show_entries format=duration -of csv=p=0 demo.mp4` (must be < 90).
- Post text (≤ 280 chars; each link counts as 23):
  > Agents that say "I don't have access" when the answer is in your notes? grounded-recall searches your
  > markdown, sessions and secret *titles* before every turn and labels each fact CURRENT / UNVERIFIED / HISTORICAL.
  > Built for the @NousResearch Hermes plugin hackathon. <repo link>
- Record the post URL.

## Form (owner submits once)

1. Open https://form.typeform.com/to/FwJLvOln
2. Fill every field: project title `grounded-recall`, short description (the README's first two sentences),
   email, Discord handle, X post URL, catalog PR URL.
3. Re-read every field (some forms drop the first typed character), then submit **before Wed 11 Nov 16:00 Mexico City**.
4. Keep a screenshot of the confirmation.

## Record

| Item | Value |
|---|---|
| Repo URL | TODO |
| Pinned SHA / version | TODO / 1.0.0 |
| Catalog PR URL | TODO |
| X post URL | TODO |
| Form submitted at | TODO |
