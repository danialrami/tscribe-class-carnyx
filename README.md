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
| `POST /jobs` | `X-API-Key` | **primary** — JSON `{"audio_url": "..."}`; carnyx *pulls* the audio (no tunnel body limit) |
| `POST /jobs/upload` | `X-API-Key` | secondary — multipart file upload (only for files under the tunnel's ~100 MB limit) |
| `GET /jobs/{id}` | `X-API-Key` | poll status; on `done`, returns `transcript` + `report` |

### Why `audio_url` is the primary path

Cloudflare's proxied-body limit (100 MB Free/Pro, 200 MB Business, 500 MB Ent)
**applies to tunnels** and returns `413` on a too-large upload. A 4-hour class is
~230 MB. With `audio_url`, carnyx downloads the file itself (outbound, no edge
limit); only a small JSON request and the small transcript cross the tunnel. See
[deploy/README.md](deploy/README.md).

> Google Drive caveat: direct-download links for files **>100 MB** hit Drive's
> virus-scan interstitial and need the `confirm=` token (or the Drive API with an
> access token). Use an API-authenticated download for real 4-hour classes; the
> 44 MB orientation file downloads cleanly either way.

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

## Tests

```bash
TSCRIBE_API_KEY=test-key pytest -q   # mock transcriber; needs ffmpeg on PATH
```

Proves auth is enforced and that a job runs through the real `class_pipeline` and
surfaces an `ok` verification report (`4 passed`).

## Deploy

See [deploy/README.md](deploy/README.md) for the Cloudflare Tunnel setup, the
systemd unit, and the file-size guidance.
