"""Validated email tools backed by the shared local mail service."""

from __future__ import annotations

from core.mail import (
    EmailError,
    format_draft,
    format_message,
    format_summary,
    parse_email_input,
)
from tools.base import ToolError, ToolResult


def _service(provider):
  service = provider() if callable(provider) else provider
  if service is None:
    raise ToolError("Email is disabled.")
  return service


def _fail(exc):
  raise ToolError(str(exc)) from exc


def _draft_data(draft):
  return {
      "id": draft.id,
      "to": draft.recipient,
      "cc": draft.cc,
      "subject": draft.subject,
      "body": draft.body,
      "status": draft.status,
      "in_reply_to": draft.in_reply_to,
  }


def _message_data(message):
  return {
      "uid": message.uid,
      "from": message.sender,
      "subject": message.subject,
      "received_at": message.received_at,
      "body": message.body,
      "message_id": message.message_id,
  }


def draft_email(provider, recipient, cc="", subject="", body=""):
  service = _service(provider)
  try:
    draft = service.add(
        recipient, subject=subject, body=body, cc=cc
    )
  except EmailError as exc:
    _fail(exc)
  return ToolResult(
      True,
      f"Draft #{draft.id} saved: {format_draft(draft)}.",
      _draft_data(draft),
  )


def list_drafts(provider, limit=20):
  service = _service(provider)
  try:
    drafts = service.list(limit=limit)
  except EmailError as exc:
    _fail(exc)
  if not drafts:
    return ToolResult(True, "No email drafts.", {"count": 0, "drafts": []})
  return ToolResult(
      True,
      "Email drafts:\n" + "\n".join(format_draft(draft) for draft in drafts),
      {"count": len(drafts), "drafts": [_draft_data(draft) for draft in drafts]},
  )


def show_draft(provider, id):
  service = _service(provider)
  try:
    draft = service.get(id)
  except EmailError as exc:
    _fail(exc)
  if draft is None:
    raise ToolError("there is no draft with that id.")
  return ToolResult(
      True,
      format_draft(draft, detail=True),
      _draft_data(draft),
  )


def update_draft(
    provider, id, recipient="", cc="", subject="", body=""
):
  service = _service(provider)
  fields = {}
  if recipient:
    fields["recipient"] = recipient
  if cc:
    fields["cc"] = cc
  if subject:
    fields["subject"] = subject
  if body:
    fields["body"] = body
  if not fields:
    raise ToolError("nothing to update.")
  try:
    draft = service.update(id, **fields)
  except EmailError as exc:
    _fail(exc)
  return ToolResult(
      True,
      f"Draft #{draft.id} updated: {format_draft(draft)}.",
      _draft_data(draft),
  )


def delete_draft(provider, id):
  service = _service(provider)
  try:
    draft = service.delete(id)
  except EmailError as exc:
    _fail(exc)
  return ToolResult(
      True,
      f"Draft #{draft.id} removed.",
      _draft_data(draft),
  )


def send_draft(provider, id):
  service = _service(provider)
  try:
    draft = service.send(id)
  except EmailError as exc:
    _fail(exc)
  return ToolResult(
      True,
      f"Email draft #{draft.id} sent to {draft.recipient}.",
      _draft_data(draft),
  )


def search_inbox(provider, query="", limit=0):
  service = _service(provider)
  try:
    messages = service.search(query, limit=limit or None)
  except EmailError as exc:
    _fail(exc)
  if not messages:
    return ToolResult(True, "No matching email.", {"count": 0, "messages": []})
  return ToolResult(
      True,
      "Email search results:\n"
      + "\n".join(format_message(message) for message in messages),
      {
          "count": len(messages),
          "messages": [_message_data(message) for message in messages],
      },
  )


def read_message(provider, uid):
  service = _service(provider)
  try:
    message = service.read(uid)
  except EmailError as exc:
    _fail(exc)
  return ToolResult(
      True,
      format_message(message, detail=True),
      _message_data(message),
  )


def summarize_message(provider, uid):
  service = _service(provider)
  try:
    message, summary = service.summarize(uid)
  except EmailError as exc:
    _fail(exc)
  return ToolResult(
      True,
      format_summary(message, summary),
      {**_message_data(message), "summary": summary},
  )


def reply_message(provider, uid):
  service = _service(provider)
  try:
    message, draft = service.reply(uid)
  except EmailError as exc:
    _fail(exc)
  return ToolResult(
      True,
      f"Reply draft #{draft.id} saved for {message.sender}:"
      f" {format_draft(draft)}.",
      {**_draft_data(draft), "reply_to": message.uid},
  )


def draft_from_text(provider, text):
  """Convenience for callers that already have a natural draft body."""
  parsed = parse_email_input(text)
  return draft_email(
      provider,
      parsed["recipient"],
      parsed["cc"],
      parsed["subject"],
      parsed["body"],
  )
