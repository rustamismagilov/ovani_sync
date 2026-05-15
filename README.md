# ovani_sync

Sync your Ovani Sound library to a local folder. Detects new orders, skips files you already have, and records every download in a manifest.

> **Windows only.** Tested on Windows 11 with PowerShell. May work on macOS/Linux but has not yet been verified.

## WHY

Built this mostly for myself because I'm a bit of a digital hoarder and wanted a way to keep my full Ovani library on disk and in sync without manually clicking through every order each time.

THIS IS A TOOL FOR DOWNLOADING LEGALLY ACQUIRED OVANI ASSETS FROM THE OFFICIAL OVANI WEBSITE (https://ovanisound.com). OVANI'S TERMS OF SERVICE APPLY TO THE ASSETS. THE SCRIPT DOES NOT CHANGE ANYTHING ABOUT LICENSING OR ATTRIBUTION.

## HOW

### Quick start

```powershell
git clone https://github.com/rustamismagilov/ovani_sync.git
cd ovani_sync
pip install -r requirements.txt
python ovani_sync.py --path "path/to/folder" --dry-run
```

The first run with `--dry-run` shows you exactly what will be downloaded and where, without touching the filesystem. Drop `--dry-run` to actually download.

### Cookies setup

The script authenticates by reading cookies your browser already has after you log into ovanisound.com. There is no automated login.

1. Log into https://ovanisound.com in your browser.
2. Install **Get cookies.txt LOCALLY** for [Firefox](https://addons.mozilla.org/en-US/firefox/addon/get-cookies-txt-locally/) or [Chrome](https://chromewebstore.google.com/detail/get-cookiestxt-locally/cclelndahbckbenkjhflpdbgdldlbecc).
3. While on ovanisound.com, click the extension and export cookies as `cookies.txt`.
4. Save it next to `ovani_sync.py` (or pass `--cookies "path/to/cookies.txt"`).

When cookies expire, the script reports "Authentication failed. Cookies may have expired." Repeat the process to refresh.

### CLI flags

| Flag | Description |
|---|---|
| `--path "path/to/folder"` | **Required.** Local folder where assets live. |
| `--cookies "path/to/cookies.txt"` | Path to `cookies.txt` (Netscape format). Defaults to `cookies.txt` next to the script. |
| `--dry-run` | Print the plan and exit. No files written. |
| `--force` | Re-download every file, ignoring the manifest. |
| `--workers N` | Concurrent downloads. Default 4. |

### Common workflows

```powershell
# Preview what would be downloaded
python ovani_sync.py --path "path/to/folder" --dry-run
```

```powershell
# Sync everything
python ovani_sync.py --path "path/to/folder"
```

```powershell
# Sync everything with 8 workers (faster, but can hit download limits)
python ovani_sync.py --path "path/to/folder" --workers 8
```

```powershell
# Sync with a cookies file in a different location
python ovani_sync.py --path "path/to/folder" --cookies "path/to/cookies.txt"
```

```powershell
# Force re-download EVERY file (useful if something seems corrupted)
python ovani_sync.py --path "path/to/folder" --force
```

### Folder layout

Regular assets land in the root folder using their original filename. Weekly Wav drops are routed into a `Weekly Wav/` subfolder, with the `M/D/YYYY` date in the title rewritten to `M.D.YYYY` (because slashes are illegal in Windows paths):

```
path/to/folder/
├── manifest.json
├── Some Pack.zip
├── Another Track.wav
└── Weekly Wav/
    ├── zzz-3.6.2026 Weekly Wav - Sci-Fi Level Up.wav
    ├── zzz-3.19.2026 Weekly Wav - Analysis Complete.wav
    └── zzz-4.10.2026 Weekly Wav - Ice Bubble Spell.wav
```

### Output

After scraping, you will see a plan table of every file that needs to be downloaded with columns `Status / Order / Filename / Destination`, plus a summary table showing per-action counts:

```
SUMMARY:
╭────────────────────┬───────╮
│ Action             │ Count │
├────────────────────┼───────┤
│ first-download     │     9 │
├────────────────────┼───────┤
│ skip               │   227 │
├────────────────────┼───────┤
│ TOTAL TO DOWNLOAD: │     9 │
╰────────────────────┴───────╯
```

Actions:

- **`first-download`**: the file is not on disk yet, so it will be downloaded.
- **`redownload`**: either `--force` is on, or the remote size disagrees with what we have locally (likely a re-upload at higher quality). The file will be re-downloaded.
- **`skip`**: the file is on disk and the remote size matches. Nothing happens.

### Up-to-date check

Ovani's orders page lists each asset's file size next to its filename (e.g. `wav • 449.13 KB`). The script extracts that size during the same scrape that gathers the download URLs and compares it against your local file. Mismatches trigger a `redownload`. Re-encodes at higher quality differ by many percent in byte count, so they're easy to catch. No extra HTTP requests per asset, so the whole planning pass is near-instant.

Details:

- If a file is on disk and a manifest entry exists, the manifest's recorded `size_bytes` is compared against the page-published size.
- If a file is on disk but has no manifest entry (e.g. left over from a pre-manifest run), the local file's actual size is compared against the page-published size, and a manifest entry is silently backfilled so future runs are pure `skip` / `first-download`.
- If a size is missing from the page entirely, the script conservatively `skip`s any file already on disk (and `first-download`s anything missing).

A small tolerance (1% of file size, or 2 KB, whichever is larger) absorbs the page's display rounding to two decimal places.

In `--dry-run` the script stops after printing the tables. In normal mode it prompts:

```
[•] Download 9 files? [Y/n]:
```

Press `Y` or `Enter` to start, anything else to abort. After download finishes:

- If all files are OK, you will see: `[✓] Done.`
- If some files failed, you will see an interactive prompt `[•] N download(s) failed. Retry? [Y/n]:`. Press `Y` or `Enter` to re-run only the failed items, press anything else to cancel. It will loop until everything is downloaded or you interrupt (with `Ctrl+C`).

A `manifest.json` is written into `--path` recording every successful download (sha256, size, source URL, timestamp).

### Some facts

- The script forces **IPv4** for all HTTPS connections. On a lot of Windows networks IPv6 is turned on in the OS but the router or ISP doesn't actually carry IPv6 traffic. Browsers retry over IPv4 when that happens, but urllib3 doesn't, so connections to Cloudflare R2 hang and fail with `WinError 10051` ("network unreachable"). Forcing IPv4 avoids the problem and R2 works fine over IPv4 anyway.
- Ovani's storefront is Shopify, but the actual files live on **Cloudflare R2** (Cloudflare's S3-compatible object storage). When you click a download link you get a 302 redirect to a signed R2 URL like `https://<id>.r2.cloudflarestorage.com/...?X-Amz-Signature=...`.
- Ovani's orders page uses **decimal SI prefixes** for sizes. `1 KB` means `1000 bytes`, `1 MB` means `1 000 000 bytes`, and so on. That's why `_SIZE_UNITS` in the script uses 1000-multiples instead of 1024.
- The download anchor on each asset row carries the URL inside an Alpine.js `x-bind:href` expression wrapped in a `(linksEnabled) ? '<url>' : '#'` ternary, not a plain `href`. The scraper pulls the real URL out of that expression.
- Ovani's CDN returns HTTP 503 (not an empty page) when asked for a paginated page past the end. The pagination loop treats any 4xx or 5xx response beyond page 1 as end-of-list.

### Known issues

- **Authentication failed**: `cookies.txt` is missing required entries or expired. Repeat the Cookies setup step.
- **`[WinError 32]` on rename**: Windows antivirus briefly locks the freshly downloaded `.part` file. The retry prompt at the end of the run almost always clears it.
- **`[WinError 10051] A socket operation was attempted to an unreachable network`**: high `--workers` (e.g. 64) makes many concurrent connections to Cloudflare R2 from one IP, which their anti-abuse layer silently drops. Windows then reports the network as unreachable. **Recommended `--workers` is 4-8.** If you see a whole batch fail with this error, drop the worker count and re-run.
- **Ctrl+C unresponsive during downloads**: the first press sets a stop flag and lets the script unwind gracefully, but it has to wait for in-flight TCP connections to finish (up to 10s with the connect timeout) before workers see the flag. **Press Ctrl+C a second time to force-quit immediately.** A force-quit skips the manifest write for whatever was in flight. The next run re-evaluates from page-published sizes.
