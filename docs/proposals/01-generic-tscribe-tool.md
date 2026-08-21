# Proposal: A Generic `tscribe` Tool (Client + Server + Bootstrap)

**Status: proposal, not yet approved for implementation.** This document does not change anything about how `tscribe-class-carnyx` runs today — it stays exactly as-is, serving the nightly CompTIA class pipeline (carnyx SUBMIT/COLLECT runs). This is a spec for a NEW, separate, generic tool that this repo's API contract and transcription logic could eventually feed into.

## Why
Daniel wants to reuse the transcription pipeline (record → transcribe → durable transcript) outside the CompTIA context — starting with therapy-session audio (two channels: his mic + call audio) and generalizing to "any audio file, any agent." Today that means hand-building a bespoke integration each time, the way `tscribe-class-carnyx` was hand-built for the class pipeline.

## Confirmed current state (as of 2026-08-21, verified by direct repo read)
- `tscribe-class-carnyx` is a pure HTTP server (`uvicorn server.app:app`), no CLI, no config file (env vars only: `TSCRIBE_API_KEY`, `LITELLM_BASE_URL`, `LITELLM_API_KEY`, `TRANSCRIPTION_MODEL`, `CARNYX_DRIVE_SA_JSON`, worker-tuning vars).
- "Carnyx" is Daniel's GPU host machine (100.89.168.11), not a service schema — the actual backend protocol is LiteLLM (an OpenAI-compatible proxy running on that box).
- Backend selection is hardcoded: `transcription_tool.Transcriber(use_remote=True)` — always tries LiteLLM remote first, falls back to local faster-whisper on failure. No way to flip this via config; it's a code constant.
- No `bootstrap`-style self-initialization — deployment is a manual systemd unit + Cloudflare Tunnel setup (see `deploy/README.md`).
- No queue/batch support — one thread per job (`server/jobs.py`, in-memory `_JOBS` dict); `TSCRIBE_WORKERS` only parallelizes chunks *within* one job.
- The **API contract is solid and reusable as-is**: `POST /jobs` (accepts `drive_file_id` or `audio_url`), `GET /jobs/{id}` (returns `verified` bool as the explicit delivery gate, `transcript`, `report` with per-check results, `contract_fingerprint`). Verification never discards evidence on failure — transcript + report are retained for audit even when `verified: false`. This is exactly the "proven, not exited 0" shape and should be kept, not redesigned.
- Related repos already exist and are relevant: `tscribe-transcription-tool` (the local/remote transcription logic library `tscribe-class-carnyx` depends on) and `tscribe-capture-rig` (shell scripts for recording — worth Daniel checking whether this already overlaps with what he wants from lufs-recorder's voice-call automation before building that fresh).

## What Daniel is actually asking for (confirmed different from the above)
A generic tool — tentatively still called `tscribe` — that:
1. Can be cloned onto a server machine OR a client machine (same clone, different mode).
2. Reads a config file (YAML or TOML) that sets `mode: server | client` and `backend: local | remote` (remote = default), plus the remote endpoint.
3. In remote mode, speaks to a carnyx-style backend using the *existing* `tscribe-class-carnyx` API contract (reuse, don't reinvent — see above).
4. Has a `tscribe bootstrap` command that turns a fresh clone into a running server (mirrors the `lsbx` tool's bootstrap pattern) — i.e., self-provisioning, not a manual systemd writeup each time.
5. Eventually gets an interactive TUI (drag-and-drop / batch file queue, Transmission-style: queue → "start transcription" → per-file cancel, including mid-transcription, fails cleanly → clickable/keyboard-shortcuttable output link once done). TUI visual/interaction design is explicitly deferred to the cross-tool TUI style guide currently being scoped with Amacher — this proposal covers the CLI/config/bootstrap shape only.

## Recommendation
Do NOT rename or restructure `tscribe-class-carnyx` in place — it's a live production dependency of the nightly CompTIA pipeline (carnyx Run A/B, see the SRS-flashcards pipeline). Recommend a NEW repo (name TBD — `tscribe` is available conceptually but the decision to claim that name, and what happens to `tscribe-class-carnyx`'s name/identity afterward, is Daniel's call, not something to execute unilaterally) that:
- Wraps/depends on `tscribe-transcription-tool` for the actual transcription logic (already shared).
- Implements the config-driven mode switching + `bootstrap` command as new code.
- Exposes the same `/jobs` API contract shape in server mode, for continuity.
- `tscribe-class-carnyx` can later be migrated to run ON TOP of the new generic tool once it's proven, as a non-breaking follow-up — not a prerequisite.

## Open questions
- Final repo name and whether/when `tscribe-class-carnyx` gets renamed or deprecated in favor of it.
- Whether `tscribe-capture-rig`'s existing shell scripts should be absorbed into the new tool's recording-adjacent tooling, or stay separate (recording is lufs-recorder's job; tscribe's job is transcription).
- Exact YAML/TOML schema for the config file — not drafted here; belongs in the new repo's own SPEC.md once Daniel confirms this direction.

## Non-goals for this document
Not an implementation. Not a decision to create the new repo yet — that's Daniel's call given the naming and migration questions above.