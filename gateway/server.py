"""
LoClicky Local Gateway

FastAPI gateway that binds only to 127.0.0.1:5000 and acts as the single
integration boundary for the macOS Swift frontend. All routing decisions for
chat, transcription, and TTS are driven by config.json.
"""

from __future__ import annotations

import atexit
import base64
import io
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import traceback
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

import httpx
import uvicorn
from cryptography.fernet import Fernet
from fastapi import FastAPI, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator


APP_NAME = "LoClicky"
LOOPBACK_GATEWAY_HOST = "127.0.0.1"
LOOPBACK_PROVIDER_HOSTS = {LOOPBACK_GATEWAY_HOST, "localhost", "::1"}
OFFLINE_FASTER_WHISPER_MODEL_PATH = "./gateway/whisper-model"

SUPPORTED_CHAT_PROVIDERS = {
    "local": {"ollama", "llama_cpp"},
    "cloud": {"anthropic", "openai"},
}
SUPPORTED_TRANSCRIPTION_PROVIDERS = {
    "local": {"faster_whisper"},
    "cloud": {"openai", "assemblyai"},
}
SUPPORTED_TTS_PROVIDERS = {
    "local": {"macos_say"},
    "cloud": {"elevenlabs"},
}


def resolve_config_path() -> Path:
    candidate_paths: list[Path] = []

    if getattr(sys, "frozen", False):
        candidate_paths.append(Path(sys.executable).resolve().parent / "config.json")

    pyinstaller_bundle_dir = getattr(sys, "_MEIPASS", None)
    if pyinstaller_bundle_dir:
        candidate_paths.append(Path(pyinstaller_bundle_dir) / "config.json")

    candidate_paths.append(Path(__file__).resolve().parent / "config.json")

    for candidate_path in candidate_paths:
        if candidate_path.exists():
            return candidate_path

    return candidate_paths[0]


CONFIG_PATH = resolve_config_path()


def load_config() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        raise FileNotFoundError(
            f"config.json was not found. Expected it at '{CONFIG_PATH}'."
        )

    with CONFIG_PATH.open("r", encoding="utf-8") as config_file:
        loaded_config = json.load(config_file)

    if not isinstance(loaded_config, dict):
        raise ValueError("config.json must contain a top-level JSON object.")

    return loaded_config


CONFIG = load_config()


def validate_provider_selection(
    *,
    domain_name: str,
    domain_config: dict[str, Any],
    supported_providers: dict[str, set[str]],
) -> None:
    mode = domain_config.get("mode")
    if mode not in {"local", "cloud"}:
        raise ValueError(
            f"config.json: '{domain_name}.mode' must be 'local' or 'cloud'."
        )

    provider_block = domain_config.get(mode)
    if not isinstance(provider_block, dict):
        raise ValueError(
            f"config.json: '{domain_name}.{mode}' must be an object."
        )

    provider_name = provider_block.get("provider")
    if not isinstance(provider_name, str) or not provider_name:
        raise ValueError(
            f"config.json: '{domain_name}.{mode}.provider' must be set."
        )

    if provider_name not in supported_providers[mode]:
        supported_list = ", ".join(sorted(supported_providers[mode]))
        raise ValueError(
            f"config.json: unsupported provider '{provider_name}' for "
            f"'{domain_name}.{mode}'. Supported providers: {supported_list}."
        )


def validate_required_cloud_key(
    *,
    domain_name: str,
    provider_block: dict[str, Any],
    key_name: str,
) -> None:
    key_value = provider_block.get(key_name)
    if not isinstance(key_value, str) or not key_value.strip():
        raise ValueError(
            f"config.json: '{domain_name}.cloud' is active but '{key_name}' is empty."
        )


def validate_loopback_url(url_value: str, config_key: str) -> None:
    parsed_url = urlparse(url_value)
    if parsed_url.scheme not in {"http", "https"}:
        raise ValueError(
            f"config.json: '{config_key}' must use http or https."
        )

    if parsed_url.hostname not in LOOPBACK_PROVIDER_HOSTS:
        raise ValueError(
            f"config.json: '{config_key}' must point to a loopback host."
        )


def validate_config(config: dict[str, Any]) -> None:
    gateway_config = config.get("gateway")
    if not isinstance(gateway_config, dict):
        raise ValueError("config.json: 'gateway' must be an object.")

    if gateway_config.get("host") != LOOPBACK_GATEWAY_HOST:
        raise ValueError(
            "config.json: 'gateway.host' must be exactly '127.0.0.1'."
        )

    validate_provider_selection(
        domain_name="chat",
        domain_config=config.get("chat", {}),
        supported_providers=SUPPORTED_CHAT_PROVIDERS,
    )
    validate_provider_selection(
        domain_name="transcription",
        domain_config=config.get("transcription", {}),
        supported_providers=SUPPORTED_TRANSCRIPTION_PROVIDERS,
    )
    validate_provider_selection(
        domain_name="tts",
        domain_config=config.get("tts", {}),
        supported_providers=SUPPORTED_TTS_PROVIDERS,
    )

    history_config = config.get("history")
    if history_config is None:
        raise ValueError("config.json: 'history' must be present.")
    if not isinstance(history_config, dict):
        raise ValueError("config.json: 'history' must be an object.")

    chat_config = config["chat"]
    if chat_config["mode"] == "local":
        local_chat_config = chat_config["local"]
        if local_chat_config["provider"] == "ollama":
            validate_loopback_url(
                local_chat_config.get("ollama_base_url", ""),
                "chat.local.ollama_base_url",
            )
        elif local_chat_config["provider"] == "llama_cpp":
            validate_loopback_url(
                local_chat_config.get("llama_cpp_base_url", ""),
                "chat.local.llama_cpp_base_url",
            )
    else:
        cloud_chat_config = chat_config["cloud"]
        if cloud_chat_config["provider"] == "anthropic":
            validate_required_cloud_key(
                domain_name="chat",
                provider_block=cloud_chat_config,
                key_name="anthropic_api_key",
            )
        elif cloud_chat_config["provider"] == "openai":
            validate_required_cloud_key(
                domain_name="chat",
                provider_block=cloud_chat_config,
                key_name="openai_api_key",
            )

    transcription_config = config["transcription"]
    if transcription_config["mode"] == "cloud":
        cloud_transcription_config = transcription_config["cloud"]
        if cloud_transcription_config["provider"] == "openai":
            validate_required_cloud_key(
                domain_name="transcription",
                provider_block=cloud_transcription_config,
                key_name="openai_api_key",
            )
        elif cloud_transcription_config["provider"] == "assemblyai":
            validate_required_cloud_key(
                domain_name="transcription",
                provider_block=cloud_transcription_config,
                key_name="assemblyai_api_key",
            )

    tts_config = config["tts"]
    if tts_config["mode"] == "cloud":
        cloud_tts_config = tts_config["cloud"]
        if cloud_tts_config["provider"] == "elevenlabs":
            validate_required_cloud_key(
                domain_name="tts",
                provider_block=cloud_tts_config,
                key_name="elevenlabs_api_key",
            )


validate_config(CONFIG)


LOG_LEVEL = str(CONFIG.get("gateway", {}).get("log_level", "info")).upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
)
logger = logging.getLogger("gateway")

audit_logger = logging.getLogger("audit")
audit_logger.setLevel(logging.INFO)
audit_logger.propagate = False


DATA_DIR = Path.home() / "Library" / "Application Support" / APP_NAME
DATA_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH = DATA_DIR / "history.db"
KEY_PATH = DATA_DIR / "history.key"
AUDIT_LOG_PATH = DATA_DIR / "audit.log"
LLAMA_CPP_LOG_PATH = DATA_DIR / "llama-server.log"

