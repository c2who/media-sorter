# Media Sorter

Automatically extracts and organizes downloaded media files into a folder structure compatible with media servers like Jellyfin, Plex, or Emby.

## Features

- Extracts RAR, ZIP, and 7z archives (including multi-part and password-protected)
- Classifies files as movies or series based on `SxxExx` patterns
- Fuzzy-matches series names against existing folders
- Manual series name mapping via JSON config
- Multi-threaded RAR extraction
- Webhook endpoint for instant processing (e.g. triggered by JDownloader)
- Periodic polling as fallback
- Concurrent request handling with requeue support

## How It Works

1. Monitors a download directory for completed downloads (no `.part` files, files older than configured threshold)
2. Finds and extracts archives, trying passwords from a list if needed
3. Identifies video files (skipping samples)
4. **Movies** (no `SxxExx` pattern) go to the movies directory
5. **Series** (contains `SxxExx`) go to the series directory, matched by name:
   - Exact match in `series_map.json`
   - Fuzzy match against existing folders
   - Auto-creates a new folder if no match found
6. Cleans up the source download folder after successful processing

## Quick Start

### Docker Compose (recommended)

```yaml
services:
  media-sorter:
    build: .
    container_name: media-sorter
    restart: unless-stopped
    user: "1000:1000"  # match your host user
    env_file: .env
    volumes:
      - "/path/to/media:/media"               # movies, series, and downloads
      - "./series_map.json:/app/series_map.json"
      - "./passwords.txt:/app/passwords.txt"
    networks:
      - your_network
```

All directories (downloads, movies, series) should be under the same mount so files can be moved instantly via rename instead of copy.

### Standalone

```bash
# Install unrar (RARLAB version, not unrar-free)
# See https://www.rarlab.com/rar_add.htm

# Configure
cp .env.example .env
# Edit .env with your paths

# Run once
python3 media_sorter.py

# Run as daemon
python3 media_sorter.py --daemon
```

## Configuration

Copy `.env.example` to `.env` and adjust:

| Variable | Default | Description |
|---|---|---|
| `DOWNLOAD_DIR` | `/media/downloads` | Where your download client saves files |
| `MOVIES_DIR` | `/media/movies` | Destination for movies |
| `SERIES_DIR` | `/media/series` | Destination for series |
| `SERIES_MAP_FILE` | `/app/series_map.json` | Path to series name mapping |
| `PASSWORDS_FILE` | `/app/passwords.txt` | Path to archive passwords |
| `MIN_AGE_SECONDS` | `300` | Min file age before processing (seconds) |
| `WEBHOOK_PORT` | `8765` | HTTP webhook port |
| `POLL_INTERVAL` | `300` | Polling interval (seconds) |
| `FUZZY_THRESHOLD` | `0.6` | Series name match threshold (0.0-1.0) |
| `UNRAR_THREADS` | `8` | Threads for RAR extraction |
| `LOG_LEVEL` | `INFO` | Logging level |

### Series Name Mapping

Create `series_map.json` for series whose download names don't match your folder names:

```json
{
    "percy jackson": "Percy-Jackson-Die-Serie",
    "the office us": "The-Office"
}
```

Keys are lowercase parsed series names (text before `SxxExx`, dots/underscores replaced with spaces).

### Passwords

Create `passwords.txt` with one password per line. Passwords are tested without extracting (fast), then the correct one is cached for remaining archives in the same folder.

## JDownloader Integration

Add this script in JDownloader's Event Scripter with trigger **"Package Finished"**:

```javascript
getPage("http://media-sorter:8765/process");
```

Replace `media-sorter` with the container name or IP. This triggers immediate processing, skipping the file age check (since JDownloader confirms the download is complete).

## Requirements

- Python 3.10+
- [RARLAB unrar](https://www.rarlab.com/rar_add.htm) (not `unrar-free`)
- `unzip` (for ZIP archives, optional)
- `7z` (for 7z archives, optional)
