#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import signal
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from http.cookiejar import MozillaCookieJar
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin

import socket
import requests
import urllib3.util.connection
from bs4 import BeautifulSoup
from tqdm import tqdm

urllib3.util.connection.allowed_gai_family = lambda: socket.AF_INET  # force IPv4

BASE = "https://ovanisound.com"
ORDERS_URL = f"{BASE}/apps/digital-downloads/orders/"

# Recognises Weekly Wav item titles like "zzz-4/10/2026 Weekly Wav - Ice Bubble Spell".
# Captures month, day, year, and the trailing track name.
WEEKLY_RE = re.compile(
    r"^\s*zzz-(\d{1,2})/(\d{1,2})/(\d{4})\s+Weekly\s+Wav\s*-\s*(.+?)\s*$",
    re.IGNORECASE,
)
WEEKLY_SUBDIR = "Weekly Wav"

# Characters Windows forbids in filenames. Stripped by sanitize_filename.
WINDOWS_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

# Ovani's orders page uses decimal SI prefixes (KB=1000, MB=1e6, etc.). See README.
_SIZE_RE = re.compile(r"([\d.]+)\s*(KB|MB|GB|TB|B)?", re.I)
_SIZE_UNITS = {"B": 1, "KB": 1000, "MB": 1000**2, "GB": 1000**3, "TB": 1000**4}


def _parse_size_str(size_str: str) -> Optional[int]:
    """Parse '449.13 KB' / '1.2 GB' / '512 B' into bytes. Returns None if unparseable."""
    if not size_str:
        return None
    match = _SIZE_RE.search(size_str)
    if not match:
        return None
    return int(float(match.group(1)) * _SIZE_UNITS.get((match.group(2) or "B").upper(), 1))

DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/124.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
              "image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": ORDERS_URL,
}

_STOP = threading.Event()  # set on Ctrl+C so worker threads can abort mid-download
_INTERRUPT_COUNT = 0       # number of Ctrl+C presses seen so far; second press force-quits


def _install_sigint_handler() -> None:
    """First Ctrl+C: set _STOP and let the script unwind gracefully.
    Second Ctrl+C: os._exit immediately, bypassing the executor's atexit wait."""
    def handler(signum, frame):
        del signum, frame  # required by signal.signal API; not needed here
        global _INTERRUPT_COUNT
        _INTERRUPT_COUNT += 1
        _STOP.set()
        if _INTERRUPT_COUNT >= 2:
            print(f"\n{TAG_ERR} Force-quitting.", flush=True)
            os._exit(130)
        print(f"\n{TAG_INFO} Interrupting... press Ctrl+C again to force quit.", flush=True)
        raise KeyboardInterrupt
    signal.signal(signal.SIGINT, handler)

_USE_COLOR = sys.stdout.isatty()

