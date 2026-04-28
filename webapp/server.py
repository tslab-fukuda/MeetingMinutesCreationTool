from __future__ import annotations

import math
import os
import queue
import re
import shutil
import subprocess
import threading
import time
import wave
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import sounddevice as sd
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from tools.config import load_local_settings
from tools.transcribe_audio import load_api_key

try:
    from faster_whisper import WhisperModel
except ImportError:
    WhisperModel = None

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None


ROOT_DIR = Path(__file__).resolve().parents[1]
STATIC_DIR = ROOT_DIR / "webapp" / "static"
DEFAULT_DOCUMENT = ROOT_DIR / "Texテンプレート2026" / "tmplate.tex"
AUTO_START = "% AUTO-TRANSCRIPT-START"
AUTO_END = "% AUTO-TRANSCRIPT-END"
AUTO_HEADER_LINES = [
    r"\section*{自動文字起こしメモ}",
    r"\begin{itemize}",
]
AUTO_FOOTER_LINES = [r"\end{itemize}"]
AUTO_EMPTY_ITEM = r"\item[]"

load_local_settings(ROOT_DIR)


def detect_encoding(path: Path) -> str:
    data = path.read_bytes()
    for encoding in ("utf-8", "cp932", "shift_jis"):
        try:
            data.decode(encoding)
            return encoding
        except UnicodeDecodeError:
            continue
    return "utf-8"


def read_text_with_encoding(path: Path) -> tuple[str, str]:
    encoding = detect_encoding(path)
    return path.read_text(encoding=encoding), encoding


def candidate_tex_bin_dirs() -> list[Path]:
    candidates: list[Path] = []
    env_dir = os.environ.get("TEXLIVE_BIN")
    if env_dir:
        candidates.append(Path(env_dir))

    candidates.extend(
        [
            Path(r"C:\texlive\current\bin\windows"),
            Path(r"C:\texlive\2026\bin\windows"),
            Path(r"C:\texlive\2025\bin\windows"),
        ]
    )

    texlive_root = Path(r"C:\texlive")
    if texlive_root.exists():
        for release_dir in sorted(texlive_root.iterdir(), reverse=True):
            bin_dir = release_dir / "bin" / "windows"
            candidates.append(bin_dir)

    unique_candidates: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate).lower()
        if key in seen:
            continue
        seen.add(key)
        unique_candidates.append(candidate)
    return unique_candidates


def build_tex_env() -> dict[str, str]:
    env = os.environ.copy()
    path_entries = env.get("PATH", "").split(os.pathsep) if env.get("PATH") else []
    merged_entries = list(path_entries)
    known_entries = {entry.lower() for entry in path_entries if entry}

    for candidate in candidate_tex_bin_dirs():
        if not candidate.exists():
            continue
        candidate_str = str(candidate)
        if candidate_str.lower() not in known_entries:
            merged_entries.insert(0, candidate_str)
            known_entries.add(candidate_str.lower())

    env["PATH"] = os.pathsep.join(entry for entry in merged_entries if entry)
    return env


def resolve_latexmk_command() -> tuple[str | None, dict[str, str]]:
    env = build_tex_env()
    latexmk_path = shutil.which("latexmk", path=env.get("PATH"))
    return latexmk_path, env


def env_value(name: str) -> str:
    return os.environ.get(name, "").strip()


def first_env_value(*names: str) -> str:
    for name in names:
        value = env_value(name)
        if value:
            return value
    return ""


def llm_provider_configs() -> dict[str, dict[str, Any]]:
    return {
        "openai": {
            "id": "openai",
            "label": "ChatGPT",
            "base_url": None,
            "model": env_value("OPENAI_LLM_MODEL") or "gpt-4o-mini",
            "api_key": env_value("OPENAI_API_KEY") or (load_api_key() or ""),
            "required": ["OPENAI_API_KEY"],
        },
        "local": {
            "id": "local",
            "label": "ローカルLLM",
            "base_url": first_env_value("LOCAL_LLM_BASE_URL", "BASE_URL"),
            "model": first_env_value("LOCAL_LLM_MODEL", "MODEL") or "openai/gpt-oss-120b",
            "api_key": first_env_value("LOCAL_LLM_API_KEY", "API_KEY"),
            "required": [
                ("LOCAL_LLM_BASE_URL", "BASE_URL"),
                ("LOCAL_LLM_API_KEY", "API_KEY"),
            ],
        },
    }


