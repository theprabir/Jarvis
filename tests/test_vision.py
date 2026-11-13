"""Feature set 4.17 tests: local visual intelligence without a visual model."""

from __future__ import annotations

import dataclasses
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from PIL import Image, ImageDraw

from core.assistant import Assistant
from core.config import load_settings
from core.intents import IntentRouter
from core.vision import VisionService, format_analysis


class FakeDocuments:
  def __init__(self, root):
    self.root = Path(root)
    self.texts = {}

  def resolve(self, raw):
    path = Path(str(raw or ""))
    target = (path if path.is_absolute() else self.root / path).resolve()
    if target != self.root.resolve() and self.root.resolve() not in target.parents:
      raise ValueError("outside the sandbox")
    return target

  def read(self, path):
    target = Path(path)
    text = self.texts.get(target.name, "")
    return SimpleNamespace(
        text=text,
        metadata={"ocr_confidence": 0.82, "ocr_level": "good"},
    )


class FakeClient:
  def __init__(self):
    self.calls = []

  def chat(self, messages, max_tokens=None, temperature=None):
    self.calls.append(messages)
    return SimpleNamespace(
        content="Model answer.",
        total_ms=1.0,
        completion_tokens=1,
        generated_tokens_per_second=1.0,
        finish_reason="stop",
    )

  def close(self):
    pass


def _image(path, color=(245, 245, 245), second=False):
  image = Image.new("RGB", (640, 480), color)
  draw = ImageDraw.Draw(image)
  draw.rectangle((40, 40, 300, 110), outline=(20, 20, 20), width=4)
  draw.rectangle((40, 160, 580, 300), outline=(30, 30, 30), width=3)
  draw.rectangle((400, 330, 590, 440), fill=(190, 210, 235))
  if second:
    draw.rectangle((60, 60, 250, 95), fill=(255, 210, 120))
  image.save(path)
  image.close()


class VisionServiceTests(unittest.TestCase):
  def setUp(self):
    self.tmp = tempfile.TemporaryDirectory()
    self.addCleanup(self.tmp.cleanup)
    self.root = Path(self.tmp.name)
    self.screens = self.root / "screenshots"
    self.screens.mkdir()
    self.first = self.root / "screen-one.png"
    self.second = self.root / "screen-two.png"
    _image(self.first)
    _image(self.second, second=True)
    self.documents = FakeDocuments(self.root)
    self.documents.texts["screen-one.png"] = (
        "Error: report not found\nRevenue 10 20 30\nQuarterly report"
    )
    self.documents.texts["screen-two.png"] = (
        "Status: ready\nRevenue 10 20 34\nQuarterly report updated"
    )
    base = load_settings()
    self.config = dataclasses.replace(
        base.documents,
        db=str(self.root / "vision.db"),
        ocr_enabled=True,
    )
    self.tools = dataclasses.replace(base.tools, root=str(self.root))
    self.service = VisionService(
        self.config,
        self.tools,
        self.documents,
        screenshot_provider=lambda: str(self.first),
    )

  def tearDown(self):
    self.service.close()

  def test_analyze_metadata_and_layout(self):
    result = self.service.analyze("screen-one.png")
    self.assertEqual(result.width, 640)
    self.assertEqual(result.height, 480)
    self.assertEqual(result.format, "PNG")
    self.assertIn("report", result.text)
    self.assertGreaterEqual(len(result.elements), 1)
    self.assertIn("Error", result.summary)
    rendered = format_analysis(result)
    self.assertIn("640x480", rendered)
    self.assertIn("OCR text", rendered)

  def test_text_and_summary(self):
    text, metadata = self.service.text("screen-one.png")
    self.assertIn("Revenue", text)
    self.assertEqual(metadata["ocr_level"], "good")
    summary = self.service.summary("screen-one.png")
    self.assertIn("report", summary)

  def test_chart_and_diagram_hints(self):
    chart = self.service.chart("screen-one.png")
    self.assertEqual(chart["values"][:3], [10.0, 20.0, 30.0])
    self.assertEqual(chart["trend"], "rising")
    diagram = self.service.diagram("screen-one.png")
    self.assertGreaterEqual(diagram["boxes"], 1)
    self.assertTrue(diagram["labels"])

  def test_compare_and_troubleshoot(self):
    result = self.service.compare("screen-one.png", "screen-two.png")
    self.assertLess(result["pixel_similarity"], 100.0)
    self.assertLess(result["text_similarity"], 100.0)
    self.assertTrue(result["only_first"] or result["only_second"])
    _analysis, checks = self.service.troubleshoot("screen-one.png")
    self.assertTrue(any("Error words" in check for check in checks))

  def test_index_and_search(self):
    indexed = self.service.index("screen-one.png")
    self.assertEqual(indexed["chars"], len(self.documents.texts["screen-one.png"]))
    self.service.index("screen-one.png")
    rows = self.service.search("report")
    self.assertTrue(rows)
    self.assertEqual(rows[0]["title"], "screen-one")
    self.assertEqual(len(rows), 1)

  def test_screenshot_provider_and_latest(self):
    result = self.service.screenshot()
    self.assertEqual(result.path, self.first.resolve())
    latest = self.screens / "latest.png"
    _image(latest, second=True)
    self.assertEqual(self.service.latest(), latest.resolve())

  def test_natural_vision_intents(self):
    router = IntentRouter()
    cases = (
        ("read the image at screen-one.png", "read"),
        ("summarize the screenshot at screen-one.png", "summary"),
        ("compare images screen-one.png and screen-two.png", "compare"),
        ("analyze my screen", "screen"),
        ("search images for report", "search"),
    )
    for text, action in cases:
      with self.subTest(text=text):
        intent = router.route(text)
        self.assertIsNotNone(intent)
        self.assertEqual(intent.name, "vision")
        self.assertEqual(intent.entities["action"], action)