for existing_handler in list(audit_logger.handlers):
    audit_logger.removeHandler(existing_handler)

audit_handler = logging.FileHandler(AUDIT_LOG_PATH)
audit_handler.setFormatter(logging.Formatter("%(message)s"))
audit_logger.addHandler(audit_handler)


def log_audit_event(
    *,
    event_type: str,
    provider: str,
    latency_ms: float,
    extra: Optional[dict[str, Any]] = None,
) -> None:
    event_payload: dict[str, Any] = {
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "event_type": event_type,
        "provider": provider,
        "latency_ms": round(latency_ms, 2),
    }
    if extra:
        event_payload.update(extra)

    audit_logger.info(json.dumps(event_payload))


def load_or_create_encryption_key() -> bytes:
    if KEY_PATH.exists():
        return KEY_PATH.read_bytes()

    new_key = Fernet.generate_key()
    KEY_PATH.write_bytes(new_key)
    os.chmod(KEY_PATH, 0o600)
    logger.info("Generated a new history encryption key at %s", KEY_PATH)
    return new_key


fernet = Fernet(load_or_create_encryption_key())


def initialize_history_database() -> None:
    with sqlite3.connect(DB_PATH) as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS conversation_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                prompt_enc BLOB NOT NULL,
                detailed_text_enc BLOB NOT NULL,
                action_coordinates TEXT
            )
            """
        )


initialize_history_database()


def history_is_enabled() -> bool:
    return bool(CONFIG.get("history", {}).get("enabled", True))


def emit_startup_status(message: str) -> None:
    print(f"[SETUP] {message}", flush=True)


def emit_startup_warning(message: str) -> None:
    print(f"[SETUP_WARN] {message}", flush=True)


def emit_startup_error(message: str) -> None:
    print(f"[SETUP_ERROR] {message}", flush=True)


@dataclass(frozen=True)
class LlamaCppLaunchProfile:
    huggingface_repo: str
    model_alias: str
    context_size: int


KNOWN_LLAMA_CPP_MODEL_PROFILES: dict[str, LlamaCppLaunchProfile] = {
    "gemma-3-4b-it": LlamaCppLaunchProfile(
        huggingface_repo="ggml-org/gemma-3-4b-it-GGUF",
        model_alias="gemma-3-4b-it",
        context_size=4096,
    ),
    "smolvlm-instruct": LlamaCppLaunchProfile(
        huggingface_repo="ggml-org/SmolVLM-Instruct-GGUF",
        model_alias="smolvlm-instruct",
        context_size=4096,
    ),
}

MANAGED_LLAMA_CPP_PROCESS: Optional[subprocess.Popen[bytes]] = None
MANAGED_LLAMA_CPP_LOG_FILE: Optional[io.BufferedWriter] = None


def build_llama_server_environment() -> dict[str, str]:
    environment = dict(os.environ)
    current_path_entries = [
        path_entry for path_entry in environment.get("PATH", "").split(os.pathsep) if path_entry
    ]
    preferred_binary_directories = ["/opt/homebrew/bin", "/usr/local/bin"]

    combined_path_entries: list[str] = []
    for path_entry in preferred_binary_directories + current_path_entries:
        if path_entry not in combined_path_entries:
            combined_path_entries.append(path_entry)

    environment["PATH"] = os.pathsep.join(combined_path_entries)
    return environment


def resolve_llama_server_executable_path() -> str:
    environment = build_llama_server_environment()
    llama_server_path = shutil.which("llama-server", path=environment["PATH"])
    if llama_server_path:
        return llama_server_path

    raise FileNotFoundError(
        "llama-server was not found on PATH. Install llama.cpp so the gateway "
        "can launch the local vision model automatically."
    )


def parse_loopback_service_host_and_port(base_url: str) -> tuple[str, int]:
    parsed_url = urlparse(base_url)
    scheme = parsed_url.scheme or "http"
    if scheme != "http":
        raise ValueError(
            f"Automatic llama.cpp startup only supports http loopback URLs, got {base_url!r}."
        )

    parsed_host = parsed_url.hostname or LOOPBACK_GATEWAY_HOST
    parsed_port = parsed_url.port or 8081
    normalized_host = "127.0.0.1" if parsed_host == "localhost" else parsed_host
    return normalized_host, parsed_port


def resolve_llama_cpp_launch_profile(
    local_chat_config: dict[str, Any],
) -> Optional[LlamaCppLaunchProfile]:
    explicit_huggingface_repo = local_chat_config.get("llama_cpp_hf_repo")
    expected_model_alias = local_chat_config.get("model", "gemma-3-4b-it")
    explicit_model_alias = local_chat_config.get(
        "llama_cpp_model_alias", expected_model_alias
    )
    explicit_context_size = int(
        local_chat_config.get("llama_cpp_ctx_size", 4096)
    )

    if explicit_huggingface_repo:
        return LlamaCppLaunchProfile(
            huggingface_repo=str(explicit_huggingface_repo),
            model_alias=str(explicit_model_alias),
            context_size=explicit_context_size,
        )

    return KNOWN_LLAMA_CPP_MODEL_PROFILES.get(expected_model_alias)


def fetch_llama_cpp_available_model_names(
    *,
    llama_cpp_base_url: str,
    timeout_seconds: float,
) -> set[str]:
    with httpx.Client(timeout=timeout_seconds) as client:
        models_response = client.get(f"{llama_cpp_base_url}/v1/models")
        models_response.raise_for_status()

    response_payload = models_response.json()
    return {
        model_record.get("id", "")
        for model_record in response_payload.get("data", [])
    }


def stop_managed_llama_cpp_server() -> None:
    global MANAGED_LLAMA_CPP_PROCESS
    global MANAGED_LLAMA_CPP_LOG_FILE

    managed_process = MANAGED_LLAMA_CPP_PROCESS
    if managed_process is not None and managed_process.poll() is None:
        logger.info(
            "Stopping managed llama.cpp server (PID %s).",
            managed_process.pid,
        )
        managed_process.terminate()
        try:
            managed_process.wait(timeout=10.0)
        except subprocess.TimeoutExpired:
            managed_process.kill()
            managed_process.wait(timeout=5.0)

    if MANAGED_LLAMA_CPP_LOG_FILE is not None:
        MANAGED_LLAMA_CPP_LOG_FILE.close()

    MANAGED_LLAMA_CPP_PROCESS = None
    MANAGED_LLAMA_CPP_LOG_FILE = None


def ensure_llama_cpp_server_is_running(local_chat_config: dict[str, Any]) -> None:
    global MANAGED_LLAMA_CPP_PROCESS
    global MANAGED_LLAMA_CPP_LOG_FILE

    llama_cpp_base_url = local_chat_config.get(
        "llama_cpp_base_url", "http://127.0.0.1:8081"
    )
    expected_model = local_chat_config.get("model", "gemma-3-4b-it")
    startup_timeout_seconds = float(
        local_chat_config.get("warmup_timeout_seconds", 120)
    )

    try:
        available_model_names = fetch_llama_cpp_available_model_names(
            llama_cpp_base_url=llama_cpp_base_url,
            timeout_seconds=2.0,
        )
    except Exception:
        available_model_names = set()
    else:
        if expected_model in available_model_names:
            return
        if available_model_names and (
            MANAGED_LLAMA_CPP_PROCESS is None
            or MANAGED_LLAMA_CPP_PROCESS.poll() is not None
        ):
            available_models_summary = ", ".join(sorted(available_model_names))
            raise RuntimeError(
                f"Another llama.cpp server is already responding at "
                f"{llama_cpp_base_url} with model(s): {available_models_summary}. "
                f"Stop that server or switch config.json back to one of those aliases."
            )

    if MANAGED_LLAMA_CPP_PROCESS is not None and MANAGED_LLAMA_CPP_PROCESS.poll() is not None:
        if MANAGED_LLAMA_CPP_LOG_FILE is not None:
            MANAGED_LLAMA_CPP_LOG_FILE.close()
        MANAGED_LLAMA_CPP_PROCESS = None
        MANAGED_LLAMA_CPP_LOG_FILE = None

    if MANAGED_LLAMA_CPP_PROCESS is not None and MANAGED_LLAMA_CPP_PROCESS.poll() is None:
        stop_managed_llama_cpp_server()

    launch_profile = resolve_llama_cpp_launch_profile(local_chat_config)
    if launch_profile is None:
        raise ValueError(
            f"No automatic llama.cpp launch profile is defined for model "
            f"{expected_model!r}. Add 'llama_cpp_hf_repo' to config.json or "
            f"start llama-server manually."
        )

    resolved_host, resolved_port = parse_loopback_service_host_and_port(
        llama_cpp_base_url
    )
    llama_server_executable_path = resolve_llama_server_executable_path()
    llama_server_environment = build_llama_server_environment()

    emit_startup_status(
        f"Starting local llama.cpp model '{launch_profile.model_alias}'."
    )

    LLAMA_CPP_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    MANAGED_LLAMA_CPP_LOG_FILE = open(LLAMA_CPP_LOG_PATH, "ab")
    MANAGED_LLAMA_CPP_PROCESS = subprocess.Popen(
        [
            llama_server_executable_path,
            "--hf-repo",
            launch_profile.huggingface_repo,
            "--alias",
            launch_profile.model_alias,
            "--host",
            resolved_host,
            "--port",
            str(resolved_port),
            "--ctx-size",
            str(launch_profile.context_size),
            "--no-webui",
        ],
        cwd=str(CONFIG_PATH.parent),
        env=llama_server_environment,
        stdout=MANAGED_LLAMA_CPP_LOG_FILE,
        stderr=subprocess.STDOUT,
    )

    readiness_deadline = time.time() + startup_timeout_seconds
    while time.time() < readiness_deadline:
        if MANAGED_LLAMA_CPP_PROCESS.poll() is not None:
            exit_code = MANAGED_LLAMA_CPP_PROCESS.returncode
            stop_managed_llama_cpp_server()
            raise RuntimeError(
                f"Managed llama.cpp server exited with code {exit_code}. "
                f"See {LLAMA_CPP_LOG_PATH} for details."
            )

        try:
            available_model_names = fetch_llama_cpp_available_model_names(
                llama_cpp_base_url=llama_cpp_base_url,
                timeout_seconds=2.0,
            )
        except Exception:
            time.sleep(1.0)
            continue

        if expected_model in available_model_names:
            emit_startup_status(
                f"Local llama.cpp model '{expected_model}' is ready."
            )
            return

        time.sleep(1.0)

    stop_managed_llama_cpp_server()
    raise TimeoutError(
        f"Timed out waiting for llama.cpp model '{expected_model}' to become ready. "
        f"See {LLAMA_CPP_LOG_PATH} for details."
    )


atexit.register(stop_managed_llama_cpp_server)


PRELOADED_FASTER_WHISPER_MODEL: Any = None


def resolve_offline_whisper_model_path(
    local_transcription_config: dict[str, Any]
) -> str:
    configured_model_path = local_transcription_config.get("model_path")
    if configured_model_path and configured_model_path != OFFLINE_FASTER_WHISPER_MODEL_PATH:
        raise RuntimeError(
            "transcription.local.model_path must remain './gateway/whisper-model' "
            "for air-gapped faster-whisper deployments."
        )

    repo_relative_model_path = Path(OFFLINE_FASTER_WHISPER_MODEL_PATH)
    if repo_relative_model_path.is_dir():
        return OFFLINE_FASTER_WHISPER_MODEL_PATH

    bundled_model_path = Path(__file__).resolve().parent / "whisper-model"
    if bundled_model_path.is_dir():
        return str(bundled_model_path)

    raise RuntimeError(
        "No offline faster-whisper model directory was found. Expected "
        f"'{OFFLINE_FASTER_WHISPER_MODEL_PATH}' or '{bundled_model_path}'."
    )


def verify_faster_whisper_model_files_exist(
    local_transcription_config: dict[str, Any]
) -> str:
    offline_model_path = Path(resolve_offline_whisper_model_path(local_transcription_config))
    required_model_files = [
        offline_model_path / "config.json",
        offline_model_path / "model.bin",
        offline_model_path / "tokenizer.json",
    ]
    missing_model_files = [
        str(required_model_file)
        for required_model_file in required_model_files
        if not required_model_file.exists()
    ]
    if missing_model_files:
        missing_files_summary = ", ".join(missing_model_files)
        raise RuntimeError(
            "Offline faster-whisper model files are missing: "
            f"{missing_files_summary}"
        )

    return str(offline_model_path)


def load_faster_whisper_model(local_transcription_config: dict[str, Any]) -> Any:
    global PRELOADED_FASTER_WHISPER_MODEL

    if PRELOADED_FASTER_WHISPER_MODEL is not None:
        return PRELOADED_FASTER_WHISPER_MODEL

    from faster_whisper import WhisperModel  # type: ignore[import-untyped]

    offline_model_path = verify_faster_whisper_model_files_exist(
        local_transcription_config
    )
    device = local_transcription_config.get("device", "cpu")
    compute_type = local_transcription_config.get("compute_type", "int8")
    if offline_model_path == OFFLINE_FASTER_WHISPER_MODEL_PATH:
        PRELOADED_FASTER_WHISPER_MODEL = WhisperModel(
            model_size_or_path="./gateway/whisper-model",
            device=device,
            compute_type=compute_type,
            local_files_only=True,
        )
    else:
        PRELOADED_FASTER_WHISPER_MODEL = WhisperModel(
            model_size_or_path=offline_model_path,
            device=device,
            compute_type=compute_type,
            local_files_only=True,
        )

    return PRELOADED_FASTER_WHISPER_MODEL


def prewarm_ollama_chat_model(local_chat_config: dict[str, Any]) -> None:
    if not local_chat_config.get("prewarm_on_startup", True):
        return

    ollama_base_url = local_chat_config.get(
        "ollama_base_url", "http://127.0.0.1:11434"
    )
    expected_model = local_chat_config.get("model", "llava:13b")
    warmup_timeout_seconds = float(
        local_chat_config.get("warmup_timeout_seconds", 300)
    )

    emit_startup_status(f"Prewarming Ollama model '{expected_model}'.")

    warmup_payload = {
        "model": expected_model,
        "messages": [
            {"role": "system", "content": "Reply with exactly ok."},
            {"role": "user", "content": "Say ok."},
        ],
        "stream": False,
        "options": {
            "temperature": 0.0,
            "num_predict": 8,
        },
    }

    with httpx.Client(timeout=warmup_timeout_seconds) as client:
        warmup_response = client.post(
            f"{ollama_base_url}/api/chat",
            json=warmup_payload,
        )
        warmup_response.raise_for_status()

    emit_startup_status(f"Ollama model '{expected_model}' is warm.")


def prewarm_llama_cpp_chat_model(local_chat_config: dict[str, Any]) -> None:
    if not local_chat_config.get("prewarm_on_startup", False):
        return

    llama_cpp_base_url = local_chat_config.get(
        "llama_cpp_base_url", "http://127.0.0.1:8081"
    )
    expected_model = local_chat_config.get("model", "gemma-3-4b-it")
    warmup_timeout_seconds = float(
        local_chat_config.get("warmup_timeout_seconds", 120)
    )

    emit_startup_status(f"Prewarming llama.cpp model '{expected_model}'.")

    warmup_payload = {
        "model": expected_model,
        "messages": [
            {"role": "system", "content": "Reply with exactly ok."},
            {"role": "user", "content": "Say ok."},
        ],
        "stream": False,
        "max_tokens": 8,
        "temperature": 0.0,
    }

    with httpx.Client(timeout=warmup_timeout_seconds) as client:
        warmup_response = client.post(
            f"{llama_cpp_base_url}/v1/chat/completions",
            json=warmup_payload,
        )
        warmup_response.raise_for_status()

    emit_startup_status(f"llama.cpp model '{expected_model}' is warm.")


def check_local_provider_readiness(config: dict[str, Any]) -> None:
    chat_config = config["chat"]
    if chat_config["mode"] == "local":
        local_chat_config = chat_config["local"]
        local_chat_provider_name = local_chat_config["provider"]

        if local_chat_provider_name == "ollama":
            ollama_base_url = local_chat_config.get(
                "ollama_base_url", "http://127.0.0.1:11434"
            )
            expected_model = local_chat_config.get("model", "llava:13b")

            try:
                with httpx.Client(timeout=5.0) as client:
                    tags_response = client.get(f"{ollama_base_url}/api/tags")
                    tags_response.raise_for_status()
            except Exception as ollama_error:
                logger.warning("Ollama is not reachable: %s", ollama_error)
                emit_startup_warning(
                    f"Ollama not reachable at {ollama_base_url}. Start Ollama before using local chat."
                )
            else:
                response_payload = tags_response.json()
                available_model_names = {
                    model_record.get("name", "")
                    for model_record in response_payload.get("models", [])
                }
                if expected_model not in available_model_names:
                    emit_startup_warning(
                        f"Ollama model '{expected_model}' is not installed locally. "
                        "Install it in Ollama before using local chat."
                    )
                else:
                    try:
                        prewarm_ollama_chat_model(local_chat_config)
                    except Exception as ollama_warmup_error:
                        logger.warning(
                            "Ollama model warmup failed: %s", ollama_warmup_error
                        )
                        emit_startup_warning(
                            f"Ollama model '{expected_model}' warmup failed: "
                            f"{ollama_warmup_error}"
                        )
        elif local_chat_provider_name == "llama_cpp":
            llama_cpp_base_url = local_chat_config.get(
                "llama_cpp_base_url", "http://127.0.0.1:8081"
            )
            expected_model = local_chat_config.get("model", "gemma-3-4b-it")

            try:
                ensure_llama_cpp_server_is_running(local_chat_config)
            except Exception as llama_cpp_error:
                logger.warning("llama.cpp is not reachable: %s", llama_cpp_error)
                emit_startup_warning(
                    f"llama.cpp not reachable at {llama_cpp_base_url}: {llama_cpp_error}"
                )
            else:
                available_model_names = fetch_llama_cpp_available_model_names(
                    llama_cpp_base_url=llama_cpp_base_url,
                    timeout_seconds=5.0,
                )
                if expected_model not in available_model_names:
                    emit_startup_warning(
                        f"llama.cpp model '{expected_model}' is not loaded locally. "
                        "Start llama-server with the matching alias before using local chat."
                    )
                else:
                    try:
                        prewarm_llama_cpp_chat_model(local_chat_config)
                    except Exception as llama_cpp_warmup_error:
                        logger.warning(
                            "llama.cpp model warmup failed: %s",
                            llama_cpp_warmup_error,
                        )
                        emit_startup_warning(
                            f"llama.cpp model '{expected_model}' warmup failed: "
                            f"{llama_cpp_warmup_error}"
                        )

    transcription_config = config["transcription"]
    if (
        transcription_config["mode"] == "local"
        and transcription_config["local"]["provider"] == "faster_whisper"
    ):
        local_transcription_config = transcription_config["local"]
        configured_model_path = local_transcription_config.get("model_path")
        configured_model_name = local_transcription_config.get("model", "base")
        model_identifier = configured_model_path or configured_model_name

        emit_startup_status(
            f"Checking faster-whisper model availability for '{model_identifier}'."
        )
        try:
            verify_faster_whisper_model_files_exist(local_transcription_config)
        except Exception as whisper_error:
            logger.warning("faster-whisper model is not ready locally: %s", whisper_error)
            emit_startup_warning(
                f"Local faster-whisper model '{model_identifier}' is not ready: {whisper_error}"
            )
        else:
            emit_startup_status(
                f"faster-whisper model '{model_identifier}' is available and will load on first use."
            )


check_local_provider_readiness(CONFIG)
emit_startup_status("Gateway ready.")


class ConversationHistoryEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_prompt: str
    assistant_response: str


class ScreenCapturePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str
    mime_type: str
    image_base64: str
    screenshot_width_in_pixels: int
    screenshot_height_in_pixels: int
    display_width_in_points: int
    display_height_in_points: int
    is_cursor_screen: bool


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    transcript: str
    conversation_history: list[ConversationHistoryEntry] = Field(default_factory=list)
    screen_captures: list[ScreenCapturePayload] = Field(default_factory=list)
    supports_pointing: bool = False
    requested_response_format: str = "dual_channel"

    @field_validator("transcript")
    @classmethod
    def transcript_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("transcript must not be empty.")
        return value

    @field_validator("requested_response_format")
    @classmethod
    def requested_response_format_must_match_contract(cls, value: str) -> str:
        if value != "dual_channel":
            raise ValueError(
                "requested_response_format must be 'dual_channel'."
            )
        return value


class PointTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    x: float
    y: float
    label: Optional[str] = None
    screen_index: Optional[int] = None


class ChatResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    spoken_summary: str
    detailed_text: str
    point_target: Optional[PointTarget] = None

    @field_validator("spoken_summary", "detailed_text")
    @classmethod
    def response_fields_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("response fields must not be empty.")
        return value


class TranscribeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    audio_wav_base64: str
    keyterms: list[str] = Field(default_factory=list)


class TranscribeResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    transcript: str


class TTSRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str

    @field_validator("text")
    @classmethod
    def text_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("text must not be empty.")
        return value


class HistoryItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int
    timestamp: str
    user_prompt: str
    assistant_detailed_text: str
    action_coordinates: Optional[str] = None


class HistoryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    history: list[HistoryItem]


class LLMStructuredOutputError(Exception):
    pass


@dataclass(frozen=True)
class TTSResult:
    audio_bytes: bytes
    media_type: str


CHAT_SYSTEM_PROMPT = """\
You are Clicky, a concise macOS assistant.
Return exactly one JSON object and nothing else.
Required keys: "spoken_summary", "detailed_text".
Optional key: "point_target".
"spoken_summary" should sound natural and stay under 30 words.
"detailed_text" should be concise and useful.
Only include "point_target" when the user explicitly asks you to find, locate,
or point at something visible on screen.
If you include "point_target", use screenshot-relative pixel coordinates and the
0-based "screen_index" from the provided screen capture list.
If you are not confident about coordinates, omit "point_target".
"""


def build_screen_capture_context(screen_captures: list[ScreenCapturePayload]) -> str:
    if not screen_captures:
        return "screen_captures: none"

    lines = ["screen_captures:"]
    for screen_index, screen_capture in enumerate(screen_captures):
        lines.append(
            (
                f"- index={screen_index}; label={screen_capture.label!r}; "
                f"pixels={screen_capture.screenshot_width_in_pixels}x"
                f"{screen_capture.screenshot_height_in_pixels}; "
                f"points={screen_capture.display_width_in_points}x"
                f"{screen_capture.display_height_in_points}; "
                f"cursor_screen={screen_capture.is_cursor_screen}"
            )
        )
    return "\n".join(lines)


def build_user_prompt_content(
    transcript: str,
    screen_captures: list[ScreenCapturePayload],
) -> str:
    screen_capture_context = build_screen_capture_context(screen_captures)
    return f"transcript:\n{transcript}\n\n{screen_capture_context}"


def extract_json_object(raw_content: str) -> str:
    cleaned_content = raw_content.strip()

    if cleaned_content.startswith("```"):
        cleaned_lines = cleaned_content.splitlines()
        if cleaned_lines and cleaned_lines[0].startswith("```"):
            cleaned_lines = cleaned_lines[1:]
        if cleaned_lines and cleaned_lines[-1].startswith("```"):
            cleaned_lines = cleaned_lines[:-1]
        cleaned_content = "\n".join(cleaned_lines).strip()

    if cleaned_content.startswith("{") and cleaned_content.endswith("}"):
        return cleaned_content

    object_start_index = cleaned_content.find("{")
    object_end_index = cleaned_content.rfind("}")
    if object_start_index == -1 or object_end_index == -1:
        raise LLMStructuredOutputError("No JSON object was found in the model output.")

    return cleaned_content[object_start_index : object_end_index + 1]


def parse_chat_response(
    raw_content: str,
    *,
    supports_pointing: bool,
) -> ChatResponse:
    try:
        cleaned_json = extract_json_object(raw_content)
    except (ValueError, json.JSONDecodeError) as parse_error:
        raise LLMStructuredOutputError(str(parse_error)) from parse_error

    try:
        parsed_response = ChatResponse.model_validate_json(cleaned_json)
    except ValidationError as parse_error:
        try:
            parsed_payload = json.loads(cleaned_json)
        except Exception:
            raise LLMStructuredOutputError(str(parse_error)) from parse_error

        if not isinstance(parsed_payload, dict):
            raise LLMStructuredOutputError(str(parse_error)) from parse_error

        spoken_summary = parsed_payload.get("spoken_summary")
        detailed_text = parsed_payload.get("detailed_text")
        if not isinstance(spoken_summary, str) or not isinstance(detailed_text, str):
            raise LLMStructuredOutputError(str(parse_error)) from parse_error

        sanitized_payload: dict[str, Any] = {
            "spoken_summary": spoken_summary,
            "detailed_text": detailed_text,
        }

        point_target_payload = parsed_payload.get("point_target")
        if isinstance(point_target_payload, dict):
            try:
                sanitized_payload["point_target"] = PointTarget.model_validate(
                    point_target_payload
                ).model_dump()
            except ValidationError:
                sanitized_payload["point_target"] = None
        else:
            sanitized_payload["point_target"] = None

        try:
            parsed_response = ChatResponse.model_validate(sanitized_payload)
        except ValidationError as sanitized_parse_error:
            raise LLMStructuredOutputError(str(sanitized_parse_error)) from parse_error

    if not supports_pointing and parsed_response.point_target is not None:
        parsed_response = parsed_response.model_copy(update={"point_target": None})

    return parsed_response


def transcript_explicitly_requests_pointing(transcript: str) -> bool:
    normalized_transcript = transcript.lower()
    pointing_patterns = [
        r"\bpoint\b",
        r"\bpoint at\b",
        r"\bshow me where\b",
        r"\blocate\b",
        r"\bfind\b",
        r"\bwhere is\b",
        r"\bhighlight\b",
        r"\bwhich button\b",
        r"\bwhich menu\b",
        r"\bwhich icon\b",
    ]
    return any(
        re.search(pointing_pattern, normalized_transcript)
        for pointing_pattern in pointing_patterns
    )


class BaseChatProvider(ABC):
    @abstractmethod
    async def generate_response(
        self,
        *,
        transcript: str,
        conversation_history: list[ConversationHistoryEntry],
        screen_captures: list[ScreenCapturePayload],
        supports_pointing: bool,
    ) -> ChatResponse:
        raise NotImplementedError


class OllamaChatProvider(BaseChatProvider):
    def __init__(self, local_chat_config: dict[str, Any]):
        self.base_url = local_chat_config.get(
            "ollama_base_url", "http://127.0.0.1:11434"
        )
        self.model = local_chat_config.get("model", "llava:13b")
        self.temperature = local_chat_config.get("temperature", 0.2)
        self.max_tokens = local_chat_config.get("max_tokens", 2048)
        self.request_timeout_seconds = float(
            local_chat_config.get("request_timeout_seconds", 90.0)
        )

    async def generate_response(
        self,
        *,
        transcript: str,
        conversation_history: list[ConversationHistoryEntry],
        screen_captures: list[ScreenCapturePayload],
        supports_pointing: bool,
    ) -> ChatResponse:
        messages = self.build_messages(
            transcript=transcript,
            conversation_history=conversation_history,
            screen_captures=screen_captures,
        )

        async with httpx.AsyncClient(timeout=self.request_timeout_seconds) as client:
            response = await client.post(
                f"{self.base_url}/api/chat",
                json={
                    "model": self.model,
                    "messages": messages,
                    "stream": False,
                    "format": "json",
                    "options": {
                        "temperature": self.temperature,
                        "num_predict": self.max_tokens,
                    },
                },
            )
            response.raise_for_status()

        raw_content = response.json().get("message", {}).get("content", "{}")
        return parse_chat_response(raw_content, supports_pointing=supports_pointing)

    def build_messages(
        self,
        *,
        transcript: str,
        conversation_history: list[ConversationHistoryEntry],
        screen_captures: list[ScreenCapturePayload],
    ) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": CHAT_SYSTEM_PROMPT}
        ]

        for history_entry in conversation_history:
            messages.append({"role": "user", "content": history_entry.user_prompt})
            messages.append(
                {"role": "assistant", "content": history_entry.assistant_response}
            )

        messages.append(
            {
                "role": "user",
                "content": build_user_prompt_content(transcript, screen_captures),
                "images": [
                    screen_capture.image_base64
                    for screen_capture in screen_captures
                ],
            }
        )
        return messages


class LlamaCppChatProvider(BaseChatProvider):
    def __init__(self, local_chat_config: dict[str, Any]):
        self.base_url = local_chat_config.get(
            "llama_cpp_base_url", "http://127.0.0.1:8081"
        )
        self.model = local_chat_config.get("model", "gemma-3-4b-it")
        self.temperature = local_chat_config.get("temperature", 0.0)
        self.max_tokens = local_chat_config.get("max_tokens", 256)
        self.request_timeout_seconds = float(
            local_chat_config.get("request_timeout_seconds", 120.0)
        )

    async def generate_response(
        self,
        *,
        transcript: str,
        conversation_history: list[ConversationHistoryEntry],
        screen_captures: list[ScreenCapturePayload],
        supports_pointing: bool,
    ) -> ChatResponse:
        messages = self.build_messages(
            transcript=transcript,
            conversation_history=conversation_history,
            screen_captures=screen_captures,
        )

        async with httpx.AsyncClient(timeout=self.request_timeout_seconds) as client:
            response = await client.post(
                f"{self.base_url}/v1/chat/completions",
                json={
                    "model": self.model,
                    "messages": messages,
                    "stream": False,
                    "max_tokens": self.max_tokens,
                    "temperature": self.temperature,
                },
            )
            response.raise_for_status()

        response_payload = response.json()
        raw_content = (
            response_payload.get("choices", [{}])[0]
            .get("message", {})
            .get("content", "{}")
        )
        return parse_chat_response(raw_content, supports_pointing=supports_pointing)

    def build_messages(
        self,
        *,
        transcript: str,
        conversation_history: list[ConversationHistoryEntry],
        screen_captures: list[ScreenCapturePayload],
    ) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": CHAT_SYSTEM_PROMPT}
        ]

        for history_entry in conversation_history:
            messages.append({"role": "user", "content": history_entry.user_prompt})
            messages.append(
                {"role": "assistant", "content": history_entry.assistant_response}
            )

        user_content_blocks: list[dict[str, Any]] = [
            {
                "type": "text",
                "text": build_user_prompt_content(transcript, screen_captures),
            }
        ]
        for screen_capture in screen_captures:
            user_content_blocks.append(
                {
                    "type": "image_url",
                    "image_url": {
                        "url": (
                            f"data:{screen_capture.mime_type};base64,"
                            f"{screen_capture.image_base64}"
                        )
                    },
                }
            )

        messages.append({"role": "user", "content": user_content_blocks})
        return messages


class AnthropicChatProvider(BaseChatProvider):
    def __init__(self, cloud_chat_config: dict[str, Any]):
        self.api_key = cloud_chat_config["anthropic_api_key"]
        self.model = cloud_chat_config.get(
            "anthropic_model", "claude-sonnet-4-20250514"
        )
        self.max_tokens = cloud_chat_config.get("max_tokens", 2048)
        self.temperature = cloud_chat_config.get("temperature", 0.2)

    async def generate_response(
        self,
        *,
        transcript: str,
        conversation_history: list[ConversationHistoryEntry],
        screen_captures: list[ScreenCapturePayload],
        supports_pointing: bool,
    ) -> ChatResponse:
        messages = self.build_messages(
            transcript=transcript,
            conversation_history=conversation_history,
            screen_captures=screen_captures,
        )

        async with httpx.AsyncClient(timeout=120.0) as client:
            response = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": self.api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": self.model,
                    "max_tokens": self.max_tokens,
                    "temperature": self.temperature,
                    "system": CHAT_SYSTEM_PROMPT,
                    "messages": messages,
                },
            )
            response.raise_for_status()

        response_payload = response.json()
        raw_content_parts: list[str] = []
        for content_block in response_payload.get("content", []):
            if content_block.get("type") == "text":
                raw_content_parts.append(content_block.get("text", ""))

        raw_content = "".join(raw_content_parts)
        return parse_chat_response(raw_content, supports_pointing=supports_pointing)

    def build_messages(
        self,
        *,
        transcript: str,
        conversation_history: list[ConversationHistoryEntry],
        screen_captures: list[ScreenCapturePayload],
    ) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []

        for history_entry in conversation_history:
            messages.append({"role": "user", "content": history_entry.user_prompt})
            messages.append(
                {"role": "assistant", "content": history_entry.assistant_response}
            )

        user_content_blocks: list[dict[str, Any]] = [
            {
                "type": "text",
                "text": build_user_prompt_content(transcript, screen_captures),
            }
        ]
        for screen_capture in screen_captures:
            user_content_blocks.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": screen_capture.mime_type,
                        "data": screen_capture.image_base64,
                    },
                }
            )

        messages.append({"role": "user", "content": user_content_blocks})
        return messages


class OpenAIChatProvider(BaseChatProvider):
    def __init__(self, cloud_chat_config: dict[str, Any]):
        self.api_key = cloud_chat_config["openai_api_key"]
        self.model = cloud_chat_config.get("openai_model", "gpt-4o")
        self.max_tokens = cloud_chat_config.get("max_tokens", 2048)
        self.temperature = cloud_chat_config.get("temperature", 0.2)

    async def generate_response(
        self,
        *,
        transcript: str,
        conversation_history: list[ConversationHistoryEntry],
        screen_captures: list[ScreenCapturePayload],
        supports_pointing: bool,
    ) -> ChatResponse:
        messages = self.build_messages(
            transcript=transcript,
            conversation_history=conversation_history,
            screen_captures=screen_captures,
        )

        async with httpx.AsyncClient(timeout=120.0) as client:
            response = await client.post(
                "https://api.openai.com/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self.model,
                    "max_tokens": self.max_tokens,
                    "temperature": self.temperature,
                    "messages": messages,
                    "response_format": {"type": "json_object"},
                },
            )
            response.raise_for_status()

        response_payload = response.json()
        raw_content = (
            response_payload.get("choices", [{}])[0]
            .get("message", {})
            .get("content", "{}")
        )
        return parse_chat_response(raw_content, supports_pointing=supports_pointing)

    def build_messages(
        self,
        *,
        transcript: str,
        conversation_history: list[ConversationHistoryEntry],
        screen_captures: list[ScreenCapturePayload],
    ) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": CHAT_SYSTEM_PROMPT}
        ]

        for history_entry in conversation_history:
            messages.append({"role": "user", "content": history_entry.user_prompt})
            messages.append(
                {"role": "assistant", "content": history_entry.assistant_response}
            )

        user_content_blocks: list[dict[str, Any]] = [
            {
                "type": "text",
                "text": build_user_prompt_content(transcript, screen_captures),
            }
        ]
        for screen_capture in screen_captures:
            user_content_blocks.append(
                {
                    "type": "image_url",
                    "image_url": {
                        "url": (
                            f"data:{screen_capture.mime_type};base64,"
                            f"{screen_capture.image_base64}"
                        )
                    },
                }
            )

        messages.append({"role": "user", "content": user_content_blocks})
        return messages


class BaseTranscriptionProvider(ABC):
    @abstractmethod
    async def transcribe(
        self,
        *,
        wav_audio_bytes: bytes,
        keyterms: list[str],
    ) -> str:
        raise NotImplementedError


class FasterWhisperTranscriptionProvider(BaseTranscriptionProvider):
    def __init__(self, local_transcription_config: dict[str, Any]):
        self.local_transcription_config = local_transcription_config
        self.language = local_transcription_config.get("language", "en")
        self.initial_prompt_prefix = local_transcription_config.get(
            "initial_prompt_prefix",
            "",
        )

    def ensure_model_loaded(self) -> Any:
        return load_faster_whisper_model(self.local_transcription_config)

    async def transcribe(
        self,
        *,
        wav_audio_bytes: bytes,
        keyterms: list[str],
    ) -> str:
        whisper_model = self.ensure_model_loaded()

        initial_prompt_parts = []
        if self.initial_prompt_prefix:
            initial_prompt_parts.append(self.initial_prompt_prefix)
        if keyterms:
            initial_prompt_parts.append(" ".join(keyterms))

        transcription_kwargs: dict[str, Any] = {"language": self.language}
        if initial_prompt_parts:
            transcription_kwargs["initial_prompt"] = " ".join(initial_prompt_parts)

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=True) as temporary_wav:
            temporary_wav.write(wav_audio_bytes)
            temporary_wav.flush()

            segments, _ = whisper_model.transcribe(
                temporary_wav.name,
                **transcription_kwargs,
            )
            transcript_text = "".join(segment.text for segment in segments).strip()

        return transcript_text


class OpenAIWhisperCloudTranscriptionProvider(BaseTranscriptionProvider):
    def __init__(self, cloud_transcription_config: dict[str, Any]):
        self.api_key = cloud_transcription_config["openai_api_key"]
        self.model = cloud_transcription_config.get("openai_model", "whisper-1")

    async def transcribe(
        self,
        *,
        wav_audio_bytes: bytes,
        keyterms: list[str],
    ) -> str:
        form_data: dict[str, Any] = {"model": self.model}
        if keyterms:
            form_data["prompt"] = " ".join(keyterms)

        files = {"file": ("audio.wav", io.BytesIO(wav_audio_bytes), "audio/wav")}

        async with httpx.AsyncClient(timeout=45.0) as client:
            response = await client.post(
                "https://api.openai.com/v1/audio/transcriptions",
                headers={"Authorization": f"Bearer {self.api_key}"},
                data=form_data,
                files=files,
            )
            response.raise_for_status()

        return response.json().get("text", "").strip()


class AssemblyAITranscriptionProvider(BaseTranscriptionProvider):
    def __init__(self, cloud_transcription_config: dict[str, Any]):
        self.api_key = cloud_transcription_config["assemblyai_api_key"]

    async def transcribe(
        self,
        *,
        wav_audio_bytes: bytes,
        keyterms: list[str],
    ) -> str:
        headers = {"authorization": self.api_key}

        async with httpx.AsyncClient(timeout=60.0) as client:
            upload_response = await client.post(
                "https://api.assemblyai.com/v2/upload",
                headers=headers,
                content=wav_audio_bytes,
            )
            upload_response.raise_for_status()
            upload_url = upload_response.json()["upload_url"]

            transcription_payload: dict[str, Any] = {"audio_url": upload_url}
            if keyterms:
                transcription_payload["word_boost"] = keyterms

            transcript_response = await client.post(
                "https://api.assemblyai.com/v2/transcript",
                headers=headers,
                json=transcription_payload,
            )
            transcript_response.raise_for_status()
            transcript_id = transcript_response.json()["id"]

            import asyncio

            poll_url = f"https://api.assemblyai.com/v2/transcript/{transcript_id}"
            while True:
                poll_response = await client.get(poll_url, headers=headers)
                poll_response.raise_for_status()
                poll_payload = poll_response.json()
                status = poll_payload.get("status")

                if status == "completed":
                    return (poll_payload.get("text") or "").strip()
                if status == "error":
                    raise RuntimeError(
                        f"AssemblyAI transcription failed: {poll_payload.get('error')}"
                    )

                await asyncio.sleep(1.0)


class BaseTTSProvider(ABC):
    @abstractmethod
    async def synthesize(self, *, text: str) -> TTSResult:
        raise NotImplementedError


class MacOSSayTTSProvider(BaseTTSProvider):
    def __init__(self, local_tts_config: dict[str, Any]):
        self.voice_name = local_tts_config.get("voice_name", "Samantha")
        self.speaking_rate = int(local_tts_config.get("speaking_rate_wpm", 185))

    async def synthesize(self, *, text: str) -> TTSResult:
        import asyncio

        with tempfile.NamedTemporaryFile(suffix=".aiff", delete=True) as temp_audio_file:
            command_arguments = [
                "say",
                "-v",
                self.voice_name,
                "-r",
                str(self.speaking_rate),
                "-o",
                temp_audio_file.name,
                text,
            ]
            process = await asyncio.create_subprocess_exec(
                *command_arguments,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr_data = await process.communicate()

            if process.returncode != 0:
                stderr_text = stderr_data.decode("utf-8", errors="ignore").strip()
                raise RuntimeError(
                    f"macOS say failed with exit code {process.returncode}: {stderr_text}"
                )

            temp_audio_file.seek(0)
            return TTSResult(
                audio_bytes=temp_audio_file.read(),
                media_type="audio/aiff",
            )


class ElevenLabsTTSProvider(BaseTTSProvider):
    def __init__(self, cloud_tts_config: dict[str, Any]):
        self.api_key = cloud_tts_config["elevenlabs_api_key"]
        self.voice_id = cloud_tts_config.get(
            "elevenlabs_voice_id", "21m00Tcm4TlvDq8ikWAM"
        )
        self.model_id = cloud_tts_config.get(
            "elevenlabs_model_id", "eleven_turbo_v2"
        )

    async def synthesize(self, *, text: str) -> TTSResult:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"https://api.elevenlabs.io/v1/text-to-speech/{self.voice_id}",
                headers={
                    "xi-api-key": self.api_key,
                    "Content-Type": "application/json",
                    "Accept": "audio/mpeg",
                },
                json={
                    "text": text,
                    "model_id": self.model_id,
                    "voice_settings": {
                        "stability": 0.5,
                        "similarity_boost": 0.75,
                    },
                },
            )
            response.raise_for_status()

        return TTSResult(audio_bytes=response.content, media_type="audio/mpeg")


def create_chat_provider(config: dict[str, Any]) -> BaseChatProvider:
    chat_config = config["chat"]
    active_mode = chat_config["mode"]

    if active_mode == "local":
        provider_name = chat_config["local"]["provider"]
        if provider_name == "ollama":
            return OllamaChatProvider(chat_config["local"])
        if provider_name == "llama_cpp":
            return LlamaCppChatProvider(chat_config["local"])
        raise ValueError(f"Unsupported local chat provider: {provider_name}")

    provider_name = chat_config["cloud"]["provider"]
    if provider_name == "anthropic":
        return AnthropicChatProvider(chat_config["cloud"])
    if provider_name == "openai":
        return OpenAIChatProvider(chat_config["cloud"])

    raise ValueError(f"Unsupported cloud chat provider: {provider_name}")


def create_transcription_provider(config: dict[str, Any]) -> BaseTranscriptionProvider:
    transcription_config = config["transcription"]
    active_mode = transcription_config["mode"]

    if active_mode == "local":
        return FasterWhisperTranscriptionProvider(transcription_config["local"])

    provider_name = transcription_config["cloud"]["provider"]
    if provider_name == "openai":
        return OpenAIWhisperCloudTranscriptionProvider(transcription_config["cloud"])
    if provider_name == "assemblyai":
        return AssemblyAITranscriptionProvider(transcription_config["cloud"])

    raise ValueError(f"Unsupported cloud transcription provider: {provider_name}")


def create_tts_provider(config: dict[str, Any]) -> BaseTTSProvider:
    tts_config = config["tts"]
    active_mode = tts_config["mode"]

    if active_mode == "local":
        return MacOSSayTTSProvider(tts_config["local"])

    provider_name = tts_config["cloud"]["provider"]
    if provider_name == "elevenlabs":
        return ElevenLabsTTSProvider(tts_config["cloud"])

    raise ValueError(f"Unsupported cloud TTS provider: {provider_name}")


app = FastAPI(
    title="LoClicky Local Gateway",
    description="Loopback-only AI gateway for the LoClicky macOS assistant.",
    version="0.2.0",
)

chat_provider = create_chat_provider(CONFIG)
transcription_provider = create_transcription_provider(CONFIG)
tts_provider = create_tts_provider(CONFIG)

logger.info("Loaded config from %s", CONFIG_PATH)
logger.info("Chat provider: %s", chat_provider.__class__.__name__)
logger.info("Transcription provider: %s", transcription_provider.__class__.__name__)
logger.info("TTS provider: %s", tts_provider.__class__.__name__)


@app.get("/health")
async def health_check() -> dict[str, Any]:
    managed_llama_cpp_process = MANAGED_LLAMA_CPP_PROCESS
    return {
        "status": "ok",
        "chat_provider": chat_provider.__class__.__name__,
        "transcription_provider": transcription_provider.__class__.__name__,
        "tts_provider": tts_provider.__class__.__name__,
        "history_enabled": history_is_enabled(),
        "config_path": str(CONFIG_PATH),
        "managed_llama_cpp_pid": (
            managed_llama_cpp_process.pid
            if managed_llama_cpp_process is not None
            and managed_llama_cpp_process.poll() is None
            else None
        ),
    }


@app.post("/chat", response_model=ChatResponse)
async def chat_endpoint(request_body: ChatRequest) -> ChatResponse:
    logger.info(
        "POST /chat transcript_chars=%d history_entries=%d screen_captures=%d",
        len(request_body.transcript),
        len(request_body.conversation_history),
        len(request_body.screen_captures),
    )

    started_at = time.time()
    try:
        response = await chat_provider.generate_response(
            transcript=request_body.transcript,
            conversation_history=request_body.conversation_history,
            screen_captures=request_body.screen_captures,
            supports_pointing=request_body.supports_pointing,
        )

        if (
            response.point_target is not None
            and not transcript_explicitly_requests_pointing(request_body.transcript)
        ):
            response = response.model_copy(update={"point_target": None})

        if history_is_enabled():
            encrypted_prompt = fernet.encrypt(request_body.transcript.encode("utf-8"))
            encrypted_detailed_text = fernet.encrypt(
                response.detailed_text.encode("utf-8")
            )
            point_coordinates = None
            if response.point_target is not None:
                point_coordinates = f"{response.point_target.x},{response.point_target.y}"

            with sqlite3.connect(DB_PATH) as connection:
                connection.execute(
                    """
                    INSERT INTO conversation_history (
                        timestamp,
                        prompt_enc,
                        detailed_text_enc,
                        action_coordinates
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (
                        datetime.utcnow().isoformat() + "Z",
                        encrypted_prompt,
                        encrypted_detailed_text,
                        point_coordinates,
                    ),
                )

        latency_ms = (time.time() - started_at) * 1000
        log_audit_event(
            event_type="chat",
            provider=chat_provider.__class__.__name__,
            latency_ms=latency_ms,
            extra={
                "history_entries": len(request_body.conversation_history),
                "screen_capture_count": len(request_body.screen_captures),
                "pointing_requested": request_body.supports_pointing,
                "point_target_returned": response.point_target is not None,
            },
        )
        return response
    except LLMStructuredOutputError as parse_error:
        logger.error("Unable to parse structured chat response: %s", parse_error)
        raise HTTPException(
            status_code=502,
            detail=(
                "The selected model did not return valid structured JSON for "
                "the Clicky chat contract."
            ),
        ) from parse_error
    except httpx.HTTPStatusError as upstream_error:
        logger.error(
            "Chat provider returned HTTP %d: %s",
            upstream_error.response.status_code,
            upstream_error.response.text[:1000],
        )
        raise HTTPException(
            status_code=502,
            detail=(
                f"Upstream chat provider error "
                f"(HTTP {upstream_error.response.status_code})."
            ),
        ) from upstream_error
    except Exception as unexpected_error:
        logger.error("Unexpected chat error: %s", traceback.format_exc())
        raise HTTPException(
            status_code=500,
            detail=str(unexpected_error),
        ) from unexpected_error


