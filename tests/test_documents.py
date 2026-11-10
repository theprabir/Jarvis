"""Feature set 4.10 tests: local document intelligence and retrieval."""

from __future__ import annotations

import dataclasses
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from core.assistant import Assistant
from core.config import load_settings
from core.documents import (
    DocumentError,
    DocumentService,
    chunk_text,
    compare_documents,
    extract_actions,
    extract_dates,
    read_document,
    summarize_document,
)
from core.intents import IntentRouter
from tools import build_registry


class FakeClient:
  def __init__(self, replies=("Model answer [1].",), error=None):
    self.replies = list(replies)
    self.error = error
    self.calls = []

  def chat(self, messages, max_tokens=None, temperature=None):
    self.calls.append({"messages": messages, "max_tokens": max_tokens})
    if self.error is not None:
      raise self.error
    content = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
    return SimpleNamespace(
        content=content,
        total_ms=1.0,
        completion_tokens=3,
        generated_tokens_per_second=1.0,
        finish_reason="stop",
    )


class DocumentFixture(unittest.TestCase):
  def setUp(self):
    self.tmp = tempfile.TemporaryDirectory()
    self.addCleanup(self.tmp.cleanup)
    self.root = Path(self.tmp.name) / "work"
    self.root.mkdir()
    self.docs = self.root / "Documents"
    self.docs.mkdir()
    base = load_settings()
    self.settings = dataclasses.replace(
        base,
        tools=dataclasses.replace(base.tools, root=str(self.root)),
        documents=dataclasses.replace(
            base.documents, db=str(Path(self.tmp.name) / "documents.db")
        ),
    )
    self.service = DocumentService(
        self.settings.documents, self.settings.tools
    )
    self.addCleanup(self.service.close)

  def write(self, name, text="", encoding="utf-8"):
    path = self.docs / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding=encoding)
    return path

  def make_docx(self, name="plan.docx"):
    from docx import Document

    document = Document()
    document.add_heading("Project Plan", level=1)
    document.add_paragraph("The launch date is 2026-12-01.")
    table = document.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "Owner"
    table.cell(0, 1).text = "Task"
    table.cell(1, 0).text = "Ada"
    table.cell(1, 1).text = "Write tests"
    path = self.docs / name
    document.save(str(path))
    return path

  def make_scan(self, name="scan.png"):
    from PIL import Image, ImageDraw, ImageFont

    try:
      font = ImageFont.truetype("C:/Windows/Fonts/arialbd.ttf", 64)
    except OSError:
      self.skipTest("Arial Bold is required for the OCR fixture")
    image = Image.new("RGB", (1400, 360), "white")
    draw = ImageDraw.Draw(image)
    draw.text((60, 70), "JARVIS OCR TEST", font=font, fill="black")
    draw.text((60, 180), "Budget total 1200 dollars", font=font, fill="black")
    path = self.docs / name
    image.save(str(path))
    return path

  def make_xlsx(self, name="budget.xlsx"):
    from openpyxl import Workbook

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Budget"
    sheet.append(["Item", "Cost"])
    sheet.append(["Server", 1200])
    sheet.append(["Domain", 15])
    path = self.docs / name
    workbook.save(str(path))
    workbook.close()
    return path


