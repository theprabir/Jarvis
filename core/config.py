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
class VoiceConfig:
  enabled: bool = True
  # Empty string = system default microphone; otherwise a name substring.
  input_device: str = ""
  ptt_key: str = "ctrl+space"
  vad_aggressiveness: int = 2
  silence_ms: int = 900
  min_speech_ms: int = 250
  max_record_s: float = 15.0
  stt_model: str = "models/vosk-model-small-en-us-0.15"

  @property
  def resolved_stt_model(self) -> Path:
    path = Path(self.stt_model)
    return path if path.is_absolute() else (PROJECT_ROOT / path).resolve()


@dataclass(frozen=True)
class ConversationConfig:
  enabled: bool = True
  resume_last_session: bool = True
  mode: str = "short"
  max_turns: int = 8
  summarize_after: int = 12
  max_context_chars: int = 3500
  short_max_tokens: int = 90
  detailed_max_tokens: int = 250
  technical_max_tokens: int = 320
  max_continuations: int = 1
  history_db: str = "data/jarvis.db"

  @property
  def resolved_history_db(self) -> Path:
    path = Path(self.history_db)
    return path if path.is_absolute() else (PROJECT_ROOT / path).resolve()


@dataclass(frozen=True)
class MemoryConfig:
  enabled: bool = True
  # Explicit memories share the conversation database; tables are separate.
  db: str = "data/jarvis.db"
  # Persistent memories included in the system prompt, ranked by relevance.
  inject_items: int = 6
  # Automatic learning from high-confidence first-person statements.
  auto_learn: bool = True
  learn_min_confidence: float = 0.75
  learned_ttl_days: int = 180
  episodic_ttl_days: int = 30
  episodic_items: int = 200
  short_term_items: int = 6

  @property
  def resolved_db(self) -> Path:
    path = Path(self.db)
    return path if path.is_absolute() else (PROJECT_ROOT / path).resolve()


@dataclass(frozen=True)
class WeatherConfig:
  enabled: bool = True
  # metric = °C and km/h, imperial = °F and mph.
  units: str = "metric"
  # Used when a question names no city; empty asks which city.
  default_location: str = ""
  timeout_s: float = 10.0


@dataclass(frozen=True)
class TasksConfig:
  enabled: bool = True
  # Plans share the conversation database; task tables are separate.
  db: str = "data/jarvis.db"
  max_steps: int = 5000
  max_retries: int = 1
  retry_delay_s: float = 0.2
  parallel_workers: int = 2
  approval_required: bool = True
  auto_resume: bool = True
  history_items: int = 20

  @property
  def resolved_db(self) -> Path:
    path = Path(self.db)
    return path if path.is_absolute() else (PROJECT_ROOT / path).resolve()


@dataclass(frozen=True)
class DocumentsConfig:
  enabled: bool = True
  # Document metadata and chunk index share the assistant database.
  db: str = "data/jarvis.db"
  max_file_bytes: int = 20_000_000
  max_chars: int = 2_000_000
  chunk_chars: int = 900
  chunk_overlap: int = 120
  search_limit: int = 5
  context_chars: int = 4000
  max_files: int = 500
  max_chunks: int = 20_000
  # Optional OCR for scanned PDF pages and images. Windows.Media.Ocr runs
  # locally through winocr; pages are rendered with pypdfium2.
  ocr_enabled: bool = True
  ocr_language: str = "en-US"
  ocr_dpi: int = 200
  ocr_min_chars: int = 40
  ocr_max_pages: int = 20

  @property
  def resolved_db(self) -> Path:
    path = Path(self.db)
    return path if path.is_absolute() else (PROJECT_ROOT / path).resolve()


@dataclass(frozen=True)
class ToolsConfig:
  enabled: bool = True
  root: str = "."
  max_read_bytes: int = 200_000
  search_limit: int = 20
  max_search_depth: int = 4
  command_timeout_s: float = 15.0
  allowed_apps: tuple = (
      "notepad",
      "calc",
      "mspaint",
      "explorer",
      "msedge",
      "chrome",
      "code",
  )
  allowed_commands: tuple = (
      "ipconfig",
      "whoami",
      "systeminfo",
      "tasklist",
      "hostname",
      "ping",
  )

  @property
  def resolved_root(self) -> Path:
    path = Path(self.root)
    return path if path.is_absolute() else (PROJECT_ROOT / path).resolve()


