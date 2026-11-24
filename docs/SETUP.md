# Setup

## Local workspace

Run `python -m factory` from the repository root and open [Sources](http://127.0.0.1:8765/sources). Pass `--data /absolute/path` for a separate workspace. On Windows, the Factory uses Windows Credential Manager. Linux and macOS operators can set `GTA_FACTORY_CREDENTIAL_DIR` to a dedicated, user-owned directory with mode `0700` before starting.

Use FFmpeg with `libass`, `libx264`, and AAC support and keep both `ffmpeg` and `ffprobe` on PATH. The bundled portrait layouts also require a Pillow build with RAQM.

## Docker Compose on Linux

```sh
mkdir -p .local/workspace .local/credentials .local/recordings
chmod 700 .local/credentials
cp .env.example .env
```

Set `FACTORY_UID` and `FACTORY_GID` in `.env` to the output of `id -u` and `id -g`. Then run `docker compose up --build -d`. Put recordings in `.local/recordings` and add them using paths such as `/recordings/stream.mp4`. The mount is read-only. The container binds the portal to host loopback on port 8765. For a remote host, SSH-forward that port from a trusted machine.

The Docker build uses Python 3.12 and the original Linux ARM64 dependency pins. You can run natively when a platform lacks compatible wheels.

## Story selection

Add a Sail API key in **Controls → Connections**. Run the capability test for the tier you intend to use. Allow hosted analysis on an individual job or enable it as the default after reviewing the provider permissions. The operator sets the spend cap in Controls.

## Completed YouTube replays

Local recordings are the simplest way to get started. Remote verification and download support depend on the source and your provider account. Twitch and Kick links can be saved for tracking.

- **FlashData and R2** use `GTA_FACTORY_YOUTUBE_PROVIDER=flashdata` for discovery and bounded media acquisition. Docker Compose reads the equivalent `YOUTUBE_PROVIDER=flashdata` from `.env`. The vault entry `flashdata-r2` holds a JSON object with `api_key`, `access_key`, `secret_key`, `endpoint`, and `bucket`. Use a dedicated bucket or scoped credentials. Verified imported objects are cleaned up under the Factory prefix.
- **RapidAPI full replays** reuse one server-side copy for hourly processing. Enable `GTA_FACTORY_FULL_REPLAY=1` on native installs or set `FULL_REPLAY=1` in Docker Compose. Use the RapidAPI marketplace key for `youtube-info-download-api.p.rapidapi.com`. The vault reference is `video-download-rapidapi`. FlashData can still verify completed replay metadata.
- **Local yt-dlp** is an optional YouTube adapter for native installs. Install `yt-dlp[default]` in the active Python environment and put a supported Node.js runtime on PATH. Use a provider or an owned local recording when the source cannot be accessed this way.

For native installs, provision replay keys without placing them in source files or command history. Use the same absolute data directory supplied to `--data`. In a Compose container, run the Python lines through an interactive `docker compose exec operator python` session and use `Path("/data")`.

```python
from getpass import getpass
from pathlib import Path
from factory.provider_vault import vault_for

vault = vault_for(Path("/absolute/factory/workspace"))
vault.put("video-download-rapidapi", getpass("RapidAPI key: "))
```

For FlashData, use the same `vault.put` call with reference `flashdata-r2` and enter the JSON credential object at the hidden prompt. Keep provider experiments separate from the working workspace.

## Publishing and cleanup

Set `GTA_FACTORY_INDIEPOST_WORKSPACE` to the exact name of your workspace before starting a native install. Compose uses `INDIEPOST_WORKSPACE` in `.env`. Add your IndiePost key in Controls and test the connection. Select destinations and visibility, grant the creator’s destination permissions, and enable Factory dispatch when ready.

The Storage scheduler controls automatic refill and the gap between new reels. The release worker tracks each account separately so a reel sent to four accounts counts as one reel and four posts. Publishing uses the configured local timezone and daily caps. Auto cleanup only applies to eligible Factory-owned files. Use the portal controls to review timing and keep the originals elsewhere if needed.
