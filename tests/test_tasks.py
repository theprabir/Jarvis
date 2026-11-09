"""Feature set 4.9 tests: durable planning, execution and organization.

The engine is exercised with small in-memory tools, while the organizer uses
the real sandboxed file tools against a temporary folder. No model, microphone
or desktop action is involved.
"""

from __future__ import annotations

import dataclasses
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from core.assistant import Assistant
from core.config import ConfigError, TasksConfig, load_settings
from core.intents import IntentRouter
from core.tasks import (
    AWAITING_APPROVAL,
    AWAITING_STEP,
    CANCELLED,
    COMPLETED,
    FAILED,
    PAUSED,
    TaskEngine,
    TaskError,
    TaskStep,
    plan_organize,
)
from tools import build_registry
from tools.base import Tool, ToolRegistry, ToolResult


class FakeClient:
  def __init__(self, replies=("Model answer.",)):
    self.replies = list(replies)
    self.calls = []

  def chat(self, messages, max_tokens=None, temperature=None):
    self.calls.append({"messages": messages, "max_tokens": max_tokens})
    content = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
    return SimpleNamespace(
        content=content,
        total_ms=10.0,
        completion_tokens=4,
        generated_tokens_per_second=5.0,
        finish_reason="stop",
    )


class EngineTests(unittest.TestCase):
  def setUp(self):
    self.tmp = tempfile.TemporaryDirectory()
    self.addCleanup(self.tmp.cleanup)
    self.state = Path(self.tmp.name) / "state"
    self.state.mkdir()
    self.registry = ToolRegistry()
    self.config = TasksConfig(
        db=str(self.state / "tasks.db"),
        max_steps=40,
        max_retries=0,
        retry_delay_s=0.0,
        parallel_workers=2,
    )

  def make_engine(self, **overrides):
    config = dataclasses.replace(self.config, **overrides)
    engine = TaskEngine(config, self.registry)
    self.addCleanup(engine.close)
    return engine

  def add_tool(self, name, handler, arguments=None):
    self.registry.register(
        Tool(
            name=name,
            description=name,
            arguments=dict(arguments or {}),
            handler=handler,
        )
    )

  def test_sequential_dependencies_and_verification(self):
    seen = []

    def append(value):
      seen.append(value)
      return ToolResult(True, f"added {value}", {"count": len(seen)})

    def inspect():
      return ToolResult(True, "listed", {"items": list(seen)})

    self.add_tool("append", append, {"value": str})
    self.add_tool("inspect_items", inspect)
    engine = self.make_engine()
    task = engine.create(
        "build a sequence",
        [
            TaskStep("one", "First", "append", {"value": "a"}),
            TaskStep(
                "two",
                "Second",
                "append",
                {"value": "b"},
                depends_on=("one",),
                verify={"tool": "inspect_items", "contains": ["a", "b"]},
            ),
        ],
        approval_required=False,
    )
    result = engine.run(task.id)
    self.assertEqual(result.status, COMPLETED)
    self.assertEqual(seen, ["a", "b"])
    self.assertEqual(result.done_count(), 2)
    self.assertEqual(result.verified_count(), 1)
    kinds = [event["kind"] for event in engine.events(task.id)]
    self.assertIn("completed", kinds)

  def test_condition_skips_a_step(self):
    calls = []

    def inspect():
      return ToolResult(True, "empty", {"total": 0})

    def run(value):
      calls.append(value)
      return ToolResult(True, "ran", {"ok": True})

    self.add_tool("inspect_zero", inspect)
    self.add_tool("run", run, {"value": str})
    engine = self.make_engine()
    task = engine.create(
        "conditional",
        [
            TaskStep("inspect", "Inspect", "inspect_zero"),
            TaskStep(
                "skip",
                "Skip me",
                "run",
                {"value": "skip"},
                depends_on=("inspect",),
                condition={
                    "step": "inspect",
                    "field": "total",
                    "op": ">",
                    "value": 0,
                },
            ),
            TaskStep(
                "do",
                "Do it",
                "run",
                {"value": "do"},
                depends_on=("inspect",),
                condition={
                    "step": "inspect",
                    "field": "total",
                    "op": "==",
                    "value": 0,
                },
            ),
        ],
        approval_required=False,
    )
    result = engine.run(task.id)
    self.assertEqual(result.status, COMPLETED)
    self.assertEqual(result.step("skip").status, "skipped")
    self.assertEqual(result.step("do").status, COMPLETED)
    self.assertEqual(calls, ["do"])

  def test_retry_until_success(self):
    attempts = []

    def flaky():
      attempts.append(1)
      if len(attempts) == 1:
        return ToolResult(False, "temporary failure")
      return ToolResult(True, "recovered", {"ok": True})

    self.add_tool("flaky", flaky)
    engine = self.make_engine()
    task = engine.create(
        "retry",
        [TaskStep("flaky", "Flaky step", "flaky", retries=1)],
        approval_required=False,
    )
    result = engine.run(task.id)
    self.assertEqual(result.status, COMPLETED)
    self.assertEqual(len(attempts), 2)
    self.assertEqual(result.step("flaky").attempts, 2)
    self.assertIn("retry", [event["kind"] for event in engine.events(task.id)])

  def test_failure_is_checkpointed(self):
    self.add_tool(
        "fail", lambda: ToolResult(False, "step refused")
    )
    engine = self.make_engine()
    task = engine.create(
        "failure",
        [TaskStep("bad", "Bad step", "fail")],
        approval_required=False,
    )
    result = engine.run(task.id)
    self.assertEqual(result.status, FAILED)
    self.assertEqual(result.step("bad").status, FAILED)
    self.assertIn("step refused", result.error)
    self.assertIn("failed", [event["kind"] for event in engine.events(task.id)])

  def test_task_approval_blocks_execution(self):
    calls = []
    self.add_tool(
        "act", lambda: calls.append(1) or ToolResult(True, "acted")
    )
    engine = self.make_engine()
    task = engine.create(
        "approval", [TaskStep("act", "Act", "act")]
    )
    waiting = engine.run(task.id)
    self.assertEqual(waiting.status, AWAITING_APPROVAL)
    self.assertEqual(calls, [])
    done = engine.approve(task.id)
    self.assertEqual(done.status, COMPLETED)
    self.assertEqual(len(calls), 1)

  def test_step_approval_checkpoint(self):
    calls = []
    self.add_tool(
        "act", lambda: calls.append(1) or ToolResult(True, "acted")
    )
    engine = self.make_engine()
    task = engine.create(
        "step approval",
        [TaskStep("act", "Act", "act", approval=True)],
        approval_required=False,
    )
    waiting = engine.run(task.id)
    self.assertEqual(waiting.status, AWAITING_STEP)
    self.assertEqual(calls, [])
    done = engine.approve_step(task.id)
    self.assertEqual(done.status, COMPLETED)
    self.assertEqual(len(calls), 1)

  def test_cancel_prevents_approval(self):
    self.add_tool("act", lambda: ToolResult(True, "acted"))
    engine = self.make_engine()
    task = engine.create(
        "cancel", [TaskStep("act", "Act", "act")]
    )
    cancelled = engine.cancel(task.id)
    self.assertEqual(cancelled.status, CANCELLED)
    self.assertEqual(engine.approve(task.id).status, CANCELLED)

  def test_resume_after_failure(self):
    state = {"fail": True}

    def maybe():
      if state["fail"]:
        return ToolResult(False, "not yet")
      return ToolResult(True, "worked", {"ok": True})

    self.add_tool("maybe", maybe)
    engine = self.make_engine()
    task = engine.create(
        "resume",
        [TaskStep("maybe", "Maybe", "maybe")],
        approval_required=False,
    )
    self.assertEqual(engine.run(task.id).status, FAILED)
    state["fail"] = False
    result = engine.resume(task.id)
    self.assertEqual(result.status, COMPLETED)
    self.assertIn("resumed", [event["kind"] for event in engine.events(task.id)])

  def test_dynamic_replanner_changes_arguments(self):
    attempts = []

    def target(value):
      attempts.append(value)
      if value == "bad":
        return ToolResult(False, "target is unavailable")
      return ToolResult(True, "moved", {"value": value})

    self.add_tool("move", target, {"value": str})
    engine = self.make_engine()
    engine.set_replanner(
        lambda _task, _step, _error: {"value": "good"}
    )
    task = engine.create(
        "replan",
        [TaskStep("move", "Move", "move", {"value": "bad"})],
        approval_required=False,
    )
    result = engine.run(task.id)
    self.assertEqual(result.status, COMPLETED)
    self.assertEqual(attempts, ["bad", "good"])
    self.assertIn(
        "replanned", [event["kind"] for event in engine.events(task.id)]
    )

  def test_parallel_independent_steps(self):
    barrier = threading.Barrier(2)

    def wait():
      barrier.wait(timeout=2)
      return ToolResult(True, "together", {"ok": True})

    self.add_tool("wait", wait)
    engine = self.make_engine(parallel_workers=2)
    task = engine.create(
        "parallel",
        [
            TaskStep("a", "A", "wait", parallel=True),
            TaskStep("b", "B", "wait", parallel=True),
        ],
        approval_required=False,
    )
    result = engine.run(task.id)
    self.assertEqual(result.status, COMPLETED)
    self.assertEqual(result.step("a").status, COMPLETED)
    self.assertEqual(result.step("b").status, COMPLETED)

  def test_background_execution_notifies(self):
    started = threading.Event()
    release = threading.Event()
    messages = []

    def slow():
      started.set()
      release.wait(timeout=2)
      return ToolResult(True, "finished", {"ok": True})

    self.add_tool("slow", slow)
    engine = self.make_engine()
    engine.set_notifier(messages.append)
    task = engine.create(
        "background",
        [TaskStep("slow", "Slow", "slow")],
        approval_required=False,
    )
    engine.start(task.id)
    self.assertTrue(started.wait(timeout=2))
    release.set()
    deadline = time.monotonic() + 3
    result = engine.get(task.id)
    while result.status not in {COMPLETED, FAILED, CANCELLED}:
      self.assertLess(time.monotonic(), deadline, "background task hung")
      time.sleep(0.02)
      result = engine.get(task.id)
    self.assertEqual(result.status, COMPLETED)
    self.assertTrue(any("completed" in message for message in messages))

  def test_interrupted_task_recovers_as_paused(self):
    self.add_tool("ok", lambda: ToolResult(True, "ok", {"ok": True}))
    engine = self.make_engine()
    task = engine.create(
        "recover",
        [TaskStep("ok", "Ok", "ok")],
        approval_required=False,
    )
    task.status = "running"
    task.steps[0].status = "running"
    engine._store.save(task)
    engine.close()

    reopened = TaskEngine(self.config, self.registry)
    self.addCleanup(reopened.close)
    recovered = reopened.get(task.id)
    self.assertEqual(recovered.status, PAUSED)
    self.assertEqual(recovered.steps[0].status, "pending")
    self.assertEqual(reopened.resume(task.id).status, COMPLETED)

  def test_dependency_cycle_is_rejected(self):
    engine = self.make_engine()
    with self.assertRaises(TaskError):
      engine.create(
          "cycle",
          [
              TaskStep("a", "A", "missing", depends_on=("b",)),
              TaskStep("b", "B", "missing", depends_on=("a",)),
          ],
          approval_required=False,
      )


