"""Local personal knowledge base, topic graph and spaced learning.

Feature set 4.18 keeps knowledge as plain SQLite text. Adding an item
automatically assigns a category and topics. Topics connect related items in a
small graph; flashcards and review state turn the same items into a lightweight
study system. No embedding model, vector database or external service is used.
"""

from __future__ import annotations

import logging
import math
import re
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

log = logging.getLogger(__name__)

CATEGORIES = (
    "Programming",
    "Science",
    "History",
    "Productivity",
    "Business",
    "Health",
    "Personal",
    "General",
)
CATEGORY_WORDS = {
    "Programming": {
        "python",
        "code",
        "programming",
        "function",
        "class",
        "api",
        "database",
        "algorithm",
        "javascript",
        "typescript",
        "bug",
        "software",
        "git",
        "sql",
        "html",
        "css",
        "computer",
    },
    "Science": {
        "science",
        "physics",
        "chemistry",
        "biology",
        "math",
        "mathematics",
        "research",
        "experiment",
        "theory",
        "energy",
        "atom",
        "cell",
        "space",
    },
    "History": {
        "history",
        "century",
        "war",
        "president",
        "ancient",
        "empire",
        "revolution",
        "historical",
        "treaty",
    },
    "Productivity": {
        "task",
        "goal",
        "habit",
        "schedule",
        "plan",
        "focus",
        "time",
        "deadline",
        "priority",
        "workflow",
        "review",
    },
    "Business": {
        "business",
        "market",
        "customer",
        "product",
        "finance",
        "money",
        "startup",
        "sales",
        "revenue",
        "company",
        "management",
    },
    "Health": {
        "health",
        "fitness",
        "exercise",
        "sleep",
        "diet",
        "medical",
        "doctor",
        "nutrition",
        "mental",
    },
    "Personal": {
        "personal",
        "family",
        "home",
        "friend",
        "travel",
        "hobby",
        "life",
        "birthday",
    },
}
STOPWORDS = {
    "about",
    "after",
    "again",
    "also",
    "and",
    "are",
    "because",
    "before",
    "being",
    "between",
    "could",
    "does",
    "doing",
    "for",
    "from",
    "has",
    "have",
    "into",
    "its",
    "just",
    "like",
    "make",
    "many",
    "more",
    "much",
    "need",
    "only",
    "other",
    "please",
    "really",
    "should",
    "some",
    "than",
    "that",
    "the",
    "their",
    "them",
    "then",
    "there",
    "these",
    "they",
    "thing",
    "this",
    "those",
    "very",
    "want",
    "was",
    "were",
    "what",
    "when",
    "where",
    "which",
    "while",
    "will",
    "with",
    "would",
    "your",
}
WORD = re.compile(r"[a-z0-9][a-z0-9'-]+")
SENTENCE = re.compile(r"(?<=[.!?])\s+|\n+")
BULLET = re.compile(r"^\s*(?:[-*]|\d+[.)])\s+(.+)$")
_STATEMENT = re.compile(
    r"^\s*(?P<subject>[A-Za-z][^.!?]{2,80}?)\s+"
    r"(?:is|are|was|were|means|refers to)\s+(?P<answer>.+?)[.!?]?\s*$",
    re.I,
)
REVIEW_INTERVALS = (1, 3, 7, 14, 30, 60, 120)
MAX_TOPICS = 8
MAX_CARDS = 20

