# Deploying tscribe-class-carnyx

Three pieces on carnyx: the server, a Cloudflare Tunnel in front of it, and an
API key shared with the cloud agent.

## 1. Install the server

```bash
git clone https://github.com/danialrami/tscribe-class-carnyx.git
cd tscribe-class-carnyx
uv sync                      # pulls fastapi + the tscribe class_pipeline package
```

`uv sync` installs `transcription-tool` from GitHub, which brings faster-whisper
for the local fallback. carnyx needs `ffmpeg` + `ffprobe` on PATH (it already
does, for LiteLLM).

## 2. Secrets

```bash
sudo tee /etc/tscribe-class.env >/dev/null <<EOF
TSCRIBE_API_KEY=$(openssl rand -hex 24)
LITELLM_BASE_URL=http://100.89.168.11:6280/v1
LITELLM_API_KEY=sk-...
TRANSCRIPTION_MODEL=Systran/faster-distil-whisper-large-v3
EOF
sudo chmod 600 /etc/tscribe-class.env
```

Note the `TSCRIBE_API_KEY` — the cloud agent needs the same value. The server
**fails closed** (503) if it isn't set, so it never runs open to the internet.

## 2b. Google Drive service account (read/download/write-back)

So carnyx can download big audio by file id and write the transcript back:

1. In Google Cloud Console: a project → enable **Google Drive API** → create a
   **service account** → add a **JSON key** (download it).
2. In Google Drive, share the parent `class-recordings-notes` folder with the
   service-account email as **Editor** (this also lets carnyx move/group files).
3. Put the JSON in `./credentials/` (already gitignored). The server finds it via,
   in order: `CARNYX_DRIVE_SA_JSON` → `GOOGLE_APPLICATION_CREDENTIALS` → the first
   `*.json` in `credentials/`. To be explicit, add to `/etc/tscribe-class.env`:

   ```bash
   CARNYX_DRIVE_SA_JSON=/home/carnyx/repos/tscribe-class-carnyx/credentials/<key>.json
   ```

The key is a credential — keep it `chmod 600`, never commit it. Scope used is
`drive`, but the account can only touch what you've shared with it (that folder).

## 3. Run it

```bash
sudo cp deploy/tscribe-class.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now tscribe-class
curl -s http://127.0.0.1:6390/healthz   # {"ok": true, ...}
```

## 4. Cloudflare Tunnel

```bash
cloudflared tunnel login
cloudflared tunnel create tscribe-carnyx
cloudflared tunnel route dns tscribe-carnyx tscribe-api.<your-domain>
cp deploy/cloudflared-config.example.yml ~/.cloudflared/config.yml   # edit IDs
sudo cloudflared service install
```

Verify from anywhere:

```bash
curl -s https://tscribe-api.<your-domain>/healthz
```

## The file-size question (answered)

Cloudflare's **proxied request-body limit applies to tunnels**:

| Plan | Max upload body |
|------|-----------------|
| Free / Pro | 100 MB |
| Business | 200 MB |
| Enterprise | 500 MB (raisable) |

A too-large `POST /jobs/upload` returns **413**. The 44 MB orientation file is
fine; a ~230 MB 4-hour class is **not** on Free/Pro.

**The fix is architectural, not a plan upgrade:** use `POST /jobs` with
`drive_file_id` (preferred — any size, no public link, service-account download)
or `audio_url` (public-link fallback). carnyx downloads the audio itself
(outbound — the limit only applies to *inbound proxied bodies*), so only a tiny
JSON request and the small transcript cross the tunnel. The big file never
touches the edge limit.

Two more options if you ever want direct upload of a big file:
- **Chunk client-side** under 100 MB each (the pipeline's 15-min WAV chunks are
  ~28 MB) and post them — but `audio_url` is simpler.
- **Grey-cloud an unproxied subdomain** for uploads (bypasses the limit, but
  loses Cloudflare's WAF/DDoS and exposes the origin IP — not recommended).

### Google Drive download gotcha (real 4-hour files)

Drive's public `webContentLink` for files **>100 MB** returns an HTML virus-scan
interstitial instead of bytes, requiring a `confirm=` token. For real classes,
have the puller use the **Drive API with an access token** (direct media
download, no interstitial) rather than the naked link. The 44 MB orientation file
downloads cleanly with either.
