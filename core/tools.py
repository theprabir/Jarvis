from datetime import datetime
import json
import os
import subprocess
import webbrowser
from duckduckgo_search import DDGS
import requests

DATA_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "../data"))
MEMORY_FILE = os.path.join(DATA_DIR, "memory.json")


def load_memory():
  """Loads persistent memory from data/memory.json."""
  os.makedirs(DATA_DIR, exist_ok=True)
  if os.path.exists(MEMORY_FILE):
    try:
      with open(MEMORY_FILE, "r", encoding="utf-8") as f:
        return json.load(f)
    except Exception:
      return {}
  return {}


def save_memory(memory_dict):
  """Saves persistent memory safely to data/memory.json."""
  os.makedirs(DATA_DIR, exist_ok=True)
  with open(MEMORY_FILE, "w", encoding="utf-8") as f:
    json.dump(memory_dict, f, indent=4)


def update_memory(key, value):
  """Updates or inserts a custom fact into persistent memory."""
  mem = load_memory()
  mem[key] = value
  save_memory(mem)
  return f"Memory updated, sir. I have saved your {key}."


def open_app(app_name):
  app_name = app_name.lower().strip()
  if "notepad" in app_name:
    subprocess.Popen(["notepad.exe"])
  elif "chrome" in app_name or "browser" in app_name:
    webbrowser.open("https://google.com")
  elif "calc" in app_name:
    subprocess.Popen(["calc.exe"])
  else:
    os.system(f"start {app_name}")
  return f"Opening {app_name}, sir."


def get_schedule():
  now = datetime.now()
  return (
      f"It is {now.strftime('%I:%M %p')}. You have no urgent appointments left"
      " for today."
  )

try:
  from ddgs import DDGS
except ImportError:
  from duckduckgo_search import DDGS


def search_duckduckgo(query, max_results=3):
  """Performs a live web search via DuckDuckGo and returns text snippets."""
  try:
    with DDGS() as ddgs:
      results = [r for r in ddgs.text(query, max_results=max_results)]
      if not results:
        return "No relevant search results found."
      return "\n".join(
          [f"- {r.get('title')}: {r.get('body')}" for r in results]
      )
  except Exception as e:
    return f"Search error: {str(e)}"