def provider_snapshot(provider: dict[str, Any]) -> dict[str, Any]:
    missing = [
        " or ".join(key) if isinstance(key, tuple) else key
        for key in provider["required"]
        if not (
            first_env_value(*key)
            if isinstance(key, tuple)
            else (provider.get("api_key") if key == "OPENAI_API_KEY" else env_value(key))
        )
    ]
    return {
        "id": provider["id"],
        "label": provider["label"],
        "model": provider["model"],
        "base_url": provider["base_url"],
        "configured": not missing,
        "missing": missing,
    }


def default_llm_provider() -> str:
    providers = llm_provider_configs()
    configured_default = env_value("LLM_PROVIDER") or "openai"
    if configured_default in providers and provider_snapshot(providers[configured_default])["configured"]:
        return configured_default
    for provider_id, provider in providers.items():
        if provider_snapshot(provider)["configured"]:
            return provider_id
    return configured_default if configured_default in providers else "openai"


def call_llm(provider_id: str, prompt: str, system: str | None = None) -> str:
    if OpenAI is None:
        raise RuntimeError("LLM calls require the openai package.")

    providers = llm_provider_configs()
    provider = providers.get(provider_id)
    if provider is None:
        raise RuntimeError(f"Unknown LLM provider: {provider_id}")

    snapshot = provider_snapshot(provider)
    if not snapshot["configured"]:
        missing = ", ".join(snapshot["missing"])
        raise RuntimeError(f"LLM provider '{provider_id}' is not configured: {missing}")

    client_kwargs: dict[str, Any] = {"api_key": provider["api_key"]}
    if provider["base_url"]:
        client_kwargs["base_url"] = provider["base_url"]
    client = OpenAI(**client_kwargs)

    messages: list[dict[str, str]] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    response = client.chat.completions.create(
        model=provider["model"],
        messages=messages,
    )
    return str(response.choices[0].message.content or "").strip()


def api_provider_from_transcriber_mode(transcriber_mode: str) -> str | None:
    if transcriber_mode == "api":
        return "openai"
    if transcriber_mode.startswith("api:"):
        provider_id = transcriber_mode.split(":", 1)[1].strip()
        return provider_id or "openai"
    return None


def local_transcription_model(provider: dict[str, Any]) -> str:
    return first_env_value("LOCAL_TRANSCRIBE_MODEL", "LOCAL_LLM_MODEL", "MODEL") or provider["model"]


def escape_tex(text: str) -> str:
    replacements = {
        "\\": r"\textbackslash{}",
        "&": r"\&",
        "%": r"\%",
        "$": r"\$",
        "#": r"\#",
        "_": r"\_",
        "{": r"\{",
        "}": r"\}",
        "~": r"\textasciitilde{}",
        "^": r"\textasciicircum{}",
    }
    return "".join(replacements.get(char, char) for char in text)


def normalize_device_name(name: str) -> str:
    return "".join(char for char in name if char.isprintable()).strip()


def device_dedupe_key(name: str) -> str:
    normalized = normalize_device_name(name).casefold()
    normalized = re.sub(r"^\d+[-\s]*", "", normalized)
    normalized = re.sub(r"^(?:microphone|マイク)\s*\((?:\d+-\s*)?", "", normalized)
    normalized = re.sub(r"^(?:line input|ライン入力)\s*\((?:\d+-\s*)?", "", normalized)
    normalized = re.sub(r"^(?:stereo mixer|ステレオ ミキサー)\s*\((?:\d+-\s*)?", "", normalized)
    normalized = re.sub(r"\)$", "", normalized)
    normalized = re.sub(r"\s+", " ", normalized)
    return normalized.strip()


def is_virtual_default_device(name: str) -> bool:
    normalized = normalize_device_name(name).casefold()
    return normalized in {
        "microsoft sound mapper - input",
        "プライマリ サウンド キャプチャ ドライバー",
    }


