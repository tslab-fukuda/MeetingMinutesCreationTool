from __future__ import annotations

import argparse
import math
import os
import queue
import sys
import threading
import time
import wave
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import numpy as np
import sounddevice as sd

from transcribe_audio import load_api_key

try:
    from faster_whisper import WhisperModel
except ImportError:
    WhisperModel = None

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None


@dataclass
class SegmentTask:
    index: int
    start_sec: float
    end_sec: float
    path: Path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Record a meeting while monitoring transcription."
    )
    parser.add_argument("--device", type=int, help="Input device id.")
    parser.add_argument("--samplerate", type=int, default=16000)
    parser.add_argument("--channels", type=int, default=1)
    parser.add_argument("--prefix", default="meeting_session")
    parser.add_argument("--output", help="Master WAV path.")
    parser.add_argument("--segment-seconds", type=float, default=20.0)
    parser.add_argument("--status-seconds", type=float, default=5.0)
    parser.add_argument("--warn-rms", type=float, default=120.0)
    parser.add_argument(
        "--seconds",
        type=float,
        help="Optional fixed duration for test runs.",
    )
    parser.add_argument(
        "--transcriber",
        choices=["local", "api", "none"],
        default="local",
        help="How to transcribe saved chunks. Default: local",
    )
    parser.add_argument("--local-model", default="tiny")
    parser.add_argument(
        "--api-model",
        default="gpt-4o-mini-transcribe",
        choices=["gpt-4o-mini-transcribe", "gpt-4o-transcribe", "whisper-1"],
    )
    parser.add_argument("--prompt", help="Optional transcription hint.")
    parser.add_argument("--language", help="Optional language hint such as ja.")
    parser.add_argument(
        "--keep-segments",
        action="store_true",
        help="Keep chunk WAV files after transcription.",
    )
    parser.add_argument(
        "--list-devices",
        action="store_true",
        help="Show available audio devices and exit.",
    )
    return parser


def sanitize_name(name: str) -> str:
    return "".join(ch if 32 <= ord(ch) < 127 else "?" for ch in name)


def list_devices() -> int:
    devices = sd.query_devices()
    default_input, default_output = sd.default.device
    print("Audio devices:")
    for idx, device in enumerate(devices):
        labels = []
        if idx == default_input:
            labels.append("default-input")
        if idx == default_output:
            labels.append("default-output")
        label_text = f" [{' '.join(labels)}]" if labels else ""
        name = sanitize_name(str(device["name"]))
        print(
            f"{idx:>2}: {name}{label_text} | "
            f"in={device['max_input_channels']} out={device['max_output_channels']} "
            f"default_sr={device['default_samplerate']}"
        )
    return 0


def choose_session_paths(prefix: str, output: str | None) -> tuple[Path, Path, Path]:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    stem = f"{prefix}_{stamp}"
    master_path = Path(output) if output else Path("recordings") / f"{stem}.wav"
    transcript_path = Path("transcripts") / f"{stem}.live.txt"
    segments_dir = Path("recordings") / "segments" / stem
    master_path.parent.mkdir(parents=True, exist_ok=True)
    transcript_path.parent.mkdir(parents=True, exist_ok=True)
    segments_dir.mkdir(parents=True, exist_ok=True)
    return master_path, transcript_path, segments_dir


def write_wav(path: Path, samplerate: int, channels: int, audio: np.ndarray) -> None:
    with wave.open(str(path), "wb") as wav_file:
        wav_file.setnchannels(channels)
        wav_file.setsampwidth(2)
        wav_file.setframerate(samplerate)
        wav_file.writeframes(audio.tobytes())


def rms_value(audio: np.ndarray) -> float:
    if audio.size == 0:
        return 0.0
    squared = np.square(audio.astype(np.float32))
    return math.sqrt(float(np.mean(squared)))


def format_hms(seconds: float) -> str:
    total = int(seconds)
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


class LocalTranscriber:
    def __init__(self, model_name: str, language: str | None, prompt: str | None) -> None:
        if WhisperModel is None:
            raise RuntimeError(
                "Local transcription requires faster-whisper. "
                "Run 'python -m pip install faster-whisper'."
            )
        self.model = WhisperModel(model_name, device="cpu", compute_type="int8")
        self.language = language
        self.prompt = prompt

    def transcribe(self, audio_path: Path) -> str:
        segments, _ = self.model.transcribe(
            str(audio_path),
            language=self.language,
            initial_prompt=self.prompt,
            vad_filter=True,
        )
        return " ".join(segment.text.strip() for segment in segments).strip()


