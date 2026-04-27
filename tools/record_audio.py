from __future__ import annotations

import argparse
import os
import queue
import sys
import time
import wave
from datetime import datetime
from pathlib import Path

import numpy as np
import sounddevice as sd


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Record microphone audio to a WAV file."
    )
    parser.add_argument(
        "--list-devices",
        action="store_true",
        help="Show available audio devices and exit.",
    )
    parser.add_argument(
        "--device",
        type=int,
        help="Input device id. Omit to use the default input device.",
    )
    parser.add_argument(
        "--samplerate",
        type=int,
        default=16000,
        help="Sample rate in Hz. Default: 16000",
    )
    parser.add_argument(
        "--channels",
        type=int,
        default=1,
        help="Number of input channels. Default: 1",
    )
    parser.add_argument(
        "--dtype",
        default="int16",
        choices=["int16"],
        help="Recording sample format. Only int16 is supported for WAV output.",
    )
    parser.add_argument(
        "--seconds",
        type=float,
        help="Record for a fixed duration in seconds. Omit for Ctrl+C stop mode.",
    )
    parser.add_argument(
        "--output",
        help="Output WAV path. Omit to save into recordings/ with a timestamped name.",
    )
    parser.add_argument(
        "--prefix",
        default="meeting_recording",
        help="Filename prefix when --output is omitted.",
    )
    return parser


def sanitize_name(name: str) -> str:
    return "".join(ch if 32 <= ord(ch) < 127 else "?" for ch in name)


def list_devices() -> int:
    devices = sd.query_devices()
    default_input, default_output = sd.default.device
    print("Audio devices:")
    for idx, device in enumerate(devices):
        label = []
        if idx == default_input:
            label.append("default-input")
        if idx == default_output:
            label.append("default-output")
        flags = f" [{' '.join(label)}]" if label else ""
        name = sanitize_name(str(device["name"]))
        print(
            f"{idx:>2}: {name}{flags} | "
            f"in={device['max_input_channels']} out={device['max_output_channels']} "
            f"default_sr={device['default_samplerate']}"
        )
    return 0


def choose_output_path(output: str | None, prefix: str) -> Path:
    if output:
        path = Path(output)
    else:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = Path("recordings") / f"{prefix}_{stamp}.wav"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def write_wav(path: Path, samplerate: int, channels: int, audio: np.ndarray) -> None:
    with wave.open(str(path), "wb") as wav_file:
        wav_file.setnchannels(channels)
        wav_file.setsampwidth(2)
        wav_file.setframerate(samplerate)
        wav_file.writeframes(audio.tobytes())


def fixed_duration_recording(
    device: int | None,
    samplerate: int,
    channels: int,
    seconds: float,
) -> np.ndarray:
    frame_count = int(seconds * samplerate)
    print(f"Recording for {seconds:.1f} seconds...")
    recording = sd.rec(
        frame_count,
        samplerate=samplerate,
        channels=channels,
        dtype="int16",
        device=device,
    )
    sd.wait()
    return recording


def streaming_recording(
    device: int | None,
    samplerate: int,
    channels: int,
) -> np.ndarray:
    audio_queue: queue.Queue[np.ndarray] = queue.Queue()
    chunks: list[np.ndarray] = []

    def callback(indata: np.ndarray, frames: int, time_info, status) -> None:
        if status:
            print(f"[audio-status] {status}", file=sys.stderr)
        audio_queue.put(indata.copy())

    print("Recording... Press Ctrl+C to stop.")
    with sd.InputStream(
        samplerate=samplerate,
        channels=channels,
        dtype="int16",
        callback=callback,
        device=device,
    ):
        try:
            while True:
                try:
                    chunks.append(audio_queue.get(timeout=0.5))
                except queue.Empty:
                    continue
        except KeyboardInterrupt:
            print("\nStopping recording.")

    while not audio_queue.empty():
        chunks.append(audio_queue.get())

    if not chunks:
        return np.empty((0, channels), dtype=np.int16)
    return np.concatenate(chunks, axis=0)


def validate_args(args: argparse.Namespace) -> int:
    if args.seconds is not None and args.seconds <= 0:
        print("--seconds must be greater than 0.", file=sys.stderr)
        return 2
    if args.channels <= 0:
        print("--channels must be greater than 0.", file=sys.stderr)
        return 2
    if args.samplerate <= 0:
        print("--samplerate must be greater than 0.", file=sys.stderr)
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

    output_path = choose_output_path(args.output, args.prefix)

    try:
        if args.seconds is not None:
            audio = fixed_duration_recording(
                device=args.device,
                samplerate=args.samplerate,
                channels=args.channels,
                seconds=args.seconds,
            )
        else:
            audio = streaming_recording(
                device=args.device,
                samplerate=args.samplerate,
                channels=args.channels,
            )
    except Exception as exc:
        print(f"Recording failed: {exc}", file=sys.stderr)
        return 1

    if audio.size == 0:
        print("No audio data was captured.", file=sys.stderr)
        return 1

    write_wav(
        path=output_path,
        samplerate=args.samplerate,
        channels=args.channels,
        audio=audio,
    )

    duration = len(audio) / args.samplerate
    print(f"Saved recording to: {output_path}")
    print(f"Duration: {duration:.2f} seconds")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