SCHEMA = """
CREATE TABLE IF NOT EXISTS knowledge_items (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  title TEXT NOT NULL,
  content TEXT NOT NULL,
  category TEXT NOT NULL,
  topics TEXT NOT NULL DEFAULT '',
  source TEXT NOT NULL DEFAULT 'manual',
  source_key TEXT UNIQUE,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_knowledge_category
  ON knowledge_items (category);
CREATE TABLE IF NOT EXISTS knowledge_links (
  source_id INTEGER NOT NULL,
  target_id INTEGER NOT NULL,
  topic TEXT NOT NULL,
  weight INTEGER NOT NULL DEFAULT 1,
  PRIMARY KEY (source_id, target_id),
  FOREIGN KEY (source_id) REFERENCES knowledge_items (id) ON DELETE CASCADE,
  FOREIGN KEY (target_id) REFERENCES knowledge_items (id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS flashcards (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  item_id INTEGER NOT NULL,
  question TEXT NOT NULL,
  answer TEXT NOT NULL,
  created_at TEXT NOT NULL,
  due_at TEXT NOT NULL,
  interval_days INTEGER NOT NULL DEFAULT 1,
  ease REAL NOT NULL DEFAULT 2.5,
  reviews INTEGER NOT NULL DEFAULT 0,
  correct INTEGER NOT NULL DEFAULT 0,
  streak INTEGER NOT NULL DEFAULT 0,
  last_review TEXT,
  mastery REAL NOT NULL DEFAULT 0,
  UNIQUE (item_id, question),
  FOREIGN KEY (item_id) REFERENCES knowledge_items (id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_flashcards_due ON flashcards (due_at);
"""

FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_fts USING fts5(
  title,
  content,
  topics,
  content='knowledge_items',
  content_rowid='id'
);
CREATE TRIGGER IF NOT EXISTS knowledge_ai AFTER INSERT ON knowledge_items BEGIN
  INSERT INTO knowledge_fts(rowid, title, content, topics)
  VALUES (new.id, new.title, new.content, new.topics);
END;
CREATE TRIGGER IF NOT EXISTS knowledge_ad AFTER DELETE ON knowledge_items BEGIN
  INSERT INTO knowledge_fts(knowledge_fts, rowid, title, content, topics)
  VALUES ('delete', old.id, old.title, old.content, old.topics);
END;
CREATE TRIGGER IF NOT EXISTS knowledge_au AFTER UPDATE ON knowledge_items BEGIN
  INSERT INTO knowledge_fts(knowledge_fts, rowid, title, content, topics)
  VALUES ('delete', old.id, old.title, old.content, old.topics);
  INSERT INTO knowledge_fts(rowid, title, content, topics)
  VALUES (new.id, new.title, new.content, new.topics);