class ApiTranscriber:
    def __init__(self, model_name: str, language: str | None, prompt: str | None) -> None:
        if OpenAI is None:
            raise RuntimeError(
                "API transcription requires the openai package. "
                "Run 'python -m pip install openai'."
            )
        api_key = load_api_key()
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY is not set.")
        self.client = OpenAI(api_key=api_key)
        self.model_name = model_name
        self.language = language
        self.prompt = prompt

    def transcribe(self, audio_path: Path) -> str:
        request_args = {"model": self.model_name, "response_format": "json"}
        if self.language:
            request_args["language"] = self.language
        if self.prompt:
            request_args["prompt"] = self.prompt

        with audio_path.open("rb") as audio_file:
            response = self.client.audio.transcriptions.create(
                file=audio_file,
                **request_args,
            )

        text = getattr(response, "text", None)
        if text is not None:
            return text.strip()
        if hasattr(response, "model_dump"):
            return str(response.model_dump().get("text", "")).strip()
        return str(response).strip()


def build_transcriber(args: argparse.Namespace):
    if args.transcriber == "none":
        return None
    if args.transcriber == "local":
        return LocalTranscriber(args.local_model, args.language, args.prompt)
    return ApiTranscriber(args.api_model, args.language, args.prompt)


def transcription_worker(
    task_queue: queue.Queue[SegmentTask | None],
    transcript_path: Path,
    transcriber,
    keep_segments: bool,
    write_lock: threading.Lock,
) -> None:
    while True:
        task = task_queue.get()
        if task is None:
            task_queue.task_done()
            return

        try:
            transcript = transcriber.transcribe(task.path) if transcriber else ""
        except Exception as exc:
            transcript = f"[transcription failed: {exc}]"

        header = (
            f"[segment {task.index:03d} {format_hms(task.start_sec)}"
            f" - {format_hms(task.end_sec)}]"
        )
        preview = transcript[:120].replace("\n", " ") if transcript else "no speech"
        with write_lock:
            with transcript_path.open("a", encoding="utf-8") as fh:
                fh.write(f"{header}\n{transcript}\n\n")
        print(f"[transcript] {header} {preview}")

        if not keep_segments and task.path.exists():
            try:
                task.path.unlink()
            except OSError:
                pass

        task_queue.task_done()


def validate_args(args: argparse.Namespace) -> int:
    if args.segment_seconds <= 0:
        print("--segment-seconds must be greater than 0.", file=sys.stderr)
        return 2
    if args.seconds is not None and args.seconds <= 0:
        print("--seconds must be greater than 0.", file=sys.stderr)
        return 2
    if args.status_seconds <= 0:
        print("--status-seconds must be greater than 0.", file=sys.stderr)
        return 2
    if args.samplerate <= 0 or args.channels <= 0:
        print("--samplerate and --channels must be greater than 0.", file=sys.stderr)
        return 2
    return 0


