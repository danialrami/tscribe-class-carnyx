# Next steps — standing this back up after 2026-08-07

Everything shipped on **2026-08-07**, what is and is not currently live, and an
end-to-end runbook a human or an agent can follow to redeploy.

Two machines, two independent processes:

| | machine | what runs there | needed for |
|---|---|---|---|
| **Process 1** | **carnyx** | `tscribe-class` (this server + the pipeline) | the nightly class transcription |
| **Process 2** | **tambora** | `webhookd` (CI placement) | CI actually running |

**They are independent.** Process 1 restores the class pipeline and takes ~10
minutes. Process 2 turns CI on and is a bigger lift with real prerequisites. You
can do 1 without ever doing 2. Do **not** do 2 first — it has a decision in it
that changes how 1 is run.

---

## 0. What changed on 2026-08-07

One day, three false accusations from one check, and the two real defects hiding
behind it.

### `danialrami/tscribe-transcription-tool`

| Commit | PR | What |
|---|---|---|
| `3a33bca` | #5 | Speaking-rate floor **abstains when its denominator saturates**; `ContractViolation` carries `.report` and `.transcript` |
| `9d1e4924` | #6 | **`assert_not_degenerate`** — measures transcriber looping directly; yield relation recalibrated 0.30 → 0.10; test filler de-degenerated |
| `bd57ed4e` | #7 | `ci/verify.yml` — first CI, with a mutation step |

