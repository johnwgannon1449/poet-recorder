# POET Counter Recorder

A phone-friendly page where a counter rep holds a button, says each order line the way a customer would ask for it, and lets go. Each line becomes one audio clip filed under rep + order + line. No login, no install, no typing.

Files: `app.py` (server), `db.py` (SQLite), `import_orders.py`, `export.py`, `seed_demo.py`, `backup.sh`, `static/rep.html` (rep page), `static/progress.html`, `tests/`. Everything the app creates lives under `./data/` (`recorder.db`, `audio/`).

## Install

Needs Python 3.11+ and `ffmpeg` on the PATH (`brew install ffmpeg` on the Mac, `apt install ffmpeg` on Linux).

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m pytest -q          # optional: 19 tests, no real ffmpeg needed
```

## Import the orders

One row per order line. Headers are case-insensitive and can be in any order; unknown columns (customer name, account number, etc.) are ignored and never stored.

| column | required | notes |
|---|---|---|
| `order_no` | yes | text |
| `line_no` | yes | integer |
| `qty` | yes | number |
| `sku` | yes | text; hidden from reps by default |
| `description` | yes | the MIS counter description exactly as reps see it |
| `uom` | no | text |
| `rep` | no | blank = unassigned pool |

```bash
python import_orders.py orders.xlsx                 # or orders.csv
python import_orders.py orders.xlsx --reps "Mike, Dave, Luis"   # add reps with no assignments yet
```

Re-importing upserts on (order_no, line_no): descriptions and assignments update, takes and skips are never touched. Spreadsheet row order is the queue order. For a quick look before the real sheet lands: `python seed_demo.py` (3 fake reps, 6 fake orders).

## Run on localhost

```bash
uvicorn app:app --host 0.0.0.0 --port 8000
```

- `http://localhost:8000/` pick your name → `/r/<slug>` the recording page
- `http://localhost:8000/progress` the progress table (also `/progress.csv`)

The microphone only works over HTTPS or on `localhost`, so a phone on the LAN hitting `http://<mac-ip>:8000` will **not** be able to record. Use a tunnel (next section).

## Test from a phone (tunnel from the dev Mac)

```bash
brew install cloudflared
cloudflared tunnel --url http://localhost:8000
```

It prints a `https://something.trycloudflare.com` URL. Open that on the iPhone in Safari; the first hold on TALK asks for the mic once. (`ngrok http 8000` works the same way.) The URL changes every time you restart the tunnel.

## Production behind Caddy

Caddy terminates HTTPS and proxies to uvicorn on the same box. Use the hostname and certificate IT provides.

```
# /etc/caddy/Caddyfile
poet.pacesupply.local {
    tls /etc/ssl/poet.crt /etc/ssl/poet.key    # drop this line to let Caddy fetch a public cert automatically
    reverse_proxy 127.0.0.1:8000
    request_body { max_size 50MB }
}
```

Run the app as a service (systemd example):

```
[Unit]
Description=POET Counter Recorder
After=network.target

[Service]
WorkingDirectory=/opt/poet-recorder
ExecStart=/opt/poet-recorder/.venv/bin/uvicorn app:app --host 127.0.0.1 --port 8000
Restart=always

[Install]
WantedBy=multi-user.target
```

Reps open `https://poet.pacesupply.local/` on their phone, tap their name once, and the device remembers them.

## How the queue works

A rep sees his own assigned orders in sheet order, then the unassigned pool, then other reps' orders (least-recorded first). A banner tells him when he crosses into the pool or someone else's orders. Nothing locks: two reps recording the same order is fine and both are kept.

On the progress page, "done" counts every order the rep completed, including pool orders, while "assigned" and "remaining" only count his own. The coverage cards at the bottom are across everyone, as a share of the whole import.

## Backup

```bash
./backup.sh /path/to/backups        # writes poet-recorder-YYYYMMDD-HHMM.zip
```

Restoring is copying `data/` back. Audio originals (`.webm`/`.m4a`) and the 16 kHz mono WAVs sit side by side under `data/audio/<order_no>/`; soft-deleted takes are moved to `data/audio/_trash/`.

## Export for Ryan

```bash
python export.py --out export/ --copy-audio               # internal: raw order numbers
python export.py --out export/ --hash-orders --copy-audio  # anything leaving PACE
```

Writes `takes.csv`, `lines.csv`, `orders.csv` and, with `--copy-audio`, `export/audio/{take_id}.wav`. `--hash-orders` replaces `order_no` with `sha256(SALT + order_no)` and drops the raw number; put the salt in `.env`:

```
ORDER_HASH_SALT=some-long-random-string
```

The `transcript` column in `takes.csv` is left empty for a later script. Transcription is not this app's job.
