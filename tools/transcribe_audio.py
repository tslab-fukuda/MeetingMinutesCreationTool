from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

from openai import APIStatusError, OpenAI

try:
    from tools.config import load_local_settings
except ModuleNotFoundError:
    from config import load_local_settings


DEFAULT_MODEL = "gpt-4o-mini-transcribe"
MAX_FILE_SIZE_MB = 25

load_local_settings()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Transcribe an audio file with the OpenAI Audio API."
    )
    parser.add_argument("audio_path", help="Path to the audio file to transcribe.")
    parser.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        choices=[
            "gpt-4o-mini-transcribe",
            "gpt-4o-transcribe",
            "whisper-1",
        ],
        help=f"Transcription model. Default: {DEFAULT_MODEL}",
    )
    parser.add_argument(
        "--prompt",
        help="Optional prompt to improve recognition for names, acronyms, or meeting context.",
    )
    parser.add_argument(
        "--language",
        help="Optional language hint such as 'ja'.",
    )
    parser.add_argument(
        "--output",
        help="Path to save the transcript text. Default: transcripts/<audio_stem>.txt",
    )
    parser.add_argument(
        "--save-json",
        action="store_true",
        help="Also save the raw JSON response beside the transcript text file.",
    )
    return parser


def load_api_key() -> str | None:
    env_key = os.environ.get("OPENAI_API_KEY")
    if env_key:
        return env_key.strip()

    recording_md = Path("RECORDING.md")
    if recording_md.exists():
        text = recording_md.read_text(encoding="utf-8", errors="ignore")
        match = re.search(r"(sk-[A-Za-z0-9_-]+)", text)
        if match:
            return match.group(1)
    return None


def default_output_path(audio_path: Path) -> Path:
    output_dir = Path("transcripts")
    output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir / f"{audio_path.stem}.txt"


def ensure_file_ok(audio_path: Path) -> None:
    if not audio_path.exists():
        raise FileNotFoundError(f"Audio file not found: {audio_path}")

    size_mb = audio_path.stat().st_size / (1024 * 1024)
    if size_mb > MAX_FILE_SIZE_MB:
        raise ValueError(
            f"Audio file is {size_mb:.2f} MB. "
            f"The Audio API file upload limit is {MAX_FILE_SIZE_MB} MB."
        )


def response_to_text(response) -> str:
    if isinstance(response, str):
        return response

    text = getattr(response, "text", None)
    if text:
        return text

    if isinstance(response, dict):
        return str(response.get("text", ""))

    if hasattr(response, "model_dump"):
        data = response.model_dump()
        return str(data.get("text", ""))

    return str(response)


def response_to_jsonable(response):
    if isinstance(response, str):
        return {"text": response}
    if isinstance(response, dict):
        return response
    if hasattr(response, "model_dump"):
        return response.model_dump()
    return {"text": str(response)}


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    api_key = load_api_key()
    if not api_key:
        print(
            "OpenAI API key not found. Set OPENAI_API_KEY or place a raw sk-... key in RECORDING.md.",
            file=sys.stderr,
        )
        return 1

    audio_path = Path(args.audio_path)
    try:
        ensure_file_ok(audio_path)
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1

    output_path = Path(args.output) if args.output else default_output_path(audio_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    client = OpenAI(api_key=api_key)

    request_args = {
        "model": args.model,
        "response_format": "json",
    }
    if args.prompt:
        request_args["prompt"] = args.prompt
    if args.language:
        request_args["language"] = args.language

    try:
        with audio_path.open("rb") as audio_file:
            response = client.audio.transcriptions.create(
                file=audio_file,
                **request_args,
            )
    except APIStatusError as exc:
        if exc.status_code == 429 and "insufficient_quota" in str(exc):
            print(
                "Transcription failed: API quota is exhausted for this key. "
                "Check billing or use a different key.",
                file=sys.stderr,
            )
        else:
            print(f"Transcription failed: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"Transcription failed: {exc}", file=sys.stderr)
        return 1

    transcript_text = response_to_text(response).strip()
    output_path.write_text(transcript_text, encoding="utf-8")

    print(f"Saved transcript to: {output_path}")
    if transcript_text:
        print("Transcript preview:")
        print(transcript_text[:500])
    else:
        print("Transcript was empty.")

    if args.save_json:
        json_path = output_path.with_suffix(".json")
        json_path.write_text(
            json.dumps(response_to_jsonable(response), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"Saved raw response to: {json_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
