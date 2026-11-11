"""Local email drafts with optional IMAP and SMTP accounts.

Drafts are always stored locally in SQLite and can be edited or reviewed
without a network account. Searching, reading and sending are enabled only
when the corresponding server is configured. The password is never read from
the TOML file: ``password_env`` names an environment variable instead.
"""

from __future__ import annotations

import email as email_lib
import html
import imaplib
import os
import re
import smtplib
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from email import policy
from email.message import EmailMessage as MIMEMessage
from email.parser import BytesParser
from email.utils import formatdate, make_msgid, parseaddr, parsedate_to_datetime
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS email_drafts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  recipient TEXT NOT NULL,
  cc TEXT NOT NULL DEFAULT '',
  subject TEXT NOT NULL DEFAULT '',
  body TEXT NOT NULL DEFAULT '',
  in_reply_to TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'draft',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_email_drafts_created
  ON email_drafts (created_at);
"""

COLUMNS = (
    "id, recipient, cc, subject, body, in_reply_to, status,"
    " created_at, updated_at"
)

_STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "but",
    "by",
    "for",
    "from",
    "has",
    "have",
    "i",
    "in",
    "is",
    "it",
    "its",
    "of",
    "on",
    "or",
    "that",
    "the",
    "this",
    "to",
    "was",
    "we",
    "were",
    "will",
    "with",
    "you",
    "your",
}


class EmailError(Exception):
    """Raised when an email draft or account operation cannot complete."""


@dataclass(frozen=True)
class EmailDraft:
    """One locally stored email draft."""

    id: int
    recipient: str
    cc: str
    subject: str
    body: str
    in_reply_to: str
    status: str
    created_at: str
    updated_at: str

    @property
    def to(self):
        return self.recipient


@dataclass(frozen=True)
class InboxEmail:
    """One message fetched from the configured IMAP account."""

    uid: str
    sender: str
    subject: str
    received_at: str
    body: str
    message_id: str
    references: str


def _now():
    return datetime.now().replace(microsecond=0)


def _iso(value):
    if isinstance(value, datetime):
        return value.isoformat(timespec="seconds")
    return str(value or "")


def _clean_header(value):
    """Remove line breaks so user input cannot inject extra headers."""
    return re.sub(r"[\r\n]+", " ", str(value or "")).strip()


def _clean_body(value):
    return str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()


def _valid_recipient(value):
    text = _clean_header(value)
    if not text:
        return False
    parts = [part.strip() for part in text.split(",") if part.strip()]
    return all("@" in parseaddr(part)[1] for part in parts)


def _snippet(text, limit=240):
    text = " ".join(str(text or "").split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def _strip_html(value):
    text = re.sub(
        r"<(script|style)\b.*?</\1>", " ", value, flags=re.I | re.S
    )
    text = re.sub(r"<[^>]+>", " ", text)
    return html.unescape(text)


def _body_from_message(message):
    """Return readable text from a MIME message, preferring plain text."""
    if message.is_multipart():
        parts = list(message.walk())
        for content_type in ("text/plain", "text/html"):
            for part in parts:
                if part.get_content_type() != content_type:
                    continue
                if part.get_content_disposition() == "attachment":
                    continue
                try:
                    text = part.get_content()
                except (LookupError, UnicodeError, ValueError):
                    continue
                if content_type == "text/html":
                    text = _strip_html(text)
                return _clean_body(text)
        return ""
    try:
        text = message.get_content()
    except (LookupError, UnicodeError, ValueError):
        text = ""
    if message.get_content_type() == "text/html":
        text = _strip_html(text)
    return _clean_body(text)


def _received_at(value):
    if not value:
        return ""
    try:
        parsed = parsedate_to_datetime(str(value))
    except (TypeError, ValueError, OverflowError):
        return str(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone().isoformat(timespec="minutes")


def _inbox_from_raw(raw, uid, max_body_chars):
    message = BytesParser(policy=policy.default).parsebytes(raw)
    sender = parseaddr(str(message.get("From", "")))
    sender_text = (
        f"{sender[0]} <{sender[1]}>" if sender[0] and sender[1] else sender[1]
    )
    body = _body_from_message(message)[:max_body_chars]
    return InboxEmail(
        uid=str(uid),
        sender=sender_text or str(message.get("From", "")),
        subject=_clean_header(message.get("Subject", "")),
        received_at=_received_at(message.get("Date", "")),
        body=body,
        message_id=_clean_header(message.get("Message-ID", "")),
        references=_clean_header(message.get("References", "")),
    )


def summarize_text(text, max_sentences=3, max_chars=700):
    """Build a short extractive summary without a model call."""
    body = " ".join(str(text or "").split())
    if not body:
        return "No readable text."
    sentences = [
        sentence.strip()
        for sentence in re.split(r"(?<=[.!?])\s+", body)
        if sentence.strip()
    ]
    if len(sentences) <= max_sentences:
        result = " ".join(sentences)
        return result[:max_chars].rstrip()

    words = re.findall(r"[a-zA-Z0-9']+", body.lower())
    counts = {}
    for word in words:
        if word not in _STOPWORDS:
            counts[word] = counts.get(word, 0) + 1
    scored = []
    for index, sentence in enumerate(sentences):
        terms = [
            word
            for word in re.findall(r"[a-zA-Z0-9']+", sentence.lower())
            if word not in _STOPWORDS
        ]
        score = sum(counts.get(term, 0) for term in terms)
        scored.append((score, index, sentence))
    chosen = sorted(scored, key=lambda item: (-item[0], item[1]))[:max_sentences]
    chosen.sort(key=lambda item: item[1])
    result = " ".join(sentence for _score, _index, sentence in chosen)
    return result[:max_chars].rstrip()


def parse_email_input(text):
    """Parse ``recipient [about subject]`` or ``to | subject | body``."""
    body = str(text or "").strip()
    if not body:
        return {"recipient": "", "cc": "", "subject": "", "body": ""}
    if "|" in body:
        parts = [part.strip() for part in body.split("|")]
        recipient = re.sub(r"^to\s+", "", parts[0], flags=re.I).strip()
        subject = parts[1] if len(parts) > 1 else ""
        content = " | ".join(parts[2:]).strip() if len(parts) > 2 else ""
        return {
            "recipient": recipient,
            "cc": "",
            "subject": subject,
            "body": content,
        }

    body = re.sub(r"^to\s+", "", body, flags=re.I).strip()
    match = re.match(
        r"(?P<recipient>[^\s,;]+@[^\s,;]+)"
        r"(?:\s+(?:about|regarding|re)\s+(?P<subject>.+))?$",
        body,
        re.I,
    )
    if match:
        return {
            "recipient": match.group("recipient").strip(),
            "cc": "",
            "subject": _clean_header(match.group("subject") or ""),
            "body": "",
        }
    return {"recipient": "", "cc": "", "subject": "", "body": body}


def format_draft(draft, detail=False):
    """Render a draft for a command or spoken reply."""
    subject = draft.subject or "(no subject)"
    text = (
        f"[{draft.id}] {draft.status} to {draft.recipient}"
        f" — {subject}"
    )
    if draft.cc:
        text += f" (cc {draft.cc})"
    if detail:
        text += f"\nBody: {draft.body or '(empty)'}"
    return text


def format_message(message, detail=False):
    """Render one inbox message."""
    received = f" — {message.received_at}" if message.received_at else ""
    text = (
        f"[UID {message.uid}] {message.sender or 'unknown sender'}"
        f" — {message.subject or '(no subject)'}{received}"
    )
    if detail:
        text += f"\n{message.body or '(empty message)'}"
    else:
        snippet = _snippet(message.body)
        if snippet:
            text += f"\n  {snippet}"
    return text


def format_summary(message, summary):
    return (
        f"Summary of UID {message.uid} — "
        f"{message.subject or '(no subject)'}\n{summary}"
    )


class EmailStore:
    """SQLite persistence for local email drafts."""

    def __init__(self, path):
        self.path = Path(path)
        self._lock = threading.RLock()
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._db = sqlite3.connect(
                str(self.path), check_same_thread=False, timeout=10.0
            )
            self._db.executescript(SCHEMA)
            self._db.commit()
        except (OSError, sqlite3.Error) as exc:
            raise EmailError(f"Could not open {self.path}: {exc}") from exc

    def add(
        self,
        recipient,
        subject="",
        body="",
        cc="",
        in_reply_to="",
    ):
        recipient = _clean_header(recipient)
        if not _valid_recipient(recipient):
            raise EmailError("a draft needs a valid recipient email address")
        subject = _clean_header(subject)
        if len(subject) > 300:
            raise EmailError("that subject is too long")
        body = _clean_body(body)
        now = _iso(_now())
        try:
            with self._lock:
                cursor = self._db.execute(
                    "INSERT INTO email_drafts"
                    " (recipient, cc, subject, body, in_reply_to, status,"
                    " created_at, updated_at)"
                    " VALUES (?, ?, ?, ?, ?, 'draft', ?, ?)",
                    (
                        recipient,
                        _clean_header(cc),
                        subject,
                        body,
                        _clean_header(in_reply_to),
                        now,
                        now,
                    ),
                )
                self._db.commit()
                draft_id = int(cursor.lastrowid)
        except sqlite3.Error as exc:
            raise EmailError(f"Could not save the draft: {exc}") from exc
        draft = self.get(draft_id)
        if draft is None:
            raise EmailError("the draft disappeared after saving")
        return draft

    def get(self, draft_id):
        try:
            with self._lock:
                row = self._db.execute(
                    f"SELECT {COLUMNS} FROM email_drafts WHERE id = ?",
                    (int(draft_id),),
                ).fetchone()
        except (sqlite3.Error, TypeError, ValueError):
            return None
        return self._draft(row) if row else None

    def list(self, limit=20, status=None):
        query = f"SELECT {COLUMNS} FROM email_drafts"
        arguments = []
        if status in {"draft", "sent"}:
            query += " WHERE status = ?"
            arguments.append(status)
        query += " ORDER BY updated_at DESC, id DESC LIMIT ?"
        arguments.append(max(1, int(limit)))
        try:
            with self._lock:
                rows = self._db.execute(query, arguments).fetchall()
        except sqlite3.Error as exc:
            raise EmailError(f"Could not list drafts: {exc}") from exc
        return [self._draft(row) for row in rows]

    def update(self, draft_id, **fields):
        allowed = {"recipient", "cc", "subject", "body", "in_reply_to"}
        unknown = sorted(set(fields) - allowed)
        if unknown:
            raise EmailError("cannot update " + ", ".join(unknown))
        draft = self.get(draft_id)
        if draft is None:
            raise EmailError("there is no draft with that id")
        if draft.status == "sent":
            raise EmailError("a sent draft cannot be changed")
        values = {}
        for key, value in fields.items():
            if key in {"recipient", "cc", "subject", "in_reply_to"}:
                value = _clean_header(value)
            else:
                value = _clean_body(value)
            if key == "recipient" and not _valid_recipient(value):
                raise EmailError("a draft needs a valid recipient email address")
            if key == "subject" and len(value) > 300:
                raise EmailError("that subject is too long")
            values[key] = value
        if not values:
            return draft
        values["updated_at"] = _iso(_now())
        assignments = ", ".join(f"{key} = ?" for key in values)
        try:
            with self._lock:
                self._db.execute(
                    f"UPDATE email_drafts SET {assignments} WHERE id = ?",
                    list(values.values()) + [int(draft_id)],
                )
                self._db.commit()
        except sqlite3.Error as exc:
            raise EmailError(f"Could not update the draft: {exc}") from exc
        return self.get(draft_id)

    def mark_sent(self, draft_id):
        try:
            with self._lock:
                self._db.execute(
                    "UPDATE email_drafts SET status = 'sent', updated_at = ?"
                    " WHERE id = ?",
                    (_iso(_now()), int(draft_id)),
                )
                self._db.commit()
        except sqlite3.Error as exc:
            raise EmailError(f"Could not mark the draft sent: {exc}") from exc
        return self.get(draft_id)

    def delete(self, draft_id):
        draft = self.get(draft_id)
        if draft is None:
            raise EmailError("there is no draft with that id")
        try:
            with self._lock:
                self._db.execute(
                    "DELETE FROM email_drafts WHERE id = ?", (int(draft_id),)
                )
                self._db.commit()
        except sqlite3.Error as exc:
            raise EmailError(f"Could not delete the draft: {exc}") from exc
        return draft

    def count(self):
        try:
            with self._lock:
                row = self._db.execute(
                    "SELECT COUNT(*) FROM email_drafts"
                ).fetchone()
        except sqlite3.Error as exc:
            raise EmailError(f"Could not count drafts: {exc}") from exc
        return int(row[0]) if row else 0

    def close(self):
        try:
            with self._lock:
                self._db.close()
        except sqlite3.Error:
            pass

    @staticmethod
    def _draft(row):
        keys = tuple(column.strip() for column in COLUMNS.split(","))
        return EmailDraft(**dict(zip(keys, row)))


class EmailService:
    """Local drafts plus optional IMAP search and SMTP sending."""

    def __init__(self, config, imap_factory=None, smtp_factory=None):
        self._config = config
        self._store = EmailStore(config.resolved_db)
        self._imap_factory = imap_factory
        self._smtp_factory = smtp_factory

    @property
    def can_search(self):
        return bool(
            self._config.imap_host
            and self._config.username
            and self._config.password_env
        )

    @property
    def can_send(self):
        return bool(
            self._config.smtp_host
            and self._config.username
            and self._sender_address()
            and self._config.password_env
        )

    def add(self, recipient, subject="", body="", cc="", in_reply_to=""):
        if self._store.count() >= self._config.max_drafts:
            raise EmailError(
                f"the draft limit of {self._config.max_drafts} is reached"
            )
        return self._store.add(
            recipient,
            subject=subject,
            body=body,
            cc=cc,
            in_reply_to=in_reply_to,
        )

    def get(self, draft_id):
        return self._store.get(draft_id)

    def list(self, limit=20, status=None):
        return self._store.list(limit=limit, status=status)

    def update(self, draft_id, **fields):
        return self._store.update(draft_id, **fields)

    def delete(self, draft_id):
        return self._store.delete(draft_id)

    def send(self, draft_id):
        draft = self.get(draft_id)
        if draft is None:
            raise EmailError("there is no draft with that id")
        if draft.status == "sent":
            raise EmailError("that draft was already sent")
        self._require_send_account()
        message = self._compose(draft)
        client = self._open_smtp()
        try:
            client.ehlo()
            if self._config.smtp_starttls and not self._config.smtp_ssl:
                client.starttls()
                client.ehlo()
            client.login(self._config.username, self._password())
            client.send_message(message)
        except EmailError:
            raise
        except Exception as exc:
            raise EmailError(f"could not send the email: {exc}") from exc
        finally:
            try:
                client.quit()
            except Exception:
                pass
        return self._store.mark_sent(draft.id)

    def search(self, query, limit=None):
        self._require_search_account()
        limit = max(1, int(limit or self._config.search_limit))
        client = self._open_imap()
        try:
            criteria = ("ALL",)
            if query:
                escaped = str(query).replace("\\", "\\\\").replace('"', '\\"')
                criteria = ("TEXT", f'"{escaped}"')
            status, data = client.uid("SEARCH", None, *criteria)
            if status != "OK":
                raise EmailError("the mail server rejected the search")
            uids = []
            for chunk in data or []:
                if isinstance(chunk, bytes):
                    uids.extend(chunk.decode("ascii", "ignore").split())
                elif chunk:
                    uids.extend(str(chunk).split())
            result = []
            for uid in uids[-limit:]:
                result.append(self._fetch(client, uid))
            return result
        except EmailError:
            raise
        except Exception as exc:
            raise EmailError(f"could not search the mailbox: {exc}") from exc
        finally:
            self._close_imap(client)

    def read(self, uid):
        self._require_search_account()
        client = self._open_imap()
        try:
            return self._fetch(client, uid)
        except EmailError:
            raise
        except Exception as exc:
            raise EmailError(f"could not read that message: {exc}") from exc
        finally:
            self._close_imap(client)

    def summarize(self, uid):
        message = self.read(uid)
        return message, summarize_text(message.body)

    def reply(self, uid):
        message = self.read(uid)
        subject = message.subject.strip()
        if not subject.lower().startswith("re:"):
            subject = f"Re: {subject}" if subject else "Re: your message"
        quoted = "\n".join(f"> {line}" for line in message.body.splitlines())
        body = (
            f"\n\nOn {message.received_at or 'an earlier date'},"
            f" {message.sender} wrote:\n{quoted}"
        ).strip()
        draft = self.add(
            message.sender,
            subject=subject,
            body=body,
            in_reply_to=message.message_id,
        )
        return message, draft

    def close(self):
        self._store.close()

    def _sender_address(self):
        return _clean_header(
            self._config.from_address or self._config.username
        )

    def _password(self):
        name = self._config.password_env
        value = os.environ.get(name, "")
        if not value:
            raise EmailError(
                f"set the {name} environment variable to use the mail account"
            )
        return value

    def _require_search_account(self):
        if not self.can_search:
            raise EmailError(
                "no IMAP account is configured; set [email] imap_host and"
                " username"
            )
        self._password()

    def _require_send_account(self):
        if not self.can_send:
            raise EmailError(
                "no SMTP account is configured; set [email] smtp_host,"
                " username and from_address"
            )
        self._password()

    def _open_imap(self):
        factory = self._imap_factory
        if factory is None:
            factory = (
                imaplib.IMAP4_SSL
                if self._config.imap_ssl
                else imaplib.IMAP4
            )
        client = factory(self._config.imap_host, self._config.imap_port)
        try:
            client.login(self._config.username, self._password())
            client.select(self._config.mailbox, readonly=True)
        except Exception as exc:
            try:
                client.logout()
            except Exception:
                pass
            raise EmailError(f"could not connect to the mailbox: {exc}") from exc
        return client

    def _open_smtp(self):
        factory = self._smtp_factory
        if factory is None:
            factory = (
                smtplib.SMTP_SSL
                if self._config.smtp_ssl
                else smtplib.SMTP
            )
        return factory(
            self._config.smtp_host,
            self._config.smtp_port,
            timeout=self._config.timeout_s,
        )

    def _close_imap(self, client):
        try:
            client.logout()
        except Exception:
            pass

    def _fetch(self, client, uid):
        status, payload = client.uid("FETCH", str(uid), "(RFC822)")
        if status != "OK":
            raise EmailError("the mail server could not read that message")
        raw = b""
        for part in payload or []:
            if isinstance(part, tuple) and len(part) > 1:
                raw += part[1]
        if not raw:
            raise EmailError("there is no message with that id")
        return _inbox_from_raw(
            raw, uid, self._config.max_body_chars
        )

    def _compose(self, draft):
        message = MIMEMessage()
        message["From"] = self._sender_address()
        message["To"] = draft.recipient
        if draft.cc:
            message["Cc"] = draft.cc
        message["Subject"] = draft.subject
        message["Date"] = formatdate(localtime=True)
        message["Message-ID"] = make_msgid()
        if draft.in_reply_to:
            message["In-Reply-To"] = draft.in_reply_to
        message.set_content(draft.body)
        return message