@dataclass(frozen=True)
class WebConfig:
  enabled: bool = True
  # auto = Brave when a key is present, otherwise DuckDuckGo.
  provider: str = "auto"
  # Optional self-hosted SearXNG base URL, e.g. http://127.0.0.1:8888.
  searx_url: str = ""
  max_results: int = 6
  max_queries: int = 2
  timeout_s: float = 12.0
  max_page_bytes: int = 400_000
  extract_pages: int = 3
  # Character budget for the evidence sent to the model (~4 chars per token).
  context_chars: int = 1000
  cache_ttl_s: int = 3600
  cache_entries: int = 128
  cache_db: str = "data/search_cache.db"
  brave_api_key_env: str = "BRAVE_API_KEY"

  @property
  def resolved_cache_db(self) -> Path:
    path = Path(self.cache_db)
    return path if path.is_absolute() else (PROJECT_ROOT / path).resolve()


@dataclass(frozen=True)
class Settings:
  app: AppConfig
  llama: LlamaConfig
  llm: LlmConfig
  tts: TtsConfig
  voice: VoiceConfig
  conversation: ConversationConfig
  tools: ToolsConfig
  tasks: TasksConfig
  documents: DocumentsConfig
  memory: MemoryConfig
  weather: WeatherConfig
  web: WebConfig


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

  voice = settings.voice
  _require(bool(voice.ptt_key), "[voice] ptt_key must not be empty")
  _require(
      0 <= voice.vad_aggressiveness <= 3,
      "[voice] vad_aggressiveness must be 0-3",
  )
  _require(200 <= voice.silence_ms <= 5000, "[voice] silence_ms must be 200-5000")
  _require(
      0 <= voice.min_speech_ms <= 2000,
      "[voice] min_speech_ms must be 0-2000",
  )
  _require(2.0 <= voice.max_record_s <= 60.0, "[voice] max_record_s must be 2-60")
  _require(bool(voice.stt_model), "[voice] stt_model must not be empty")

  conversation = settings.conversation
  _require(
      conversation.mode in {"short", "detailed", "technical"},
      "[conversation] mode must be short, detailed or technical",
  )
  _require(1 <= conversation.max_turns <= 50, "[conversation] max_turns must be 1-50")
  _require(
      conversation.max_turns <= conversation.summarize_after <= 100,
      "[conversation] summarize_after must be >= max_turns and <= 100",
  )
  _require(
      500 <= conversation.max_context_chars <= 8000,
      "[conversation] max_context_chars must be 500-8000",
  )
  for mode in ("short", "detailed", "technical"):
    tokens = getattr(conversation, f"{mode}_max_tokens")
    _require(
        20 <= tokens <= 600,
        f"[conversation] {mode}_max_tokens must be 20-600",
    )
  _require(
      0 <= conversation.max_continuations <= 2,
      "[conversation] max_continuations must be 0-2",
  )
  _require(
      bool(conversation.history_db),
      "[conversation] history_db must not be empty",
  )

  weather = settings.weather
  _require(
      weather.units in {"metric", "imperial"},
      "[weather] units must be metric or imperial",
  )
  _require(
      2.0 <= weather.timeout_s <= 30.0,
      "[weather] timeout_s must be 2-30",
  )

  tasks = settings.tasks
  _require(bool(tasks.db), "[tasks] db must not be empty")
  _require(
      3 <= tasks.max_steps <= 10_000,
      "[tasks] max_steps must be 3-10000",
  )
  _require(0 <= tasks.max_retries <= 3, "[tasks] max_retries must be 0-3")
  _require(
      0.0 <= tasks.retry_delay_s <= 5.0,
      "[tasks] retry_delay_s must be 0-5",
  )
  _require(
      1 <= tasks.parallel_workers <= 4,
      "[tasks] parallel_workers must be 1-4",
  )
  _require(
      1 <= tasks.history_items <= 100,
      "[tasks] history_items must be 1-100",
  )
  _require(
      isinstance(tasks.auto_resume, bool),
      "[tasks] auto_resume must be true or false",
  )

  documents = settings.documents
  _require(bool(documents.db), "[documents] db must not be empty")
  _require(
      10_000 <= documents.max_file_bytes <= 100_000_000,
      "[documents] max_file_bytes must be 10000-100000000",
  )
  _require(
      10_000 <= documents.max_chars <= 20_000_000,
      "[documents] max_chars must be 10000-20000000",
  )
  _require(
      200 <= documents.chunk_chars <= 4000,
      "[documents] chunk_chars must be 200-4000",
  )
  _require(
      0 <= documents.chunk_overlap < documents.chunk_chars,
      "[documents] chunk_overlap must be 0 and less than chunk_chars",
  )
  _require(
      1 <= documents.search_limit <= 20,
      "[documents] search_limit must be 1-20",
  )
  _require(
      500 <= documents.context_chars <= 12_000,
      "[documents] context_chars must be 500-12000",
  )
  _require(
      1 <= documents.max_files <= 5000,
      "[documents] max_files must be 1-5000",
  )
  _require(
      100 <= documents.max_chunks <= 100_000,
      "[documents] max_chunks must be 100-100000",
  )
  _require(
      isinstance(documents.ocr_enabled, bool),
      "[documents] ocr_enabled must be true or false",
  )
  _require(
      isinstance(documents.ocr_language, str)
      and bool(documents.ocr_language.strip())
      and len(documents.ocr_language) <= 32,
      "[documents] ocr_language must be a non-empty language tag",
  )
  _require(
      100 <= documents.ocr_dpi <= 400,
      "[documents] ocr_dpi must be 100-400",
  )
  _require(
      0 <= documents.ocr_min_chars <= 1000,
      "[documents] ocr_min_chars must be 0-1000",
  )
  _require(
      1 <= documents.ocr_max_pages <= 100,
      "[documents] ocr_max_pages must be 1-100",
  )

  memory = settings.memory
  _require(bool(memory.db), "[memory] db must not be empty")
  _require(
      1 <= memory.inject_items <= 20,
      "[memory] inject_items must be 1-20",
  )
  _require(
      0.5 <= memory.learn_min_confidence <= 1.0,
      "[memory] learn_min_confidence must be 0.5-1.0",
  )
  _require(
      1 <= memory.learned_ttl_days <= 3650,
      "[memory] learned_ttl_days must be 1-3650",
  )
  _require(
      1 <= memory.episodic_ttl_days <= 3650,
      "[memory] episodic_ttl_days must be 1-3650",
  )
  _require(
      10 <= memory.episodic_items <= 2000,
      "[memory] episodic_items must be 10-2000",
  )
  _require(
      2 <= memory.short_term_items <= 20,
      "[memory] short_term_items must be 2-20",
  )

  tools = settings.tools
  _require(bool(tools.root), "[tools] root must not be empty")
  _require(
      1000 <= tools.max_read_bytes <= 5_000_000,
      "[tools] max_read_bytes must be 1000-5000000",
  )
  _require(1 <= tools.search_limit <= 100, "[tools] search_limit must be 1-100")
  _require(
      1 <= tools.max_search_depth <= 10,
      "[tools] max_search_depth must be 1-10",
  )
  _require(tools.command_timeout_s > 0, "[tools] command_timeout_s must be > 0")
  for group in ("allowed_apps", "allowed_commands"):
    names = getattr(tools, group)
    _require(
        isinstance(names, (list, tuple))
        and all(isinstance(name, str) and name.strip() for name in names),
        f"[tools] {group} must be a list of names",
    )

  web = settings.web
  _require(
      web.provider in {"auto", "duckduckgo", "brave", "searxng"},
      "[web] provider must be auto, duckduckgo, brave or searxng",
  )
  _require(
      not web.searx_url
      or web.searx_url.startswith(("http://", "https://")),
      "[web] searx_url must start with http:// or https://",
  )
  _require(
      web.provider != "searxng" or bool(web.searx_url),
      "[web] provider = searxng needs searx_url",
  )
  _require(1 <= web.max_results <= 20, "[web] max_results must be 1-20")
  _require(1 <= web.max_queries <= 4, "[web] max_queries must be 1-4")
  _require(2.0 <= web.timeout_s <= 60.0, "[web] timeout_s must be 2-60")
  _require(
      50_000 <= web.max_page_bytes <= 2_000_000,
      "[web] max_page_bytes must be 50000-2000000",
  )
  _require(0 <= web.extract_pages <= 5, "[web] extract_pages must be 0-5")
  _require(
      400 <= web.context_chars <= 4000,
      "[web] context_chars must be 400-4000",
  )
  _require(
      0 <= web.cache_ttl_s <= 86_400, "[web] cache_ttl_s must be 0-86400"
  )
  _require(1 <= web.cache_entries <= 1000, "[web] cache_entries must be 1-1000")
  _require(bool(web.cache_db), "[web] cache_db must not be empty")
  _require(
      bool(web.brave_api_key_env),
      "[web] brave_api_key_env must not be empty",
  )


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
      voice=_build(VoiceConfig, raw.get("voice", {}), "voice"),
      conversation=_build(
          ConversationConfig, raw.get("conversation", {}), "conversation"
      ),
      tools=_build(ToolsConfig, raw.get("tools", {}), "tools"),
      tasks=_build(TasksConfig, raw.get("tasks", {}), "tasks"),
      documents=_build(
          DocumentsConfig, raw.get("documents", {}), "documents"
      ),
      memory=_build(MemoryConfig, raw.get("memory", {}), "memory"),
      weather=_build(WeatherConfig, raw.get("weather", {}), "weather"),
      web=_build(WebConfig, raw.get("web", {}), "web"),
  )
  _validate(settings)
  return settings