class OrganizeTests(unittest.TestCase):
  def setUp(self):
    self.tmp = tempfile.TemporaryDirectory()
    self.addCleanup(self.tmp.cleanup)
    self.work = Path(self.tmp.name) / "work"
    self.work.mkdir()
    self.state = Path(self.tmp.name) / "state"
    self.state.mkdir()
    self.base = load_settings()
    self.tools = dataclasses.replace(self.base.tools, root=str(self.work))
    self.tasks = dataclasses.replace(
        self.base.tasks,
        db=str(self.state / "tasks.db"),
        max_steps=40,
        retry_delay_s=0.0,
        parallel_workers=2,
    )
    self.registry = build_registry(
        dataclasses.replace(self.base, tools=self.tools)
    )

  def test_organize_moves_and_verifies_files(self):
    downloads = self.work / "Downloads"
    downloads.mkdir()
    (downloads / "Documents").mkdir()
    (downloads / "Documents" / "report.pdf").write_text("old", encoding="utf-8")
    (downloads / "report.pdf").write_text("new", encoding="utf-8")
    (downloads / "photo.jpg").write_text("photo", encoding="utf-8")
    (downloads / "notes.txt").write_text("notes", encoding="utf-8")

    engine = TaskEngine(self.tasks, self.registry)
    self.addCleanup(engine.close)
    task = engine.create_organize("Downloads")
    self.assertEqual(task.status, AWAITING_APPROVAL)
    self.assertEqual(task.context["files"], 3)
    self.assertGreater(len(task.steps), 5)
    result = engine.approve(task.id)
    self.assertEqual(result.status, COMPLETED)
    self.assertEqual(result.verified_count(), 2)
    self.assertTrue((downloads / "Documents" / "report (2).pdf").is_file())
    self.assertTrue((downloads / "Documents" / "notes.txt").is_file())
    self.assertTrue((downloads / "Images" / "photo.jpg").is_file())
    self.assertFalse((downloads / "report.pdf").exists())
    self.assertFalse((downloads / "photo.jpg").exists())

  def test_empty_folder_is_a_single_step(self):
    downloads = self.work / "Downloads"
    downloads.mkdir()
    engine = TaskEngine(self.tasks, self.registry)
    self.addCleanup(engine.close)
    task = engine.create_organize("Downloads")
    self.assertEqual(task.context["files"], 0)
    self.assertEqual(len(task.steps), 1)
    result = engine.approve(task.id)
    self.assertEqual(result.status, COMPLETED)

  def test_plan_rejects_unknown_folder(self):
    engine = TaskEngine(self.tasks, self.registry)
    self.addCleanup(engine.close)
    with self.assertRaises(TaskError):
      engine.create_organize("missing-folder")


