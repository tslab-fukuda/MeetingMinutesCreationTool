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
TEMPLATE_DOCUMENT = ROOT_DIR / "Texテンプレート2026" / "tmplate.tex"
MINUTES_DIR = TEMPLATE_DOCUMENT.parent
AUTO_START = "% AUTO-TRANSCRIPT-START"
AUTO_END = "% AUTO-TRANSCRIPT-END"
AUTO_HEADER_LINES = [
    r"\section*{自動文字起こしメモ}",
    r"\begin{itemize}",
]
AUTO_FOOTER_LINES = [r"\end{itemize}"]
SUMMARY_CONTEXT_SEGMENTS = 6
DEFAULT_SUMMARY_SECTION_TITLE = "報告事項"

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


def daily_document_path() -> Path:
    date_label = env_value("MEETING_MINUTES_DATE") or datetime.now().strftime("%Y%m%d")
    if not re.fullmatch(r"\d{8}", date_label):
        raise RuntimeError("MEETING_MINUTES_DATE must be in YYYYMMDD format.")
    return MINUTES_DIR / f"tmplate_minutes{date_label}.tex"


def prepare_daily_document(document_path: Path) -> tuple[str, str]:
    if document_path.exists():
        text, encoding = read_text_with_encoding(document_path)
        return ensure_auto_block(text), encoding

    template_text, encoding = read_text_with_encoding(TEMPLATE_DOCUMENT)
    text = ensure_auto_block(template_text)
    document_path.write_text(text, encoding=encoding)
    return text, encoding


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
    for index in sorted(entries):
        lines.append(f"% AUTO-SEGMENT: {index:03d}")
        lines.extend(entries[index].splitlines() or [""])
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


def transcript_entries_to_text(entries: list["TranscriptEntry"]) -> str:
    lines: list[str] = []
    for entry in entries:
        body = entry.text.strip()
        if not body or not should_summarize_transcript(body):
            continue
        lines.append(f"[{format_hms(entry.start_sec)}] {body}")
    return "\n".join(lines).strip()


def parse_report_summary_items(summary_text: str) -> list[str]:
    items: list[str] = []
    for raw_line in summary_text.splitlines():
        line = raw_line.strip()
        if not line or line in {"```", "```text"}:
            continue
        line = re.sub(r"^\s*(?:\\item|[-*・]|[0-9０-９]+[.)．、])\s*", "", line).strip()
        line = line.strip("[]\"'` ")
        if line:
            items.append(line)

    if items:
        return items

    fallback = re.sub(r"\s+", " ", summary_text).strip()
    fallback = re.sub(r"^\s*(?:\\item|[-*・])\s*", "", fallback).strip()
    return [fallback] if fallback else []


def summarize_transcript_entries(
    provider_id: str,
    entries: list["TranscriptEntry"],
    target_label: str = DEFAULT_SUMMARY_SECTION_TITLE,
) -> list[str]:
    source_text = transcript_entries_to_text(entries)
    if not source_text:
        raise RuntimeError("No transcript text is available to summarize.")

    system = (
        "あなたは日本語の会議議事録作成アシスタントです。"
        "ライブ文字起こしから、議事録TeXの選択中セクションに入れる短い日本語項目を作成してください。"
    )
    prompt = "\n".join(
        [
            f"以下の文字起こしを、議事録TeXの「{target_label}」に入れる項目として簡潔に要約してください。",
            "話題が変わる場合は項目を分け、必要な数だけ出力してください。",
            "出力は1行1項目にしてください。",
            "番号、箇条書き記号、Markdown、LaTeXコマンドは付けないでください。",
            "決定事項、依頼事項、担当者、期限が分かる場合は優先してください。",
            "",
            source_text,
        ]
    )
    summary_text = call_llm(provider_id, prompt, system)
    items = parse_report_summary_items(summary_text)
    if not items:
        raise RuntimeError("LLM returned no report summary items.")
    return items