class ExtractionTests(DocumentFixture):
  def test_markdown_and_text_are_read(self):
    markdown = self.write(
        "notes.md", "# Weekly Notes\n\nRevenue was 1200 in October 2026."
    )
    document = self.service.read(str(markdown))
    self.assertEqual(document.title, "Weekly Notes")
    self.assertEqual(document.format, "Markdown")
    self.assertIn("Revenue", document.text)

    self.write("plain.txt", "Line one\nLine two")
    plain = self.service.read("Documents/plain.txt")
    self.assertEqual(plain.format, "TXT")
    self.assertIn("Line two", plain.text)

  def test_csv_is_extracted_as_a_table(self):
    self.write(
        "expenses.csv", "Item,Cost\nServer,1200\nDomain,15\n"
    )
    document = self.service.read("Documents/expenses.csv")
    self.assertEqual(document.format, "CSV")
    self.assertEqual(len(document.tables), 1)
    self.assertEqual(document.tables[0].rows[0], ["Item", "Cost"])
    self.assertIn("Server", document.text)

  def test_docx_text_and_tables_are_extracted(self):
    path = self.make_docx()
    document = self.service.read(str(path))
    self.assertEqual(document.format, "DOCX")
    self.assertIn("Project Plan", document.text)
    self.assertIn("Write tests", document.text)
    self.assertEqual(len(document.tables), 1)
    self.assertEqual(document.tables[0].rows[0], ["Owner", "Task"])

  def test_xlsx_sheets_become_tables(self):
    path = self.make_xlsx()
    document = self.service.read(str(path))
    self.assertEqual(document.format, "XLSX")
    self.assertEqual(len(document.tables), 1)
    self.assertEqual(document.tables[0].name, "Budget")
    self.assertEqual(document.tables[0].rows[1], ["Server", "1200"])
    self.assertIn("Sheet: Budget", document.text)

  def test_pdf_round_trip(self):
    result = self.service.write(
        "report.pdf",
        "This is a generated report.\n\nIt has two paragraphs.",
        title="Generated Report",
    )
    self.assertEqual(result["format"], "PDF")
    document = self.service.read("report.pdf")
    self.assertIn("Generated Report", document.text)
    self.assertIn("two paragraphs", document.text)
    self.assertIn("[Page 1]", document.text)

  @unittest.skipUnless(sys.platform == "win32", "Windows OCR is required")
  def test_scanned_pdf_and_image_use_ocr(self):
    image = self.make_scan("scan.png")
    image_document = self.service.read(str(image))
    self.assertEqual(image_document.format, "PNG")
    self.assertTrue(image_document.metadata["ocr"])
    self.assertIn("JARVIS", image_document.text)
    self.assertIn("1200", image_document.text)

    pdf = self.docs / "scan.pdf"
    from PIL import Image

    with Image.open(str(image)) as scan:
      scan.save(str(pdf), "PDF", resolution=200)
    pdf_document = self.service.read(str(pdf))
    self.assertEqual(pdf_document.format, "PDF")
    self.assertIn("[Page 1]", pdf_document.text)
    self.assertIn("JARVIS", pdf_document.text)
    self.assertIn("1200", pdf_document.text)
    self.assertEqual(pdf_document.metadata["ocr_pages"], 1)
    self.assertGreaterEqual(pdf_document.metadata["ocr_confidence"], 0.0)
    self.assertEqual(
        pdf_document.metadata["ocr_page_details"][0]["page"], 1
    )
    indexed = self.service.index(str(pdf))
    self.assertIsNotNone(indexed["ocr_confidence"])
    self.assertTrue(
        any("Budget" in hit["text"] for hit in self.service.search("Budget"))
    )

  @unittest.skipUnless(sys.platform == "win32", "Windows OCR is required")
  def test_ocr_reports_low_confidence_and_partial_warning(self):
    image = self.make_scan("low-confidence.png")
    with mock.patch(
        "winocr.recognize_pil_sync",
        return_value={"text": "X", "lines": []},
    ):
      document = self.service.read(str(image))
    self.assertLess(document.metadata["ocr_confidence"], 0.6)
    self.assertIn(document.metadata["ocr_level"], {"partial", "fair"})
    self.assertTrue(document.metadata["ocr_partial"])
    self.assertTrue(document.metadata["ocr_warnings"])

    from PIL import Image

    pdf = self.docs / "low-confidence.pdf"
    with Image.open(str(image)) as scan:
      scan.save(str(pdf), "PDF", resolution=200)
    with mock.patch(
        "winocr.recognize_pil_sync",
        return_value={"text": "X", "lines": []},
    ):
      pdf_document = self.service.read(str(pdf))
    self.assertLess(pdf_document.metadata["ocr_confidence"], 0.6)
    self.assertEqual(pdf_document.metadata["ocr_partial_pages"], [1])
    self.assertTrue(pdf_document.metadata["ocr_warnings"])

  def test_image_ocr_can_be_disabled(self):
    image = self.make_scan("disabled.png")
    disabled = dataclasses.replace(self.service._config, ocr_enabled=False)
    with self.assertRaises(DocumentError):
      read_document(disabled, image)

  def test_unsupported_and_missing_files_are_rejected(self):
    self.write("data.json", "{}")
    with self.assertRaises(DocumentError):
      self.service.read("Documents/data.json")
    with self.assertRaises(DocumentError):
      self.service.read("Documents/missing.pdf")

  def test_paths_outside_the_tool_root_are_rejected(self):
    outside = Path(self.tmp.name) / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    with self.assertRaises(DocumentError):
      self.service.read(str(outside))
    with self.assertRaises(DocumentError):
      self.service.read("../outside.txt")