END;
"""

ITEM_COLUMNS = (
    "id, title, content, category, topics, source, created_at, updated_at"
)
CARD_COLUMNS = (
    "id, item_id, question, answer, created_at, due_at, interval_days,"
    " ease, reviews, correct, streak, last_review, mastery"
)


class KnowledgeError(Exception):
  """A knowledge item, card or review could not be read or changed."""


@dataclass(frozen=True)
class KnowledgeItem:
  id: int
  title: str
  content: str
  category: str
  topics: str
  source: str
  created_at: str
  updated_at: str

  @property
  def topic_list(self):
    return [topic for topic in self.topics.split(",") if topic]


@dataclass(frozen=True)
class Flashcard:
  id: int
  item_id: int
  question: str
  answer: str
  created_at: str
  due_at: str
  interval_days: int
  ease: float
  reviews: int
  correct: int
  streak: int
  last_review: str
  mastery: float


def _now():
  return datetime.now().isoformat(timespec="seconds")


def _future(days):
  return (datetime.now() + timedelta(days=max(0, int(days)))).isoformat(
      timespec="seconds"
  )


def _clean(text):
  return " ".join(str(text or "").split()).strip()


def _truncate(text, limit):
  value = _clean(text)
  if len(value) <= limit:
    return value
  return value[: max(0, limit - 3)].rstrip() + "..."


def _title_from(content):
  first = str(content or "").strip().splitlines()
  return _truncate(first[0] if first else "", 100)


def _words(text):
  return [
      word
      for word in WORD.findall(str(text or "").lower())
      if len(word) > 2 and word not in STOPWORDS
  ]


def classify(text, title=""):
  """Return an automatic category and up to eight lowercase topics."""
  content = str(text or "")
  words = _words(content)
  scores = {}
  for category, vocabulary in CATEGORY_WORDS.items():
    scores[category] = sum(1 for word in words if word in vocabulary)
  best = max(scores, key=lambda name: scores[name], default="General")
  if not scores.get(best):
    best = "General"
  counts = {}
  for word in _words(f"{title} {content}"):
    counts[word] = counts.get(word, 0) + 1
  topics = [
      word
      for word, _count in sorted(
          counts.items(), key=lambda item: (-item[1], item[0])
      )
      if word not in {topic.lower() for topic in CATEGORIES}
  ][:MAX_TOPICS]
  return best, topics


def _sentence_summary(text, limit=5):
  sentences = [
      _clean(sentence)
      for sentence in SENTENCE.split(str(text or ""))
      if len(_clean(sentence)) >= 25
  ]
  if not sentences:
    return _truncate(text, 500)
  counts = {}
  for word in _words(text):
    counts[word] = counts.get(word, 0) + 1
  scored = []
  for index, sentence in enumerate(sentences):
    score = sum(counts.get(word, 0) for word in _words(sentence))
    score += 2 if index == 0 else 0
    scored.append((score, index, sentence))
  chosen = sorted(
      sorted(scored, reverse=True)[: max(1, limit)],
      key=lambda item: item[1],
  )
  return " ".join(sentence for _score, _index, sentence in chosen)


def format_item(item):
  topics = ", ".join(item.topic_list) or "none"
  return (
      f"[{item.id}] {item.title} ({item.category}; topics: {topics})"
      f"\n  {_truncate(item.content, 240)}"
  )


def format_card(card, item=None):
  due = card.due_at.replace("T", " ")[:16]
  title = f" ({item.title})" if item is not None else ""
  return (
      f"[{card.id}] {card.question}{title}"
      f"\n  Answer: {_truncate(card.answer, 220)}"
      f"\n  Due: {due}; interval {card.interval_days}d;"
      f" mastery {round(card.mastery * 100)}%"
  )


class KnowledgeStore:
  """SQLite storage for items, links and reviewed flashcards."""

  def __init__(self, path):
    self.path = Path(path)
    self._lock = threading.RLock()
    self._fts = False
    self._db = None
    try:
      self.path.parent.mkdir(parents=True, exist_ok=True)
      self._db = sqlite3.connect(
          str(self.path), check_same_thread=False, timeout=10.0
      )
      self._db.row_factory = sqlite3.Row
      self._db.execute("PRAGMA foreign_keys = ON")
      self._db.executescript(SCHEMA)
      try:
        self._db.executescript(FTS_SCHEMA)
        self._fts = True
      except sqlite3.OperationalError as exc:
        log.warning("Knowledge FTS5 unavailable, using scan search: %s", exc)
      self._db.commit()
    except (OSError, sqlite3.Error) as exc:
      raise KnowledgeError(f"could not open {self.path}: {exc}") from exc

  def close(self):
    if self._db is not None:
      with self._lock:
        self._db.close()
      self._db = None

  def _require(self):
    if self._db is None:
      raise KnowledgeError("the knowledge store is closed")
    return self._db

  def add(self, title, content, category, topics, source="manual", source_key=None):
    now = _now()
    try:
      with self._lock:
        cursor = self._db.execute(
            "INSERT INTO knowledge_items"
            " (title, content, category, topics, source, source_key,"
            " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                title,
                content,
                category,
                ",".join(topics),
                source,
                source_key,
                now,
                now,
            ),
        )
        self._db.commit()
        item_id = int(cursor.lastrowid)
    except sqlite3.IntegrityError:
      return None
    except sqlite3.Error as exc:
      raise KnowledgeError(f"could not save the knowledge item: {exc}") from exc
    self.rebuild_links(item_id)
    return self.get(item_id)

  def count(self):
    row = self._require().execute(
        "SELECT COUNT(*) FROM knowledge_items"
    ).fetchone()
    return int(row[0]) if row else 0

  def get(self, item_id):
    db = self._require()
    try:
      row = db.execute(
          f"SELECT {ITEM_COLUMNS} FROM knowledge_items WHERE id = ?",
          (int(item_id),),
      ).fetchone()
    except (sqlite3.Error, TypeError, ValueError):
      return None
    return KnowledgeItem(**dict(row)) if row else None

  def find_source(self, source_key):
    if not source_key:
      return None
    row = self._require().execute(
        f"SELECT {ITEM_COLUMNS} FROM knowledge_items WHERE source_key = ?",
        (source_key,),
    ).fetchone()
    return KnowledgeItem(**dict(row)) if row else None

  def list(self, limit=50, category=""):
    db = self._require()
    try:
      if category:
        rows = db.execute(
            f"SELECT {ITEM_COLUMNS} FROM knowledge_items"
            " WHERE category = ? COLLATE NOCASE"
            " ORDER BY updated_at DESC, id DESC LIMIT ?",
            (category, max(1, int(limit))),
        ).fetchall()
      else:
        rows = db.execute(
            f"SELECT {ITEM_COLUMNS} FROM knowledge_items"
            " ORDER BY updated_at DESC, id DESC LIMIT ?",
            (max(1, int(limit)),),
        ).fetchall()
    except sqlite3.Error as exc:
      raise KnowledgeError(f"could not list knowledge: {exc}") from exc
    return [KnowledgeItem(**dict(row)) for row in rows]

  def search(self, query, limit=10):
    terms = _words(query)[:12]
    if not terms:
      return []
    db = self._require()
    if self._fts:
      expression = " OR ".join(f'"{term}"' for term in terms)
      try:
        rows = db.execute(
            f"SELECT i.{', i.'.join(ITEM_COLUMNS.split(', '))},"
            " bm25(knowledge_fts) AS rank"
            " FROM knowledge_fts"
            " JOIN knowledge_items i ON i.id = knowledge_fts.rowid"
            " WHERE knowledge_fts MATCH ?"
            " ORDER BY rank, i.updated_at DESC LIMIT ?",
            (expression, max(1, int(limit))),
        ).fetchall()
        if rows:
          names = ITEM_COLUMNS.split(", ")
          return [
              KnowledgeItem(**{name: row[name] for name in names})
              for row in rows
          ]
      except sqlite3.OperationalError as exc:
        log.warning("Knowledge FTS query failed, using scan: %s", exc)
    rows = db.execute(
        f"SELECT {ITEM_COLUMNS} FROM knowledge_items"
        " ORDER BY updated_at DESC, id DESC"
    ).fetchall()
    scored = []
    for row in rows:
      item = KnowledgeItem(**dict(row))
      text = f"{item.title} {item.content} {item.topics}".lower()
      score = sum(text.count(term) for term in terms)
      if score:
        scored.append((score, item))
    scored.sort(key=lambda pair: (pair[0], pair[1].updated_at), reverse=True)
    return [item for _score, item in scored[: max(1, int(limit))]]

  def delete(self, item_id):
    item = self.get(item_id)
    if item is None:
      return None
    db = self._require()
    try:
      with self._lock:
        db.execute("DELETE FROM knowledge_items WHERE id = ?", (int(item_id),))
        db.commit()
    except sqlite3.Error as exc:
      raise KnowledgeError(f"could not delete the knowledge item: {exc}") from exc
    return item

  def rebuild_links(self, item_id):
    """Refresh only the links touching one item, keeping adds bounded."""
    db = self._require()
    item = self.get(item_id)
    if item is None:
      return
    rows = db.execute(
        f"SELECT {ITEM_COLUMNS} FROM knowledge_items WHERE id != ?",
        (item.id,),
    ).fetchall()
    others = [KnowledgeItem(**dict(row)) for row in rows]
    item_topics = set(item.topic_list)
    try:
      with self._lock:
        db.execute(
            "DELETE FROM knowledge_links WHERE source_id = ? OR target_id = ?",
            (item.id, item.id),
        )
        for other in others:
          shared = sorted(item_topics & set(other.topic_list))
          if not shared:
            continue
          source, target = (
              (item.id, other.id)
              if item.id < other.id
              else (other.id, item.id)
          )
          db.execute(
              "INSERT OR IGNORE INTO knowledge_links"
              " (source_id, target_id, topic, weight) VALUES (?, ?, ?, ?)",
              (source, target, ",".join(shared[:4]), len(shared)),
          )
        db.commit()
    except sqlite3.Error as exc:
      raise KnowledgeError(f"could not rebuild topic links: {exc}") from exc

  def topics(self, limit=30):
    rows = self._require().execute(
        "SELECT topics FROM knowledge_items"
    ).fetchall()
    counts = {}
    for row in rows:
      for topic in str(row["topics"] or "").split(","):
        topic = topic.strip()
        if topic:
          counts[topic] = counts.get(topic, 0) + 1
    return sorted(counts.items(), key=lambda pair: (-pair[1], pair[0]))[:limit]

  def graph(self, limit=40):
    items = self.list(limit=limit)
    ids = {item.id for item in items}
    rows = self._require().execute(
        "SELECT source_id, target_id, topic, weight FROM knowledge_links"
    ).fetchall()
    edges = [
        dict(row)
        for row in rows
        if row["source_id"] in ids and row["target_id"] in ids
    ]
    return items, edges

  def ensure_cards(self, item):
    db = self._require()
    row = db.execute(
        "SELECT COUNT(*) AS count FROM flashcards WHERE item_id = ?",
        (item.id,),
    ).fetchone()
    if row and int(row["count"]):
      return
    self.generate_cards(item)

  def generate_cards(self, item):
    db = self._require()
    sentences = [
        _clean(sentence)
        for sentence in SENTENCE.split(item.content)
        if len(_clean(sentence)) >= 8
    ]
    cards = []
    for sentence in sentences:
      match = _STATEMENT.match(sentence)
      if match:
        subject = _truncate(match.group("subject"), 100)
        answer = _truncate(match.group("answer"), 500)
        cards.append((f"What is {subject}?", answer))
      if len(cards) >= 2:
        break
    if not cards:
      cards.append(
          (
              f"What do you know about {item.title}?",
              _truncate(item.content, 800),
          )
      )
    bullets = [
        _truncate(match.group(1), 300)
        for line in item.content.splitlines()
        if (match := BULLET.match(line))
    ]
    for bullet in bullets[:2]:
      cards.append((f"What does {item.title} mention?", bullet))
    unique_cards = []
    seen_questions = set()
    for question, answer in cards:
      if question in seen_questions:
        continue
      seen_questions.add(question)
      unique_cards.append((question, answer))
    try:
      with self._lock:
        db.execute("DELETE FROM flashcards WHERE item_id = ?", (item.id,))
        for question, answer in unique_cards[:MAX_CARDS]:
          db.execute(
              "INSERT INTO flashcards"
              " (item_id, question, answer, created_at, due_at)"
              " VALUES (?, ?, ?, ?, ?)",
              (item.id, question, answer, _now(), _now()),
          )
        db.commit()
    except sqlite3.Error as exc:
      raise KnowledgeError(f"could not generate flashcards: {exc}") from exc
    return self.cards(item_id=item.id)

  def cards(self, item_id=None, topic="", limit=100):
    db = self._require()
    if item_id is not None:
      rows = db.execute(
          f"SELECT {CARD_COLUMNS} FROM flashcards WHERE item_id = ?"
          " ORDER BY id",
          (int(item_id),),
      ).fetchall()
    elif topic:
      rows = db.execute(
          f"SELECT c.* FROM flashcards c JOIN knowledge_items i"
          " ON i.id = c.item_id"
          " WHERE i.topics LIKE ? OR i.title LIKE ?"
          " ORDER BY c.due_at, c.id LIMIT ?",
          (f"%{topic.lower()}%", f"%{topic}%", max(1, int(limit))),
      ).fetchall()
    else:
      rows = db.execute(
          f"SELECT {CARD_COLUMNS} FROM flashcards"
          " ORDER BY due_at, id LIMIT ?",
          (max(1, int(limit)),),
      ).fetchall()
    return [Flashcard(**dict(row)) for row in rows]

  def get_card(self, card_id):
    row = self._require().execute(
        f"SELECT {CARD_COLUMNS} FROM flashcards WHERE id = ?",
        (int(card_id),),
    ).fetchone()
    return Flashcard(**dict(row)) if row else None

  def update_card(self, card, correct):
    reviews = card.reviews + 1
    correct_count = card.correct + (1 if correct else 0)
    streak = card.streak + 1 if correct else 0
    if correct:
      interval = REVIEW_INTERVALS[min(streak - 1, len(REVIEW_INTERVALS) - 1)]
      mastery = min(1.0, card.mastery + 0.12)
      due = _future(interval)
    else:
      interval = 1
      mastery = max(0.0, card.mastery - 0.15)
      due = _now()
    ease = max(1.3, min(3.0, card.ease + (0.05 if correct else -0.1)))
    try:
      with self._lock:
        self._db.execute(
            "UPDATE flashcards SET due_at = ?, interval_days = ?, ease = ?,"
            " reviews = ?, correct = ?, streak = ?, last_review = ?,"
            " mastery = ? WHERE id = ?",
            (
                due,
                interval,
                ease,
                reviews,
                correct_count,
                streak,
                _now(),
                mastery,
                card.id,
            ),
        )
        self._db.commit()
    except sqlite3.Error as exc:
      raise KnowledgeError(f"could not update the review: {exc}") from exc
    return self.get_card(card.id)

  def due_cards(self, limit=20):
    rows = self._require().execute(
        f"SELECT {CARD_COLUMNS} FROM flashcards"
        " WHERE due_at <= ? ORDER BY due_at, id LIMIT ?",
        (_now(), max(1, int(limit))),
    ).fetchall()
    return [Flashcard(**dict(row)) for row in rows]

  def all_cards(self, limit=500):
    rows = self._require().execute(
        f"SELECT {CARD_COLUMNS} FROM flashcards"
        " ORDER BY due_at, id LIMIT ?",
        (max(1, int(limit)),),
    ).fetchall()
    return [Flashcard(**dict(row)) for row in rows]

  def item_for_card(self, card):
    return self.get(card.item_id)


class KnowledgeService:
  """Validation and learning workflows over the local knowledge store."""

  def __init__(self, config, document_service=None):
    self._config = config
    self._documents = document_service
    self._store = KnowledgeStore(config.resolved_db)

  def close(self):
    self._store.close()

  def add(self, content, title="", category="", source="manual", source_key=None):
    content = str(content or "").strip()
    if not content:
      raise KnowledgeError("a knowledge item needs some text")
    if len(content) > self._config.max_text_chars:
      raise KnowledgeError("that knowledge item is too long")
    if self._store.count() >= self._config.max_items:
      raise KnowledgeError("the knowledge base is full")
    title = _clean(title) or _title_from(content)
    auto_category, topics = classify(content, title)
    category = _clean(category).title() if category else auto_category
    if category not in CATEGORIES:
      category = auto_category
    return self._store.add(
        title, content, category, topics, source, source_key
    )

  def get(self, item_id):
    return self._store.get(item_id)

  def list(self, limit=None, category=""):
    return self._store.list(limit or self._config.max_items, category)

  def search(self, query, limit=None):
    return self._store.search(query, limit or self._config.search_limit)

  def delete(self, item_id):
    return self._store.delete(item_id)

  def topics(self, limit=30):
    return self._store.topics(limit)

  def graph(self, limit=40):
    return self._store.graph(limit)

  def import_notes(self, note_service):
    if note_service is None:
      raise KnowledgeError("notes are not available")
    added = 0
    skipped = 0
    for note in note_service.list(self._config.max_items):
      key = f"note:{note.id}"
      if self._store.find_source(key) is not None:
        skipped += 1
        continue
      item = self.add(
          note.content,
          title=note.title,
          source="note",
          source_key=key,
      )
      if item is None:
        skipped += 1
      else:
        added += 1
    return {"added": added, "skipped": skipped}

  def flashcards(self, item_id=None):
    if item_id is not None and item_id:
      item = self.get(item_id)
      if item is None:
        raise KnowledgeError("there is no knowledge item with that id")
      self._store.ensure_cards(item)
      return self._store.cards(item_id=item.id)
    for item in self.list(self._config.max_items):
      self._store.ensure_cards(item)
    return self._store.cards()

  def due(self, limit=20):
    return self._store.due_cards(limit)

  def review(self, card_id, correct):
    card = self._store.get_card(card_id)
    if card is None:
      raise KnowledgeError("there is no flashcard with that id")
    return self._store.update_card(card, correct)

  def quiz(self, count=None, topic=""):
    cards = self.flashcards()
    if topic:
      cards = [
          card
          for card in cards
          if topic.lower() in (
              self.get(card.item_id).topics.lower()
              + " "
              + self.get(card.item_id).title.lower()
              if self.get(card.item_id)
              else ""
          )
      ]
    if not cards:
      return []
    count = max(1, min(50, int(count or self._config.quiz_size)))
    answers = []
    for card in cards:
      if card.answer not in answers:
        answers.append(card.answer)
    questions = []
    for index, card in enumerate(cards[:count]):
      distractors = [
          answer for answer in answers if answer != card.answer
      ]
      distractors = sorted(distractors, key=lambda value: len(value))[:3]
      correct_index = index % (len(distractors) + 1)
      choices = list(distractors)
      choices.insert(correct_index, card.answer)
      questions.append(
          {
              "card_id": card.id,
              "question": card.question,
              "choices": choices,
              "answer": card.answer,
              "correct_index": correct_index,
          }
      )
    return questions

  def study_plan(self, days=None):
    days = max(1, min(60, int(days or self._config.plan_days)))
    cards = self.flashcards()
    due = {card.id for card in self.due(500)}
    buckets = []
    for offset in range(days):
      date = (datetime.now() + timedelta(days=offset)).date().isoformat()
      selected = [
          card for card in cards
          if (offset == 0 and card.id in due)
          or (offset > 0 and card.due_at[:10] == date)
      ]
      if offset == 0 and not selected:
        selected = sorted(cards, key=lambda card: card.due_at)[:5]
      buckets.append(
          {
              "date": date,
              "cards": selected[:8],
              "minutes": max(5, math.ceil(len(selected[:8]) * 1.5)),
          }
      )
    return buckets

  def progress(self, topic=""):
    items = self.list(self._config.max_items)
    if topic:
      lowered = topic.lower()
      items = [
          item for item in items
          if lowered in item.title.lower()
          or lowered in item.topics.lower()
      ]
    cards = self._store.all_cards(5000)
    if topic:
      ids = {item.id for item in items}
      cards = [card for card in cards if card.item_id in ids]
    reviews = sum(card.reviews for card in cards)
    correct = sum(card.correct for card in cards)
    mastery = (
        round(sum(card.mastery for card in cards) / len(cards) * 100, 1)
        if cards
        else 0.0
    )
    now = _now()
    return {
        "items": len(items),
        "cards": len(cards),
        "reviews": reviews,
        "correct": correct,
        "accuracy": round(correct / reviews * 100, 1) if reviews else 0.0,
        "mastery": mastery,
        "due": sum(1 for card in cards if card.due_at <= now),
    }

  def summary(self, topic=""):
    items = self.search(topic, limit=self._config.search_limit) if topic else self.list(20)
    if not items:
      return {"topic": topic, "items": [], "summary": "No matching knowledge items."}
    text = "\n".join(f"{item.title}: {item.content}" for item in items)
    topics = []
    for item in items:
      topics.extend(item.topic_list)
    return {
        "topic": topic,
        "items": items,
        "topics": sorted(set(topics))[:12],
        "summary": _sentence_summary(text, 6),
    }

  def cross(self, query, document_service=None):
    query = _clean(query)
    if not query:
      raise KnowledgeError("a query is required")
    knowledge = self.search(query, limit=6)
    evidence = ""
    hits = []
    service = document_service or self._documents
    if service is not None:
      try:
        evidence, hits = service.context(query)
      except Exception as exc:
        log.info("Document context unavailable for cross reasoning: %s", exc)
    topics = sorted({topic for item in knowledge for topic in item.topic_list})
    source_lines = [f"{item.title}: {item.content}" for item in knowledge]
    if hits:
      source_lines.extend(
          f"{hit['title']}: {hit['text']}" for hit in hits[:5]
      )
    return {
        "query": query,
        "knowledge": knowledge,
        "document_hits": hits,
        "evidence": evidence,
        "topics": topics,
        "summary": _sentence_summary("\n".join(source_lines), 7),
    }
