# tscribe-class-carnyx

A thin HTTP server that runs the **verified** tscribe class pipeline on carnyx
(the homelab GPU box) and exposes it to a cloud agent over a Cloudflare Tunnel.

It is deliberately small. All transcription, chunking, and **verification** logic
lives in `transcription_tool.class_pipeline` (the
[tscribe-transcription-tool](https://github.com/danialrami/tscribe-transcription-tool)
package — single source of truth). This repo only adds the job lifecycle and the
HTTP surface. No algorithm is reimplemented here.

## What it does

A cloud agent hands carnyx an audio reference; carnyx splits on silences,
transcribes the chunks (carnyx-first via LiteLLM, local fallback), reassembles a
timestamped transcript, and **proves it correct** against the pipeline's contract
before returning it. A failed contract returns `failed` with the reason — never a
transcript it couldn't prove.

## API

| Route | Auth | Purpose |
|-------|------|---------|
| `GET /healthz` | none | liveness |
| `POST /jobs` | `X-API-Key` | **primary** — JSON `{"drive_file_id": "..."}`; carnyx downloads via the service account and (optionally) writes the transcript back |
| `POST /jobs/upload` | `X-API-Key` | secondary — multipart file upload (only for files under the tunnel's ~100 MB limit) |
| `GET /jobs/{id}` | `X-API-Key` | poll status; on `done`, returns `transcript`, `report`, and (if written back) `transcript_file_id` |

### `POST /jobs` body

```json
{
  "drive_file_id": "<audio file id>",      // preferred (service-account download)
  "audio_url":     "<public link>",         // fallback for sub-100 MB public files
  "dest_folder_id":"<dated folder id>",     // optional: write the transcript back here
  "move_file_ids": ["<id>", "<id>"],        // optional: group these into dest_folder_id
  "source_name":   "20260629_class.m4a"     // optional: name for audio/transcript stem
}
```

### Why the service account (`drive_file_id`) is the primary path

Cloudflare's proxied-body limit (100 MB Free/Pro, 200 MB Business, 500 MB Ent)
**applies to tunnels** and returns `413` on a too-large upload — a 4-hour class is
~230 MB. And public download links for files **>100 MB** hit Drive's virus-scan
interstitial. The service account sidesteps both: carnyx downloads the file by id
via the Drive API (any size, no interstitial, outbound), then writes the verified
transcript back to `dest_folder_id` and groups the source assets with a
non-destructive **move**. Only a small JSON request and the small transcript cross
the tunnel. See [deploy/README.md](deploy/README.md).

**Data safety:** the transcript text is always returned in the job result even if
the Drive write-back fails (`upload_error` is reported, the transcript is never
lost). Grouping uses move (reparent), never copy-then-delete, and nothing is
hard-deleted.

## Run locally

```bash
uv sync   # or: pip install -e ".[dev]"
export TSCRIBE_API_KEY=$(openssl rand -hex 24)
export LITELLM_BASE_URL=http://100.89.168.11:6280/v1
export LITELLM_API_KEY=sk-...
export TRANSCRIPTION_MODEL=Systran/faster-distil-whisper-large-v3
uvicorn server.app:app --host 127.0.0.1 --port 6390
```

## Configuration (env)

| Var | Default | Purpose |
|-----|---------|---------|
| `TSCRIBE_API_KEY` | — (required) | shared secret for `X-API-Key`; server fails closed if unset |
| `LITELLM_BASE_URL` / `LITELLM_API_KEY` / `TRANSCRIPTION_MODEL` | tscribe defaults | carnyx transcription endpoint |
| `TSCRIBE_CHUNK_MINUTES` | 15 | chunk length |
| `TSCRIBE_WORKERS` | 2 | parallel transcription workers |
| `TSCRIBE_SNAP_WINDOW_S` | 10 | silence-snap window for chunk seams |
| `TSCRIBE_MAX_AUDIO_BYTES` | 2 GiB | upload/pull sanity guard |
| `TSCRIBE_DOWNLOAD_TIMEOUT_S` | 600 | timeout (seconds) for pulling audio via `audio_url` |
| `CARNYX_DRIVE_SA_JSON` | `./credentials/*.json` | path to the Drive service-account key (falls back to `GOOGLE_APPLICATION_CREDENTIALS`, then autodiscovery in `credentials/`) |

## Tests

```bash
TSCRIBE_API_KEY=test-key pytest -q   # mock transcriber; needs ffmpeg on PATH
```

Proves auth is enforced and that a job runs through the real `class_pipeline` and
surfaces an `ok` verification report (`4 passed`).

## Deploy

See [deploy/README.md](deploy/README.md) for the Cloudflare Tunnel setup, the
systemd unit, and the file-size guidance.
