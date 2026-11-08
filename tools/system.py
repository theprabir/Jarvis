"""System information and process listing through psutil."""

from __future__ import annotations

import logging
import os
from pathlib import Path

import psutil

from tools.base import ToolError, ToolResult

log = logging.getLogger(__name__)

KINDS = ("all", "cpu", "memory", "disk", "battery")


def _gb(value):
  return value / (1024 ** 3)


def _mb(value):
  return value / (1024 ** 2)


def system_info(config, kind="all"):
  """Report CPU, memory, disk or battery status."""
  kind = (kind or "all").strip().lower()
  if kind not in KINDS:
    raise ToolError(
        "kind must be one of " + ", ".join(KINDS)
    )
  data = {}
  parts = []

  if kind in {"all", "cpu"}:
    percent = psutil.cpu_percent(interval=0.2)
    cores = psutil.cpu_count(logical=True) or 1
    data.update(cpu_percent=percent, cpu_cores=cores)
    parts.append(f"CPU usage is {percent:.0f} percent on {cores} logical cores.")

  if kind in {"all", "memory"}:
    memory = psutil.virtual_memory()
    data.update(
        memory_percent=memory.percent,
        memory_used_gb=round(_gb(memory.used), 2),
        memory_total_gb=round(_gb(memory.total), 2),
    )
    parts.append(
        f"Memory usage is {memory.percent:.0f} percent:"
        f" {_gb(memory.used):.1f} GB used of {_gb(memory.total):.1f} GB."
    )

  if kind in {"all", "disk"}:
    root = config.resolved_root
    drive = Path(root.anchor or os.sep)
    usage = psutil.disk_usage(str(drive))
    data.update(
        disk_drive=str(drive),
        disk_percent=usage.percent,
        disk_free_gb=round(_gb(usage.free), 2),
        disk_total_gb=round(_gb(usage.total), 2),
    )
    parts.append(
        f"Disk {drive} is {usage.percent:.0f} percent full:"
        f" {_gb(usage.free):.1f} GB free of {_gb(usage.total):.1f} GB."
    )

  if kind in {"all", "battery"}:
    battery = psutil.sensors_battery()
    if battery is None:
      data["battery_percent"] = None
      parts.append("No battery detected, sir.")
    else:
      state = "plugged in" if battery.power_plugged else "on battery"
      data.update(
          battery_percent=round(float(battery.percent), 1),
          battery_plugged=bool(battery.power_plugged),
      )
      parts.append(f"Battery is at {battery.percent:.0f} percent, {state}.")

  return ToolResult(True, " ".join(parts), data)


def top_processes(config, limit=8):
  """List the processes using the most memory."""
  limit = int(limit)
  if not 1 <= limit <= 25:
    raise ToolError("limit must be between 1 and 25")

  rows = []
  for process in psutil.process_iter(["name", "memory_info"]):
    try:
      info = process.info
      memory = info.get("memory_info")
      rows.append((info.get("name") or "unknown", memory.rss if memory else 0))
    except (psutil.NoSuchProcess, psutil.AccessDenied):
      continue

  rows.sort(key=lambda item: item[1], reverse=True)
  top = [(name, round(_mb(rss), 1)) for name, rss in rows[:limit]]
  if not top:
    raise ToolError("I could not read the process list")

  summary = "; ".join(f"{name} {size} MB" for name, size in top)
  return ToolResult(
      True,
      f"Top {len(top)} processes by memory: {summary}.",
      {"processes": [{"name": name, "memory_mb": size} for name, size in top]},
  )