class IndexTests(DocumentFixture):
  def test_index_search_and_remove(self):
    self.write(
        "budget.md",
        "# Budget\n\nRevenue was 1200 in October 2026.\n\n"
        "The server budget is 400.",
    )
    result = self.service.index("Documents/budget.md")
    self.assertGreaterEqual(result["chunks"], 1)
    hits = self.service.search("server budget")
    self.assertTrue(hits)
    self.assertEqual(hits[0]["title"], "Budget")
    self.assertIn("server", hits[0]["text"].lower())

    self.service.remove("Documents/budget.md")
    self.assertEqual(self.service.search("server budget"), [])

  def test_folder_index_skips_unchanged_documents(self):
    self.write("a.md", "# Alpha\n\nRevenue was 100.")
    self.write("b.txt", "Beta document with expenses.")
    self.write("ignore.json", "{}")
    first = self.service.index_folder("Documents")
    self.assertEqual(first["indexed"], 2)
    self.assertEqual(first["failed"], 0)
    second = self.service.index_folder("Documents")
    self.assertEqual(second["indexed"], 0)
    self.assertEqual(second["skipped"], 2)

  def test_chunking_keeps_overlap(self):
    text = "Sentence number one. " * 120
    chunks = chunk_text(text, size=300, overlap=50)
    self.assertGreater(len(chunks), 2)
    self.assertTrue(all(len(chunk) <= 300 for chunk in chunks))
    self.assertIn("Sentence", chunks[0])

  def test_scan_fallback_when_fts_is_unavailable(self):
    self.write("fallback.md", "# Fallback\n\nThe launch plan is ready.")
    self.service.index("Documents/fallback.md")
    self.service._store._fts = False
    hits = self.service.search("launch")
    self.assertTrue(hits)
    self.assertEqual(hits[0]["mode"], "scan")


class IntelligenceTests(DocumentFixture):
  def test_summary_dates_and_actions(self):
    path = self.write(
        "plan.md",
        "# Launch Plan\n\nRevenue was 1200 in October 2026. "
        "The launch date is 2026-12-01. This paragraph gives the summary "
        "enough words to be selected as a useful key point by the local "
        "extractive summarizer.\n\n"
        "## Action Items\n- [ ] Review the budget\n"
        "- [ ] Send the final report\n",
    )
    document = self.service.read(str(path))
    summary = summarize_document(document)
    self.assertIn("Launch Plan", summary)
    self.assertIn("Key points", summary)

    dates = extract_dates(document.text)
    self.assertTrue(any(item["text"] == "2026-12-01" for item in dates))
    self.assertTrue(any("October 2026" in item["text"] for item in dates))
    actions = extract_actions(document.text)
    self.assertIn("Review the budget", actions)
    self.assertIn("Send the final report", actions)

  def test_compare_reports_similarity_and_differences(self):
    first = self.write("first.md", "# Plan\n\nAlpha beta gamma.")
    second = self.write("second.md", "# Plan\n\nAlpha beta delta.")
    result = compare_documents(
        self.service.read(str(first)), self.service.read(str(second))
    )
    self.assertGreater(result["similarity"], 0)
    self.assertLess(result["similarity"], 100)
    self.assertTrue(result["added"] or result["removed"])

  def test_report_and_conversion(self):
    first = self.write("first.md", "# Alpha\n\nThe first report.")
    self.make_docx()
    report = self.service.report(
        [str(first), "Documents/plan.docx"], "documents-report.md"
    )
    self.assertEqual(report["documents"], 2)
    report_document = self.service.read("documents-report.md")
    self.assertIn("Alpha", report_document.text)
    self.assertIn("Project Plan", report_document.text)

    converted = self.service.convert(
        "Documents/plan.docx", format="csv"
    )
    converted_document = self.service.read(converted["path"])
    self.assertEqual(converted_document.format, "CSV")
    self.assertIn("Write tests", converted_document.text)

  def test_convert_without_tables_is_rejected(self):
    self.write("plain.md", "# Plain\n\nNo tables here.")
    with self.assertRaises(DocumentError):
      self.service.convert("Documents/plain.md", format="csv")


