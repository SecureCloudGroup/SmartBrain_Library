# SmartBrain Library API

The small service the SmartBrain app talks to instead of Git (operator ruling R7, `docs/PLAN.md` section 4).
It will run on the VPS behind the existing Caddy at `https://smartbrain.securecloudgroup.com/library/v1/`.

| Endpoint | Who | What |
|---|---|---|
| `POST /library/v1/votes` | the app | `{"source_id", "verdict": "yes"\|"no"\|"broken", "app_version"}` for a Library source |
| `POST /library/v1/suggestions` | the app | `{"record", "via": "form"\|"yes", "app_version"}`: a new source, parameter values removed |
| `GET /library/v1/manifest` | the app | the current pack `{tag, url, sha256, bytes}`, from the operator's manifest file |
| `GET /library/v1/healthz` | anyone | `{"ok": true}` |
| `GET /library/v1/admin/queue?since=&limit=` | operator | queued items after `since` (bearer token) |
| `POST /library/v1/admin/ack` | operator | `{"through": id}` deletes the queue up to that id (bearer token) |

Accepted submissions answer `202`. Refusals answer `422` with a list of problems, `413` (body over 16 KB),
`415` (not JSON), `429` with `Retry-After` (rate limit) or `503` (queue full).

## What it checks
- Every suggestion runs through the same `validate_record` rules as CI (`sourcetool/schema.py`), plus
  `sourcetool/submission.py`: https only, a public host (no private or loopback IPs, no `.local`), no
  credential in the address, headers or userinfo, **no parameter values** (`example` must be null), known
  categories, kinds and parameter kinds, only known fields, and size caps on every text and list.
- Nothing the client says about trust is kept. The tier becomes `harvested`, votes start at zero,
  validation is `unvalidated`, the terms status is `unverified`, the provider's authority is `community`,
  the origin is `user-suggestion`, and the id is derived from the host and name (never the client's id).
- The service **never fetches** a submitted URL. `sourcetool ingest` re-checks and probes on the
  operator's side.

## Privacy and security
- No accounts, cookies, install ids or device ids. Request models reject any field they do not name.
- The queue stores only what the app sends (a source id and verdict, or a record with parameter values
  removed), the app version, and the **day** it arrived. Never the ask text, a location, a key, or an
  identifier.
- Client addresses exist only in the in-memory rate limiter (30 votes and 10 suggestions per hour per
  address); they are never written to disk. Uvicorn runs with `--no-access-log`. Request bodies are never
  logged. Error messages name the field that is wrong and do not echo values back.
- Behind Caddy, the address is the single `X-Forwarded-For` entry Caddy sets, and it is believed only
  when the request comes from `LIBRARY_TRUSTED_PROXIES`.
- No CORS headers (the desktop app calls the API from its server side), JSON-only POSTs (a browser
  cannot send a cross-site simple form post), `nosniff`, `no-store`, `DENY` framing and a `default-src
  'none'` CSP on every response. No docs or OpenAPI endpoints.
- The admin endpoints need `Authorization: Bearer <LIBRARY_ADMIN_TOKEN>` (compared in constant time). A
  token shorter than 32 characters, or none, disables them.
- The queue holds at most 100,000 items; beyond that submissions get `503` until the operator ingests.

## Run the tests
```bash
python3.12 -m venv .venv-service && .venv-service/bin/pip install -r service/requirements-test.txt
.venv-service/bin/python -m pytest service/tests tests/test_ingest.py
```

## Deploy on the VPS (operator)
The VPS's Caddy runs in the `sb_caddy` container, so the service listens on the host's `docker0`
address (not `127.0.0.1`, which the container cannot reach, and never a public address). Python 3.12 is
required (`python3 --version`).

On the VPS (`ssh sb-signaling`), as a sudoer:

```bash
# 1. a user with no login and no home, and the code (read-only to that user)
sudo useradd --system --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin sblibrary
sudo git clone https://github.com/SecureCloudGroup/SmartBrain_Library /opt/smartbrain-library
sudo python3 -m venv /opt/smartbrain-library/.venv
sudo /opt/smartbrain-library/.venv/bin/pip install -r /opt/smartbrain-library/service/requirements.txt

# 2. configuration: the token and the manifest
ip -4 addr show docker0          # expect 172.17.0.1; if not, use that address below and in library.caddy
sudo install -d -m 750 -o root -g sblibrary /etc/smartbrain-library
sudo install -m 640 -o root -g sblibrary /opt/smartbrain-library/service/deploy/env.example /etc/smartbrain-library/env
TOKEN=$(openssl rand -hex 32); echo "$TOKEN"   # keep it; the Mac needs it for `sourcetool ingest`
sudo sed -i "s/^LIBRARY_ADMIN_TOKEN=.*/LIBRARY_ADMIN_TOKEN=$TOKEN/" /etc/smartbrain-library/env
TAG=v1.1.0; BASE=https://github.com/SecureCloudGroup/SmartBrain_Library/releases/download/$TAG
SHA=$(curl -fsSL $BASE/library.duckdb.gz.sha256 | cut -d' ' -f1)
BYTES=$(curl -fsSL $BASE/library.duckdb.gz | wc -c)
printf '{"tag": "%s", "url": "%s/library.duckdb.gz", "sha256": "%s", "bytes": %s}\n' "$TAG" "$BASE" "$SHA" "$BYTES" \
  | sudo install -m 644 /dev/stdin /etc/smartbrain-library/manifest.json

# 3. the service
sudo cp /opt/smartbrain-library/service/deploy/smartbrain-library.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now smartbrain-library
systemctl status smartbrain-library --no-pager
curl -s http://172.17.0.1:8095/library/v1/healthz        # {"ok":true}

# 4. Caddy: paste service/deploy/library.caddy INSIDE the {$LANDING_DOMAIN} { ... } block, above file_server
nano ~/sb-node/compose/conf.d/landing.caddy
docker exec sb_caddy caddy validate --config /etc/caddy/Caddyfile
docker exec sb_caddy caddy reload --config /etc/caddy/Caddyfile
curl -s https://smartbrain.securecloudgroup.com/library/v1/healthz      # {"ok":true}
curl -s https://smartbrain.securecloudgroup.com/library/v1/manifest     # the pack
curl -s -o /dev/null -w '%{http_code}\n' https://smartbrain.securecloudgroup.com/library/v1/admin/queue  # 401
```

Make the same `landing.caddy` change in SmartBrain_3000's `compose/conf.d/landing.caddy`, so a future
redeploy of the node from that repo keeps the route.

**Update** the service: `cd /opt/smartbrain-library && sudo git pull --ff-only && sudo .venv/bin/pip install
-r service/requirements.txt && sudo systemctl restart smartbrain-library`.
**New pack**: rewrite `/etc/smartbrain-library/manifest.json` (step 2); it is read on every request.
**Backup**: `/var/lib/smartbrain-library/queue.sqlite3` holds only what has not been ingested yet.

## Ingest (the operator's machine)
```bash
export LIBRARY_ADMIN_TOKEN=<the token>        # LIBRARY_API_URL defaults to the VPS
.venv/bin/python -m sourcetool ingest --dry-run   # what is queued; writes nothing, clears nothing
.venv/bin/python -m sourcetool ingest             # write votes + sources/suggested/<date>.jsonl, probe, clear the queue
.venv/bin/python -m sourcetool ingest --commit    # the same, then a local branch + commit (never pushes)
```
Then review the diff, run `check`, and open the PR. The queue is cleared once the files are written, so
keep the branch until it is merged.