class AssistantTaskTests(unittest.TestCase):
  def setUp(self):
    self.tmp = tempfile.TemporaryDirectory()
    self.addCleanup(self.tmp.cleanup)
    self.work = Path(self.tmp.name) / "work"
    self.work.mkdir()
    self.state = Path(self.tmp.name) / "state"
    self.state.mkdir()
    self.downloads = self.work / "Downloads"
    self.downloads.mkdir()
    (self.downloads / "report.pdf").write_text("report", encoding="utf-8")
    (self.downloads / "photo.jpg").write_text("photo", encoding="utf-8")

  def make_assistant(self, tasks_enabled=True):
    base = load_settings()
    settings = dataclasses.replace(
        base,
        conversation=dataclasses.replace(base.conversation, enabled=False),
        memory=dataclasses.replace(base.memory, enabled=False),
        tools=dataclasses.replace(base.tools, root=str(self.work)),
        tasks=dataclasses.replace(
            base.tasks,
            enabled=tasks_enabled,
            db=str(self.state / "assistant.db"),
            retry_delay_s=0.0,
            parallel_workers=1,
        ),
    )
    client = FakeClient()
    assistant = Assistant(settings, client=client)
    self.addCleanup(assistant.close)
    return assistant, client

  def test_organize_requires_confirmation_then_runs(self):
    assistant, client = self.make_assistant()
    prompt = assistant.reply("organize my downloads folder")
    self.assertIn("Task #1", prompt)
    self.assertIn("confirm", prompt.lower())
    self.assertFalse((self.downloads / "Documents").exists())

    done = assistant.reply("yes")
    self.assertIn("completed", done.lower())
    self.assertTrue((self.downloads / "Documents" / "report.pdf").is_file())
    self.assertTrue((self.downloads / "Images" / "photo.jpg").is_file())
    self.assertEqual(client.calls, [])
    self.assertIn("completed", assistant.reply("/tasks"))
    self.assertIn("completed", assistant.reply("/tasks history"))

  def test_cancel_keeps_files_in_place(self):
    assistant, _client = self.make_assistant()
    assistant.reply("organize my downloads folder")
    self.assertEqual(assistant.reply("no"), "Cancelled, sir.")
    self.assertTrue((self.downloads / "report.pdf").is_file())
    status = assistant.reply("/tasks")
    self.assertIn("cancelled", status.lower())

  def test_tasks_can_be_disabled(self):
    assistant, client = self.make_assistant(tasks_enabled=False)
    self.assertIn("disabled", assistant.reply("organize my downloads folder"))
    self.assertEqual(client.calls, [])

  def test_show_and_control_commands(self):
    assistant, _client = self.make_assistant()
    assistant.reply("organize my downloads folder")
    detail = assistant.reply("/tasks show 1")
    self.assertIn("awaiting_approval", detail)
    approved = assistant.reply("/tasks approve 1")
    self.assertIn("completed", approved.lower())
    self.assertIn("completed", assistant.reply("/tasks show 1"))

  def test_background_request_is_announced(self):
    assistant, _client = self.make_assistant()
    prompt = assistant.reply(
        "organize my downloads folder in the background"
    )
    self.assertIn("background", prompt)
    reply = assistant.reply("confirm")
    self.assertIn("background", reply.lower())
    deadline = time.monotonic() + 3
    detail = assistant.reply("/tasks show 1")
    while "completed" not in detail.lower():
      self.assertLess(time.monotonic(), deadline, "background task hung")
      time.sleep(0.02)
      detail = assistant.reply("/tasks show 1")
    self.assertIn("completed", detail.lower())