class DocumentToolTests(DocumentFixture):
  def setUp(self):
    super().setUp()
    self.registry = build_registry(self.settings, self.service)

  def test_document_tools_are_registered_with_permissions(self):
    names = set(self.registry.names())
    for name in {
        "document_read",
        "document_index",
        "document_index_folder",
        "document_search",
        "document_extract",
        "document_convert",
        "document_write",
        "document_report",
    }:
      self.assertIn(name, names)
    self.assertTrue(self.registry.requires_confirmation("document_convert"))
    self.assertTrue(self.registry.requires_confirmation("document_write"))
    self.assertFalse(self.registry.requires_confirmation("document_read"))

  def test_read_index_and_search_tools(self):
    self.write("tool.md", "# Tool Document\n\nSearchable telescope facts.")
    read = self.registry.execute(
        "document_read", {"path": "Documents/tool.md"}
    )
    self.assertTrue(read.ok, read.text)
    self.assertEqual(read.data["format"], "Markdown")
    indexed = self.registry.execute(
        "document_index", {"path": "Documents/tool.md"}
    )
    self.assertTrue(indexed.ok, indexed.text)
    found = self.registry.execute(
        "document_search", {"query": "telescope"}
    )
    self.assertTrue(found.ok)
    self.assertEqual(found.data["hits"][0]["title"], "Tool Document")

  def test_write_tool_creates_and_indexes_a_document(self):
    result = self.registry.execute(
        "document_write",
        {
            "path": "written.pdf",
            "content": "A generated document for testing.",
            "title": "Written",
        },
    )
    self.assertTrue(result.ok, result.text)
    self.assertTrue((self.root / "written.pdf").is_file())
    document = self.service.read("written.pdf")
    self.assertIn("generated document", document.text)