@app.post("/transcribe", response_model=TranscribeResponse)
async def transcribe_endpoint(request_body: TranscribeRequest) -> TranscribeResponse:
    logger.info(
        "POST /transcribe audio_base64_chars=%d keyterms=%d",
        len(request_body.audio_wav_base64),
        len(request_body.keyterms),
    )

    try:
        wav_audio_bytes = base64.b64decode(
            request_body.audio_wav_base64,
            validate=True,
        )
    except Exception as decode_error:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid base64 audio payload: {decode_error}",
        ) from decode_error

    started_at = time.time()
    try:
        transcript_text = await transcription_provider.transcribe(
            wav_audio_bytes=wav_audio_bytes,
            keyterms=request_body.keyterms,
        )
        latency_ms = (time.time() - started_at) * 1000
        log_audit_event(
            event_type="transcribe",
            provider=transcription_provider.__class__.__name__,
            latency_ms=latency_ms,
            extra={"keyterm_count": len(request_body.keyterms)},
        )
        return TranscribeResponse(transcript=transcript_text)
    except httpx.HTTPStatusError as upstream_error:
        logger.error(
            "Transcription provider returned HTTP %d: %s",
            upstream_error.response.status_code,
            upstream_error.response.text[:1000],
        )
        raise HTTPException(
            status_code=502,
            detail=(
                f"Upstream transcription provider error "
                f"(HTTP {upstream_error.response.status_code})."
            ),
        ) from upstream_error
    except Exception as unexpected_error:
        logger.error("Unexpected transcription error: %s", traceback.format_exc())
        raise HTTPException(
            status_code=500,
            detail=str(unexpected_error),
        ) from unexpected_error