def _color(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _USE_COLOR else text

TAG_INFO = _color("93", "[•]")   # yellow
TAG_OK   = _color("92", "[✓]")   # green
TAG_ERR  = _color("91", "[✗]")   # red

# tqdm bar template. @TOTAL@ is a placeholder we substitute as cumulative bytes grow.
_BAR_FMT = ("{desc}: {percentage:3.0f}%|{bar}| {n_fmt}/{total_fmt} "
            "[RUN {elapsed}, ETA {remaining}, TOTAL @TOTAL@]")


_WINERR_RE = re.compile(r"\[WinError \d+\][^\"']*", re.IGNORECASE)


def _short_error(exc: BaseException) -> str:
    """Produce a one-line error string without the multi-hundred-char signed URL."""
    msg = str(exc)
    match = _WINERR_RE.search(msg)
    if match:
        return f"{type(exc).__name__}: {match.group(0).strip()}"
    # urllib3 wraps the underlying cause as "Caused by NewConnectionError('...: <real msg>')"
    if "Caused by" in msg:
        tail = msg.split("Caused by", 1)[1]
        return f"{type(exc).__name__}: {tail.strip(' :()<>')[:200]}"
    return f"{type(exc).__name__}: {msg[:200]}"


def _format_bytes(num_bytes: float) -> str:
    """Render a byte count as B/KB/MB/GB for human-readable summary output."""
    if num_bytes < 1024:
        return f"{num_bytes:.0f}B"
    if num_bytes < 1024**2:
        return f"{num_bytes / 1024:.0f}KB"
    if num_bytes < 1024**3:
        return f"{num_bytes / 1024**2:.0f}MB"
    return f"{num_bytes / 1024**3:.1f}GB"


def sanitize_filename(name: str) -> str:
    """Strip Windows-illegal characters and collapse any whitespace they leave behind."""
    out = WINDOWS_ILLEGAL.sub("", name)
    out = re.sub(r"\s+", " ", out)
    return out.rstrip(". ").strip()


@dataclass
class Asset:
    """One downloadable file."""
    order_num: str
    item_title: str
    original_filename: str
    download_url: str
    size_bytes: Optional[int] = None

    @property
    def is_weekly(self) -> bool:
        return bool(WEEKLY_RE.match(self.item_title))

    def target(self, base: Path) -> Path:
        """Resolve where this asset should land on disk under `base`."""
        if self.is_weekly:
            match = WEEKLY_RE.match(self.item_title)
            assert match is not None
            month, day, year, name = match.group(1), match.group(2), match.group(3), match.group(4)
            # Slashes in M/D/YYYY are illegal in Windows paths, so rewrite to dots.
            ext = Path(self.original_filename).suffix or ".wav"
            safe_name = sanitize_filename(name)
            fname = f"zzz-{month}.{day}.{year} Weekly Wav - {safe_name}{ext}"
            return base / WEEKLY_SUBDIR / fname
        return base / sanitize_filename(self.original_filename)


def make_session(cookies_path: Path) -> requests.Session:
    """Build a requests.Session preloaded with the user's exported Netscape cookies."""
    if not cookies_path.exists():
        sys.exit(
            f"{TAG_ERR} Cookies file not found: {cookies_path}\n"
            f"    Export your ovanisound.com cookies as Netscape format and save here."
        )
    jar = MozillaCookieJar(str(cookies_path))
    # ignore_discard/expires keeps session cookies and any past-expiry entries.
    jar.load(ignore_discard=True, ignore_expires=True)
    session = requests.Session()
    session.cookies = jar
    session.headers.update(DEFAULT_HEADERS)
    return session


def verify_auth(session: requests.Session) -> None:
    """Probe the orders URL once. Exit cleanly if cookies are expired/missing."""
    try:
        response = session.get(ORDERS_URL, allow_redirects=True, timeout=(10, 30))
    except requests.RequestException as exc:
        sys.exit(f"{TAG_ERR} Could not reach {ORDERS_URL}: {exc}")
    # Shopify bounces unauthenticated requests to /account/login. Treat that as auth-failure.
    final_url = response.url.lower()
    if response.status_code in (302, 401, 403) or "login" in final_url or "account/login" in final_url:
        sys.exit(
            f"{TAG_ERR} Authentication failed. Cookies may have expired. "
            f"Re-export cookies.txt while logged into ovanisound.com."
        )
    if response.status_code >= 400:
        sys.exit(f"{TAG_ERR} Orders page returned HTTP {response.status_code}.")


# The download anchor uses Alpine.js's x-bind:href instead of a static href,
# with a ternary like:  (linksEnabled) ? 'https://.../download/UUID?...' : '#'
_XBIND_URL_RE = re.compile(r"'(https?://[^']+)'")


def _extract_download_url(asset_el) -> Optional[str]:
    """Pull the real download URL out of the Alpine-bound anchor."""
    anchor = asset_el.select_one(".dda-order__asset-link a")
    if anchor is None:
        return None
    # Prefer a plain href if one exists; fall back to Alpine's x-bind:href.
    href = anchor.get("href")
    if href and href != "#":
        return href
    xbind = anchor.get("x-bind:href") or anchor.get(":href")
    if not xbind:
        return None
    match = _XBIND_URL_RE.search(xbind)
    return match.group(1) if match else None


def scrape_assets(session: requests.Session) -> list[Asset]:
    """Walk every page of the orders endpoint and collect every asset."""
    assets: list[Asset] = []
    page = 1
    seen_urls: set[str] = set()
    while True:
        params = {"page": page} if page > 1 else None
        response = session.get(ORDERS_URL, params=params, timeout=(10, 30))
        # Ovani's CDN returns 503 (and sometimes 404/410) for out-of-range pages
        # instead of an empty page. Treat any non-2xx beyond page 1 as end-of-list.
        if response.status_code >= 400:
            if page == 1:
                response.raise_for_status()
            tqdm.write(f"  page {page}: HTTP {response.status_code} — assuming end of orders")
            break
        soup = BeautifulSoup(response.text, "html.parser")
        orders = soup.select(".dda-order")
        if not orders:
            break
        new_on_page = 0
        for order in orders:
            num_el = order.select_one(".dda-order__number")
            order_num = num_el.get_text(strip=True) if num_el else ""
            for item in order.select(".dda-order__item"):
                title_el = item.select_one(".dda-order__item-name")
                title = title_el.get_text(strip=True) if title_el else ""
                for asset_el in item.select(".dda-order__asset"):
                    filename_el = asset_el.select_one(".dda-order__asset-filename")
                    filename = filename_el.get_text(strip=True) if filename_el else ""
                    href = _extract_download_url(asset_el)
                    if not href:
                        continue
                    href = urljoin(BASE, href)
                    # Dedupe: the same download URL can appear under multiple orders.
                    if href in seen_urls:
                        continue
                    seen_urls.add(href)
                    meta_el = asset_el.select_one(".dda-order__asset-meta")
                    size_bytes = _parse_size_str(meta_el.get_text(strip=True)) if meta_el else None
                    assets.append(Asset(
                        order_num=order_num,
                        item_title=title,
                        original_filename=filename,
                        download_url=href,
                        size_bytes=size_bytes,
                    ))
                    new_on_page += 1
        tqdm.write(f"  page {page}: {len(orders)} orders, {new_on_page} files")
        if new_on_page == 0:
            break
        page += 1
        time.sleep(0.4)
    return assets


def _order_id_num(order_num: str) -> int:
    """Numeric part of an order label like '#117070' -> 117070, else 0."""
    digits = re.sub(r"\D", "", order_num)
    return int(digits) if digits else 0


def dedupe_assets(assets: list[Asset], base: Path) -> list[Asset]:
    """Collapse assets that resolve to the same destination path.

    The same file often appears in several orders, each with its own download
    URL, so the URL dedupe in scrape_assets misses them. Keep the copy from the
    highest order id and drop the rest. Survivors keep their first-seen order.
    """
    best: dict[Path, Asset] = {}
    order: list[Path] = []
    for asset in assets:
        target = asset.target(base)
        current = best.get(target)
        if current is None:
            best[target] = asset
            order.append(target)
        elif _order_id_num(asset.order_num) > _order_id_num(current.order_num):
            best[target] = asset
    return [best[target] for target in order]


def download_one(session: requests.Session, asset: Asset, target: Path,
                 pbar_position: int = 0) -> tuple[Path, str, int]:
    """Download one asset to `target`. Returns (path, sha256_hex, size_bytes).
    Aborts mid-stream by raising KeyboardInterrupt if _STOP is set."""
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".part")
    hasher = hashlib.sha256()
    size_bytes = 0
    with session.get(asset.download_url, stream=True, allow_redirects=True, timeout=(10, 120)) as response:
        response.raise_for_status()
        total = int(response.headers.get("Content-Length") or 0)
        with tqdm(
            total=total or None,
            unit="B", unit_scale=True, unit_divisor=1024,
            desc=target.name[:40],
            position=pbar_position, leave=False, ascii=" ░▒▓█",
        ) as bar, open(tmp, "wb") as out_file:
            for chunk in response.iter_content(chunk_size=1 << 16):
                # Cooperative cancellation point. See _install_sigint_handler.
                if _STOP.is_set():
                    raise KeyboardInterrupt
                if not chunk:
                    continue
                out_file.write(chunk)
                hasher.update(chunk)
                size_bytes += len(chunk)
                bar.update(len(chunk))
    # Atomic rename: only commit the .part file once the body completed.
    os.replace(tmp, target)
    return target, hasher.hexdigest(), size_bytes


