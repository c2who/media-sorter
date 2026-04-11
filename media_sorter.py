#!/usr/bin/env python3
"""
Media Sorter — Automatically extracts and organizes downloaded media files.

Monitors a download directory for completed archives, extracts them,
and sorts video files into movie and series folders for media servers
like Jellyfin, Plex, or Emby.
"""

import argparse
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from difflib import SequenceMatcher
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

# ── Config (all configurable via environment variables) ─────────────────────────

DOWNLOAD_DIR = Path(os.environ.get("DOWNLOAD_DIR", "/media/downloads"))
MOVIES_DIR = Path(os.environ.get("MOVIES_DIR", "/media/movies"))
SERIES_DIR = Path(os.environ.get("SERIES_DIR", "/media/series"))
SERIES_MAP_FILE = Path(os.environ.get("SERIES_MAP_FILE", "/app/series_map.json"))
PASSWORDS_FILE = Path(os.environ.get("PASSWORDS_FILE", "/app/passwords.txt"))

MIN_AGE_SECONDS = int(os.environ.get("MIN_AGE_SECONDS", "300"))
WEBHOOK_PORT = int(os.environ.get("WEBHOOK_PORT", "8765"))
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "300"))
FUZZY_THRESHOLD = float(os.environ.get("FUZZY_THRESHOLD", "0.6"))
UNRAR_THREADS = os.environ.get("UNRAR_THREADS", "8")

VIDEO_EXTENSIONS = {".mkv", ".mp4", ".avi", ".mov", ".wmv", ".flv", ".webm", ".m4v", ".ts"}
SERIES_PATTERN = re.compile(r"[Ss]\d{2}[Ee]\d{2}")
SERIES_NAME_PATTERN = re.compile(r"^(.*?)[.\s_-]+[Ss]\d{2}[Ee]\d{2}")

_processing_lock = threading.Lock()
_reprocess_flag = threading.Event()

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("media_sorter")

# ── Helpers ─────────────────────────────────────────────────────────────────────


def load_series_map() -> dict[str, str]:
    if SERIES_MAP_FILE.exists():
        with open(SERIES_MAP_FILE) as f:
            return json.load(f)
    return {}


def load_passwords() -> list[str]:
    if PASSWORDS_FILE.exists():
        with open(PASSWORDS_FILE) as f:
            return [line.strip() for line in f if line.strip()]
    return []


def is_download_complete(folder: Path, skip_age_check: bool = False) -> bool:
    now = time.time()
    for entry in folder.rglob("*"):
        if entry.suffix == ".part":
            log.debug("Skipping %s — .part file found: %s", folder.name, entry.name)
            return False
        if not skip_age_check and entry.is_file() and (now - entry.stat().st_mtime) < MIN_AGE_SECONDS:
            log.debug("Skipping %s — file too recent: %s", folder.name, entry.name)
            return False
    return True


def find_archives(folder: Path) -> list[Path]:
    """Find all distinct archives in a folder (one per multi-part set)."""
    archives = []

    # RAR: find all .part1.rar or standalone .rar files
    rar_files = sorted(folder.glob("*.rar"))
    if rar_files:
        part1_files = [c for c in rar_files if re.search(r"\.part0*1\.rar$", c.name)]
        if part1_files:
            archives.extend(part1_files)
        else:
            archives.extend(rar_files)

    # ZIP and 7z
    for pattern in ("*.zip", "*.7z"):
        archives.extend(sorted(folder.glob(pattern)))

    return archives


def archive_cleanup_targets(archive: Path) -> list[Path]:
    """Return the archive files that can be deleted after successful extraction."""
    if archive.suffix.lower() != ".rar":
        return [archive]

    match = re.match(r"^(?P<base>.+)\.part\d+\.rar$", archive.name, re.IGNORECASE)
    if not match:
        return [archive]

    pattern = f"{match.group('base')}.part*.rar"
    return sorted(p for p in archive.parent.glob(pattern) if p.is_file())


def cleanup_extracted_archives(archives: list[Path]):
    """Delete only archive files that were successfully extracted."""
    cleanup_targets: set[Path] = set()
    for archive in archives:
        cleanup_targets.update(archive_cleanup_targets(archive))

    for target in sorted(cleanup_targets):
        try:
            target.unlink()
            log.info("Removed extracted archive: %s", target.name)
        except FileNotFoundError:
            continue