**The saturation guard (#5).** `_chunk_levels()` derives speech as
`duration − silence` from a −30 dBFS **level gate, not voice activity**. A room of
twenty people talking at once never dips below that gate, so `silencedetect` finds
zero silence, `speech_s == duration_s`, and "words per minute of speech" becomes
numerically identical to words per minute of wall clock — reproducing the
2026-08-05 false accusation through a different door. The floor now abstains above
a speech/wall-clock ratio of **0.98** and records an advisory. Calibrated on real
data: the 2026-08-05 class measured **0.12–0.72**; the crosstalk chunk measured
**0.999**.

**Evidence retention (#5).** A failed contract used to discard the report *and* the
transcript. A 29-minute GPU run over a 2.5-hour class survived as one sentence,
produced by a check nobody could inspect. Now the exception carries both.

**The degeneracy check (#6).** Whisper handed audio it cannot resolve does not go
quiet — it **loops**. The 2026-08-06 class ended `chunk[7]` with *"It's a big
part."* forty-one times in a row. The contract had no vocabulary for that: its
ceiling catches too *many* words, but a loop is slow, so 879 words across fifteen
minutes cleared every rate bound. The defect kept landing on the speaking-rate
checks, which called it *"content likely dropped"* — close enough to act on, wrong
enough to mislead. Nothing was dropped; it was replaced with fabricated repetition.
Now measured directly: healthy chunks 0.0–1.3%, the two defects 8.5% and 18.7%, so
the threshold sits at **5%**, in the gap. Fatal only when the speech measure
underneath was *clean* — a loop over unintelligible crosstalk destroys nothing.

### `danialrami/tscribe-class-carnyx` (this repo)

| Commit | PR | What |
|---|---|---|
| `b90a72a` | #6 | A contract failure **flags rather than empties**; explicit `Job.verified` |
| `02beec66` | #7 | **`uv.lock` committed** (un-ignored) |
| `bb9758a1` | #8 | Test filler de-degenerated |
| `182559e1` | #9 | **`GET /version`** + `ci/verify.yml` |

**The lockfile (#7).** `pyproject.toml` declares the pipeline as an *unpinned* git
dependency, and nothing was locked in git. Two bad properties at once: nobody could
answer "what code is on carnyx?" from the repo, and a fresh deploy took whatever
had last landed on the tool's default branch, unreviewed. For the *verifier*, that
is the wrong semantics. Upgrades are now a reviewable one-line diff.

**`GET /version` (#9).** See §3.5 — this is the fix for a check that was wrong.

### `danialrami/comptia-study`

`44865d13` (#14) — the **2026-08-06** class record. Career Week Day 4, zero
objectives, zero cards. Filed on an explicit override and says so; the machine gate
did not clear it.

### What is live right now

| | live on carnyx | on `main` |
|---|---|---|
| pipeline | **`9d1e4924`** ✅ | `bd57ed4e` (adds only `ci/`, no runtime change) |
| server | `bb9758a1` | **`182559e1`** ⬅ not deployed |

**The pipeline is current.** The nightly run is protected. The only thing not
deployed is the server change, which adds `/version` — operational convenience, no
effect on transcription. Nothing is broken by leaving it.

---

## 1. Prep — what to show up with

### 1.1 Decide first: are the repos moving to `lufs-audio`?

This is the fork in the road, and it determines whether Process 2 happens at all.

The self-hosted runners are registered to the **`lufs-audio` org** and are
**private-repos-only**. Both tscribe repos are under `danialrami/`. So
`runs-on: [self-hosted, lufs]` — which the placement contract *requires* — has
nothing to schedule onto until the repos are transferred.

- **Not transferring?** Do Process 1 only. `ci/verify.yml` sits in both repos,
  reviewed and gate-passing, costing nothing until you want it. **Skip the PAT.**
- **Transferring?** Do both. Read §1.2 before you start, and note §2.2 — transfer
  changes how carnyx is redeployed.

I deliberately did **not** author these against `ubuntu-latest` to make them run
today. That violates the contract, and after the "90% of CI minutes" scare it is
not a decision to make quietly on your behalf.

### 1.2 The PAT — what it is for, and exactly how much it can do

**Only needed for Process 2, and only for this repo's CI.** If you are doing
Process 1 only, skip this entirely.

#### Why a credential is needed at all

`tscribe-class-carnyx` depends on `transcription-tool`, which lives in a **private**
repo. On carnyx that works because carnyx has an SSH key GitHub knows about, and
the dependency URL is `ssh://git@github.com/...`.

CI has no such identity. Fleet jobs run in **ephemeral containers** — one job per
`docker run --rm`, no persistent keys, nothing carried between runs. To install the
pipeline, the job needs a credential handed to it.

#### What I considered, and why this one

| Option | Verdict |
|---|---|
| **SSH deploy key** as a secret | Works, but needs `ssh-agent` setup in-job, and a key is harder to scope-audit at a glance than a permissions checkbox |
| **GitHub App installation token** | The right end state, and already on the roadmap. Doesn't exist yet; building it to unblock CI would be a much larger detour |
| **Fine-grained PAT, read-only, one repo** ✅ | Narrowest thing that works today, auditable in one screen, revocable in one click |
| Make the tool repo public | No |
| Vendor the pipeline into this repo | No — it would break the single-source-of-truth that the whole architecture rests on |

#### Exactly how to scope it

GitHub → Settings → Developer settings → **Personal access tokens** →
**Fine-grained tokens** → Generate new token.

| Field | Value | Why |
|---|---|---|
| **Token name** | `tscribe-ci-pipeline-read` | Says what it is when you find it in a year |
| **Resource owner** | the account that **owns the tool repo** | ⚠️ If you transfer the tool repo to `lufs-audio`, the owner must be **`lufs-audio`**, not your personal account. A personal-owned token cannot reach org repos |
| **Expiration** | 90 days | Not "no expiration." Put the rotation in your calendar now |
| **Repository access** | **Only select repositories** → **`tscribe-transcription-tool`** and nothing else | The single most important setting on this page |
| **Repository permissions** | **Contents: Read-only.** Nothing else. | This is the whole grant |
| Metadata: Read-only | auto-granted, mandatory | GitHub forces it on every fine-grained token; it cannot be removed |

Leave **every** other permission on "No access" — no Actions, no Workflows, no
Secrets, no Administration, no Pull requests, and no account permissions at all.

#### Blast radius, stated plainly

Since granting permissions is not a small thing, here is precisely what this token
is and is not:

**It can:** read the file contents and git history of one private repository —
`tscribe-transcription-tool`.

**It cannot:**
- write, push, or delete anything, anywhere
- read any other repository, including this one
- read or write repository secrets
- trigger, cancel, or read workflow runs
- change settings, collaborators, or branch protection
- act on your account, your org, or your billing
- do anything at all after its expiry date

**If it leaks:** someone can read the source of the transcription tool. That is the
entire consequence. There is no write path and no lateral movement. Compare that to
a classic PAT, which is account-wide by default — that difference is the reason for
using a fine-grained token here.

#### Where it goes, and how the workflow protects it

Store it as a **repository secret** on `tscribe-class-carnyx` (Settings → Secrets
and variables → Actions → New repository secret):

- **Name:** `TSCRIBE_TOOL_TOKEN` (the workflow reads exactly this)
- **Value:** the token

Two layers of protection in the workflow:
1. Actions masks registered secrets in logs automatically.
2. `ci/verify.yml` injects it via `git config --global url."…".insteadOf` rather
   than putting it in the install URL, so it never reaches a `pip` command line or
   `pip`'s output — belt and braces, because masking is a filter and filters can be
   defeated by string-splitting.

The workflow **fails closed** with an explicit message if the secret is unset. It
will not silently degrade.

#### Rotating and revoking

- **Revoke:** delete the token on the same settings page. Effective immediately.
- **What breaks:** CI for `tscribe-class-carnyx`, and nothing else. Not carnyx, not
  the nightly run, not the pipeline — carnyx uses its own SSH key and never sees
  this token.
- **Rotate:** generate a new one, update the repo secret, delete the old. No code
  change.
- **Retiring it:** when the GitHub App (Model B, on the roadmap) lands, CI uses a
  short-lived installation token and this PAT is deleted for good.

### 1.3 Access checklist

Have these in hand before starting:

- [ ] SSH to **carnyx** as `carnyx`
- [ ] **`sudo` password on carnyx** — `systemctl restart` needs it and there is no
      passwordless sudo. An agent cannot do this step; a human must be present for it
- [ ] SSH to **tambora** as `hermes` *(Process 2 only)*
- [ ] GitHub admin on both repos *(Process 2 only — transfer + secrets + webhooks)*
- [ ] The PAT from §1.2 *(Process 2 only)*

### 1.4 Timing

**Do this when no transcription is running.** The nightly schedule is Run A at
**01:30 CT** and Run B at **03:00 CT**; a class job takes ~30 minutes. Any window
outside roughly 01:00–04:00 CT is safe. A restart mid-job loses that job — the job
store is in memory.

---

## 2. Process 1 — carnyx: redeploy `tscribe-class`

**~10 minutes.** Brings `/version` live and picks up any pipeline changes.

### 2.1 Confirm nothing is in flight

```bash
ssh carnyx
systemctl is-active tscribe-class          # expect: active
curl -s localhost:6390/healthz             # expect: {"ok":true,...}
journalctl -u tscribe-class --since "1 hour ago" | tail -20
```

If a class job is running, wait. There is no drain — a restart drops it.

### 2.2 ⚠️ Only if you transferred the repos in §1.1

Transfer breaks the local remote and leaves the dependency URL pointing at the old
path. GitHub redirects, so it keeps *working*, which is exactly what makes it easy
to leave wrong for months:

```bash
cd /home/carnyx/repos/tscribe-class-carnyx
git remote set-url origin git@github.com:lufs-audio/tscribe-class-carnyx.git
sed -i 's|danialrami/tscribe-transcription-tool|lufs-audio/tscribe-transcription-tool|' \
  pyproject.toml uv.lock
```

Commit that change on a branch and PR it — it is a real repo change, not local
config. If you did **not** transfer, skip this entirely.

### 2.3 Deploy

```bash
cd /home/carnyx/repos/tscribe-class-carnyx

git pull --ff-only

# THE LOAD-BEARING LINE. A bare `uv sync` re-resolves the git dependency to the
# commit already pinned in uv.lock and exits 0 having changed nothing. It looks
# done. This is deploy/README.md §3b, and it is how carnyx sat on the broken
# verifier while main had moved on.
uv lock --upgrade-package transcription-tool

uv sync

sudo systemctl restart tscribe-class       # human required: sudo needs a password

git add uv.lock
git commit -m "chore: pin transcription-tool to <new-sha>"
git push
```

Push the lockfile bump on a branch and open a PR — that diff *is* the audit trail
of what production runs. (Hand the branch to Ciani to merge, or merge it yourself.)

### 2.4 Verify — the part that used to be wrong

**Do not** use the old `uv run python -c ...` check. It reads the **venv**, not the
running process, and on 2026-08-07 it printed the right answers *before* the
service had been restarted. It would have certified a stale server: the exact "it
looks done" failure §3b warns about, reproduced by the tool meant to detect it.

Use the endpoint. It is served **by** the process, so a stale server cannot report
fresh code:

```bash
KEY=$(grep -E '^TSCRIBE_API_KEY=' /etc/tscribe-class.env | cut -d= -f2-)
curl -s -H "X-API-Key: $KEY" localhost:6390/version | python3 -m json.tool
```

Expect:

```json
{
  "service": "tscribe-class-carnyx",
  "server_version": "0.2.0",
  "pipeline": {
    "version": "0.1.0",
    "commit": "9d1e49247574016064805970edd03e7b8feee5f9",
    "contract_fingerprint": "bffec2f0d893",
    "contract_fields": 17,
    "contract_checks": 8
  }
}
```

Check **`pipeline.commit` equals the tool `main` sha you expected.** If it doesn't,
the upgrade didn't take — go back to `uv lock --upgrade-package`. Do not
restart-and-hope.

`contract_fingerprint` hashes the contract's field and check names, so it moves
when the contract's *shape* changes and not when a docstring does. If the commit is
right but the fingerprint is unexpected, someone hand-edited the box.

Second, independent proof the **process** restarted (the job store is in memory, so
a surviving job id means a surviving process):

```bash
curl -s -o /dev/null -w '%{http_code}\n' \
  -H "X-API-Key: $KEY" localhost:6390/jobs/<any-job-id-from-before-the-restart>
# 404 = restarted and dropped its memory.  200 = same process, restart did NOT take.
```

### 2.5 Rollback

```bash
cd /home/carnyx/repos/tscribe-class-carnyx
git checkout <previous-sha> -- uv.lock
uv sync
sudo systemctl restart tscribe-class
```

Then re-run §2.4 and confirm `pipeline.commit` is the old one.

---

## 3. Process 2 — tambora: wire CI placement

**Prerequisite: §1.1 done (repos in `lufs-audio`) and the §1.2 PAT created.**
Without the transfer there is nothing for `runs-on: [self-hosted, lufs]` to
schedule onto, and placing the workflow just produces red runs.

### Why this machine, and why a daemon at all

The cloud GitHub App has **no `workflow` scope** — it can commit to `ci/` but never
to `.github/workflows`. That is why the workflows live in `ci/`. `webhookd` on
tambora holds an SSH identity that *can* write workflow files, so it does the
privileged copy: on a push it runs the **same** house-rules contract the cloud
pre-ran, and only on pass copies `ci/` → `.github/workflows` and pushes over SSH.

No agent decides anything. The contract does. It is **fail-closed** — a violation
places nothing and leaves the repo byte-unchanged.

### 3.1 Confirm webhookd is healthy

```bash
ssh tambora            # user: hermes
systemctl --user is-active webhookd
curl -s localhost:${WEBHOOK_PORT:-8646}/health
# -> {"status":"ok","routes":["knowledge-site","place-workflows-self",...]}
which actionlint && actionlint -version     # required: routes set require_actionlint
```

If `actionlint` is missing, install it (webhookd already has `~/.local/bin` on its
handler PATH):

```bash
mkdir -p ~/.local/bin && cd /tmp
bash <(curl -fsSL https://raw.githubusercontent.com/rhysd/actionlint/main/scripts/download-actionlint.bash)
mv /tmp/actionlint ~/.local/bin/actionlint && chmod +x ~/.local/bin/actionlint
```

### 3.2 Clone both target repos on tambora

`place-workflows` pushes **from a local clone**, so each target repo needs one, with
an **SSH** remote (the SSH identity is what has workflow-write):

```bash
cd ~
git clone git@github.com:lufs-audio/tscribe-transcription-tool.git
git clone git@github.com:lufs-audio/tscribe-class-carnyx.git
# prove the identity can push workflow files before trusting the route:
git -C ~/tscribe-class-carnyx push --dry-run origin main
```

### 3.3 Add a route per repo

Two files, modelled exactly on `routes/place-workflows-self/route.yaml`:

```bash
mkdir -p ~/webhook-deploy/routes/place-workflows-tscribe-tool
cat > ~/webhook-deploy/routes/place-workflows-tscribe-tool/route.yaml <<'EOF'
name: place-workflows-tscribe-tool
handler: place-workflows
repo: lufs-audio/tscribe-transcription-tool
event: push
branch: refs/heads/main
secret_env: WEBHOOKD_TSCRIBE_TOOL_SECRET
params:
  repo_path: /home/hermes/tscribe-transcription-tool
  ci_dir: ci
  workflows_dir: .github/workflows
  branch: main
  git_remote: origin
  push: "true"
  require_actionlint: "true"
EOF

mkdir -p ~/webhook-deploy/routes/place-workflows-tscribe-carnyx
cat > ~/webhook-deploy/routes/place-workflows-tscribe-carnyx/route.yaml <<'EOF'
name: place-workflows-tscribe-carnyx
handler: place-workflows
repo: lufs-audio/tscribe-class-carnyx
event: push
branch: refs/heads/main
secret_env: WEBHOOKD_TSCRIBE_CARNYX_SECRET
params:
  repo_path: /home/hermes/tscribe-class-carnyx
  ci_dir: ci
  workflows_dir: .github/workflows
  branch: main
  git_remote: origin
  push: "true"
  require_actionlint: "true"
EOF
```

Note `push: "true"` and `require_actionlint: "true"` are **quoted** — the daemon
compares them as the string `"true"`.

### 3.4 Generate the route secrets and restart

```bash
umask 077
for v in WEBHOOKD_TSCRIBE_TOOL_SECRET WEBHOOKD_TSCRIBE_CARNYX_SECRET; do
  printf '%s=%s\n' "$v" "$(python3 -c 'import secrets;print(secrets.token_hex(32))')" \
    >> ~/.config/webhookd/webhookd.env
done
systemctl --user restart webhookd
curl -s localhost:${WEBHOOK_PORT:-8646}/health     # both new routes must be listed
```

Keep both secret values for §3.5.

### 3.5 Add the GitHub webhooks

On **each** repo → Settings → Webhooks → Add webhook:

| Field | Value |
|---|---|
| Payload URL | your public webhookd endpoint + `/webhook/place-workflows-tscribe-tool` (and `…-tscribe-carnyx`) |
| Content type | `application/json` |
| Secret | the matching secret from §3.4 |
| Events | **just `push`** |

If the Cloudflare Tunnel ingress is path-specific, broaden it to `/webhook/*` and
reload `cloudflared`, or the new paths won't reach the daemon.

### 3.6 Add the PAT secret

On **`tscribe-class-carnyx`** → Settings → Secrets and variables → Actions → New
repository secret: `TSCRIBE_TOOL_TOKEN` = the §1.2 token.

The tool repo needs **no** secret — its CI installs nothing private.

### 3.7 Fire the first placement

Test locally before trusting the tunnel:

```bash
PORT=${WEBHOOK_PORT:-8646}
SECRET=$(grep -E '^WEBHOOKD_TSCRIBE_TOOL_SECRET=' ~/.config/webhookd/webhookd.env | cut -d= -f2-)
AFTER=$(git -C ~/tscribe-transcription-tool rev-parse origin/main)
BODY='{"ref":"refs/heads/main","after":"'"$AFTER"'"}'
SIG="sha256=$(printf '%s' "$BODY" | openssl dgst -sha256 -hmac "$SECRET" | awk '{print $NF}')"
curl -sS -XPOST localhost:$PORT/webhook/place-workflows-tscribe-tool \
  -H "X-GitHub-Event: push" -H "X-Hub-Signature-256: $SIG" \
  -H "Content-Type: application/json" -d "$BODY"
# -> {"status":"accepted",...}

sleep 15
curl -s localhost:$PORT/webhook/log | python3 -m json.tool | tail -30
# last entry: status "ok", steps run/verify/commit, marker == $AFTER
```

Repeat for the carnyx route.

**If `verify` fails, nothing was placed and the repo is untouched.** Read
`steps[].tail` and `journalctl --user -u webhookd`. **Do not hand-place the file.**
Fix the cause, or — if the contract rejects something genuinely legitimate — tell
Ciani so it goes in the route's `allow_hosted_runs_on` / `allow_unpinned_uses`
escape hatch. Never loosen the contract itself.

### 3.8 Confirm the loop

1. `.github/workflows/verify.yml` now exists on each repo's `main`, authored by
   `webhookd`.
2. That push triggered **Verify** on the self-hosted fleet. Confirm green.
3. The push also re-fired the placement route — confirm the newest log entry is an
   idempotent **no-op** (`commit: no change`). That is the self-trigger loop
   terminating correctly, not an error.

### 3.9 What green actually proves

The tool's workflow ends with a **mutation step**. A passing suite proves the code
does what the tests say; it does not prove the tests would notice if the code
stopped. So CI disables `max_loop_fraction` and requires `pytest` to **fail** —
erroring with `VACUOUS` if it doesn't, and also failing if the mutation didn't
apply, so a rename can't silently turn the check into a no-op.

The carnyx workflow resolves the pipeline commit **from `uv.lock`**, installs
exactly that sha, then re-reads `direct_url.json` and fails if they disagree. It is
the same question `/version` answers at runtime, asked at build time.

### 3.10 Rollback

Remove or rename the two `routes/place-workflows-tscribe-*/` directories (or delete
the GitHub webhooks) and `systemctl --user restart webhookd`. To undo a placement,
revert the commit on `main`. Nothing here touches the `knowledge-site` KB deploy.

---

## 4. Known gaps

Deliberately not done, so they don't get discovered as surprises:

- **`deploy/README.md` §3b still recommends the venv check.** It is the check that
  would have certified a stale server. §2.4 above supersedes it; the file itself
  should be rewritten to use `/version` and to make `uv sync --frozen` the normal
  path now that the lockfile is committed.
- **The real fix for the whole rate-check family is a voice-activity detector**
  (webrtcvad or silero) replacing the −30 dBFS level gate, which would make the
  denominator a measurement in *both* directions. That is a dependency plus a
  calibration exercise against real class audio — and shipping an unvalidated VAD
  to silence an inconvenient check would be the same sin in a new costume.
- **`place-workflows`'s contract does a naive substring scan** for the banned
  setup-python action, so merely *naming it in a YAML comment* trips the gate. My
  first draft of both workflows failed for explaining the rule it was obeying.
  Stripping comments before that check would fix it.
- **The GitHub App (Model B)** retires the PAT and closes the run-status blind spot
  (the cloud App gets 403 on check-runs today, which is why merges gate on
  `mergeable_state` instead).