def main() -> int:
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    parser = build_parser()
    args = parser.parse_args()

    validation_error = validate_args(args)
    if validation_error:
        return validation_error

    if args.list_devices:
        return list_devices()

    try:
        transcriber = build_transcriber(args)
    except Exception as exc:
        print(f"Failed to initialize transcriber: {exc}", file=sys.stderr)
        return 1

    master_path, transcript_path, segments_dir = choose_session_paths(
        args.prefix,
        args.output,
    )
    audio_queue: queue.Queue[np.ndarray] = queue.Queue()
    task_queue: queue.Queue[SegmentTask | None] = queue.Queue()
    write_lock = threading.Lock()
    worker = threading.Thread(
        target=transcription_worker,
        args=(task_queue, transcript_path, transcriber, args.keep_segments, write_lock),
        daemon=True,
    )
    worker.start()

    total_frames = 0
    segment_frames = 0
    segment_index = 1
    silence_warnings = 0
    segment_audio: list[np.ndarray] = []
    segment_target_frames = int(args.segment_seconds * args.samplerate)
    started_at = time.time()
    last_status_time = started_at

    def callback(indata: np.ndarray, frames: int, time_info, status) -> None:
        if status:
            print(f"[audio-status] {status}", file=sys.stderr)
        audio_queue.put(indata.copy())

    print(f"Master recording: {master_path}")
    print(f"Live transcript: {transcript_path}")
    print(
        f"Monitoring with transcriber={args.transcriber}, "
        f"segment={args.segment_seconds:.1f}s"
    )
    print("Recording... Press Ctrl+C to stop.")

    try:
        with wave.open(str(master_path), "wb") as wav_file:
            wav_file.setnchannels(args.channels)
            wav_file.setsampwidth(2)
            wav_file.setframerate(args.samplerate)

            with sd.InputStream(
                samplerate=args.samplerate,
                channels=args.channels,
                dtype="int16",
                callback=callback,
                device=args.device,
            ):
                while True:
                    chunk = audio_queue.get(timeout=0.5)
                    wav_file.writeframes(chunk.tobytes())
                    total_frames += len(chunk)
                    segment_frames += len(chunk)
                    segment_audio.append(chunk)
                    audio_queue.task_done()

                    now = time.time()
                    if now - last_status_time >= args.status_seconds:
                        elapsed = total_frames / args.samplerate
                        current_rms = rms_value(chunk)
                        file_size_mb = master_path.stat().st_size / (1024 * 1024)
                        line = (
                            f"[status] elapsed={format_hms(elapsed)} "
                            f"size={file_size_mb:.2f}MB rms={current_rms:.1f}"
                        )
                        if current_rms < args.warn_rms:
                            silence_warnings += 1
                            line += " warning=low-input"
                        else:
                            silence_warnings = 0
                        if silence_warnings >= 3:
                            line += " check microphone or input device"
                        print(line)
                        last_status_time = now

                    if segment_frames >= segment_target_frames:
                        merged = np.concatenate(segment_audio, axis=0)
                        segment_end = total_frames / args.samplerate
                        segment_start = max(0.0, segment_end - (len(merged) / args.samplerate))
                        segment_path = segments_dir / f"segment_{segment_index:03d}.wav"
                        write_wav(segment_path, args.samplerate, args.channels, merged)
                        task_queue.put(
                            SegmentTask(
                                index=segment_index,
                                start_sec=segment_start,
                                end_sec=segment_end,
                                path=segment_path,
                            )
                        )
                        print(
                            f"[segment] saved {segment_path.name} "
                            f"({segment_end - segment_start:.1f}s)"
                        )
                        segment_index += 1
                        segment_frames = 0
                        segment_audio = []

                    if args.seconds is not None:
                        elapsed = total_frames / args.samplerate
                        if elapsed >= args.seconds:
                            print("Reached requested duration.")
                            break
    except KeyboardInterrupt:
        print("\nStopping recording.")
    except Exception as exc:
        print(f"Recording failed: {exc}", file=sys.stderr)
        return 1
    finally:
        while True:
            try:
                chunk = audio_queue.get_nowait()
            except queue.Empty:
                break
            total_frames += len(chunk)
            segment_frames += len(chunk)
            segment_audio.append(chunk)
            audio_queue.task_done()

        if segment_audio:
            merged = np.concatenate(segment_audio, axis=0)
            segment_end = total_frames / args.samplerate
            segment_start = max(0.0, segment_end - (len(merged) / args.samplerate))
            segment_path = segments_dir / f"segment_{segment_index:03d}.wav"
            write_wav(segment_path, args.samplerate, args.channels, merged)
            task_queue.put(
                SegmentTask(
                    index=segment_index,
                    start_sec=segment_start,
                    end_sec=segment_end,
                    path=segment_path,
                )
            )
            print(
                f"[segment] saved {segment_path.name} "
                f"({segment_end - segment_start:.1f}s)"
            )

        task_queue.put(None)
        task_queue.join()
        worker.join(timeout=1.0)

    duration = total_frames / args.samplerate if args.samplerate else 0.0
    print(f"Saved recording to: {master_path}")
    print(f"Duration: {duration:.2f} seconds")
    if args.transcriber != "none":
        print(f"Live transcript saved to: {transcript_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
