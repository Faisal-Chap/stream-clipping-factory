"""Local word alignment against the start of a retained video."""

import hashlib
import json
import math
import sys
import wave
from array import array
from pathlib import Path
from statistics import median
from time import perf_counter

from . import media

RATE = 16000
BLOCK = 360
OVERLAP = 3
RECIPE = "faster-whisper-1.2.1-cpu-int8-beam2-v1"
MODELS = {
    "base.en": "Systran/faster-whisper-base.en",
    "small.en": "Systran/faster-whisper-small.en",
    "small": "Systran/faster-whisper-small",
}


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def write(path, value):
    temp = path.with_suffix(path.suffix + ".partial")
    temp.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    temp.replace(path)


def extract(root, asset):
    folder = root / "streamer-audio" / asset["hash"]
    folder.mkdir(parents=True, exist_ok=True)
    audio = folder / "audio-16k.wav"
    if audio.is_file():
        return audio
    pending = folder / "audio-16k.partial.wav"
    result = media.run([media.require("ffmpeg"), "-v", "error", "-y",
                        "-protocol_whitelist", "file", "-i", str(root / asset["path"]), "-map", "0:a:0",
                        "-vn", "-af", f"aresample={RATE}:async=1:first_pts=0",
                        "-ac", "1", "-c:a", "pcm_s16le", str(pending)], 1800)
    if result.returncode:
        raise media.MediaError("Could not extract original audio from the archive.")
    pending.replace(audio)
    return audio


def levels(audio):
    result = []
    with wave.open(str(audio), "rb") as stream:
        while data := stream.readframes(RATE):
            samples = array("h", data)
            power = sum(sample * sample for sample in samples) / len(samples)
            result.append(round(10 * math.log10(max(power, 1) / (32768**2)), 1))
    active = [level for level in result if level > -48]
    return result, median(active) if active else None


def transcribe(root, asset, config, update, coverage=None):
    import importlib.metadata
    import os

    dependencies = media.TOOLS / "streamer-asr"
    if dependencies.is_dir() and str(dependencies) not in sys.path:
        sys.path.insert(0, str(dependencies))
    try:
        import numpy as np
        from faster_whisper import WhisperModel
        from huggingface_hub import snapshot_download
    except ImportError:
        raise media.MediaError("Install S1's local ASR dependencies using the README command.") from None
    started = perf_counter()
    audio = extract(root, asset)
    digest = media.checksum(audio)
    model_name = config["asr_model"]
    language = None if model_name=="small" else "en"
    cache = Path(os.environ.get("GTA_FACTORY_MODEL_DIR",str(media.TOOLS/"streamer-whisper-models")))
    try:
        model_path = snapshot_download(MODELS[model_name], cache_dir=str(cache), local_files_only=True)
    except OSError:
        update(f"Downloading Whisper {model_name} model weights for local transcription")
        try:
            os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
            model_path = snapshot_download(MODELS[model_name], cache_dir=str(cache),
                                           allow_patterns=["model.bin", "config.json", "tokenizer.json",
                                                           "vocabulary.*", "preprocessor_config.json"])
        except Exception:
            raise media.MediaError("Could not load the Whisper model. Check model download connectivity.") from None
    revision = Path(model_path).name
    version = importlib.metadata.version("faster-whisper")
    code = hashlib.sha256(f"{RECIPE}:{version}:{model_name}:{revision}:{language or 'auto'}".encode()).hexdigest()[:20]
    folder = root / "streamer-transcripts" / digest / code
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / "transcript.json"
    if target.is_file():
        update("Reused the complete word-timed transcript")
        saved = read(target)
        if coverage:
            coverage(saved["duration"],saved["duration"])
        return target, saved
    words, new_blocks, model = [], 0, None
    with wave.open(str(audio), "rb") as stream:
        frames = stream.getnframes()
        duration = frames / RATE
        total = math.ceil(duration / BLOCK)
        for index in range(total):
            core_start, core_end = index * BLOCK, min(duration, (index + 1) * BLOCK)
            start, end = max(0, core_start - OVERLAP), min(duration, core_end + OVERLAP)
            checkpoint = folder / f"block-{index:03d}.json"
            if checkpoint.is_file():
                words.extend(read(checkpoint))
                if coverage:
                    coverage(core_end, duration)
                continue
            update(f"Transcribing audio block {index + 1}/{total} on CPU")
            if model is None:
                from .readiness import thread_limit
                model = WhisperModel(model_path, device="cpu", compute_type="int8",
                                     cpu_threads=thread_limit(config["asr_threads"]))
            stream.setpos(round(start * RATE))
            data = stream.readframes(round(end * RATE) - round(start * RATE))
            signal = np.frombuffer(data, dtype="<i2").astype(np.float32) / 32768
            segments, _ = model.transcribe(signal, language=language, multilingual=language is None, word_timestamps=True,
                                           beam_size=2, vad_filter=True,
                                           condition_on_previous_text=False)
            block = []
            for segment in segments:
                for word in segment.words or []:
                    midpoint = start + (word.start + word.end) / 2
                    if core_start <= midpoint < core_end and word.word.strip() and word.end > word.start:
                        block.append([round(start + word.start, 3), round(start + word.end, 3),
                                      word.word.strip(), round(word.probability, 3)])
            block.sort()
            write(checkpoint, block)
            words.extend(block)
            new_blocks += 1
            if coverage:
                coverage(core_end, duration)
    words.sort()
    clean = []
    for word in words:
        if clean and word[2].casefold() == clean[-1][2].casefold() and word[0] < clean[-1][1]:
            if word[3] > clean[-1][3]:
                clean[-1] = word
        else:
            clean.append(word)
    rms, baseline = levels(audio)
    result = {"audio_hash": digest, "audio_path": audio.relative_to(root).as_posix(),
              "model": model_name, "model_revision": revision, "engine": version, "language": language or "auto",
              "duration": duration, "source_duration": asset["duration"], "blocks": total,
              "new_blocks": new_blocks, "elapsed_seconds": round(perf_counter() - started, 3),
              "words": [[index, *word] for index, word in enumerate(clean, 1)],
              "rms_dbfs_by_second": rms, "active_median_dbfs": baseline}
    write(target, result)
    return target, result