def _print_summary(counts: dict[str, int]) -> None:
    """Render the box-drawn SUMMARY table at the end of planning."""
    sum_rows: list[tuple[str, str]] = []
    for action, count in sorted(counts.items()):
        sum_rows.append((action, str(count)))
    to_download = sum(count for action, count in counts.items()
                      if action in ("first-download", "redownload"))
    sum_rows.append(("TOTAL TO DOWNLOAD:", str(to_download)))
    sum_headers = ("Action", "Count")
    # Column widths grow to fit the longest cell content.
    label_w = max(len(sum_headers[0]), max((len(label) for label, _ in sum_rows), default=0))
    count_w = max(len(sum_headers[1]), max((len(value) for _, value in sum_rows), default=1))

    def _hline(left: str, mid: str, right: str) -> str:
        return left + mid.join("─" * (width + 2) for width in (label_w, count_w)) + right

    def _row(label: str, value: str) -> str:
        return f"│ {label:<{label_w}} │ {value:>{count_w}} │"

    print("\nSUMMARY:")
    print(_hline("╭", "┬", "╮"))
    print(_row(sum_headers[0], sum_headers[1]))
    print(_hline("├", "┼", "┤"))
    for idx, (action, value) in enumerate(sum_rows):
        print(_row(action, value))
        if idx < len(sum_rows) - 1:
            print(_hline("├", "┼", "┤"))
    print(_hline("╰", "┴", "╯"))