def summarize_incremental_report_items(
    provider_id: str,
    previous_items: list[str],
    recent_entries: list["TranscriptEntry"],
    new_entries: list["TranscriptEntry"],
    target_label: str = DEFAULT_SUMMARY_SECTION_TITLE,
) -> list[str]:
    source_text = transcript_entries_to_text(new_entries)
    if not source_text:
        raise RuntimeError("No new transcript text is available to summarize.")

    previous_text = "\n".join(f"- {item}" for item in previous_items) or "(まだありません)"
    recent_text = transcript_entries_to_text(recent_entries) or "(まだありません)"
    system = (
        "あなたは日本語の会議議事録作成アシスタントです。"
        "15秒ごとの文字起こし断片を、前後の文脈と整合する議事録項目へ整理します。"
    )
    prompt = "\n".join(
        [
            f"対象セクションは「{target_label}」です。",
            "既存の項目候補と新しい文字起こしを統合し、更新後の項目候補を出力してください。",
            "15秒区切りで文章が途中で切れるため、新しい断片が前の話題の続きなら既存項目を修正・統合してください。",
            "話題が明確に変わった場合だけ、新しい項目を追加してください。",
            "出力は更新後の全項目を1行1項目にしてください。",
            "番号、箇条書き記号、Markdown、LaTeXコマンドは付けないでください。",
            "決定事項、依頼事項、担当者、期限が分かる場合は優先してください。",
            "",
            "[現在の項目候補]",
            previous_text,
            "",
            "[直近の文字起こし文脈]",
            recent_text,
            "",
            "[今回新しく追加された文字起こし]",
            source_text,
        ]
    )
    summary_text = call_llm(provider_id, prompt, system)
    items = parse_report_summary_items(summary_text)
    if not items:
        raise RuntimeError("LLM returned no report summary items.")
    return items


def line_number_at(text: str, index: int) -> int:
    safe_index = max(0, min(index, len(text)))
    return text.count("\n", 0, safe_index) + 1


def line_preview_at(text: str, index: int) -> str:
    safe_index = max(0, min(index, len(text)))
    line_start = text.rfind("\n", 0, safe_index) + 1
    line_end = text.find("\n", safe_index)
    if line_end == -1:
        line_end = len(text)
    return text[line_start:line_end].strip()


def strip_tex_comment(line: str) -> str:
    for index, char in enumerate(line):
        if char == "%" and (index == 0 or line[index - 1] != "\\"):
            return line[:index]
    return line


def clean_tex_section_title(raw_title: str) -> str:
    text = strip_tex_comment(raw_title).strip()
    text = re.sub(r"\\textbf\{([^{}]*)\}", r"\1", text)
    text = re.sub(r"\\[a-zA-Z]+(?:\[[^\]]*\])?", "", text)
    text = text.replace("{", "").replace("}", "")
    text = text.replace("　", " ")
    return re.sub(r"\s+", " ", text).strip()


def find_matching_list_end(tex_text: str, begin_index: int) -> int:
    first_match = re.match(r"\\begin\{(itemize|enumerate)\}", tex_text[begin_index:])
    if not first_match:
        return -1

    stack: list[str] = []
    pattern = re.compile(r"\\(begin|end)\{(itemize|enumerate)\}")
    for match in pattern.finditer(tex_text, begin_index):
        action, env_name = match.group(1), match.group(2)
        if action == "begin":
            stack.append(env_name)
            continue
        if not stack:
            return -1
        if stack[-1] != env_name:
            return -1
        stack.pop()
        if not stack:
            return match.start()
    return -1


def find_next_list_begin(lines: list[str], offsets: list[int], line_index: int) -> int | None:
    for next_index in range(line_index + 1, len(lines)):
        content = strip_tex_comment(lines[next_index])
        if not content.strip():
            continue
        begin_match = re.search(r"\\begin\{(itemize|enumerate)\}", content)
        if begin_match and not content[: begin_match.start()].strip():
            return offsets[next_index] + begin_match.start()
        if re.match(r"^\s*\\(?:item|end)\b", content):
            return None
    return None


def section_options_for_client(sections: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "id": section["id"],
            "title": section["title"],
            "label": section["label"],
            "line": section["line"],
        }
        for section in sections
    ]


