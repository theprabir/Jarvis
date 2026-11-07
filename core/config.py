"""Typed configuration loading for JARVIS.

The single source of truth is ``config/settings.toml``. Values are
validated on load so that a typo fails fast instead of surfacing later as
a confusing runtime error.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, fields
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "settings.toml"


class ConfigError(Exception):
  """Raised when the configuration file is missing or invalid."""


@dataclass(frozen=True)
class AppConfig:
  name: str = "JARVIS"
  log_level: str = "INFO"
  log_dir: str = "logs"


@dataclass(frozen=True)
class LlamaConfig:
  executable: str = "llama-server"
  model_path: str = "models/Llama-3.2-1B-Instruct-Q4_K_M.gguf"
  host: str = "127.0.0.1"
  port: int = 8080
  context_size: int = 2048
  threads: int = 2
  threads_batch: int = 2
  batch_size: int = 64
  ubatch_size: int = 64
  parallel_slots: int = 1
  gpu_layers: int = 0
  startup_timeout_s: float = 120.0
  auto_start: bool = True

  @property
  def base_url(self) -> str:
    return f"http://{self.host}:{self.port}"

  @property
  def resolved_model_path(self) -> Path:
    path = Path(self.model_path)
    return path if path.is_absolute() else (PROJECT_ROOT / path).resolve()


@dataclass(frozen=True)
class LlmConfig:
  connect_timeout_s: float = 5.0
  read_timeout_s: float = 120.0
  max_tokens: int = 50
  temperature: float = 0.2
  max_retries: int = 1


@dataclass(frozen=True)
class TtsConfig:
  enabled: bool = True
  rate: int = 175
  volume: float = 1.0
  voice: str = ""


@dataclass(frozen=True)
class Settings:
  app: AppConfig
  llama: LlamaConfig
  llm: LlmConfig
  tts: TtsConfig


def _build(cls, data, section):
  """Build a frozen dataclass from a TOML section, rejecting unknown keys."""
  if not isinstance(data, dict):
    raise ConfigError(f"Section [{section}] must be a table")
  known = {field.name for field in fields(cls)}
  unknown = sorted(set(data) - known)
  if unknown:
    raise ConfigError(
        f"Unknown option(s) in [{section}]: {', '.join(unknown)}"
    )
  try:
    return cls(**data)
  except TypeError as exc:
    raise ConfigError(f"Invalid value in [{section}]: {exc}") from exc


def _require(condition, message):
  if not condition:
    raise ConfigError(message)


def _validate(settings):
  app = settings.app
  _require(
      app.log_level.upper() in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"},
      f"[app] log_level is invalid: {app.log_level!r}",
  )

  llama = settings.llama
  _require(bool(llama.executable), "[llama] executable must not be empty")
  _require(bool(llama.host), "[llama] host must not be empty")
  _require(1 <= llama.port <= 65535, "[llama] port must be 1-65535")
  _require(256 <= llama.context_size <= 8192, "[llama] context_size must be 256-8192")
  _require(1 <= llama.threads <= 4, "[llama] threads must be 1-4 on this CPU")
  _require(1 <= llama.threads_batch <= 4, "[llama] threads_batch must be 1-4")
  _require(1 <= llama.batch_size <= 2048, "[llama] batch_size must be 1-2048")
  _require(1 <= llama.ubatch_size <= llama.batch_size, "[llama] ubatch_size must be <= batch_size")
  _require(1 <= llama.parallel_slots <= 4, "[llama] parallel_slots must be 1-4")
  _require(llama.gpu_layers >= 0, "[llama] gpu_layers must be >= 0")
  _require(llama.startup_timeout_s > 0, "[llama] startup_timeout_s must be > 0")

  llm = settings.llm
  _require(llm.connect_timeout_s > 0, "[llm] connect_timeout_s must be > 0")
  _require(llm.read_timeout_s > 0, "[llm] read_timeout_s must be > 0")
  _require(1 <= llm.max_tokens <= 512, "[llm] max_tokens must be 1-512")
  _require(0.0 <= llm.temperature <= 2.0, "[llm] temperature must be 0-2")
  _require(0 <= llm.max_retries <= 3, "[llm] max_retries must be 0-3")

  tts = settings.tts
  _require(80 <= tts.rate <= 400, "[tts] rate must be 80-400")
  _require(0.0 <= tts.volume <= 1.0, "[tts] volume must be 0-1")


def load_settings(path=None):
  """Load and validate settings from TOML.

  The path may be passed explicitly or via the JARVIS_CONFIG environment
  variable; otherwise config/settings.toml in the project root is used.
  """
  if path is None:
    path = os.environ.get("JARVIS_CONFIG") or DEFAULT_CONFIG_PATH
  config_path = Path(path)
  if not config_path.is_file():
    raise ConfigError(f"Configuration file not found: {config_path}")

  try:
    with config_path.open("rb") as handle:
      raw = tomllib.load(handle)
  except tomllib.TOMLDecodeError as exc:
    raise ConfigError(f"Invalid TOML in {config_path}: {exc}") from exc
  except OSError as exc:
    raise ConfigError(f"Could not read {config_path}: {exc}") from exc

  settings = Settings(
      app=_build(AppConfig, raw.get("app", {}), "app"),
      llama=_build(LlamaConfig, raw.get("llama", {}), "llama"),
      llm=_build(LlmConfig, raw.get("llm", {}), "llm"),
      tts=_build(TtsConfig, raw.get("tts", {}), "tts"),
  )
  _validate(settings)
  return settings