def device_priority(index: int, device: Any, default_input: int) -> tuple[int, int, int]:
    if index == default_input:
        return (0, 0, index)
    hostapi = int(device.get("hostapi", 99))
    hostapi_priority = {
        2: 1,  # Windows WASAPI
        0: 2,  # MME
        1: 3,  # Windows DirectSound
        3: 4,  # Windows WDM-KS
    }.get(hostapi, 9)
    return (1, hostapi_priority, index)


def dedupe_input_devices(devices: Any, default_input: int) -> list[tuple[int, Any]]:
    selected: dict[str, tuple[int, Any]] = {}
    for index, device in enumerate(devices):
        if int(device["max_input_channels"]) <= 0:
            continue
        if is_virtual_default_device(str(device["name"])):
            continue
        key = device_dedupe_key(str(device["name"]))
        if not key:
            key = str(index)
        current = selected.get(key)
        if current is None or device_priority(index, device, default_input) < device_priority(
            current[0],
            current[1],
            default_input,
        ):
            selected[key] = (index, device)
    return sorted(selected.values(), key=lambda item: item[0])


def format_hms(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def rms_value(audio: np.ndarray) -> float:
    if audio.size == 0:
        return 0.0
    squared = np.square(audio.astype(np.float32))
    return math.sqrt(float(np.mean(squared)))


def choose_session_paths(prefix: str) -> tuple[Path, Path, Path]:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    stem = f"{prefix}_{stamp}"
    master_path = ROOT_DIR / "recordings" / f"{stem}.wav"
    transcript_path = ROOT_DIR / "transcripts" / f"{stem}.live.txt"
    segments_dir = ROOT_DIR / "recordings" / "segments" / stem
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


def ensure_auto_block(text: str) -> str:
    if AUTO_START in text and AUTO_END in text:
        return text

    block_lines = [
        AUTO_START,
        *AUTO_HEADER_LINES,
        *AUTO_FOOTER_LINES,
        AUTO_END,
    ]
    block = "\n".join(block_lines) + "\n"
    end_document = r"\end{document}"
    if end_document in text:
        return text.replace(end_document, block + end_document, 1)
    return text.rstrip() + "\n\n" + block


def split_auto_block(text: str) -> tuple[str, str, str]:
    ensured = ensure_auto_block(text)
    start_index = ensured.index(AUTO_START)
    end_index = ensured.index(AUTO_END) + len(AUTO_END)
    prefix = ensured[:start_index]
    block = ensured[start_index:end_index]
    suffix = ensured[end_index:]
    return prefix, block, suffix


def parse_auto_entries(block: str) -> dict[int, str]:
    pattern = re.compile(
        r"% AUTO-SEGMENT:\s*(\d+)\n(?P<body>(?:.(?!% AUTO-SEGMENT:))*?.*?)(?=\n% AUTO-SEGMENT:|\n\\end\{itemize\})",
        re.DOTALL,
    )
    entries: dict[int, str] = {}
    for match in pattern.finditer(block):
        entries[int(match.group(1))] = match.group("body").strip("\n")
    return entries


def extract_block_header_footer(block: str) -> tuple[list[str], list[str]]:
    lines = block.splitlines()
    header: list[str] = []
    footer: list[str] = []
    state = "header"
    for line in lines:
        if state == "header":
            header.append(line)
            if line.strip() == r"\begin{itemize}":
                state = "middle"
        elif state == "middle":
            if line.strip() == r"\end{itemize}":
                footer.append(line)
                state = "footer"
        else:
            footer.append(line)
    if not footer:
        footer = [r"\end{itemize}", AUTO_END]
    return header, footer


def rebuild_auto_block(header: list[str], entries: dict[int, str], footer: list[str]) -> str:
    lines = list(header)
    if entries:
        for index in sorted(entries):
            lines.append(f"% AUTO-SEGMENT: {index:03d}")
            lines.extend(entries[index].splitlines() or [""])
    else:
        lines.append(AUTO_EMPTY_ITEM)
    lines.extend(footer)
    return "\n".join(lines)


def append_transcript_entry(tex_text: str, entry_id: int, time_label: str, transcript: str) -> str:
    prefix, block, suffix = split_auto_block(tex_text)
    header, footer = extract_block_header_footer(block)
    entries = parse_auto_entries(block)
    body = f"\\item [{time_label}] {escape_tex(transcript)}"
    entries[entry_id] = body
    new_block = rebuild_auto_block(header, entries, footer)
    merged = prefix + new_block + suffix
    return merged


def merge_document_with_server(submitted_text: str, current_text: str) -> str:
    submitted = ensure_auto_block(submitted_text)
    current = ensure_auto_block(current_text)
    submitted_prefix, submitted_block, submitted_suffix = split_auto_block(submitted)
    current_prefix, current_block, current_suffix = split_auto_block(current)
    submitted_header, submitted_footer = extract_block_header_footer(submitted_block)
    current_entries = parse_auto_entries(current_block)
    submitted_entries = parse_auto_entries(submitted_block)
    max_submitted_id = max(submitted_entries.keys(), default=0)
    for entry_id, body in sorted(current_entries.items()):
        if entry_id > max_submitted_id:
            submitted_entries[entry_id] = body
    merged_block = rebuild_auto_block(submitted_header, submitted_entries, submitted_footer)
    prefix = submitted_prefix if submitted_prefix != current_prefix else current_prefix
    suffix = submitted_suffix if submitted_suffix != current_suffix else current_suffix
    return prefix + merged_block + suffix


@dataclass
class TranscriptEntry:
    index: int
    start_sec: float
    end_sec: float
    text: str
    source: str


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
    def __init__(
        self,
        model_name: str,
        language: str | None,
        prompt: str | None,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        missing_message: str = "OPENAI_API_KEY is not set.",
    ) -> None:
        if OpenAI is None:
            raise RuntimeError(
                "API transcription requires the openai package. "
                "Run 'python -m pip install openai'."
            )
        api_key = (api_key or load_api_key() or "").strip()
        if not api_key:
            raise RuntimeError(missing_message)
        client_kwargs: dict[str, Any] = {"api_key": api_key}
        if base_url:
            client_kwargs["base_url"] = base_url
        self.client = OpenAI(**client_kwargs)
        self.model_name = model_name
        self.language = language
        self.prompt = prompt

    def transcribe(self, audio_path: Path) -> str:
        request_args: dict[str, Any] = {
            "model": self.model_name,
            "response_format": "json",
        }
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


class RecordingController:
    def __init__(
        self,
        manager: "MeetingAppState",
        *,
        device: int | None,
        samplerate: int,
        channels: int,
        segment_seconds: float,
        status_seconds: float,
        warn_rms: float,
        transcriber_mode: str,
        local_model: str,
        api_model: str,
        language: str | None,
        prompt: str | None,
    ) -> None:
        self.manager = manager
        self.device = device
        self.samplerate = samplerate
        self.channels = channels
        self.segment_seconds = segment_seconds
        self.status_seconds = status_seconds
        self.warn_rms = warn_rms
        self.language = language
        self.prompt = prompt
        self.master_path, self.transcript_path, self.segments_dir = choose_session_paths(
            "meeting_session"
        )
        self.audio_queue: queue.Queue[np.ndarray] = queue.Queue()
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.transcriber = self._build_transcriber(
            transcriber_mode,
            local_model,
            api_model,
            language,
            prompt,
        )
        self.transcriber_mode = transcriber_mode

    def _build_transcriber(
        self,
        transcriber_mode: str,
        local_model: str,
        api_model: str,
        language: str | None,
        prompt: str | None,
    ):
        if transcriber_mode == "none":
            return None
        if transcriber_mode == "local":
            return LocalTranscriber(local_model, language, prompt)
        api_provider = api_provider_from_transcriber_mode(transcriber_mode)
        if api_provider == "openai":
            model_name = env_value("OPENAI_TRANSCRIBE_MODEL") or api_model
            return ApiTranscriber(model_name, language, prompt)
        if api_provider:
            providers = llm_provider_configs()
            provider = providers.get(api_provider)
            if provider is None:
                raise RuntimeError(f"Unknown API provider: {api_provider}")
            snapshot = provider_snapshot(provider)
            if not snapshot["configured"]:
                missing = ", ".join(snapshot["missing"])
                raise RuntimeError(f"API provider '{api_provider}' is not configured: {missing}")
            return ApiTranscriber(
                local_transcription_model(provider),
                language,
                prompt,
                api_key=provider["api_key"],
                base_url=provider["base_url"],
                missing_message=f"API provider '{api_provider}' is not configured.",
            )
        raise RuntimeError(f"Unknown transcriber mode: {transcriber_mode}")

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        self.thread.join(timeout=5.0)

    def _transcribe_segment(self, path: Path) -> str:
        if self.transcriber is None:
            return ""
        return self.transcriber.transcribe(path)

    def _run(self) -> None:
        total_frames = 0
        segment_frames = 0
        segment_index = 1
        silence_warnings = 0
        segment_audio: list[np.ndarray] = []
        segment_target_frames = int(self.segment_seconds * self.samplerate)
        last_status_time = time.time()

        def callback(indata: np.ndarray, frames: int, time_info, status) -> None:
            if status:
                self.manager.add_log(f"audio-status: {status}")
            self.audio_queue.put(indata.copy())

        self.manager.on_recording_started(
            self.master_path,
            self.transcript_path,
            self.transcriber_mode,
        )

        try:
            with wave.open(str(self.master_path), "wb") as wav_file:
                wav_file.setnchannels(self.channels)
                wav_file.setsampwidth(2)
                wav_file.setframerate(self.samplerate)

                with sd.InputStream(
                    samplerate=self.samplerate,
                    channels=self.channels,
                    dtype="int16",
                    callback=callback,
                    device=self.device,
                ):
                    while not self.stop_event.is_set():
                        try:
                            chunk = self.audio_queue.get(timeout=0.5)
                        except queue.Empty:
                            continue

                        wav_file.writeframes(chunk.tobytes())
                        total_frames += len(chunk)
                        segment_frames += len(chunk)
                        segment_audio.append(chunk)
                        self.audio_queue.task_done()

                        now = time.time()
                        if now - last_status_time >= self.status_seconds:
                            elapsed = total_frames / self.samplerate
                            current_rms = rms_value(chunk)
                            file_size_mb = self.master_path.stat().st_size / (1024 * 1024)
                            warning = ""
                            if current_rms < self.warn_rms:
                                silence_warnings += 1
                                warning = "low-input"
                            else:
                                silence_warnings = 0
                            if silence_warnings >= 3:
                                warning = "check microphone or input device"
                            self.manager.on_status(elapsed, file_size_mb, current_rms, warning)
                            last_status_time = now

                        if segment_frames >= segment_target_frames:
                            merged = np.concatenate(segment_audio, axis=0)
                            segment_end = total_frames / self.samplerate
                            segment_start = max(
                                0.0,
                                segment_end - (len(merged) / self.samplerate),
                            )
                            segment_path = self.segments_dir / f"segment_{segment_index:03d}.wav"
                            write_wav(segment_path, self.samplerate, self.channels, merged)
                            self._handle_segment(
                                segment_index,
                                segment_start,
                                segment_end,
                                segment_path,
                            )
                            segment_index += 1
                            segment_frames = 0
                            segment_audio = []
        except Exception as exc:
            self.manager.add_log(f"recording failed: {exc}")
        finally:
            while True:
                try:
                    chunk = self.audio_queue.get_nowait()
                except queue.Empty:
                    break
                total_frames += len(chunk)
                segment_frames += len(chunk)
                segment_audio.append(chunk)
                self.audio_queue.task_done()

            if segment_audio:
                merged = np.concatenate(segment_audio, axis=0)
                segment_end = total_frames / self.samplerate
                segment_start = max(0.0, segment_end - (len(merged) / self.samplerate))
                segment_path = self.segments_dir / f"segment_{segment_index:03d}.wav"
                write_wav(segment_path, self.samplerate, self.channels, merged)
                self._handle_segment(segment_index, segment_start, segment_end, segment_path)

            duration = total_frames / self.samplerate if self.samplerate else 0.0
            self.manager.on_recording_finished(duration)

    def _handle_segment(
        self,
        index: int,
        start_sec: float,
        end_sec: float,
        segment_path: Path,
    ) -> None:
        try:
            transcript = self._transcribe_segment(segment_path)
        except Exception as exc:
            transcript = f"[transcription failed: {exc}]"

        header = f"[segment {index:03d} {format_hms(start_sec)} - {format_hms(end_sec)}]"
        with self.transcript_path.open("a", encoding="utf-8") as fh:
            fh.write(f"{header}\n{transcript}\n\n")
        self.manager.on_transcript(
            TranscriptEntry(
                index=index,
                start_sec=start_sec,
                end_sec=end_sec,
                text=transcript,
                source=self.transcriber_mode,
            )
        )
        try:
            segment_path.unlink()
        except OSError:
            pass


@dataclass
class MeetingAppState:
    document_path: Path = DEFAULT_DOCUMENT
    document_encoding: str = field(default="utf-8")
    document_text: str = field(default="")
    document_version: int = field(default=0)
    recording_active: bool = field(default=False)
    recording_started_at: float | None = field(default=None)
    recording_path: str | None = field(default=None)
    transcript_path: str | None = field(default=None)
    transcriber_mode: str = field(default="local")
    current_rms: float = field(default=0.0)
    elapsed_seconds: float = field(default=0.0)
    file_size_mb: float = field(default=0.0)
    status_warning: str = field(default="")
    transcript_entries: list[TranscriptEntry] = field(default_factory=list)
    auto_reflect: bool = field(default=True)
    logs: list[str] = field(default_factory=list)
    compile_log: str = field(default="")
    compile_ok: bool | None = field(default=None)
    pdf_path: str | None = field(default=None)
    llm_provider: str = field(default_factory=default_llm_provider)

    def __post_init__(self) -> None:
        self.lock = threading.RLock()
        self.controller: RecordingController | None = None
        text, encoding = read_text_with_encoding(self.document_path)
        self.document_text = ensure_auto_block(text)
        self.document_encoding = encoding
        self.document_version = 1

    def add_log(self, message: str) -> None:
        with self.lock:
            stamp = datetime.now().strftime("%H:%M:%S")
            self.logs.append(f"{stamp} {message}")
            self.logs = self.logs[-120:]

    def _write_document(self, text: str) -> None:
        self.document_path.write_text(text, encoding=self.document_encoding)
        self.document_text = text
        self.document_version += 1

    def get_document(self) -> dict[str, Any]:
        with self.lock:
            return {
                "path": str(self.document_path.relative_to(ROOT_DIR)),
                "text": self.document_text,
                "version": self.document_version,
            }

    def save_document(self, submitted_text: str) -> dict[str, Any]:
        with self.lock:
            merged = merge_document_with_server(submitted_text, self.document_text)
            self._write_document(merged)
            self.add_log("tex saved")
            return {
                "ok": True,
                "version": self.document_version,
            }

    def set_auto_reflect(self, enabled: bool) -> None:
        with self.lock:
            self.auto_reflect = enabled
        self.add_log(f"auto-reflect={'on' if enabled else 'off'}")

    def on_recording_started(
        self,
        recording_path: Path,
        transcript_path: Path,
        transcriber_mode: str,
    ) -> None:
        with self.lock:
            self.recording_active = True
            self.recording_started_at = time.time()
            self.recording_path = str(recording_path.relative_to(ROOT_DIR))
            self.transcript_path = str(transcript_path.relative_to(ROOT_DIR))
            self.transcriber_mode = transcriber_mode
            self.current_rms = 0.0
            self.elapsed_seconds = 0.0
            self.file_size_mb = 0.0
            self.status_warning = ""
            self.transcript_entries = []
        self.add_log("recording started")

    def on_status(
        self,
        elapsed_seconds: float,
        file_size_mb: float,
        current_rms: float,
        warning: str,
    ) -> None:
        with self.lock:
            self.elapsed_seconds = elapsed_seconds
            self.file_size_mb = file_size_mb
            self.current_rms = current_rms
            self.status_warning = warning

    def on_transcript(self, entry: TranscriptEntry) -> None:
        with self.lock:
            self.transcript_entries.append(entry)
            if self.auto_reflect:
                time_label = format_hms(entry.start_sec)
                updated_text = append_transcript_entry(
                    self.document_text,
                    entry.index,
                    time_label,
                    entry.text or "(no speech)",
                )
                self._write_document(updated_text)
        self.add_log(f"transcript segment {entry.index:03d} received")

    def on_recording_finished(self, duration: float) -> None:
        with self.lock:
            self.recording_active = False
            self.elapsed_seconds = duration
            self.recording_started_at = None
            self.controller = None
        self.add_log("recording stopped")

    def start_recording(
        self,
        *,
        device: int | None,
        transcriber_mode: str,
        auto_reflect: bool,
        language: str | None,
    ) -> None:
        with self.lock:
            if self.recording_active:
                raise RuntimeError("Recording is already active.")
            self.auto_reflect = auto_reflect
        api_provider = api_provider_from_transcriber_mode(transcriber_mode)
        if api_provider:
            self.set_llm_provider(api_provider)

        controller = RecordingController(
            self,
            device=device,
            samplerate=16000,
            channels=1,
            segment_seconds=15.0,
            status_seconds=2.0,
            warn_rms=120.0,
            transcriber_mode=transcriber_mode,
            local_model="tiny",
            api_model="gpt-4o-mini-transcribe",
            language=language,
            prompt="会議の文字起こし。固有名詞を丁寧に扱う。",
        )
        with self.lock:
            self.controller = controller
        controller.start()

    def stop_recording(self) -> None:
        with self.lock:
            controller = self.controller
        if controller is None:
            return
        controller.stop()

    def compile_document(self) -> dict[str, Any]:
        latexmk_command, tex_env = resolve_latexmk_command()
        if not latexmk_command:
            message = (
                "latexmk was not found. Run setup_texlive.bat or install TeX Live "
                "with uplatex and dvipdfmx."
            )
            with self.lock:
                self.compile_log = message
                self.compile_ok = False
                self.pdf_path = None
            self.add_log("latex compile failed")
            return {
                "ok": False,
                "log": message,
                "pdf_path": None,
            }

        result = subprocess.run(
            [latexmk_command, str(self.document_path)],
            cwd=ROOT_DIR,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=tex_env,
        )
        compile_log = (result.stdout + "\n" + result.stderr).strip()
        pdf_candidates = [
            ROOT_DIR / f"{self.document_path.stem}.pdf",
            self.document_path.with_suffix(".pdf"),
        ]
        pdf_path = next((path for path in pdf_candidates if path.exists()), None)
        with self.lock:
            self.compile_log = compile_log
            self.compile_ok = result.returncode == 0
            self.pdf_path = (
                str(pdf_path.relative_to(ROOT_DIR)) if pdf_path is not None else None
            )
        self.add_log("latex compiled" if result.returncode == 0 else "latex compile failed")
        return {
            "ok": result.returncode == 0,
            "log": compile_log,
            "pdf_path": self.pdf_path,
        }

    def llm_provider_snapshot(self) -> dict[str, Any]:
        with self.lock:
            selected = self.llm_provider
        providers = [provider_snapshot(provider) for provider in llm_provider_configs().values()]
        return {
            "selected": selected,
            "providers": providers,
        }

    def set_llm_provider(self, provider_id: str) -> dict[str, Any]:
        providers = llm_provider_configs()
        provider = providers.get(provider_id)
        if provider is None:
            raise RuntimeError(f"Unknown LLM provider: {provider_id}")
        snapshot = provider_snapshot(provider)
        if not snapshot["configured"]:
            missing = ", ".join(snapshot["missing"])
            raise RuntimeError(f"LLM provider '{provider_id}' is not configured: {missing}")
        with self.lock:
            self.llm_provider = provider_id
        self.add_log(f"llm-provider={provider_id}")
        return {"ok": True, "selected": provider_id}

    def chat_with_llm(
        self,
        *,
        prompt: str,
        system: str | None = None,
        provider: str | None = None,
    ) -> dict[str, Any]:
        with self.lock:
            provider_id = provider or self.llm_provider
        text = call_llm(provider_id, prompt, system)
        self.add_log(f"llm chat completed provider={provider_id}")
        return {
            "ok": True,
            "provider": provider_id,
            "text": text,
        }

    def status_snapshot(self) -> dict[str, Any]:
        with self.lock:
            return {
                "recording_active": self.recording_active,
                "elapsed_seconds": self.elapsed_seconds,
                "current_rms": self.current_rms,
                "file_size_mb": self.file_size_mb,
                "status_warning": self.status_warning,
                "recording_path": self.recording_path,
                "transcript_path": self.transcript_path,
                "transcriber_mode": self.transcriber_mode,
                "auto_reflect": self.auto_reflect,
                "document_path": str(self.document_path.relative_to(ROOT_DIR)),
                "document_version": self.document_version,
                "transcript_entries": [
                    {
                        "index": entry.index,
                        "start_sec": entry.start_sec,
                        "end_sec": entry.end_sec,
                        "text": entry.text,
                        "source": entry.source,
                    }
                    for entry in self.transcript_entries
                ],
                "logs": list(self.logs),
                "compile_ok": self.compile_ok,
                "compile_log": self.compile_log,
                "pdf_path": self.pdf_path,
                "llm_provider": self.llm_provider,
            }


manager = MeetingAppState()


class StartRequest(BaseModel):
    device: int | None = None
    transcriber: str = "local"
    auto_reflect: bool = True
    language: str | None = "ja"


class SaveDocumentRequest(BaseModel):
    text: str


class AutoReflectRequest(BaseModel):
    enabled: bool


class LlmProviderRequest(BaseModel):
    provider: str


class LlmChatRequest(BaseModel):
    prompt: str
    system: str | None = None
    provider: str | None = None


app = FastAPI(title="Meeting Minutes UI")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/status")
def get_status() -> dict[str, Any]:
    return manager.status_snapshot()


@app.get("/api/document")
def get_document() -> dict[str, Any]:
    return manager.get_document()


@app.post("/api/document")
def save_document(request: SaveDocumentRequest) -> dict[str, Any]:
    return manager.save_document(request.text)


@app.post("/api/recording/start")
def start_recording(request: StartRequest) -> dict[str, Any]:
    try:
        manager.start_recording(
            device=request.device,
            transcriber_mode=request.transcriber,
            auto_reflect=request.auto_reflect,
            language=request.language,
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True}


@app.post("/api/recording/stop")
def stop_recording() -> dict[str, Any]:
    manager.stop_recording()
    return {"ok": True}


@app.post("/api/auto-reflect")
def set_auto_reflect(request: AutoReflectRequest) -> dict[str, Any]:
    manager.set_auto_reflect(request.enabled)
    return {"ok": True}


@app.post("/api/compile")
def compile_document() -> dict[str, Any]:
    return manager.compile_document()


@app.get("/api/llm/providers")
def get_llm_providers() -> dict[str, Any]:
    return manager.llm_provider_snapshot()


@app.post("/api/llm/provider")
def set_llm_provider(request: LlmProviderRequest) -> dict[str, Any]:
    try:
        return manager.set_llm_provider(request.provider)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/llm/chat")
def chat_with_llm(request: LlmChatRequest) -> dict[str, Any]:
    try:
        return manager.chat_with_llm(
            prompt=request.prompt,
            system=request.system,
            provider=request.provider,
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/devices")
def get_devices() -> dict[str, Any]:
    devices = sd.query_devices()
    default_input, default_output = sd.default.device
    items = []
    for index, device in dedupe_input_devices(devices, default_input):
        items.append(
            {
                "id": index,
                "name": normalize_device_name(str(device["name"])),
                "max_input_channels": device["max_input_channels"],
                "is_default_input": index == default_input,
                "is_default_output": index == default_output,
            }
        )
    return {"devices": items}