def find_summary_target_sections(tex_text: str) -> list[dict[str, Any]]:
    lines = tex_text.splitlines(keepends=True)
    offsets: list[int] = []
    offset = 0
    for line in lines:
        offsets.append(offset)
        offset += len(line)

    sections: list[dict[str, Any]] = []
    current_depth = 0
    titles_by_depth: dict[int, str] = {}
    item_pattern = re.compile(r"^\s*\\item(?:\[[^\]]*\])?\s*(?P<title>.*)$")
    list_pattern = re.compile(r"\\(begin|end)\{(itemize|enumerate)\}")

    for line_index, raw_line in enumerate(lines):
        content = strip_tex_comment(raw_line).rstrip("\r\n")
        item_match = item_pattern.match(content)
        if item_match:
            title = clean_tex_section_title(item_match.group("title"))
            if title:
                parents = [
                    titles_by_depth[depth]
                    for depth in sorted(titles_by_depth)
                    if depth < current_depth
                ]
                begin_index = find_next_list_begin(lines, offsets, line_index)
                end_index = (
                    find_matching_list_end(tex_text, begin_index)
                    if begin_index is not None
                    else -1
                )
                if begin_index is not None and end_index != -1:
                    label = " > ".join([*parents, title])
                    section_id = f"section-{len(sections) + 1}"
                    sections.append(
                        {
                            "id": section_id,
                            "title": title,
                            "label": label,
                            "line": line_number_at(tex_text, offsets[line_index]),
                            "preview": line_preview_at(tex_text, offsets[line_index]),
                            "begin_index": begin_index,
                            "end_index": end_index,
                            "depth": current_depth,
                        }
                    )

                titles_by_depth[current_depth] = title
                for depth in [depth for depth in titles_by_depth if depth > current_depth]:
                    del titles_by_depth[depth]

        for list_match in list_pattern.finditer(content):
            if list_match.group(1) == "begin":
                if current_depth == 0:
                    titles_by_depth.clear()
                current_depth += 1
            else:
                current_depth = max(0, current_depth - 1)
                for depth in [depth for depth in titles_by_depth if depth > current_depth]:
                    del titles_by_depth[depth]

    return sections


def choose_summary_section(
    sections: list[dict[str, Any]],
    section_id: str | None,
) -> dict[str, Any] | None:
    if section_id:
        for section in sections:
            if section["id"] == section_id:
                return section

    for section in sections:
        if section["title"] == DEFAULT_SUMMARY_SECTION_TITLE:
            return section

    for section in sections:
        if section["label"].startswith(f"{DEFAULT_SUMMARY_SECTION_TITLE} >"):
            return section

    return sections[0] if sections else None


def resolve_summary_section(tex_text: str, section_id: str | None = None) -> dict[str, Any]:
    sections = find_summary_target_sections(tex_text)
    section = choose_summary_section(sections, section_id)
    if section is None:
        return {
            "ok": False,
            "reason": "Could not find a selectable TeX section with a nested list.",
            "sections": [],
        }
    return {
        "ok": True,
        "section": section,
        "sections": sections,
    }


def find_blank_item_targets_in_section(
    tex_text: str,
    section: dict[str, Any],
) -> list[dict[str, Any]]:
    begin_index = int(section["begin_index"])
    end_index = int(section["end_index"])
    body = tex_text[begin_index:end_index]
    targets: list[dict[str, Any]] = []
    for match in re.finditer(r"(?m)^(?P<indent>[ \t]*)\\item[ \t]*$", body):
        start = begin_index + match.start()
        end = begin_index + match.end()
        line_end = tex_text.find("\n", end)
        if line_end == -1:
            line_end = end
        else:
            line_end += 1
        targets.append(
            {
                "start_index": start,
                "end_index": end,
                "line_end_index": line_end,
                "line": line_number_at(tex_text, start),
                "indent": match.group("indent"),
                "preview": line_preview_at(tex_text, start),
            }
        )
    return targets


def find_blank_report_item_targets(
    tex_text: str,
    section_id: str | None = None,
) -> list[dict[str, Any]]:
    resolved = resolve_summary_section(tex_text, section_id)
    if not resolved["ok"]:
        return []
    return find_blank_item_targets_in_section(tex_text, resolved["section"])


def default_item_indent_for_section(tex_text: str, section: dict[str, Any]) -> str:
    begin_index = int(section["begin_index"])
    end_index = int(section["end_index"])
    body = tex_text[begin_index:end_index]
    first_item = re.search(r"(?m)^(?P<indent>[ \t]*)\\item(?:\s|$)", body)
    if first_item:
        return first_item.group("indent")
    section_line = line_preview_at(tex_text, int(section["begin_index"]))
    return re.match(r"^[ \t]*", section_line).group(0) + "\t"


