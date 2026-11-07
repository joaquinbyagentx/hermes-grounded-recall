# Demo video script (target 85 s, hard limit < 90 s)

Setup: dark terminal at a large font, split screen with Hermes on the left and an editor
or `tail -f` of the audit log on the right. Use a **throwaway demo vault** (`~/demo-notes`)
with fake facts, a fake profile and a fake secret-manager vault holding dummy titles
only. No real names, hosts or keys may appear in any frame.

Demo vault contents:
- `infra/backups.md`, dated this week: "Nightly backup runs at 03:00 to the NAS."
- `vendors/support.md`, dated 5 months ago: "Support plan: Basic."
- `vendors/support-2026.md`, dated this week: "Support plan upgraded to Pro."
- Secret-manager dummy item titled `NAS admin`.

| Time | Screen | Voice-over / caption |
|---|---|---|
| 0:00–0:07 | Title card: "grounded-recall for Hermes Agent" | "Your agent says 'I don't have access' when the answer is in your notes. Let's fix that." |
| 0:07–0:20 | **Before:** plugin disabled. Ask: "When does the backup run, and do we have NAS credentials?" Agent: "I don't have access to that information." | "Without the plugin, a blank answer." |
| 0:20–0:28 | `hermes plugins enable grounded-recall`, then show the 4-line config pointing at `~/demo-notes`. | "One hook, your existing markdown, no new database." |
| 0:28–0:45 | **After:** same question. Agent: "03:00 to the NAS (backups.md, current). There's a secret-manager item titled 'NAS admin'. I can't see its value, so you'd open it there." | "It searched first. It found the note and the credential's title, never the secret." |
| 0:45–0:60 | Ask: "What support plan are we on?" Agent answers **Pro** and cites the current note. The 5-month-old "Basic" note shows as UNVERIFIED/HISTORICAL. | "Every hit carries a freshness label, so old facts can't pass as current." |
| 0:60–0:72 | Right pane: `tail -n 2` of the audit log showing the JSONL lines (sources, labels, char count, ms). | "Every injection is logged per profile. You can see exactly what the model saw." |
| 0:72–0:82 | Quick text card: "Titles only, never values · Evidence, not instructions · Capped and time-boxed · Local, no network" | "Safe by default: capped, time-boxed, read-only, local." |
| 0:82–0:87 | End card: repo URL, `hermes plugins install grounded-recall`, "@NousResearch Hermes plugin hackathon" | "grounded-recall. Search before you say you can't." |

Recording notes:
- Record the real run. Don't stage or splice in fake output. Cuts between shots are fine.
- Show both answers in full; don't fast-forward through the model reply.
- Export as H.264 yuv420p, 1080p30, AAC, `-movflags +faststart`. Verify the duration is under 90 s with `ffprobe`.
- Also export a 10–15 s GIF of the "After" answer for `docs/demo.gif`.