class AssistantDocumentTests(DocumentFixture):
  def make_assistant(self, replies=("Revenue was 1200 [1].",)):
    settings = dataclasses.replace(
        self.settings,
        conversation=dataclasses.replace(
            self.settings.conversation, enabled=False
        ),
        memory=dataclasses.replace(self.settings.memory, enabled=False),
        tasks=dataclasses.replace(
            self.settings.tasks,
            db=str(Path(self.tmp.name) / "tasks.db"),
        ),
    )
    client = FakeClient(replies)
    assistant = Assistant(settings, client=client)
    self.addCleanup(assistant.close)
    return assistant, client

  def test_intent_router_recognizes_document_requests(self):
    router = IntentRouter()
    cases = {
        "index my documents": "document_index",
        "search documents for budget": "document_search",
        "ask documents about revenue": "document_ask",
        "summarize report.pdf": "document_summarize",
        "read document report.pdf": "document_read",
        "find dates in report.pdf": "document_dates",
        "list action items from report.pdf": "document_actions",
        "show tables from report.pdf": "document_tables",
        "compare a.pdf and b.pdf": "document_compare",
        "explain report.pdf": "document_explain",
        "convert report.pdf to docx": "document_convert",
        "create a pdf document named weekly.pdf about the budget":
            "document_generate",
        "generate a report from a.pdf and b.pdf": "document_report",
    }
    for text, expected in cases.items():
      intent = router.route(text)
      self.assertIsNotNone(intent, text)
      self.assertEqual(intent.name, expected, text)

  def test_commands_index_search_ask_and_summarize(self):
    self.write(
        "budget.md",
        "# Budget Plan\n\nRevenue was 1200 in October 2026.\n\n"
        "## Action Items\n- [ ] Review the budget\n",
    )
    assistant, client = self.make_assistant()
    indexed = assistant.reply("/documents index Documents")
    self.assertIn("Indexed 1", indexed)
    listed = assistant.reply("/documents list")
    self.assertIn("Budget Plan", listed)
    search = assistant.reply("/documents search revenue")
    self.assertIn("Budget Plan", search)
    answer = assistant.reply("/documents ask what was the revenue")
    self.assertIn("1200", answer)
    self.assertIn("Sources:", answer)
    summary = assistant.reply(
        "/documents summarize Documents/budget.md"
    )
    self.assertIn("Budget Plan", summary)
    dates = assistant.reply("/documents dates Documents/budget.md")
    self.assertIn("October 2026", dates)
    actions = assistant.reply(
        "/documents actions Documents/budget.md"
    )
    self.assertIn("Review the budget", actions)
    self.assertTrue(client.calls)

  def test_natural_question_uses_citations(self):
    self.write("facts.md", "# Facts\n\nRevenue was 1200 in 2026.")
    assistant, client = self.make_assistant()
    assistant.reply("/documents index Documents")
    answer = assistant.reply("ask documents about revenue")
    self.assertIn("Revenue was 1200 [1].", answer)
    self.assertIn("Sources:", answer)
    self.assertEqual(client.calls[-1]["messages"][0]["role"], "system")

  def test_convert_and_generate_require_confirmation(self):
    self.write("source.md", "# Source\n\nSome source text.")
    assistant, _client = self.make_assistant(
        ("A generated budget overview.",)
    )
    pending = assistant.reply("convert Documents/source.md to docx")
    self.assertIn("confirm", pending.lower())
    self.assertFalse((self.docs / "source.docx").exists())
    assistant.reply("yes")
    self.assertTrue((self.docs / "source.docx").exists())

    pending = assistant.reply(
        "create a pdf document named generated.pdf about the budget"
    )
    self.assertIn("confirm", pending.lower())
    self.assertFalse((self.root / "generated.pdf").exists())
    assistant.reply("confirm")
    self.assertTrue((self.root / "generated.pdf").is_file())

  def test_create_report_and_remove_commands(self):
    first = self.write("one.md", "# One\n\nFirst document.")
    second = self.write("two.md", "# Two\n\nSecond document.")
    assistant, _client = self.make_assistant()
    created = assistant.reply("/documents create note.md | Hello document")
    self.assertIn("Created", created)
    report = assistant.reply(
        "/documents report summary.md | Documents/one.md | Documents/two.md"
    )
    self.assertIn("Created a report", report)
    self.assertTrue((self.root / "summary.md").is_file())
    self.service.index("Documents/one.md")
    removed = assistant.reply("/documents remove Documents/one.md")
    self.assertIn("Removed", removed)

  @unittest.skipUnless(sys.platform == "win32", "Windows OCR is required")
  def test_read_command_reports_partial_ocr(self):
    image = self.make_scan("assistant-scan.png")
    pdf = self.docs / "assistant-scan.pdf"
    from PIL import Image

    with Image.open(str(image)) as scan:
      scan.save(str(pdf), "PDF", resolution=200)
    assistant, _client = self.make_assistant()
    with mock.patch(
        "winocr.recognize_pil_sync",
        return_value={"text": "X", "lines": []},
    ):
      answer = assistant.reply(f"/documents read {pdf}")
    self.assertIn("OCR confidence", answer)
    self.assertIn("Warning", answer)
    self.assertIn("partially recognized", answer)
    self.assertIn("page(s) 1", answer)

  def test_document_commands_enforce_the_sandbox(self):
    outside = Path(self.tmp.name) / "outside.md"
    outside.write_text("# Outside", encoding="utf-8")
    assistant, _client = self.make_assistant()
    answer = assistant.reply(f"/documents read {outside}")
    self.assertIn("couldn't read", answer)


if __name__ == "__main__":
  unittest.main()