def describe_report_summary_target(
    tex_text: str,
    section_id: str | None = None,
) -> dict[str, Any]:
    resolved = resolve_summary_section(tex_text, section_id)
    sections = section_options_for_client(resolved.get("sections", []))
    if not resolved["ok"]:
        return {
            **resolved,
            "sections": sections,
        }

    section = resolved["section"]
    blank_targets = find_blank_item_targets_in_section(tex_text, section)
    target_lines = [
        {
            "line": target["line"],
            "preview": target["preview"] or r"\item",
        }
        for target in blank_targets[:10]
    ]
    common = {
        "ok": True,
        "sections": sections,
        "section_id": section["id"],
        "section_title": section["title"],
        "section_label": section["label"],
        "section_line": section["line"],
        "section_preview": section["preview"],
        "report_line": section["line"],
        "report_preview": section["preview"],
    }
    if blank_targets:
        first_target = blank_targets[0]
        return {
            **common,
            "mode": "fill-empty-items",
            "line": first_target["line"],
            "detail": f"{section['label']} 内にある既存の空の \\item 行へ上から順に入力します。",
            "preview": first_target["preview"] or r"\item",
            "blank_item_count": len(blank_targets),
            "target_lines": target_lines,
        }

    insert_index = tex_text.rfind("\n", 0, int(section["end_index"]))
    insert_index = int(section["end_index"]) if insert_index == -1 else insert_index + 1
    return {
        **common,
        "mode": "append-items",
        "line": line_number_at(tex_text, insert_index),
        "detail": f"{section['label']} 内に空の \\item がないため、セクション末尾へ \\item を追加します。",
        "preview": line_preview_at(tex_text, insert_index),
        "blank_item_count": 0,
        "target_lines": [],
        "append_index": insert_index,
    }


def insert_report_summary_items(
    tex_text: str,
    items: list[str],
    section_id: str | None = None,
) -> str:
    if not items:
        raise RuntimeError("No report summary items were generated.")

    target = describe_report_summary_target(tex_text, section_id)
    if not target["ok"]:
        raise RuntimeError(str(target["reason"]))

    section = resolve_summary_section(tex_text, target["section_id"])["section"]
    blank_targets = find_blank_item_targets_in_section(tex_text, section)
    if blank_targets:
        replacements: list[tuple[int, int, str]] = []
        for item, blank_target in zip(items, blank_targets):
            replacement = f"{blank_target['indent']}\\item {escape_tex(item)}"
            replacements.append(
                (
                    int(blank_target["start_index"]),
                    int(blank_target["end_index"]),
                    replacement,
                )
            )

        extra_items = items[len(blank_targets) :]
        if extra_items:
            last_target = blank_targets[min(len(items), len(blank_targets)) - 1]
            extra_lines = [
                f"{last_target['indent']}\\item {escape_tex(item)}" for item in extra_items
            ]
            insertion_index = int(last_target["line_end_index"])
            replacements.append((insertion_index, insertion_index, "\n".join(extra_lines) + "\n"))

        updated = tex_text
        for start, end, replacement in sorted(replacements, reverse=True):
            updated = updated[:start] + replacement + updated[end:]
        return updated

    insert_index = tex_text.rfind("\n", 0, int(section["end_index"]))
    insert_index = int(section["end_index"]) if insert_index == -1 else insert_index + 1
    indent = default_item_indent_for_section(tex_text, section)
    lines = [f"{indent}\\item {escape_tex(item)}" for item in items]
    return tex_text[:insert_index] + "\n".join(lines) + "\n" + tex_text[insert_index:]