@app.post("/tts")
async def tts_endpoint(request_body: TTSRequest) -> Response:
    logger.info("POST /tts text_chars=%d", len(request_body.text))

    started_at = time.time()
    try:
        tts_result = await tts_provider.synthesize(text=request_body.text)
        latency_ms = (time.time() - started_at) * 1000
        log_audit_event(
            event_type="tts",
            provider=tts_provider.__class__.__name__,
            latency_ms=latency_ms,
            extra={"text_length": len(request_body.text)},
        )
        return Response(
            content=tts_result.audio_bytes,
            media_type=tts_result.media_type,
        )
    except httpx.HTTPStatusError as upstream_error:
        logger.error(
            "TTS provider returned HTTP %d: %s",
            upstream_error.response.status_code,
            upstream_error.response.text[:1000],
        )
        raise HTTPException(
            status_code=502,
            detail=(
                f"Upstream TTS provider error "
                f"(HTTP {upstream_error.response.status_code})."
            ),
        ) from upstream_error
    except subprocess.CalledProcessError as tts_error:
        raise HTTPException(status_code=500, detail=str(tts_error)) from tts_error
    except Exception as unexpected_error:
        logger.error("Unexpected TTS error: %s", traceback.format_exc())
        raise HTTPException(
            status_code=500,
            detail=str(unexpected_error),
        ) from unexpected_error