def _test_password(archive: Path, password: str | None = None) -> tuple[bool, str]:
    """Test if a password works without extracting (fast)."""
    ext = archive.suffix.lower()
    if ext == ".rar":
        cmd = ["unrar", "t", "-y"]
        if password:
            cmd.append(f"-p{password}")
        else:
            cmd.append("-p-")
        cmd.append(str(archive))
    elif ext == ".zip":
        cmd = ["unzip", "-t"]
        if password:
            cmd += ["-P", password]
        cmd.append(str(archive))
    elif ext == ".7z":
        cmd = ["7z", "t", str(archive), "-y"]
        if password:
            cmd.append(f"-p{password}")
    else:
        return False, f"Unknown format: {ext}"

    result = subprocess.run(cmd, capture_output=True, text=True)
    output = (result.stdout + result.stderr).strip()
    return result.returncode == 0, output


def _run_extract(archive: Path, dest: Path, password: str | None = None) -> tuple[bool, str]:
    """Extract archive with optional password and multithreading."""
    ext = archive.suffix.lower()
    if ext == ".rar":
        cmd = ["unrar", "x", "-o+", "-y", f"-mt{UNRAR_THREADS}"]
        if password:
            cmd.append(f"-p{password}")
        else:
            cmd.append("-p-")
        cmd += [str(archive), str(dest) + "/"]
    elif ext == ".zip":
        cmd = ["unzip", "-o"]
        if password:
            cmd += ["-P", password]
        cmd += [str(archive), "-d", str(dest)]
    elif ext == ".7z":
        cmd = ["7z", "x", str(archive), f"-o{dest}", "-y", "-mmt=on"]
        if password:
            cmd.append(f"-p{password}")
    else:
        return False, f"Unknown format: {ext}"

    result = subprocess.run(cmd, capture_output=True, text=True)
    output = (result.stdout + result.stderr).strip()
    return result.returncode == 0, output


def _find_password(archive: Path) -> str | None:
    """Find the correct password by testing without extracting."""
    success, _ = _test_password(archive)
    if success:
        return ""

    passwords = load_passwords()
    for pw in passwords:
        success, _ = _test_password(archive, pw)
        if success:
            log.info("Found correct password: %s", pw[:2] + "***")
            return pw

    return None


def extract_archive(archive: Path, dest: Path, cached_password: str | None = None) -> tuple[bool, str | None]:
    """Extract archive. Returns (success, password_used)."""
    try:
        # Try cached password first
        if cached_password is not None:
            success, output = _run_extract(archive, dest, cached_password or None)
            if success:
                log.info("Extraction complete: %s", archive.name)
                return True, cached_password

        # Full password search
        password = _find_password(archive)
        if password is None:
            log.error("No valid password found for %s (tried %d passwords)", archive, len(load_passwords()))
            return False, None

        pw = password if password else None
        success, output = _run_extract(archive, dest, pw)
        if success:
            log.info("Extraction complete: %s", archive.name)
            return True, password

        log.error("Extraction failed for %s: %s", archive, output[-300:])
        return False, None
    except FileNotFoundError as e:
        log.error("Extraction tool not found: %s", e)
        return False, None


def find_video_files(folder: Path) -> list[Path]:
    return [
        f for f in folder.rglob("*")
        if f.is_file()
        and f.suffix.lower() in VIDEO_EXTENSIONS
        and "sample" not in f.stem.lower()
    ]


def parse_series_name(filename: str) -> str | None:
    match = SERIES_NAME_PATTERN.match(filename)
    if not match:
        return None
    return match.group(1).replace(".", " ").replace("_", " ").strip()


def resolve_series_folder(parsed_name: str, series_map: dict[str, str]) -> Path:
    key = parsed_name.lower()

    # 1. Manual mapping
    if key in series_map:
        folder = SERIES_DIR / series_map[key]
        log.info("Series mapped: '%s' -> '%s'", parsed_name, series_map[key])
        return folder

    # 2. Fuzzy match against existing folders
    existing = [d.name for d in SERIES_DIR.iterdir() if d.is_dir()] if SERIES_DIR.exists() else []
    best_match = None
    best_score = 0.0
    for folder_name in existing:
        score = SequenceMatcher(None, key, folder_name.lower().replace("-", " ")).ratio()
        if score > best_score:
            best_score = score
            best_match = folder_name

    if best_match and best_score >= FUZZY_THRESHOLD:
        log.info("Series fuzzy matched: '%s' -> '%s' (score: %.2f)", parsed_name, best_match, best_score)
        return SERIES_DIR / best_match

    # 3. Auto-create from parsed name (capitalize words, use dashes)
    folder_name = "-".join(word.capitalize() for word in parsed_name.split())
    log.info("Series new folder: '%s'", folder_name)
    return SERIES_DIR / folder_name


