"""Phase 1 baseline measurement: memory and latency.

Run with::

    python -m core.benchmark

Starts the server with the configured flags (or reuses a healthy one),
measures working sets and one representative request, prints a report and
stops the server if it was started here.
"""

from __future__ import annotations

import argparse
import time

import psutil

from brain.llm_client import LLMClient
from core.config import load_settings
from core.logger import setup_logging
from core.server_manager import LlamaServer, ServerError

PROBE_SYSTEM = "You are a concise assistant. Answer in one sentence."
PROBE_USER = "In one sentence, what does a CPU do?"


def _find_listening_pid(port):
  """Best-effort lookup of the process listening on a TCP port."""
  try:
    connections = psutil.net_connections(kind="tcp")
  except (psutil.AccessDenied, OSError):
    connections = []
  for connection in connections:
    if (
        connection.status == psutil.CONN_LISTEN
        and connection.laddr
        and connection.laddr.port == port
        and connection.pid
    ):
      return connection.pid
  for process in psutil.process_iter(["name"]):
    name = (process.info.get("name") or "").lower()
    if name.startswith("llama-server"):
      return process.pid
  return None


def _memory_mb(pid):
  """Return (rss, peak, private) in MB for a process, or None."""
  if pid is None:
    return None
  try:
    info = psutil.Process(pid).memory_info()
  except (psutil.Error, OSError):
    return None
  rss = info.rss / 1048576.0
  peak = getattr(info, "peak_wset", info.rss) / 1048576.0
  private = getattr(info, "private", info.rss) / 1048576.0
  return rss, peak, private


def run(config_path=None):
  settings = load_settings(config_path)
  setup_logging(settings.app.log_level, settings.app.log_dir)

  server = LlamaServer(settings.llama, settings.app.log_dir)
  try:
    started = server.ensure_running()
  except ServerError as exc:
    print(f"[Error] {exc}")
    return 1

  client = LLMClient(settings.llama, settings.llm)
  try:
    python_mb = psutil.Process().memory_info().rss / 1048576.0
    server_pid = server.pid or _find_listening_pid(settings.llama.port)
    server_mem = _memory_mb(server_pid)
    system = psutil.virtual_memory()

    request_start = time.monotonic()
    result = client.chat(
        [
            {"role": "system", "content": PROBE_SYSTEM},
            {"role": "user", "content": PROBE_USER},
        ]
    )
    wall_s = time.monotonic() - request_start

    server_after = _memory_mb(server_pid)

    print()
    print(f"{settings.app.name} Phase 1 baseline")
    print("-" * 52)
    mode = "started" if started else "reused existing server"
    startup = (
        f"{server.startup_seconds:.1f}s" if started else "n/a"
    )
    print(f"server            {mode} (startup {startup})")
    print(f"model             {settings.llama.resolved_model_path.name}")
    print(
        "settings          ctx=%s slots=%s threads=%s batch=%s/%s"
        % (
            settings.llama.context_size,
            settings.llama.parallel_slots,
            settings.llama.threads,
            settings.llama.batch_size,
            settings.llama.ubatch_size,
        )
    )
    print()
    print("Memory (working set)")
    print(f"  python            {python_mb:8.1f} MB")
    if server_mem:
      rss, peak, private = server_mem
      print(f"  llama-server      {rss:8.1f} MB  (peak {peak:.1f}, private {private:.1f})")
      print(f"  assistant total   {python_mb + rss:8.1f} MB")
    else:
      print("  llama-server        n/a (process not found)")
    print(
        "  system available  %8.1f MB of %.0f MB"
        % (system.available / 1048576.0, system.total / 1048576.0)
    )
    print()
    print(f"Latency: {PROBE_USER!r}")
    print(f"  prompt tokens     {result.prompt_tokens:8d}")
    print(
        "  prefill           %8.2f s  (%.1f tok/s)"
        % (result.prompt_ms / 1000.0, result.prompt_tokens_per_second)
    )
    print(
        "  generation        %8.2f s  (%.1f tok/s)"
        % (result.predicted_ms / 1000.0, result.generated_tokens_per_second)
    )
    print(f"  wall time         {wall_s:8.2f} s")
    print(f"  reply             {result.content[:120]}")
    print()
    peak_total = python_mb + (server_after[1] if server_after else 0.0)
    print(f"Peak measured working set: {peak_total:.1f} MB (target 1300 MB)")
    print()
    return 0
  finally:
    client.close()
    server.stop()


def main(argv=None):
  parser = argparse.ArgumentParser(description="JARVIS baseline benchmark")
  parser.add_argument("--config", default=None, help="path to settings.toml")
  args = parser.parse_args(argv)
  return run(args.config)


if __name__ == "__main__":
  raise SystemExit(main())