@app.get("/history", response_model=HistoryResponse)
async def history_endpoint() -> HistoryResponse:
    if not history_is_enabled():
        return HistoryResponse(history=[])

    try:
        with sqlite3.connect(DB_PATH) as connection:
            cursor = connection.cursor()
            cursor.execute(
                """
                SELECT id, timestamp, prompt_enc, detailed_text_enc, action_coordinates
                FROM conversation_history
                ORDER BY id ASC
                """
            )
            rows = cursor.fetchall()

        history_items: list[HistoryItem] = []
        for entry_id, timestamp, prompt_enc, text_enc, action_coordinates in rows:
            try:
                user_prompt = fernet.decrypt(prompt_enc).decode("utf-8")
                assistant_detailed_text = fernet.decrypt(text_enc).decode("utf-8")
            except Exception as decrypt_error:
                logger.error(
                    "Skipping unreadable history row %s: %s",
                    entry_id,
                    decrypt_error,
                )
                continue

            history_items.append(
                HistoryItem(
                    id=entry_id,
                    timestamp=timestamp,
                    user_prompt=user_prompt,
                    assistant_detailed_text=assistant_detailed_text,
                    action_coordinates=action_coordinates or None,
                )
            )

        return HistoryResponse(history=history_items)
    except Exception as unexpected_error:
        logger.error("Unexpected history error: %s", traceback.format_exc())
        raise HTTPException(
            status_code=500,
            detail=str(unexpected_error),
        ) from unexpected_error


if __name__ == "__main__":
    gateway_config = CONFIG["gateway"]
    host = gateway_config.get("host", LOOPBACK_GATEWAY_HOST)
    port = int(gateway_config.get("port", 5000))

    logger.info("Starting LoClicky Local Gateway on %s:%d", host, port)

    try:
        uvicorn.run(
            app,
            host=host,
            port=port,
            log_level=LOG_LEVEL.lower(),
        )
    finally:
        stop_managed_llama_cpp_server()