def move_file(src: Path, dest: Path):
    """Move file, preferring rename (instant on same filesystem)."""
    os.rename(str(src), str(dest))


def process_folder(folder: Path, series_map: dict[str, str]) -> bool:
    log.info("Processing: %s", folder.name)

    archives = find_archives(folder)
    if not archives:
        log.warning("No archive found in %s, skipping", folder.name)
        return False

    extract_dir = folder / "extracted"
    extract_dir.mkdir(exist_ok=True)

    # Extract all archives, caching password from first success
    cached_password = None
    had_errors = False
    extracted_archives = []
    for archive in archives:
        log.info("Extracting: %s -> %s", archive.name, extract_dir)
        success, password = extract_archive(archive, extract_dir, cached_password)
        if success:
            cached_password = password
            extracted_archives.append(archive)
        else:
            log.error("Failed to extract %s, skipping", archive.name)
            had_errors = True

    videos = find_video_files(extract_dir)
    if not videos:
        log.warning("No video files found after extraction in %s", folder.name)
        return False

    for video in videos:
        filename = video.name

        if SERIES_PATTERN.search(filename):
            parsed_name = parse_series_name(filename)
            if not parsed_name:
                log.warning("Could not parse series name from: %s", filename)
                continue
            dest_dir = resolve_series_folder(parsed_name, series_map)
            dest_dir.mkdir(parents=True, exist_ok=True)
        else:
            dest_dir = MOVIES_DIR
            dest_dir.mkdir(parents=True, exist_ok=True)

        dest_file = dest_dir / filename
        if dest_file.exists():
            log.warning("Destination already exists, skipping: %s", dest_file)
            continue

        move_file(video, dest_file)
        log.info("Moved: %s -> %s", filename, dest_dir)

    if had_errors:
        log.warning("Some extractions failed, keeping source folder: %s", folder.name)
        return False

    cleanup_extracted_archives(extracted_archives)

    if extract_dir.exists():
        shutil.rmtree(extract_dir)

    remaining_archives = find_archives(folder)
    remaining_files = [entry for entry in folder.iterdir()]

    if remaining_archives:
        log.info("Keeping folder with remaining archives: %s", folder.name)
        return True

    if not remaining_files:
        folder.rmdir()
        log.info("Cleaned up empty folder: %s", folder.name)
    else:
        log.info("Keeping folder with remaining files: %s", folder.name)
    return True


# ── Processing modes ────────────────────────────────────────────────────────────


def process_webhook():
    """Webhook-triggered processing with requeue support."""
    if not _processing_lock.acquire(blocking=False):
        log.info("Already processing, queuing reprocess")
        _reprocess_flag.set()
        return
    try:
        _run_process(skip_age_check=True)
        while _reprocess_flag.is_set():
            _reprocess_flag.clear()
            log.info("Reprocessing (webhook received during previous run)")
            _run_process(skip_age_check=True)
    finally:
        _processing_lock.release()


def process_poll():
    """Poll-triggered processing, skips if already running."""
    if not _processing_lock.acquire(blocking=False):
        log.info("Already processing, skipping poll")
        return
    try:
        _run_process(skip_age_check=False)
    finally:
        _processing_lock.release()


def _run_process(skip_age_check: bool = False):
    if not DOWNLOAD_DIR.exists():
        log.error("Download directory does not exist: %s", DOWNLOAD_DIR)
        return

    series_map = load_series_map()
    folders = [d for d in DOWNLOAD_DIR.iterdir() if d.is_dir()]

    if not folders:
        log.info("No download folders found")
        return

    for folder in folders:
        if not is_download_complete(folder, skip_age_check):
            log.info("Not ready: %s", folder.name)
            continue
        process_folder(folder, series_map)


# ── Webhook server ──────────────────────────────────────────────────────────────


class WebhookHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/process":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"OK\n")
            log.info("Webhook triggered, processing downloads...")
            threading.Thread(target=process_webhook, daemon=True).start()
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass


def run_daemon():
    """Run as daemon: webhook server + periodic polling."""
    server = HTTPServer(("0.0.0.0", WEBHOOK_PORT), WebhookHandler)
    log.info("Webhook listening on http://0.0.0.0:%d/process", WEBHOOK_PORT)

    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    while True:
        process_poll()
        log.info("Next poll in %d seconds", POLL_INTERVAL)
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Media sorter for download automation")
    parser.add_argument("--daemon", action="store_true", help="Run as daemon with webhook server and periodic polling")
    args = parser.parse_args()

    if args.daemon:
        run_daemon()
    else:
        process_poll()