def clear_report_summary_items(
    tex_text: str,
    previous_items: list[str],
    section_id: str | None = None,
) -> str:
    if not previous_items:
        return tex_text

    resolved = resolve_summary_section(tex_text, section_id)
    if not resolved["ok"]:
        return tex_text

    section = resolved["section"]
    begin_index = int(section["begin_index"])
    end_index = int(section["end_index"])
    replacements: list[tuple[int, int, str]] = []
    search_start = begin_index
    for item in previous_items:
        escaped_item = escape_tex(item)
        pattern = re.compile(
            r"(?m)^(?P<indent>[ \t]*)\\item[ \t]+"
            + re.escape(escaped_item)
            + r"[ \t]*$"
        )
        match = pattern.search(tex_text, search_start, end_index)
        if not match:
            continue
        replacements.append(
            (
                match.start(),
                match.end(),
                f"{match.group('indent')}\\item",
            )
        )
        search_start = match.end()

    updated = tex_text
    for start, end, replacement in sorted(replacements, reverse=True):
        updated = updated[:start] + replacement + updated[end:]
    return updated


def replace_report_summary_items(
    tex_text: str,
    previous_items: list[str],
    new_items: list[str],
    section_id: str | None = None,
) -> str:
    cleared_text = clear_report_summary_items(tex_text, previous_items, section_id)
    return insert_report_summary_items(cleared_text, new_items, section_id)


def should_summarize_transcript(text: str) -> bool:
    stripped = text.strip()
    if not stripped or stripped == "(no speech)":
        return False
    return not stripped.startswith("[transcription failed:")