def _print_plan(plan: list[tuple[Asset, Path, str]]) -> None:
    """Render the per-asset plan table. Skipped rows are omitted to reduce noise."""
    rows = []
    for asset, target, action in plan:
        if action == "skip":
            continue
        rows.append((f"[{action}]", asset.order_num, target.name, str(target.parent)))
    if not rows:
        return
    headers = ("Status", "Order", "Filename", "Destination")
    # Column widths grow to fit the longest cell in each column.
    widths = [max(len(headers[col]), max(len(row[col]) for row in rows)) for col in range(4)]

    def _hline(left: str, mid: str, right: str) -> str:
        return left + mid.join("─" * (width + 2) for width in widths) + right

    def _row(cells: tuple[str, ...]) -> str:
        return "│ " + " │ ".join(cell.ljust(width) for cell, width in zip(cells, widths)) + " │"

    print()
    print(_hline("╭", "┬", "╮"))
    print(_row(headers))
    print(_hline("├", "┼", "┤"))
    for idx, row in enumerate(rows):
        print(_row(row))
        if idx < len(rows) - 1:
            print(_hline("├", "┼", "┤"))
    print(_hline("╰", "┴", "╯"))


def _sizes_match(size_a: int, size_b: int) -> bool:
    """Byte-count equality with a small tolerance for the orders page's display rounding.
    Re-uploads at higher quality differ by many percent, so this only swallows rounding."""
    if size_a == size_b:
        return True
    return abs(size_a - size_b) <= max(2048, int(max(size_a, size_b) * 0.01))


