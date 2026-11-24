# Stream Clipping Factory

Turn long streams into captioned vertical reels from one self-hosted workspace.

## Demo

<!-- Paste the demo video’s public URL on its own line here. -->

## How it works

Add a recording or completed replay in **Sources**. The Factory processes it in source-hour jobs, finds standalone stories, and turns them into vertical clips you can play in the portal. Review and publish the reels yourself, or enable automatic publishing.

| Step | What happens |
| --- | --- |
| 1. Add footage | Use a local recording or an eligible completed YouTube replay. RapidAPI full-replay mode caches the stream once so later hours can reuse the same file. FlashData with R2 is available for bounded audio, preview, and clip requests. |
| 2. Transcribe locally | FFmpeg prepares the audio and Faster-Whisper transcribes it on your machine or server. The Factory keeps the original word timings for precise cuts and captions. Cached recordings supply their own audio. |
| 3. Find stories with DeepSeek | The Factory sends a compact transcript for each source hour to DeepSeek through Sail. DeepSeek proposes complete 25–150 second stories with timestamps, word references, a short on-screen hook, and a separate publishing title. The Factory validates the cuts, checks the evidence, and removes duplicates. An hour can yield zero reels. |
| 4. Confirm the cut | The Factory makes a JPEG storyboard around each selected moment and sends it with timing context through Sail to refine the start and end. The hook and publishing title come from the story pass. |
| 5. Render and review | The editor cuts the footage into portrait reels with original audio, timed captions, and a center hook. It can detect a webcam box for supported layouts. In **Jobs**, you can follow progress, inspect the transcript and model replies, and play finished clips. |
| 6. Publish with IndiePost | IndiePost is the optional publishing bridge. It takes the finished reel and title, sends them to your connected social accounts, and reports delivery per account. You manage destinations, spacing, caps, and visibility in the Factory. |

Hosted analysis requires operator approval. Sail receives transcript text and selected JPEG storyboards. Transcription runs locally. If you enable IndiePost, it receives the finished reels you send for publishing.

## What you control

| Page | Controls |
| --- | --- |
| **Sources** | Add, edit, and start sources. See automation readiness and separate daily and monthly totals for unique reels and platform posts. |
| **Jobs** | Follow download, transcription, and render progress. Pause or resume a run or an entire cached replay, expand logs and available evidence, and play outputs. |
| **Factory → Done** | Review rendered reels and see delivery status for each destination. |
| **Edit templates** | Preview and choose bundled layouts for the hook, captions, gameplay, and webcam. |
| **Storage** | Open saved files, check disk usage, set the low and high buffer thresholds, adjust the publishing gap, and manage cleanup. |
| **Controls** | Test connections, set the model budget and local processing options, and choose publishing accounts, daily limits, and visibility. |

Automatic refill starts eligible source work when the ready buffer is low and checks the buffer again after the current work finishes. The publishing queue spaces new reels and tracks confirmed deliveries per account. You can enable cleanup of fully delivered portraits after a grace period.

Cached full replays have a separate expiry countdown. You can extend the cache lifetime from the source or job view while the recording is still available.

## Run on your computer

Install **Python 3.12** and **FFmpeg** with `ffprobe`, `libx264`, AAC, and `libass`. Keep `ffmpeg` and `ffprobe` on your PATH.

```sh
git clone https://github.com/Faisal-Chap/stream-clipping-factory.git
cd stream-clipping-factory
python -m venv .venv
```

On Windows, run these commands in PowerShell.

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m factory --open
```

On Linux or macOS, use the virtual environment’s `bin` directory.

```sh
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m factory --open
```

Open [Sources](http://127.0.0.1:8765/sources). The first transcription may download the local Whisper model.

## Deploy on a Linux server

Install Git and Docker with the Compose plugin. Clone the repository as shown above, then run these commands in the checkout.

```sh
mkdir -p .local/workspace .local/credentials .local/recordings
chmod 700 .local/credentials
cp .env.example .env
```

Edit `.env` and set `FACTORY_UID` and `FACTORY_GID` to the outputs of `id -u` and `id -g`. Start the service.

```sh
docker compose up --build -d
docker compose logs --tail=50 operator
```

The portal binds to the server’s loopback on port `8765`. From your computer, open an SSH tunnel and visit [Sources](http://127.0.0.1:8765/sources).

```sh
ssh -N -L 8765:127.0.0.1:8765 user@your-server
```

Put a recording in `.local/recordings` on the server and enter its container path, such as `/recordings/stream.mp4`, in Sources. Workspace and credentials persist on the host. Keep the portal behind the tunnel.

## Start your first job

1. In **Controls → Connections**, add your Sail key and test DeepSeek text and storyboard access. Set a spending cap.
2. In **Sources**, add an absolute path to a recording you’re allowed to use. Start the job and allow hosted analysis.
3. Open **Jobs**. Follow transcription, story selection, and rendering, then play the finished reels.
4. To publish, set your IndiePost workspace as shown in the setup guide, add its key under **Controls → Connections**, and select your accounts. Review the limit and spacing settings before enabling automatic publishing. Auto publishing and portrait cleanup start off.

Recording paths, replay providers, and vault setup are covered in the [setup guide](docs/SETUP.md). For an AI-assisted setup, share this README and the setup guide. Tell your assistant your operating system, whether you want native Python or Docker, and whether you’ll start with a local recording or a replay. Keep API keys and private media in your own environment.

## Development

Run `python -m unittest discover -s tests` from the repository root. The suite uses synthetic media and mocked provider calls.

The application is [MIT-licensed](LICENSE). Bundled fonts retain their [SIL Open Font License notices](docs/videoedit/fonts/manifest.v1.json). See [Security](SECURITY.md) for deployment and reporting guidance.
