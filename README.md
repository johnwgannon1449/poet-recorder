# POET Counter Recorder

A phone-friendly page where a counter rep taps TALK, reads a whole sales order the way a customer would ask for it, and taps again. Lines he doesn't know get an **X** first. Each read becomes one audio clip filed under rep + order, with the list of lines he read and the ones he X'd. No login, no install, no typing.

Files: `app.py` (server), `db.py` (SQLite), `import_orders.py`, `assign_reps.py`, `export.py`, `seed_demo.py`, `backup.sh`, `static/rep.html` (rep page), `static/progress.html`, `tests/`. Everything the app creates lives under `./data/` (`recorder.db`, `audio/`).

## Install

Needs Python 3.11+ and `ffmpeg` on the PATH (`brew install ffmpeg` on the Mac, `apt install ffmpeg` on Linux).

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m pytest -q          # optional: 20 tests, no real ffmpeg needed
```

## Get orders in: the MIS export → a batch → import

**1. Export counter sales from MIS** (the standard "Export-YYYYMMDD" CSV works as is). The importer reads these columns and ignores everything else; customer names and account numbers are never stored:

| MIS column | becomes | required |
|---|---|---|
| Invoice Number | order_no | yes |
| Line# | line_no | yes |
| Code (Product) | sku | yes (shown before the description on every line) |
| Name (Product) | description | yes |
| Selling Unit (Product) | uom | no |
| Quantity / Qty Shipped | qty | no (the standard export has none; add it if you want reps to say quantities) |
| Ship Branch, Name (Division), Type (Price Code), Size (Price Code), Name (Customer Region), Order Date | order context | no |

A hand-made sheet with plain headers (`order_no, line_no, sku, description, qty, uom, rep`) works too.

**2. Pick a batch and assign reps:**

```bash
python assign_reps.py Export-20261028.csv --reps "Eric, John" --per-rep 20 --out batch1.xlsx
python assign_reps.py Export-20261028.csv --reps "Eric, John" --per-rep 20 \
       --focus "Eric=FIRE PROTECTION|WATER WORK; John=HVAC|HYDRONIC" --out batch2.xlsx
```

Each rep gets a spread across divisions (or only his `--focus` divisions), 2 to 15 lines per order by default (`--min-lines`, `--max-lines`), and orders already in the database are never picked twice. It prints what each rep got. Open the xlsx and change the `rep` column by hand if you want.

**3. Import the batch:**

```bash
python import_orders.py batch1.xlsx
python import_orders.py batch1.xlsx --reps "Mike, Dave"   # also add reps who have no assignments yet
```

Re-importing upserts on (order_no, line_no): descriptions and assignments update, recordings and X marks are never touched. Row order is the queue order. For a quick look with fake data: `python seed_demo.py`.

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

## How the rep page works

One order at a time. The rep taps **X** on any line he doesn't know (tap again to put it back), taps **TALK**, reads the rest of the order out loud the way a customer would say it, and taps TALK again. Holding TALK and letting go also works. The take saves and the next order loads. **BACK** reopens the previous order to listen, delete, or record it again; **SKIP ORDER** is for an order he can't read at all.

A rep sees his own assigned orders in sheet order, then the unassigned pool, then other reps' orders (least-recorded first). A banner tells him when he crosses into the pool or someone else's orders. Nothing locks: two reps recording the same order is fine and both are kept.

On the progress page, "done" counts every order the rep recorded, including pool orders, while "assigned" and "remaining" only count his own. "Lines recorded" are lines in orders he read minus the ones he X'd. The coverage cards at the bottom are across everyone, as a share of the whole import.

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

Writes `takes.csv` (one row per whole-order read, with `lines_read` and `lines_skipped` as line numbers), `lines.csv` (every line with `n_takes` = reads that covered it), `orders.csv` (with branch, division, customer type) and, with `--copy-audio`, `export/audio/{take_id}.wav`. `--hash-orders` replaces `order_no` with `sha256(SALT + order_no)` and drops the raw number; put the salt in `.env`:

```
ORDER_HASH_SALT=some-long-random-string
```

The `transcript` column in `takes.csv` is left empty for a later script. Transcription is not this app's job.