def decide_action(target: Path, manifest_entry: Optional[dict], page_size_bytes: Optional[int],
                  force: bool) -> str:
    """Decide what to do with one asset.

    Returns one of: "first-download", "redownload", "skip".
    "redownload" is used both when --force is on AND when the page-published size
    disagrees with what we have on disk / in the manifest (likely a re-upload)."""
    if force:
        # User asked to re-fetch everything regardless of state.
        return "redownload" if manifest_entry else "first-download"
    if not target.exists():
        return "first-download"
    if page_size_bytes is None:
        # No size on the page to compare against. Trust whatever is on disk.
        return "skip"
    if manifest_entry and manifest_entry.get("size_bytes"):
        # Tracked file. Compare its recorded size against the live page size.
        return "skip" if _sizes_match(int(manifest_entry["size_bytes"]), page_size_bytes) else "redownload"
    # Untracked file on disk. Fall back to its real on-disk size for comparison.
    try:
        local_size = target.stat().st_size
    except OSError:
        return "first-download"
    return "skip" if _sizes_match(local_size, page_size_bytes) else "redownload"


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass

    _install_sigint_handler()

    parser = argparse.ArgumentParser(description="Sync Ovani Sound library to a local folder.")
    parser.add_argument("--path", required=True,
                        help=r'Base output folder, e.g. "E:\Game Assets\Ovani"')
    parser.add_argument("--cookies", default="cookies.txt",
                        help="Path to cookies.txt (Netscape format). Default: cookies.txt next to script.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print the plan and exit. No files written.")
    parser.add_argument("--force", action="store_true",
                        help="Re-download every asset, ignoring the manifest.")
    parser.add_argument("--workers", type=int, default=4,
                        help="Concurrent downloads (default 4).")
    args = parser.parse_args()

    root = Path(args.path).expanduser().resolve()
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        sys.exit(f"{TAG_ERR} Cannot create {root}: {exc}")

    cookies_path = Path(args.cookies)
    if not cookies_path.is_absolute():
        cookies_path = Path(__file__).parent / cookies_path

    print(f"{TAG_INFO} Output folder: {root}")
    print(f"{TAG_INFO} Weekly folder: {root / WEEKLY_SUBDIR}")
    print(f"{TAG_INFO} Loading cookies from {cookies_path}")

    session = make_session(cookies_path)
    verify_auth(session)

    manifest_path = root / "manifest.json"
    manifest: dict = {}
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"{TAG_ERR} Could not read existing manifest at {manifest_path}: {exc}")
            print(f"{TAG_INFO} Treating as empty.")

    print(f"{TAG_INFO} Scraping orders...")
    assets = scrape_assets(session)
    before = len(assets)
    assets = dedupe_assets(assets, root)
    collapsed = before - len(assets)
    weekly = sum(1 for asset in assets if asset.is_weekly)
    print(f"{TAG_OK} Found {len(assets)} downloadable asset(s). Weekly: {weekly}, Regular: {len(assets) - weekly}")
    if collapsed:
        print(f"{TAG_INFO} Found {collapsed} EXACT duplicates across multiple orders.")

    # Build the plan from page-scraped sizes. No per-asset HTTP round-trip.
    targets = [asset.target(root) for asset in assets]
    # Manifest key is the relative path with forward slashes (stable across OSes).
    keys = [str(target.relative_to(root)).replace("\\", "/") for target in targets]
    plan: list[tuple[Asset, Path, str]] = []
    counts: dict[str, int] = {}
    backfill_keys: list[int] = []  # plan indices needing a quiet manifest write
    total_library_bytes = 0
    for idx, asset in enumerate(assets):
        target = targets[idx]
        key = keys[idx]
        action = decide_action(target, manifest.get(key), asset.size_bytes, args.force)
        plan.append((asset, target, action))
        counts[action] = counts.get(action, 0) + 1
        # File exists locally but isn't tracked yet: queue a silent manifest entry.
        if action == "skip" and key not in manifest:
            backfill_keys.append(idx)
        if asset.size_bytes:
            total_library_bytes += asset.size_bytes
    if total_library_bytes:
        print(f"{TAG_INFO} Total library size on Ovani: {_format_bytes(total_library_bytes)}")

    # Silent manifest backfill: register on-disk-only files using local size.
    # sha256 is left null to avoid hashing tens of GB on every first run.
    # Real downloads still record sha256 normally.
    if backfill_keys and not args.dry_run:
        for idx in backfill_keys:
            asset = assets[idx]
            target = targets[idx]
            key = keys[idx]
            try:
                local_size = target.stat().st_size
            except OSError:
                continue
            manifest[key] = {
                "order_num": asset.order_num,
                "item_title": asset.item_title,
                "original_filename": asset.original_filename,
                "download_url": asset.download_url,
                "sha256": None,
                "size_bytes": local_size,
                "downloaded_at": None,
                "backfilled_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            }
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    _print_plan(plan)
    _print_summary(counts)

    if args.dry_run:
        return 0

    to_download = [(asset, target, action) for asset, target, action in plan
                   if action in ("first-download", "redownload")]
    if not to_download:
        print(f"{TAG_OK} Nothing to download. Every file is up to date.")
        return 0

    try:
        answer = input(f"\n{TAG_INFO} Download {len(to_download)} files? [Y/n]: ").strip().lower()
    except EOFError:
        answer = "n"
    if answer not in ("", "y", "yes"):
        print(f"{TAG_INFO} Aborted.")
        return 0

    def _do(item: tuple[Asset, Path, str]) -> tuple[tuple[Asset, Path, str], Optional[Path], Optional[str], Optional[int], Optional[str]]:
        asset, target, _ = item
        try:
            path, sha, size = download_one(session, asset, target)
            return item, path, sha, size, None
        except KeyboardInterrupt:
            return item, None, None, None, "cancelled"
        except Exception as exc:
            return item, None, None, None, _short_error(exc)

    remaining = list(to_download)
    failed_items: list = []
    attempt = 0
    downloaded_bytes_total = 0
    while remaining:
        attempt += 1
        label = "Downloading" if attempt == 1 else f"Retry {attempt - 1}: re-downloading"
        print(f"\n{TAG_INFO} {label} {len(remaining)} files with {args.workers} workers...")
        failed_items = []
        consecutive_net_fails = 0
        gave_up = False
        with ThreadPoolExecutor(max_workers=args.workers) as executor:
            try:
                futures = [executor.submit(_do, item) for item in remaining]
                files_bar = tqdm(
                    as_completed(futures), total=len(futures), desc="Files",
                    ascii=" ░▒▓█",
                    bar_format=_BAR_FMT.replace("@TOTAL@", "0B"),
                )
                for future in files_bar:
                    item, path, sha, size, err = future.result()
                    asset = item[0]
                    if err:
                        tqdm.write(f"  {TAG_ERR} {asset.original_filename}: {err}")
                        failed_items.append(item)
                        if any(needle in err for needle in ("ConnectTimeout", "WinError 10051", "NewConnectionError")):
                            consecutive_net_fails += 1
                        else:
                            consecutive_net_fails = 0
                        if consecutive_net_fails >= 3:
                            # Repeated connect failures: bail out instead of waiting for every
                            # remaining worker to time out. See "Known issues" in the README.
                            tqdm.write(f"  {TAG_INFO} 3 consecutive connection failures. Cloudflare R2 looks unreachable from this network. Cancelling the rest of this batch.")
                            for pending in futures:
                                pending.cancel()
                            _STOP.set()
                            gave_up = True
                            # Drain anything still in flight into failed_items.
                            for pending_item in remaining:
                                if pending_item not in failed_items and pending_item is not item:
                                    failed_items.append(pending_item)
                            break
                        continue
                    consecutive_net_fails = 0
                    downloaded_bytes_total += size or 0
                    files_bar.bar_format = _BAR_FMT.replace("@TOTAL@", _format_bytes(downloaded_bytes_total))
                    key = str(path.relative_to(root)).replace("\\", "/")
                    manifest[key] = {
                        "order_num": asset.order_num,
                        "item_title": asset.item_title,
                        "original_filename": asset.original_filename,
                        "download_url": asset.download_url,
                        "sha256": sha,
                        "size_bytes": size,
                        "downloaded_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    }
                    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
            except KeyboardInterrupt:
                _STOP.set()
                executor.shutdown(wait=False, cancel_futures=True)
                raise

        if not failed_items:
            break

        if gave_up:
            print(f"\n{TAG_ERR} Stopped after repeated connect failures. Triage from this shell:\n"
                  f"    Test-NetConnection 30a2ebdf22f65d0fa9265f9512100820.r2.cloudflarestorage.com -Port 443\n"
                  f"If that also fails, check VPN/proxy/firewall settings — nothing this script can change will help until R2 is reachable.")
            break

        # If every file in the batch failed and the user picked a high --workers,
        # the most likely cause is Cloudflare R2's anti-abuse dropping the burst.
        if len(failed_items) == len(remaining) and args.workers > 8:
            print(f"{TAG_INFO} All {len(failed_items)} downloads failed in the same batch. "
                  f"Cloudflare R2 often blocks bursts — try re-running with --workers 4.")

        try:
            answer = input(f"\n{TAG_INFO} {len(failed_items)} download(s) failed. Retry? [Y/n]: ").strip().lower()
        except EOFError:
            answer = "n"
        if answer not in ("", "y", "yes"):
            break
        remaining = failed_items

    if failed_items:
        print(f"{TAG_ERR} {len(failed_items)} download(s) still failed after retries.")
        return 1
    print(f"{TAG_OK} Done.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print(f"\n{TAG_INFO} Interrupted by user")
        sys.exit(130)