class TaskIntentTests(unittest.TestCase):
  def setUp(self):
    self.router = IntentRouter()

  def test_organize_intent_and_background(self):
    intent = self.router.route("organize my downloads folder")
    self.assertEqual(intent.name, "task_organize")
    self.assertEqual(intent.entities["path"], "Downloads")
    background = self.router.route(
        "organize my downloads folder in the background"
    )
    self.assertTrue(background.entities["background"])
    absolute = self.router.route("organize C:/temp/inbox")
    self.assertEqual(absolute.entities["path"], "C:/temp/inbox")

  def test_task_controls(self):
    cases = {
        "list tasks": "task_list",
        "what tasks are running": "task_list",
        "task status": "task_list",
        "show task 2": "task_show",
        "approve task 2": "task_approve",
        "resume task 2": "task_resume",
        "cancel task 2": "task_cancel",
        "task history": "task_history",
    }
    for text, name in cases.items():
      intent = self.router.route(text)
      self.assertIsNotNone(intent, text)
      self.assertEqual(intent.name, name, text)
      if "task 2" in text:
        self.assertEqual(intent.entities["id"], 2)

  def test_plain_organize_thoughts_is_not_a_task(self):
    self.assertIsNone(self.router.route("organize my thoughts"))


class TasksConfigTests(unittest.TestCase):
  def test_invalid_parallel_workers_is_rejected(self):
    with tempfile.TemporaryDirectory() as tmp:
      path = Path(tmp) / "settings.toml"
      path.write_text(
          "[tasks]\nparallel_workers = 0\n", encoding="utf-8"
      )
      with self.assertRaises(ConfigError):
        load_settings(path)


if __name__ == "__main__":
  unittest.main()