def merge_document_with_server(submitted_text: str, base_text: str, current_text: str) -> str:
    submitted = ensure_auto_block(submitted_text)
    base = ensure_auto_block(base_text)
    current = ensure_auto_block(current_text)
    submitted_prefix, submitted_block, submitted_suffix = split_auto_block(submitted)
    _base_prefix, base_block, _base_suffix = split_auto_block(base)
    current_prefix, current_block, current_suffix = split_auto_block(current)
    submitted_header, submitted_footer = extract_block_header_footer(submitted_block)
    base_entries = parse_auto_entries(base_block)
    current_entries = parse_auto_entries(current_block)
    submitted_entries = parse_auto_entries(submitted_block)
    max_base_id = max(base_entries.keys(), default=0)
    for entry_id, body in sorted(current_entries.items()):
        if entry_id > max_base_id and entry_id not in submitted_entries:
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
    document_path: Path = field(default_factory=daily_document_path)
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
    auto_summarize: bool = field(default=True)
    logs: list[str] = field(default_factory=list)
    compile_log: str = field(default="")
    compile_ok: bool | None = field(default=None)
    pdf_path: str | None = field(default=None)
    llm_provider: str = field(default_factory=default_llm_provider)
    summary_section_id: str | None = field(default=None)
    report_summary_items_by_section: dict[str, list[str]] = field(default_factory=dict)
    report_summary_last_entry_index: int = field(default=0)
    report_summary_status: str = field(default="待機中")
    report_summary_error: str = field(default="")

    def __post_init__(self) -> None:
        self.lock = threading.RLock()
        self.controller: RecordingController | None = None
        self.document_history: dict[int, str] = {}
        self.summary_queue: queue.Queue[object] = queue.Queue()
        self.summary_worker = threading.Thread(
            target=self._summary_worker_loop,
            daemon=True,
        )
        text, encoding = prepare_daily_document(self.document_path)
        self.document_text = text
        self.document_encoding = encoding
        self.document_version = 1
        self.document_history[self.document_version] = text
        resolved = resolve_summary_section(text, self.summary_section_id)
        if resolved["ok"]:
            self.summary_section_id = resolved["section"]["id"]
        self.summary_worker.start()

    def add_log(self, message: str) -> None:
        with self.lock:
            stamp = datetime.now().strftime("%H:%M:%S")
            self.logs.append(f"{stamp} {message}")
            self.logs = self.logs[-120:]

    def _write_document(self, text: str) -> None:
        self.document_path.write_text(text, encoding=self.document_encoding)
        self.document_text = text
        self.document_version += 1
        self.document_history[self.document_version] = text
        if len(self.document_history) > 50:
            oldest_version = min(self.document_history)
            del self.document_history[oldest_version]

    def get_document(self) -> dict[str, Any]:
        with self.lock:
            return {
                "path": str(self.document_path.relative_to(ROOT_DIR)),
                "text": self.document_text,
                "version": self.document_version,
            }

    def save_document(self, submitted_text: str, submitted_version: int) -> dict[str, Any]:
        with self.lock:
            base_text = self.document_history.get(submitted_version)
            if base_text is None:
                raise RuntimeError(
                    "The document changed while you were editing. Reload the latest text and try again."
                )
            merged = merge_document_with_server(submitted_text, base_text, self.document_text)
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

    def set_auto_summarize(self, enabled: bool) -> None:
        with self.lock:
            self.auto_summarize = enabled
            if enabled:
                self.report_summary_status = "待機中"
        self.add_log(f"auto-summary={'on' if enabled else 'off'}")
        if enabled:
            self.request_auto_summary()

    def request_auto_summary(self) -> None:
        try:
            self.summary_queue.put_nowait(object())
        except Exception as exc:
            self.add_log(f"auto-summary queue failed: {exc}")

    def _summary_worker_loop(self) -> None:
        while True:
            self.summary_queue.get()
            while True:
                try:
                    self.summary_queue.get_nowait()
                except queue.Empty:
                    break
            try:
                self._summarize_pending_transcripts()
            except Exception as exc:
                with self.lock:
                    self.report_summary_status = "失敗"
                    self.report_summary_error = str(exc)
                self.add_log(f"auto-summary failed: {exc}")

    def _summarize_pending_transcripts(self) -> None:
        with self.lock:
            if not self.auto_summarize:
                return
            pending_entries = [
                entry
                for entry in self.transcript_entries
                if entry.index > self.report_summary_last_entry_index
                and should_summarize_transcript(entry.text)
            ]
            if not pending_entries:
                return
            recent_entries = [
                entry
                for entry in self.transcript_entries
                if should_summarize_transcript(entry.text)
            ][-SUMMARY_CONTEXT_SEGMENTS:]
            target = describe_report_summary_target(self.document_text, self.summary_section_id)
            if not target["ok"]:
                raise RuntimeError(str(target["reason"]))
            section_id = str(target["section_id"])
            section_label = str(target["section_label"])
            self.summary_section_id = section_id
            previous_items = list(self.report_summary_items_by_section.get(section_id, []))
            provider_id = self.llm_provider
            self.report_summary_status = "要約中"
            self.report_summary_error = ""

        items = summarize_incremental_report_items(
            provider_id,
            previous_items,
            recent_entries,
            pending_entries,
            section_label,
        )
        last_entry_index = max(entry.index for entry in pending_entries)
        with self.lock:
            updated_text = replace_report_summary_items(
                self.document_text,
                previous_items,
                items,
                section_id,
            )
            self._write_document(updated_text)
            self.report_summary_items_by_section[section_id] = items
            self.report_summary_last_entry_index = last_entry_index
            self.report_summary_status = "待機中"
            self.report_summary_error = ""
            version = self.document_version
        self.add_log(
            f"auto-summary updated section={section_label} items={len(items)} "
            f"through segment={last_entry_index:03d} provider={provider_id}"
        )
        return version

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
            self.report_summary_items_by_section = {}
            self.report_summary_last_entry_index = 0
            self.report_summary_status = "待機中"
            self.report_summary_error = ""
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
            should_request_summary = self.auto_summarize and should_summarize_transcript(entry.text)
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
        if should_request_summary:
            self.request_auto_summary()

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
        auto_summarize: bool,
        language: str | None,
    ) -> None:
        with self.lock:
            if self.recording_active:
                raise RuntimeError("Recording is already active.")
            self.auto_reflect = auto_reflect
            self.auto_summarize = auto_summarize
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

    def summarize_report_items(self, provider: str | None = None) -> dict[str, Any]:
        with self.lock:
            entries = list(self.transcript_entries)
            provider_id = provider or self.llm_provider
            target = describe_report_summary_target(self.document_text, self.summary_section_id)
            if not target["ok"]:
                raise RuntimeError(str(target["reason"]))
            section_id = str(target["section_id"])
            section_label = str(target["section_label"])
            self.summary_section_id = section_id
            previous_items = list(self.report_summary_items_by_section.get(section_id, []))

        items = summarize_transcript_entries(provider_id, entries, section_label)
        with self.lock:
            updated_text = replace_report_summary_items(
                self.document_text,
                previous_items,
                items,
                section_id,
            )
            self._write_document(updated_text)
            self.report_summary_items_by_section[section_id] = items
            self.report_summary_last_entry_index = max(
                (entry.index for entry in entries),
                default=self.report_summary_last_entry_index,
            )
            self.report_summary_status = "待機中"
            self.report_summary_error = ""
            version = self.document_version
        self.add_log(
            f"report summary inserted section={section_label} "
            f"items={len(items)} provider={provider_id}"
        )
        return {
            "ok": True,
            "provider": provider_id,
            "section_id": section_id,
            "section_label": section_label,
            "item_count": len(items),
            "items": items,
            "version": version,
        }

    def report_summary_target_snapshot(self) -> dict[str, Any]:
        with self.lock:
            target = describe_report_summary_target(self.document_text, self.summary_section_id)
            if target["ok"]:
                self.summary_section_id = str(target["section_id"])
            entries = list(self.transcript_entries)
            document_path = str(self.document_path.relative_to(ROOT_DIR))
            summary_count = len(
                self.report_summary_items_by_section.get(self.summary_section_id or "", [])
            )
            auto_summarize = self.auto_summarize
            summary_status = self.report_summary_status
            summary_error = self.report_summary_error
            last_summarized_segment = self.report_summary_last_entry_index

        transcript_text = transcript_entries_to_text(entries)
        return {
            "document_path": document_path,
            "transcript_entry_count": len(entries),
            "transcript_text_chars": len(transcript_text),
            "report_summary_count": summary_count,
            "auto_summarize": auto_summarize,
            "summary_status": summary_status,
            "summary_error": summary_error,
            "last_summarized_segment": last_summarized_segment,
            **target,
        }

    def set_summary_section(self, section_id: str) -> dict[str, Any]:
        with self.lock:
            target = describe_report_summary_target(self.document_text, section_id)
            if not target["ok"]:
                raise RuntimeError(str(target["reason"]))
            self.summary_section_id = str(target["section_id"])
            self.report_summary_status = "待機中"
            self.report_summary_error = ""
            selected = self.summary_section_id
            label = str(target["section_label"])
        self.add_log(f"summary-section={label}")
        return {
            "ok": True,
            "selected": selected,
            "label": label,
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
                "auto_summarize": self.auto_summarize,
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
                "summary_section_id": self.summary_section_id,
                "report_summary_count": len(
                    self.report_summary_items_by_section.get(self.summary_section_id or "", [])
                ),
                "report_summary_status": self.report_summary_status,
                "report_summary_error": self.report_summary_error,
                "last_summarized_segment": self.report_summary_last_entry_index,
            }