class VisionAssistantTests(unittest.TestCase):
  def setUp(self):
    self.tmp = tempfile.TemporaryDirectory()
    self.addCleanup(self.tmp.cleanup)
    self.root = Path(self.tmp.name)
    self.first = self.root / "screen-one.png"
    self.second = self.root / "screen-two.png"
    _image(self.first)
    _image(self.second, second=True)
    documents = FakeDocuments(self.root)
    documents.texts["screen-one.png"] = "Error: report not found"
    documents.texts["screen-two.png"] = "Status: ready"
    base = load_settings()
    settings = dataclasses.replace(
        base,
        conversation=dataclasses.replace(
            base.conversation,
            enabled=False,
            history_db=str(self.root / "history.db"),
        ),
        memory=dataclasses.replace(base.memory, enabled=False),
        tools=dataclasses.replace(base.tools, root=str(self.root)),
        documents=dataclasses.replace(
            base.documents, db=str(self.root / "vision.db")
        ),
    )
    self.assistant = Assistant(settings, client=FakeClient())
    self.addCleanup(self.assistant.close)
    self.assistant._vision = VisionService(
        settings.documents,
        settings.tools,
        documents,
        screenshot_provider=lambda: str(self.first),
    )

  def test_vision_help_and_natural_read(self):
    self.assertIn("/vision", self.assistant.reply("/help"))
    self.assertIn("Vision commands", self.assistant.reply("/vision"))
    reply = self.assistant.reply("read the image at screen-one.png")
    self.assertIn("Error", reply)
    self.assertIn("report", reply)

  def test_vision_command_analyze_search_compare_and_chart(self):
    reply = self.assistant.reply("/vision analyze screen-one.png")
    self.assertIn("640x480", reply)
    self.assertIn("Error", reply)
    indexed = self.assistant.reply("/vision index .")
    self.assertIn("Indexed", indexed)
    found = self.assistant.reply("/vision search report")
    self.assertIn("screen-one", found)
    compared = self.assistant.reply(
        "/vision compare screen-one.png | screen-two.png"
    )
    self.assertIn("Pixel similarity", compared)
    chart = self.assistant.reply("/vision chart screen-one.png")
    self.assertIn("Trend heuristic", chart)


if __name__ == "__main__":
  unittest.main()