manager = MeetingAppState()


class StartRequest(BaseModel):
    device: int | None = None
    transcriber: str = "local"
    auto_reflect: bool = True
    auto_summarize: bool = True
    language: str | None = "ja"


class SaveDocumentRequest(BaseModel):
    text: str
    version: int


class AutoReflectRequest(BaseModel):
    enabled: bool


class AutoSummaryRequest(BaseModel):
    enabled: bool


class SummarySectionRequest(BaseModel):
    section_id: str


class LlmProviderRequest(BaseModel):
    provider: str


class LlmChatRequest(BaseModel):
    prompt: str
    system: str | None = None
    provider: str | None = None


class ReportSummaryRequest(BaseModel):
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
    try:
        return manager.save_document(request.text, request.version)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/recording/start")
def start_recording(request: StartRequest) -> dict[str, Any]:
    try:
        manager.start_recording(
            device=request.device,
            transcriber_mode=request.transcriber,
            auto_reflect=request.auto_reflect,
            auto_summarize=request.auto_summarize,
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


@app.post("/api/auto-summary")
def set_auto_summary(request: AutoSummaryRequest) -> dict[str, Any]:
    manager.set_auto_summarize(request.enabled)
    return {"ok": True}


@app.post("/api/report-summary/section")
def set_report_summary_section(request: SummarySectionRequest) -> dict[str, Any]:
    try:
        return manager.set_summary_section(request.section_id)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


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


@app.post("/api/report-summary")
def summarize_report_items(request: ReportSummaryRequest) -> dict[str, Any]:
    try:
        return manager.summarize_report_items(provider=request.provider)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/report-summary/target")
def get_report_summary_target() -> dict[str, Any]:
    return manager.report_summary_target_snapshot()


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
