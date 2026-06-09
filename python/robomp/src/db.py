"""SQLite-backed durable event queue + bot state."""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from typing import Any, Literal

EventState = Literal["queued", "running", "done", "failed", "skipped"]
INACTIVE_EVENT_STATES: tuple[EventState, ...] = ("done", "failed", "skipped")

IssueState = Literal[
    "new",
    "reproducing",
    "fixing",
    "reviewing",
    "opened",
    "merged",
    "closed",
    "abandoned",
]

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS events (
  delivery_id   TEXT PRIMARY KEY,
  event_type    TEXT NOT NULL,
  repo          TEXT,
  issue_key     TEXT,
  payload_json  TEXT NOT NULL,
  received_at   TEXT NOT NULL,
  state         TEXT NOT NULL
    CHECK (state IN ('queued','running','done','failed','skipped')),
  attempts      INTEGER NOT NULL DEFAULT 0,
  last_error    TEXT,
  started_at    TEXT,
  finished_at   TEXT,
  available_at  TEXT,
  model         TEXT,
  task          TEXT,
  route_reason  TEXT,
  route_version INTEGER NOT NULL DEFAULT 1,
  outcome       TEXT
);

CREATE INDEX IF NOT EXISTS events_state_received
  ON events(state, received_at);


CREATE INDEX IF NOT EXISTS events_issue_state
  ON events(issue_key, state);

CREATE TABLE IF NOT EXISTS issues (
  key            TEXT PRIMARY KEY,
  repo           TEXT NOT NULL,
  number         INTEGER NOT NULL,
  branch         TEXT,
  session_dir    TEXT,
  pr_number      INTEGER,
  state          TEXT NOT NULL,
  classification TEXT,         -- bug|enhancement|question|proposal|documentation|invalid|duplicate
  updated_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tool_calls (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  issue_key     TEXT NOT NULL,
  tool          TEXT NOT NULL,
  args_json     TEXT NOT NULL,
  result_json   TEXT,
  error         TEXT,
  ts            TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS tool_calls_issue ON tool_calls(issue_key, ts);

CREATE TABLE IF NOT EXISTS side_effects (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  operation_key TEXT NOT NULL,
  state         TEXT NOT NULL CHECK (state IN ('pending','succeeded','failed')),
  error         TEXT,
  created_at    TEXT NOT NULL,
  updated_at    TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_side_effects_active
  ON side_effects(operation_key)
  WHERE state IN ('pending','succeeded');
CREATE INDEX IF NOT EXISTS idx_side_effects_operation_updated
  ON side_effects(operation_key, updated_at);

CREATE TABLE IF NOT EXISTS pr_review_comments (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  issue_key   TEXT NOT NULL,
  path        TEXT NOT NULL,
  line        INTEGER NOT NULL,
  side        TEXT NOT NULL DEFAULT 'RIGHT',
  start_line  INTEGER,
  start_side  TEXT,
  body        TEXT NOT NULL,
  created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pr_review_comments_key
  ON pr_review_comments(issue_key);

CREATE TABLE IF NOT EXISTS submissions (
  delivery_id   TEXT PRIMARY KEY,
  login         TEXT NOT NULL,
  repo          TEXT,
  ts            TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS submissions_login_ts ON submissions(login, ts);

CREATE TABLE IF NOT EXISTS pending_closures (
  issue_key     TEXT PRIMARY KEY,
  repo          TEXT NOT NULL,
  number        INTEGER NOT NULL,
  comment_id    INTEGER NOT NULL,
  issue_author  TEXT NOT NULL,
  close_at      TEXT NOT NULL,
  state         TEXT NOT NULL CHECK (state IN ('pending','claimed','closed','cancelled')),
  cancel_reason TEXT,
  created_at    TEXT NOT NULL,
  updated_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS pending_closures_state_close_at
  ON pending_closures(state, close_at);


CREATE TABLE IF NOT EXISTS pr_review_lifecycle_events (
  delivery_id TEXT PRIMARY KEY,
  event_type TEXT NOT NULL,
  action TEXT NOT NULL,
  repo TEXT NOT NULL,
  pr_number INTEGER NOT NULL,
  head_sha TEXT,
  actor_login TEXT,
  actor_type TEXT,
  author_association TEXT,
  object_kind TEXT NOT NULL CHECK (object_kind IN ('pull_request','review','review_comment','conversation_comment','review_thread')),
  object_id TEXT,
  parent_id TEXT,
  review_state TEXT,
  path TEXT,
  line INTEGER,
  start_line INTEGER,
  body TEXT,
  body_hash TEXT,
  suggestion_hash TEXT,
  thread_id TEXT,
  thread_resolved INTEGER,
  merged INTEGER,
  created_at TEXT NOT NULL,
  observed_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pr_lifecycle_repo_pr_time ON pr_review_lifecycle_events(repo, pr_number, created_at);
CREATE INDEX IF NOT EXISTS idx_pr_lifecycle_object ON pr_review_lifecycle_events(repo, object_kind, object_id);
CREATE INDEX IF NOT EXISTS idx_pr_lifecycle_body_hash ON pr_review_lifecycle_events(body_hash);
CREATE INDEX IF NOT EXISTS idx_pr_lifecycle_path_line ON pr_review_lifecycle_events(repo, pr_number, path, line);

CREATE TABLE IF NOT EXISTS pr_review_posted_findings (
  finding_id TEXT PRIMARY KEY,
  issue_key TEXT NOT NULL,
  repo TEXT NOT NULL,
  pr_number INTEGER NOT NULL,
  head_sha TEXT,
  review_id INTEGER,
  comment_id INTEGER,
  path TEXT,
  line INTEGER,
  start_line INTEGER,
  body TEXT NOT NULL,
  body_hash TEXT NOT NULL,
  severity TEXT NOT NULL,
  intent TEXT NOT NULL,
  category TEXT,
  suggestion_replacement TEXT,
  suggestion_hash TEXT,
  status TEXT NOT NULL CHECK (status IN ('posted','resolved','unaddressed','obsolete','suggestion_applied','suggestion_modified','suggestion_ignored','false_positive')),
  status_reason TEXT,
  posted_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pr_posted_repo_pr ON pr_review_posted_findings(repo, pr_number, head_sha);
CREATE INDEX IF NOT EXISTS idx_pr_posted_comment ON pr_review_posted_findings(comment_id);
CREATE INDEX IF NOT EXISTS idx_pr_posted_path_line ON pr_review_posted_findings(repo, pr_number, path, line);
CREATE INDEX IF NOT EXISTS idx_pr_posted_status ON pr_review_posted_findings(status);

CREATE TABLE IF NOT EXISTS pr_review_gap_events (
  gap_id TEXT PRIMARY KEY,
  repo TEXT NOT NULL,
  pr_number INTEGER NOT NULL,
  head_sha TEXT,
  gap_kind TEXT NOT NULL CHECK (gap_kind IN ('missed_by_agent','duplicate_of_agent','human_disagreement','false_positive_signal','suggestion_applied','suggestion_modified','suggestion_ignored','merged_with_unaddressed_required')),
  source_label TEXT NOT NULL CHECK (source_label IN ('human','copilot','agent','unknown')),
  actor_login TEXT,
  event_delivery_id TEXT,
  source_object_kind TEXT NOT NULL,
  source_object_id TEXT,
  agent_review_id INTEGER,
  agent_comment_id INTEGER,
  matched_posted_finding_id TEXT,
  path TEXT,
  line INTEGER,
  start_line INTEGER,
  body TEXT,
  body_hash TEXT,
  severity_hint TEXT NOT NULL,
  thread_id TEXT,
  confidence REAL NOT NULL,
  reason TEXT NOT NULL,
  created_at TEXT NOT NULL,
  observed_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pr_gap_repo_pr ON pr_review_gap_events(repo, pr_number, created_at);
CREATE INDEX IF NOT EXISTS idx_pr_gap_kind ON pr_review_gap_events(gap_kind);
CREATE INDEX IF NOT EXISTS idx_pr_gap_body_hash ON pr_review_gap_events(body_hash);
CREATE INDEX IF NOT EXISTS idx_pr_gap_match ON pr_review_gap_events(matched_posted_finding_id);

CREATE TABLE IF NOT EXISTS pr_review_completed_reviews (
  completed_review_key TEXT PRIMARY KEY,
  issue_key TEXT NOT NULL,
  repo TEXT NOT NULL,
  pr_number INTEGER NOT NULL,
  head_sha TEXT,
  github_review_id INTEGER,
  event TEXT NOT NULL,
  submitted_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pr_completed_reviews_submitted ON pr_review_completed_reviews(submitted_at);

CREATE TABLE IF NOT EXISTS pr_review_self_improvement_runs (
  run_id TEXT PRIMARY KEY,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  status TEXT NOT NULL CHECK (status IN ('running','done','failed','skipped')),
  trigger_count INTEGER NOT NULL,
  review_count_since_last INTEGER NOT NULL,
  first_completed_review_key TEXT,
  last_completed_review_key TEXT,
  scanned_since TEXT NOT NULL,
  scanned_until TEXT NOT NULL,
  session_count INTEGER NOT NULL DEFAULT 0,
  recommendation_count INTEGER NOT NULL DEFAULT 0,
  files_changed INTEGER NOT NULL DEFAULT 0,
  commit_id TEXT,
  pushed_bookmark TEXT,
  model TEXT,
  report_path TEXT,
  quality_gate_json TEXT,
  error TEXT
);
CREATE INDEX IF NOT EXISTS idx_pr_self_improve_runs_started ON pr_review_self_improvement_runs(started_at);

CREATE TABLE IF NOT EXISTS pr_review_self_improvement_recommendations (
  recommendation_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  severity TEXT NOT NULL CHECK (severity IN ('critical','required','optional')),
  category TEXT NOT NULL CHECK (category IN ('streamline','durability','bug','policy','delegation','prompt','tooling','learning')),
  title TEXT NOT NULL,
  summary TEXT NOT NULL,
  evidence_json TEXT NOT NULL,
  proposed_change TEXT NOT NULL,
  verification TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('candidate','implemented','rejected')),
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pr_self_improve_recs_run ON pr_review_self_improvement_recommendations(run_id);
CREATE INDEX IF NOT EXISTS idx_pr_self_improve_recs_status ON pr_review_self_improvement_recommendations(status, severity);
"""


def _utcnow() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def iso_seconds_ago(seconds: float) -> str:
    """ISO-UTC timestamp for `seconds` ago, matching the format `_utcnow` writes."""
    return (datetime.now(UTC) - timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def iso_seconds_from_now(seconds: float) -> str:
    """ISO-UTC timestamp for `seconds` from now, matching the format `_utcnow` writes."""
    return (datetime.now(UTC) + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


@dataclass(slots=True, frozen=True)
class EventRow:
    delivery_id: str
    event_type: str
    repo: str | None
    issue_key: str | None
    payload: dict[str, Any]
    received_at: str
    state: EventState
    attempts: int
    last_error: str | None
    task: str | None = None
    route_reason: str | None = None
    route_version: int = 1
    outcome: str | None = None
    available_at: str | None = None


@dataclass(slots=True, frozen=True)
class IssueRow:
    key: str
    repo: str
    number: int
    branch: str | None
    session_dir: str | None
    pr_number: int | None
    state: IssueState
    updated_at: str
    classification: str | None = None


@dataclass(slots=True, frozen=True)
class StagedReviewComment:
    id: int
    issue_key: str
    path: str
    line: int
    side: str
    body: str
    created_at: str
    start_line: int | None = None
    start_side: str | None = None


def _event_row_from_db_row(row: sqlite3.Row) -> EventRow:
    return EventRow(
        delivery_id=row["delivery_id"],
        event_type=row["event_type"],
        repo=row["repo"],
        issue_key=row["issue_key"],
        payload=json.loads(row["payload_json"]),
        received_at=row["received_at"],
        state=row["state"],
        attempts=int(row["attempts"]),
        last_error=row["last_error"],
        task=row["task"],
        route_reason=row["route_reason"],
        route_version=int(row["route_version"]),
        outcome=row["outcome"],
        available_at=row["available_at"],
    )


@dataclass(slots=True, frozen=True)
class SubmissionAdmission:
    accepted: bool
    duplicate: bool
    used: int


PendingClosureState = Literal["pending", "claimed", "closed", "cancelled"]


@dataclass(slots=True, frozen=True)
class PendingClosureRow:
    issue_key: str
    repo: str
    number: int
    comment_id: int
    issue_author: str
    close_at: str
    state: PendingClosureState
    cancel_reason: str | None
    created_at: str
    updated_at: str




@dataclass(slots=True, frozen=True)
class PrReviewLifecycleEvent:
    delivery_id: str
    event_type: str
    action: str
    repo: str
    pr_number: int
    head_sha: str | None
    actor_login: str | None
    actor_type: str | None
    author_association: str | None
    object_kind: str
    object_id: str | None
    parent_id: str | None
    review_state: str | None
    path: str | None
    line: int | None
    start_line: int | None
    body: str | None
    body_hash: str | None
    suggestion_hash: str | None
    thread_id: str | None
    thread_resolved: bool | None
    merged: bool | None
    created_at: str
    observed_at: str


@dataclass(slots=True, frozen=True)
class PrReviewPostedFinding:
    finding_id: str
    issue_key: str
    repo: str
    pr_number: int
    head_sha: str | None
    review_id: int | None
    comment_id: int | None
    path: str | None
    line: int | None
    start_line: int | None
    body: str
    body_hash: str
    severity: str
    intent: str
    category: str | None
    suggestion_replacement: str | None
    suggestion_hash: str | None
    status: str
    status_reason: str | None
    posted_at: str
    updated_at: str


@dataclass(slots=True, frozen=True)
class PrReviewGapEvent:
    gap_id: str
    repo: str
    pr_number: int
    head_sha: str | None
    gap_kind: str
    source_label: str
    actor_login: str | None
    event_delivery_id: str | None
    source_object_kind: str
    source_object_id: str | None
    agent_review_id: int | None
    agent_comment_id: int | None
    matched_posted_finding_id: str | None
    path: str | None
    line: int | None
    start_line: int | None
    body: str | None
    body_hash: str | None
    severity_hint: str
    thread_id: str | None
    confidence: float
    reason: str
    created_at: str
    observed_at: str


def _bool_from_db(value: object) -> bool | None:
    if value is None:
        return None
    return bool(value)


def _body_hash(body: str | None) -> str | None:
    if body is None:
        return None
    normalized = body.replace("\r\n", "\n")
    return sha256(normalized.encode("utf-8")).hexdigest()


def _hash_parts(*parts: object) -> str:
    h = sha256()
    for part in parts:
        h.update(str(part or "").encode("utf-8"))
        h.update(b"\0")
    return h.hexdigest()


def _int_or_none(value: object) -> int | None:
    if value is None or value == "":
        return None
    return int(value)


def _issue_row_from_db_row(row: sqlite3.Row) -> IssueRow:
    return IssueRow(
        key=row["key"],
        repo=row["repo"],
        number=int(row["number"]),
        branch=row["branch"],
        session_dir=row["session_dir"],
        pr_number=row["pr_number"],
        state=row["state"],
        classification=row["classification"],
        updated_at=row["updated_at"],
    )


def _posted_finding_from_row(row: sqlite3.Row) -> PrReviewPostedFinding:
    return PrReviewPostedFinding(
        finding_id=row["finding_id"], issue_key=row["issue_key"], repo=row["repo"], pr_number=int(row["pr_number"]),
        head_sha=row["head_sha"], review_id=row["review_id"], comment_id=row["comment_id"], path=row["path"],
        line=row["line"], start_line=row["start_line"], body=row["body"], body_hash=row["body_hash"],
        severity=row["severity"], intent=row["intent"], category=row["category"],
        suggestion_replacement=row["suggestion_replacement"], suggestion_hash=row["suggestion_hash"],
        status=row["status"], status_reason=row["status_reason"], posted_at=row["posted_at"], updated_at=row["updated_at"],
    )


def _gap_event_from_row(row: sqlite3.Row) -> PrReviewGapEvent:
    return PrReviewGapEvent(
        gap_id=row["gap_id"], repo=row["repo"], pr_number=int(row["pr_number"]), head_sha=row["head_sha"],
        gap_kind=row["gap_kind"], source_label=row["source_label"], actor_login=row["actor_login"],
        event_delivery_id=row["event_delivery_id"], source_object_kind=row["source_object_kind"],
        source_object_id=row["source_object_id"], agent_review_id=row["agent_review_id"],
        agent_comment_id=row["agent_comment_id"], matched_posted_finding_id=row["matched_posted_finding_id"],
        path=row["path"], line=row["line"], start_line=row["start_line"], body=row["body"],
        body_hash=row["body_hash"], severity_hint=row["severity_hint"], thread_id=row["thread_id"],
        confidence=float(row["confidence"]), reason=row["reason"], created_at=row["created_at"], observed_at=row["observed_at"],
    )

def _pending_closure_from_row(row: sqlite3.Row) -> PendingClosureRow:
    return PendingClosureRow(
        issue_key=row["issue_key"],
        repo=row["repo"],
        number=int(row["number"]),
        comment_id=int(row["comment_id"]),
        issue_author=row["issue_author"],
        close_at=row["close_at"],
        state=row["state"],
        cancel_reason=row["cancel_reason"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def issue_key(repo: str, number: int) -> str:
    return f"{repo}#{number}"


class Database:
    """Thread-safe sqlite wrapper. One connection per thread via locks."""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._migrate()

    def _migrate(self) -> None:
        # SQLite-friendly forward migrations. Each is idempotent.
        issue_cols = {row[1] for row in self._conn.execute("PRAGMA table_info(issues)").fetchall()}
        if "classification" not in issue_cols:
            self._conn.execute("ALTER TABLE issues ADD COLUMN classification TEXT")
        event_cols = {row[1] for row in self._conn.execute("PRAGMA table_info(events)").fetchall()}
        if "model" not in event_cols:
            self._conn.execute("ALTER TABLE events ADD COLUMN model TEXT")
        if "task" not in event_cols:
            self._conn.execute("ALTER TABLE events ADD COLUMN task TEXT")
        if "route_reason" not in event_cols:
            self._conn.execute("ALTER TABLE events ADD COLUMN route_reason TEXT")
        if "route_version" not in event_cols:
            self._conn.execute("ALTER TABLE events ADD COLUMN route_version INTEGER NOT NULL DEFAULT 1")
        if "outcome" not in event_cols:
            self._conn.execute("ALTER TABLE events ADD COLUMN outcome TEXT")
        if "available_at" not in event_cols:
            self._conn.execute("ALTER TABLE events ADD COLUMN available_at TEXT")
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS events_state_available ON events(state, available_at, received_at)"
        )

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @contextmanager
    def _txn(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise

    # ---- events ----
    def record_event(
        self,
        *,
        delivery_id: str,
        event_type: str,
        repo: str | None,
        issue_key: str | None,
        payload: Mapping[str, Any],
        state: EventState = "queued",
        last_error: str | None = None,
        task: str | None = None,
        route_reason: str | None = None,
        route_version: int = 1,
        outcome: str | None = None,
    ) -> bool:
        """Insert a webhook event. Returns False if duplicate (by delivery id).

        `last_error` is the reason text surfaced on the dashboard for non-queued
        states (skipped, failed). Ignored when state == 'queued'.
        """
        now = _utcnow()
        stored_outcome = outcome if outcome is not None else (state if state != "running" else None)
        with self._lock:
            cur = self._conn.execute(
                """
                INSERT OR IGNORE INTO events
                  (delivery_id, event_type, repo, issue_key, payload_json, received_at, state, last_error,
                   task, route_reason, route_version, outcome)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    delivery_id,
                    event_type,
                    repo,
                    issue_key,
                    json.dumps(payload, separators=(",", ":")),
                    now,
                    state,
                    last_error,
                    task,
                    route_reason,
                    route_version,
                    stored_outcome,
                ),
            )
            return cur.rowcount > 0

    def claim_next_event(self) -> EventRow | None:
        """Atomically dequeue one unblocked queued event into running state."""
        with self._txn() as conn:
            row = conn.execute(
                """
                SELECT queued.delivery_id, queued.event_type, queued.repo, queued.issue_key,
                       queued.payload_json, queued.received_at, queued.state, queued.attempts,
                       queued.last_error, queued.task, queued.route_reason, queued.route_version,
                       queued.outcome, queued.available_at
                FROM events AS queued
                WHERE queued.state = 'queued'
                  AND (queued.available_at IS NULL OR queued.available_at <= ?)
                  AND (
                    queued.issue_key IS NULL
                    OR NOT EXISTS (
                      SELECT 1
                      FROM events AS running
                      WHERE running.state = 'running'
                        AND running.issue_key = queued.issue_key
                    )
                  )
                ORDER BY COALESCE(queued.available_at, queued.received_at), queued.received_at
                LIMIT 1
                """,
                (_utcnow(),),
            ).fetchone()
            if row is None:
                return None
            now = _utcnow()
            conn.execute(
                "UPDATE events SET state='running', attempts=attempts+1, started_at=?, available_at=NULL WHERE delivery_id=?",
                (now, row["delivery_id"]),
            )
            return EventRow(
                delivery_id=row["delivery_id"],
                event_type=row["event_type"],
                repo=row["repo"],
                issue_key=row["issue_key"],
                payload=json.loads(row["payload_json"]),
                received_at=row["received_at"],
                state="running",
                attempts=int(row["attempts"]) + 1,
                last_error=row["last_error"],
                task=row["task"],
                route_reason=row["route_reason"],
                route_version=int(row["route_version"]),
                outcome=row["outcome"],
                available_at=None,
            )

    def mark_event(self, delivery_id: str, state: EventState, *, error: str | None = None) -> None:
        with self._lock:
            if state in INACTIVE_EVENT_STATES:
                self._conn.execute(
                    "UPDATE events SET state=?, last_error=?, outcome=?, finished_at=? WHERE delivery_id=?",
                    (state, error, state, _utcnow(), delivery_id),
                )
            else:
                self._conn.execute(
                    "UPDATE events SET state=?, last_error=?, finished_at=? WHERE delivery_id=?",
                    (state, error, _utcnow(), delivery_id),
                )

    def set_event_model(self, delivery_id: str, model: str) -> None:
        """Persist the model the worker actually picked for this event.

        Called once per run, right after `pick_model()`, so the dashboard and
        post-mortems can attribute behavior to the exact model used.
        """
        with self._lock:
            self._conn.execute(
                "UPDATE events SET model=? WHERE delivery_id=?",
                (model, delivery_id),
            )

    def reset_stuck_running(self) -> int:
        """Recover events that were running at shutdown."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE events SET state='queued', outcome='queued' WHERE state='running'",
            )
            return cur.rowcount

    def list_events(self, *, limit: int = 50) -> list[EventRow]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT delivery_id, event_type, repo, issue_key, payload_json, received_at,
                       state, attempts, last_error, task, route_reason, route_version, outcome, available_at
                FROM events
                ORDER BY received_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [_event_row_from_db_row(row) for row in rows]

    def remove_event(self, delivery_id: str) -> None:
        """Hard-delete an event row. Used to clear stale state before a manual re-trigger."""
        with self._lock:
            self._conn.execute("DELETE FROM events WHERE delivery_id=?", (delivery_id,))

    def replace_event_if_state_in(
        self,
        *,
        delivery_id: str,
        event_type: str,
        repo: str | None,
        issue_key: str | None,
        payload: Mapping[str, Any],
        state: EventState = "queued",
        allowed_existing_states: tuple[EventState, ...],
        task: str | None = None,
        route_reason: str | None = None,
        route_version: int = 1,
        outcome: str | None = None,
    ) -> bool:
        """Replace an existing event only when its current state is permitted."""
        now = _utcnow()
        stored_outcome = outcome if outcome is not None else (state if state != "running" else None)
        with self._txn() as conn:
            row = conn.execute(
                "SELECT state FROM events WHERE delivery_id = ?",
                (delivery_id,),
            ).fetchone()
            if row is not None:
                if row["state"] not in allowed_existing_states:
                    return False
                conn.execute("DELETE FROM events WHERE delivery_id = ?", (delivery_id,))
            conn.execute(
                """
                INSERT INTO events
                  (delivery_id, event_type, repo, issue_key, payload_json, received_at, state,
                   task, route_reason, route_version, outcome)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    delivery_id,
                    event_type,
                    repo,
                    issue_key,
                    json.dumps(payload, separators=(",", ":")),
                    now,
                    state,
                    task,
                    route_reason,
                    route_version,
                    stored_outcome,
                ),
            )
            return True

    def latest_event_for_issue(self, key: str, *, include_skipped: bool = False) -> EventRow | None:
        """Return the newest event for an issue.

        By default this ignores `skipped` rows. Those are usually webhook noise
        (`issues.labeled ignored`, bot/self comments) and must not hide the last
        real processing run when the dashboard retries a failed issue.
        """
        state_filter = "" if include_skipped else "AND state <> 'skipped'"
        with self._lock:
            row = self._conn.execute(
                f"""
                SELECT delivery_id, event_type, repo, issue_key, payload_json, received_at,
                       state, attempts, last_error, task, route_reason, route_version, outcome, available_at
                FROM events
                WHERE issue_key = ?
                  {state_filter}
                ORDER BY received_at DESC, rowid DESC
                LIMIT 1
                """,
                (key,),
            ).fetchone()
        if row is None:
            return None
        return _event_row_from_db_row(row)

    def latest_events_for_issues(
        self,
        keys: Iterable[str],
        *,
        include_skipped: bool = False,
    ) -> dict[str, EventRow]:
        """Return newest event rows keyed by issue key for a bounded issue set."""
        unique = tuple({k for k in keys if k})
        if not unique:
            return {}
        state_filter = "" if include_skipped else "AND state <> 'skipped'"
        out: dict[str, EventRow] = {}
        with self._lock:
            for start in range(0, len(unique), 500):
                batch = unique[start : start + 500]
                placeholders = ",".join("?" * len(batch))
                rows = self._conn.execute(
                    f"""
                    SELECT delivery_id, event_type, repo, issue_key, payload_json, received_at,
                           state, attempts, last_error, task, route_reason, route_version, outcome, available_at
                    FROM events
                    WHERE issue_key IN ({placeholders})
                      {state_filter}
                    ORDER BY issue_key ASC, received_at DESC, rowid DESC
                    """,
                    batch,
                ).fetchall()
                for row in rows:
                    issue = row["issue_key"]
                    if issue not in out:
                        out[issue] = _event_row_from_db_row(row)
        return out

    def event_state_counts(self) -> dict[str, int]:
        """Return current row counts per event state, including states with zero rows."""
        with self._lock:
            rows = self._conn.execute("SELECT state, COUNT(*) AS n FROM events GROUP BY state").fetchall()
        counts: dict[str, int] = dict.fromkeys(("queued", "running", "done", "failed", "skipped"), 0)
        for row in rows:
            counts[row["state"]] = int(row["n"])
        return counts

    def event_counts_by_state(self) -> dict[str, int]:
        """Return current row counts per event state for readiness/metrics."""
        return self.event_state_counts()

    def oldest_queued_age_seconds(self) -> float | None:
        """Age in seconds of the oldest queued event, or None when queue is empty."""
        with self._lock:
            row = self._conn.execute(
                "SELECT received_at FROM events WHERE state='queued' ORDER BY received_at LIMIT 1"
            ).fetchone()
        if row is None:
            return None
        received = datetime.fromisoformat(str(row["received_at"]).replace("Z", "+00:00"))
        return max(0.0, (datetime.now(UTC) - received).total_seconds())

    def recent_failure_count(self, since_seconds: float) -> int:
        """Count failed events whose terminal timestamp is within the window."""
        since = iso_seconds_ago(since_seconds)
        with self._lock:
            row = self._conn.execute(
                """
                SELECT COUNT(*) AS n
                FROM events
                WHERE state='failed'
                  AND COALESCE(finished_at, received_at) >= ?
                """,
                (since,),
            ).fetchone()
        return int(row["n"])

    def latest_issue_event_state_counts(self) -> dict[str, int]:
        """Count each issue by its newest non-skipped event state.

        This is the dashboard's "current issue event" view: a later successful
        run clears an older failure for that issue, and ignored webhook noise
        does not make a failed issue look skipped.
        """
        counts: dict[str, int] = dict.fromkeys(("queued", "running", "done", "failed", "skipped"), 0)
        seen: set[str] = set()
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT issue_key, state
                FROM events
                WHERE issue_key IS NOT NULL
                  AND state <> 'skipped'
                ORDER BY issue_key ASC, received_at DESC, rowid DESC
                """
            ).fetchall()
        for row in rows:
            key = row["issue_key"]
            if key in seen:
                continue
            seen.add(key)
            counts[row["state"]] += 1
        return counts

    def list_running_events(self) -> list[dict[str, Any]]:
        """Snapshot of currently-running events.

        Returns elapsed-time inputs (`started_at`) plus per-run telemetry:
        - `model`: the omp model the worker picked for this run, set after
          `pick_model()` so it reflects the actual pool selection.
        - `last_tool` / `last_tool_ts`: the most recent host-tool call audited
          on the same `issue_key` since `started_at`. Scoping by start time
          prevents stale entries from a prior run on the same issue leaking
          into the dashboard before this run has emitted any tool calls.
        """
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT e.delivery_id, e.event_type, e.repo, e.issue_key, e.received_at,
                       e.started_at, e.attempts, e.model, e.task, e.route_reason,
                       e.route_version, e.outcome,
                       (SELECT tool FROM tool_calls
                          WHERE issue_key = e.issue_key AND ts >= e.started_at
                          ORDER BY ts DESC LIMIT 1) AS last_tool,
                       (SELECT ts FROM tool_calls
                          WHERE issue_key = e.issue_key AND ts >= e.started_at
                          ORDER BY ts DESC LIMIT 1) AS last_tool_ts
                FROM events e
                WHERE e.state = 'running'
                ORDER BY COALESCE(e.started_at, e.received_at)
                """
            ).fetchall()
        return [
            {
                "delivery_id": r["delivery_id"],
                "event_type": r["event_type"],
                "repo": r["repo"],
                "issue_key": r["issue_key"],
                "received_at": r["received_at"],
                "started_at": r["started_at"],
                "attempts": int(r["attempts"]),
                "model": r["model"],
                "task": r["task"],
                "route_reason": r["route_reason"],
                "route_version": int(r["route_version"]),
                "outcome": r["outcome"],
                "last_tool": r["last_tool"],
                "last_tool_ts": r["last_tool_ts"],
            }
            for r in rows
        ]

    def get_event(self, delivery_id: str) -> EventRow | None:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT delivery_id, event_type, repo, issue_key, payload_json, received_at,
                       state, attempts, last_error, task, route_reason, route_version, outcome, available_at
                FROM events WHERE delivery_id = ?
                """,
                (delivery_id,),
            ).fetchone()
        if row is None:
            return None
        return _event_row_from_db_row(row)

    def requeue_event(
        self,
        delivery_id: str,
        *,
        from_states: tuple[EventState, ...] | None = None,
        error: str | None = None,
        available_at: str | None = None,
    ) -> bool:
        """Move an event back to queued, optionally updating last_error and availability."""
        with self._lock:
            if error is not None:
                set_clause = "state='queued', outcome='queued', last_error=?, available_at=?"
                values: tuple[Any, ...] = (error, available_at, delivery_id)
            else:
                set_clause = "state='queued', outcome='queued', available_at=?"
                values = (available_at, delivery_id)
            if from_states is None:
                cur = self._conn.execute(
                    f"UPDATE events SET {set_clause} WHERE delivery_id=?",
                    values,
                )
            elif not from_states:
                return False
            else:
                placeholders = ",".join("?" for _ in from_states)
                cur = self._conn.execute(
                    f"UPDATE events SET {set_clause} WHERE delivery_id=? AND state IN ({placeholders})",
                    (*values, *from_states),
                )
            return cur.rowcount > 0

    # ---- issues ----
    def upsert_issue(
        self,
        *,
        key: str,
        repo: str,
        number: int,
        state: IssueState,
        branch: str | None = None,
        session_dir: str | None = None,
        pr_number: int | None = None,
    ) -> IssueRow:
        now = _utcnow()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO issues (key, repo, number, branch, session_dir, pr_number, state, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                  branch = COALESCE(excluded.branch, issues.branch),
                  session_dir = COALESCE(excluded.session_dir, issues.session_dir),
                  pr_number = COALESCE(excluded.pr_number, issues.pr_number),
                  state = excluded.state,
                  updated_at = excluded.updated_at
                """,
                (key, repo, number, branch, session_dir, pr_number, state, now),
            )
        got = self.get_issue(key)
        assert got is not None
        return got

    def set_issue_state(self, key: str, state: IssueState) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE issues SET state=?, updated_at=? WHERE key=?",
                (state, _utcnow(), key),
            )

    def set_issue_pr(self, key: str, pr_number: int) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE issues SET pr_number=?, updated_at=? WHERE key=?",
                (pr_number, _utcnow(), key),
            )

    def set_issue_classification(self, key: str, classification: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE issues SET classification=?, updated_at=? WHERE key=?",
                (classification, _utcnow(), key),
            )

    def set_issue_branch(self, key: str, branch: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE issues SET branch=?, updated_at=? WHERE key=?",
                (branch, _utcnow(), key),
            )

    def get_issue(self, key: str) -> IssueRow | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT key, repo, number, branch, session_dir, pr_number, state, classification, updated_at FROM issues WHERE key=?",
                (key,),
            ).fetchone()
        if row is None:
            return None
        return IssueRow(
            key=row["key"],
            repo=row["repo"],
            number=int(row["number"]),
            branch=row["branch"],
            session_dir=row["session_dir"],
            pr_number=int(row["pr_number"]) if row["pr_number"] is not None else None,
            state=row["state"],
            updated_at=row["updated_at"],
            classification=row["classification"],
        )

    def find_issue_by_pr(self, repo: str, pr_number: int) -> IssueRow | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT key, repo, number, branch, session_dir, pr_number, state, classification, updated_at FROM issues WHERE repo=? AND pr_number=?",
                (repo, pr_number),
            ).fetchone()
        if row is None:
            return None
        return IssueRow(
            key=row["key"],
            repo=row["repo"],
            number=int(row["number"]),
            branch=row["branch"],
            session_dir=row["session_dir"],
            pr_number=int(row["pr_number"]),
            state=row["state"],
            updated_at=row["updated_at"],
            classification=row["classification"],
        )

    def find_issue_by_branch(self, repo: str, branch: str) -> IssueRow | None:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT key, repo, number, branch, session_dir, pr_number, state, classification, updated_at
                FROM issues
                WHERE repo=? AND branch=?
                ORDER BY updated_at DESC
                LIMIT 1
                """,
                (repo, branch),
            ).fetchone()
        if row is None:
            return None
        return IssueRow(
            key=row["key"],
            repo=row["repo"],
            number=int(row["number"]),
            branch=row["branch"],
            session_dir=row["session_dir"],
            pr_number=int(row["pr_number"]) if row["pr_number"] is not None else None,
            state=row["state"],
            updated_at=row["updated_at"],
            classification=row["classification"],
        )

    def list_issues(self, limit: int = 100) -> list[IssueRow]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT key, repo, number, branch, session_dir, pr_number, state, classification, updated_at FROM issues ORDER BY updated_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            IssueRow(
                key=r["key"],
                repo=r["repo"],
                number=int(r["number"]),
                branch=r["branch"],
                session_dir=r["session_dir"],
                pr_number=int(r["pr_number"]) if r["pr_number"] is not None else None,
                state=r["state"],
                updated_at=r["updated_at"],
                classification=r["classification"],
            )
            for r in rows
        ]

    def processed_issue_keys(self, keys: Iterable[str]) -> set[str]:
        """Return the subset of `keys` that have a row in the `issues` table.

        Membership in `issues` means robomp has at minimum upserted state for the
        issue — i.e. it has been picked up by the dispatcher at least once. Used
        by the browse panel to hide issues we've already started on.
        """
        unique = tuple({k for k in keys if k})
        if not unique:
            return set()
        # SQLite parameter limit is 999 by default; chunk to stay well under it.
        out: set[str] = set()
        with self._lock:
            for start in range(0, len(unique), 500):
                batch = unique[start : start + 500]
                placeholders = ",".join("?" * len(batch))
                rows = self._conn.execute(
                    f"SELECT key FROM issues WHERE key IN ({placeholders})",
                    batch,
                ).fetchall()
                out.update(r["key"] for r in rows)
        return out

    # ---- tool_calls ----
    def log_tool_call(
        self,
        *,
        issue_key: str,
        tool: str,
        args: Mapping[str, Any],
        result: Mapping[str, Any] | None = None,
        error: str | None = None,
    ) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO tool_calls (issue_key, tool, args_json, result_json, error, ts) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    issue_key,
                    tool,
                    json.dumps(args, separators=(",", ":"), default=str),
                    json.dumps(result, separators=(",", ":"), default=str) if result is not None else None,
                    error,
                    _utcnow(),
                ),
            )
            return int(cur.lastrowid or 0)

    # ---- side effects ----
    def reserve_side_effect(self, operation_key: str) -> bool:
        """Reserve an external side effect before calling the remote service.

        Returns False when the same operation is already pending or succeeded.
        Failed reservations are retryable and do not block a new reservation.
        """
        now = _utcnow()
        with self._lock:
            try:
                cur = self._conn.execute(
                    """
                    INSERT INTO side_effects (operation_key, state, created_at, updated_at)
                    VALUES (?, 'pending', ?, ?)
                    """,
                    (operation_key, now, now),
                )
            except sqlite3.IntegrityError:
                return False
            return cur.rowcount > 0

    def mark_side_effect_succeeded(self, operation_key: str) -> bool:
        now = _utcnow()
        with self._lock:
            cur = self._conn.execute(
                """
                UPDATE side_effects
                SET state='succeeded', error=NULL, updated_at=?
                WHERE id = (
                  SELECT id
                  FROM side_effects
                  WHERE operation_key=? AND state='pending'
                  ORDER BY id DESC
                  LIMIT 1
                )
                """,
                (now, operation_key),
            )
            return cur.rowcount > 0

    def mark_side_effect_failed(self, operation_key: str, error: str) -> bool:
        now = _utcnow()
        with self._lock:
            cur = self._conn.execute(
                """
                UPDATE side_effects
                SET state='failed', error=?, updated_at=?
                WHERE id = (
                  SELECT id
                  FROM side_effects
                  WHERE operation_key=? AND state='pending'
                  ORDER BY id DESC
                  LIMIT 1
                )
                """,
                (error, now, operation_key),
            )
            return cur.rowcount > 0

    def side_effect_succeeded(self, operation_key: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT 1
                FROM side_effects
                WHERE operation_key=? AND state='succeeded'
                LIMIT 1
                """,
                (operation_key,),
            ).fetchone()
        return row is not None

    def has_successful_tool_call(self, issue_key: str, tool: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT 1
                FROM tool_calls
                WHERE issue_key=? AND tool=? AND error IS NULL
                ORDER BY id DESC
                LIMIT 1
                """,
                (issue_key, tool),
            ).fetchone()
        return row is not None

    # ---- PR review comment staging ----
    def stage_review_comment(
        self,
        *,
        issue_key: str,
        path: str,
        line: int,
        body: str,
        side: str = "RIGHT",
        start_line: int | None = None,
        start_side: str | None = None,
    ) -> StagedReviewComment:
        with self._lock:
            cur = self._conn.execute(
                """
                INSERT INTO pr_review_comments
                  (issue_key, path, line, side, start_line, start_side, body, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (issue_key, path, line, side, start_line, start_side, body, _utcnow()),
            )
            row = self._conn.execute(
                """
                SELECT id, issue_key, path, line, side, start_line, start_side, body, created_at
                FROM pr_review_comments
                WHERE id=?
                """,
                (int(cur.lastrowid or 0),),
            ).fetchone()
        assert row is not None
        return StagedReviewComment(
            id=int(row["id"]),
            issue_key=row["issue_key"],
            path=row["path"],
            line=int(row["line"]),
            side=row["side"],
            body=row["body"],
            created_at=row["created_at"],
            start_line=int(row["start_line"]) if row["start_line"] is not None else None,
            start_side=row["start_side"],
        )

    def list_staged_review_comments(self, issue_key: str) -> list[StagedReviewComment]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT id, issue_key, path, line, side, start_line, start_side, body, created_at
                FROM pr_review_comments
                WHERE issue_key=?
                ORDER BY id
                """,
                (issue_key,),
            ).fetchall()
        return [
            StagedReviewComment(
                id=int(row["id"]),
                issue_key=row["issue_key"],
                path=row["path"],
                line=int(row["line"]),
                side=row["side"],
                body=row["body"],
                created_at=row["created_at"],
                start_line=int(row["start_line"]) if row["start_line"] is not None else None,
                start_side=row["start_side"],
            )
            for row in rows
        ]

    def clear_staged_review_comments(self, issue_key: str) -> int:
        with self._lock:
            cur = self._conn.execute("DELETE FROM pr_review_comments WHERE issue_key=?", (issue_key,))
            return int(cur.rowcount or 0)

    # ---- submissions (per-user rate limiting) ----
    def admit_submission(
        self,
        *,
        delivery_id: str,
        login: str,
        repo: str | None,
        since: str,
        cap: int | None,
    ) -> SubmissionAdmission:
        """Atomically check a submitter's rolling cap and record this delivery.

        Duplicate delivery ids are accepted without inserting a second row, so a
        webhook retry remains idempotent even after the submitter reaches the cap.
        `used` is the matching submission count after acceptance, or the count
        that caused rejection when `accepted` is False.
        """
        normalized_login = login.lower()
        with self._txn() as conn:
            existing = conn.execute(
                "SELECT 1 FROM submissions WHERE delivery_id=?",
                (delivery_id,),
            ).fetchone()
            if existing is not None:
                row = conn.execute(
                    "SELECT COUNT(*) AS n FROM submissions WHERE login=? AND ts>=?",
                    (normalized_login, since),
                ).fetchone()
                return SubmissionAdmission(
                    accepted=True,
                    duplicate=True,
                    used=int(row["n"]) if row is not None else 0,
                )

            row = conn.execute(
                "SELECT COUNT(*) AS n FROM submissions WHERE login=? AND ts>=?",
                (normalized_login, since),
            ).fetchone()
            used = int(row["n"]) if row is not None else 0
            if cap is not None and used >= cap:
                return SubmissionAdmission(accepted=False, duplicate=False, used=used)

            conn.execute(
                "INSERT INTO submissions (delivery_id, login, repo, ts) VALUES (?, ?, ?, ?)",
                (delivery_id, normalized_login, repo, _utcnow()),
            )
            return SubmissionAdmission(accepted=True, duplicate=False, used=used + 1)

    def record_submission(
        self,
        *,
        delivery_id: str,
        login: str,
        repo: str | None,
    ) -> bool:
        """Idempotently log a queue-worthy submission by `login`.

        Returns False if the delivery_id was already recorded (webhook retry).
        """
        now = _utcnow()
        with self._lock:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO submissions (delivery_id, login, repo, ts) VALUES (?, ?, ?, ?)",
                (delivery_id, login.lower(), repo, now),
            )
            return cur.rowcount > 0

    def count_submissions_since(self, login: str, since: str) -> int:
        """Count submissions by `login` (case-insensitive) with ts >= `since`."""
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM submissions WHERE login=? AND ts>=?",
                (login.lower(), since),
            ).fetchone()
        return int(row["n"]) if row is not None else 0

    # ---- pending_closures ----
    def upsert_pending_closure(
        self,
        *,
        issue_key: str,
        repo: str,
        number: int,
        comment_id: int,
        issue_author: str,
        close_at: str,
    ) -> None:
        """Schedule (or reschedule) a question issue to auto-close.

        A follow-up bot answer on the same issue overwrites the prior schedule:
        we always watch the latest comment and can roll the close_at forward.
        Resets state to `pending` and clears any prior cancel_reason so a row
        previously closed/cancelled becomes a live schedule again.
        """
        now = _utcnow()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO pending_closures
                  (issue_key, repo, number, comment_id, issue_author, close_at,
                   state, cancel_reason, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, 'pending', NULL, ?, ?)
                ON CONFLICT(issue_key) DO UPDATE SET
                  repo = excluded.repo,
                  number = excluded.number,
                  comment_id = excluded.comment_id,
                  issue_author = excluded.issue_author,
                  close_at = excluded.close_at,
                  state = 'pending',
                  cancel_reason = NULL,
                  updated_at = excluded.updated_at
                """,
                (issue_key, repo, number, comment_id, issue_author.lower(), close_at, now, now),
            )

    def claim_due_closures(self, *, now: str, limit: int = 50) -> list[PendingClosureRow]:
        """Atomically flip due `pending` rows to `claimed` and return them.

        Atomic claim prevents two scheduler ticks (or a tick racing a
        cancellation) from acting on the same row twice. Caller is responsible
        for finalizing each claimed row via `finalize_closure` or returning
        it to `pending` via `requeue_claimed_closure` after a transient error.
        """
        with self._txn() as conn:
            rows = conn.execute(
                """
                UPDATE pending_closures
                SET state = 'claimed', updated_at = ?
                WHERE issue_key IN (
                  SELECT issue_key FROM pending_closures
                  WHERE state = 'pending' AND close_at <= ?
                  ORDER BY close_at
                  LIMIT ?
                )
                RETURNING issue_key, repo, number, comment_id, issue_author,
                          close_at, state, cancel_reason, created_at, updated_at
                """,
                (now, now, int(limit)),
            ).fetchall()
        return [_pending_closure_from_row(row) for row in rows]

    def finalize_closure(
        self,
        issue_key: str,
        *,
        state: PendingClosureState,
        reason: str | None,
    ) -> None:
        """Mark a claimed row terminal (`closed` / `cancelled`)."""
        if state not in ("closed", "cancelled"):
            raise ValueError(f"finalize_closure: invalid terminal state {state!r}")
        with self._lock:
            self._conn.execute(
                """
                UPDATE pending_closures
                SET state = ?, cancel_reason = ?, updated_at = ?
                WHERE issue_key = ?
                """,
                (state, reason, _utcnow(), issue_key),
            )

    def requeue_claimed_closure(self, issue_key: str) -> bool:
        """Return a `claimed` row to `pending` so the next tick retries it.

        Used by the scheduler when a transient GitHub error prevents the
        close from completing. Only flips `claimed -> pending`; rows in any
        other state are left untouched.
        """
        with self._lock:
            cur = self._conn.execute(
                """
                UPDATE pending_closures
                SET state = 'pending', updated_at = ?
                WHERE issue_key = ? AND state = 'claimed'
                """,
                (_utcnow(), issue_key),
            )
            return cur.rowcount > 0

    def cancel_pending_closure(self, issue_key: str, *, reason: str) -> bool:
        """Cancel a scheduled close. No-op when state is not `pending`.

        A row already `claimed` is left for the scheduler tick that owns it
        to finalize — racing a cancel against a claim must not double-write
        the row's terminal state.
        """
        with self._lock:
            cur = self._conn.execute(
                """
                UPDATE pending_closures
                SET state = 'cancelled', cancel_reason = ?, updated_at = ?
                WHERE issue_key = ? AND state = 'pending'
                """,
                (reason, _utcnow(), issue_key),
            )
            return cur.rowcount > 0

    def get_pending_closure(self, issue_key: str) -> PendingClosureRow | None:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT issue_key, repo, number, comment_id, issue_author,
                       close_at, state, cancel_reason, created_at, updated_at
                FROM pending_closures WHERE issue_key = ?
                """,
                (issue_key,),
            ).fetchone()
        return _pending_closure_from_row(row) if row is not None else None


    # ---- PR review learning ----
    def record_pr_review_lifecycle_event(
        self, *, delivery_id: str, event_type: str, action: str, repo: str,
        pr_number: int, object_kind: str, head_sha: str | None = None,
        actor_login: str | None = None, actor_type: str | None = None,
        author_association: str | None = None, object_id: str | None = None,
        parent_id: str | None = None, review_state: str | None = None,
        path: str | None = None, line: int | None = None,
        start_line: int | None = None, body: str | None = None,
        suggestion_hash: str | None = None, thread_id: str | None = None,
        thread_resolved: bool | None = None, merged: bool | None = None,
        created_at: str | None = None,
    ) -> bool:
        now = _utcnow()
        created = created_at or now
        with self._lock:
            cur = self._conn.execute(
                """
                INSERT OR IGNORE INTO pr_review_lifecycle_events
                  (delivery_id, event_type, action, repo, pr_number, head_sha, actor_login, actor_type,
                   author_association, object_kind, object_id, parent_id, review_state, path, line,
                   start_line, body, body_hash, suggestion_hash, thread_id, thread_resolved, merged,
                   created_at, observed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    delivery_id, event_type, action, repo, pr_number, head_sha, actor_login, actor_type,
                    author_association, object_kind, object_id, parent_id, review_state, path, line,
                    start_line, body, _body_hash(body), suggestion_hash, thread_id,
                    None if thread_resolved is None else int(thread_resolved),
                    None if merged is None else int(merged), created, now,
                ),
            )
            return cur.rowcount > 0

    def record_pr_review_posted_findings(
        self, *, issue_key: str, repo: str, pr_number: int, head_sha: str | None,
        review_id: int | None, findings: Sequence[Mapping[str, Any]],
        posted_comments: Sequence[Mapping[str, Any]],
    ) -> int:
        now = _utcnow()
        comments = list(posted_comments)
        used: set[int] = set()
        inserted = 0
        with self._lock:
            for finding in findings:
                path = finding.get("path")
                line = _int_or_none(finding.get("line") or finding.get("end_line") or finding.get("original_line"))
                start_line = _int_or_none(finding.get("start_line"))
                body = str(finding.get("body") or finding.get("comment") or finding.get("message") or finding.get("description") or "")
                if not body:
                    body = str(finding)
                body_hash = _body_hash(body) or ""
                suggestion_replacement = finding.get("suggestion_replacement") or finding.get("suggestion")
                suggestion_hash = finding.get("suggestion_hash")
                if suggestion_hash is None and suggestion_replacement is not None:
                    suggestion_hash = _body_hash(str(suggestion_replacement))
                comment_id: int | None = None
                for idx, comment in enumerate(comments):
                    if idx in used:
                        continue
                    c_path = comment.get("path")
                    c_line = _int_or_none(comment.get("line") or comment.get("original_line"))
                    c_body_hash = _body_hash(str(comment.get("body") or ""))
                    if c_path == path and c_line == line and c_body_hash == body_hash:
                        comment_id = _int_or_none(comment.get("id") or comment.get("comment_id"))
                        used.add(idx)
                        break
                if comment_id is None:
                    for idx, comment in enumerate(comments):
                        if idx in used:
                            continue
                        c_path = comment.get("path")
                        c_line = _int_or_none(comment.get("line") or comment.get("original_line"))
                        if c_path == path and c_line == line:
                            comment_id = _int_or_none(comment.get("id") or comment.get("comment_id"))
                            used.add(idx)
                            break
                finding_id = _hash_parts(repo, pr_number, head_sha, review_id, path, line, body_hash, suggestion_hash)
                cur = self._conn.execute(
                    """
                    INSERT OR IGNORE INTO pr_review_posted_findings
                      (finding_id, issue_key, repo, pr_number, head_sha, review_id, comment_id, path, line,
                       start_line, body, body_hash, severity, intent, category, suggestion_replacement,
                       suggestion_hash, status, status_reason, posted_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'posted', NULL, ?, ?)
                    """,
                    (
                        finding_id, issue_key, repo, pr_number, head_sha, review_id, comment_id, path, line,
                        start_line, body, body_hash, str(finding.get("severity") or "required"),
                        str(finding.get("intent") or "finding"), finding.get("category"),
                        None if suggestion_replacement is None else str(suggestion_replacement), suggestion_hash,
                        now, now,
                    ),
                )
                inserted += int(cur.rowcount or 0)
        return inserted

    def update_pr_review_posted_finding_status(
        self, *, status: str, reason: str,
        comment_id: int | None = None, finding_id: str | None = None,
    ) -> int:
        if comment_id is None and finding_id is None:
            return 0
        now = _utcnow()
        with self._lock:
            if finding_id is not None:
                cur = self._conn.execute(
                    "UPDATE pr_review_posted_findings SET status=?, status_reason=?, updated_at=? WHERE finding_id=?",
                    (status, reason, now, finding_id),
                )
            else:
                cur = self._conn.execute(
                    "UPDATE pr_review_posted_findings SET status=?, status_reason=?, updated_at=? WHERE comment_id=?",
                    (status, reason, now, comment_id),
                )
            return int(cur.rowcount or 0)

    def record_pr_review_gap_event(
        self, *, gap_kind: str, repo: str, pr_number: int, head_sha: str | None,
        source_label: str, source_object_kind: str, source_object_id: str | None,
        severity_hint: str, confidence: float, reason: str,
        actor_login: str | None = None, event_delivery_id: str | None = None,
        agent_review_id: int | None = None, agent_comment_id: int | None = None,
        matched_posted_finding_id: str | None = None, path: str | None = None,
        line: int | None = None, start_line: int | None = None, body: str | None = None,
        thread_id: str | None = None, created_at: str | None = None,
    ) -> bool:
        now = _utcnow()
        created = created_at or now
        bh = _body_hash(body)
        gap_id = _hash_parts(repo, pr_number, gap_kind, source_object_kind, source_object_id, bh, matched_posted_finding_id)
        with self._lock:
            cur = self._conn.execute(
                """
                INSERT OR IGNORE INTO pr_review_gap_events
                  (gap_id, repo, pr_number, head_sha, gap_kind, source_label, actor_login, event_delivery_id,
                   source_object_kind, source_object_id, agent_review_id, agent_comment_id,
                   matched_posted_finding_id, path, line, start_line, body, body_hash, severity_hint,
                   thread_id, confidence, reason, created_at, observed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    gap_id, repo, pr_number, head_sha, gap_kind, source_label, actor_login, event_delivery_id,
                    source_object_kind, source_object_id, agent_review_id, agent_comment_id,
                    matched_posted_finding_id, path, line, start_line, body, bh, severity_hint,
                    thread_id, confidence, reason, created, now,
                ),
            )
            return cur.rowcount > 0

    def list_pr_review_posted_findings(self, repo: str, pr_number: int) -> list[PrReviewPostedFinding]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM pr_review_posted_findings WHERE repo=? AND pr_number=? ORDER BY posted_at, finding_id",
                (repo, pr_number),
            ).fetchall()
        return [_posted_finding_from_row(row) for row in rows]

    def list_pr_review_gap_events(self, repo: str | None = None, pr_number: int | None = None, limit: int = 100) -> list[PrReviewGapEvent]:
        clauses: list[str] = []
        params: list[object] = []
        if repo is not None:
            clauses.append("repo=?")
            params.append(repo)
        if pr_number is not None:
            clauses.append("pr_number=?")
            params.append(pr_number)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM pr_review_gap_events{where} ORDER BY created_at DESC LIMIT ?",
                (*params, limit),
            ).fetchall()
        return [_gap_event_from_row(row) for row in rows]

    def record_pr_review_completed_review(
        self, *, issue_key: str, repo: str, pr_number: int, head_sha: str | None,
        github_review_id: int | None, event: str,
    ) -> bool:
        submitted_at = _utcnow()
        key = _hash_parts(repo, pr_number, github_review_id or submitted_at)
        with self._lock:
            cur = self._conn.execute(
                """
                INSERT OR IGNORE INTO pr_review_completed_reviews
                  (completed_review_key, issue_key, repo, pr_number, head_sha, github_review_id, event, submitted_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (key, issue_key, repo, pr_number, head_sha, github_review_id, event, submitted_at),
            )
            return cur.rowcount > 0

    def _last_done_self_improvement_key(self) -> str | None:
        row = self._conn.execute(
            "SELECT last_completed_review_key FROM pr_review_self_improvement_runs WHERE status='done' ORDER BY finished_at DESC, started_at DESC LIMIT 1"
        ).fetchone()
        return None if row is None else row["last_completed_review_key"]

    def count_pr_reviews_since_last_self_improvement(self) -> int:
        with self._lock:
            last_key = self._last_done_self_improvement_key()
            if last_key is None:
                row = self._conn.execute("SELECT COUNT(*) AS n FROM pr_review_completed_reviews").fetchone()
            else:
                marker = self._conn.execute(
                    "SELECT submitted_at FROM pr_review_completed_reviews WHERE completed_review_key=?",
                    (last_key,),
                ).fetchone()
                submitted_at = marker["submitted_at"] if marker is not None else ""
                row = self._conn.execute(
                    "SELECT COUNT(*) AS n FROM pr_review_completed_reviews WHERE submitted_at > ?",
                    (submitted_at,),
                ).fetchone()
            return int(row["n"])

    def claim_pr_review_self_improvement_run(
        self, *, batch_size: int, scanned_since: str, scanned_until: str, model: str | None,
    ) -> tuple[str, list[Mapping[str, Any]]] | None:
        with self._txn() as conn:
            running = conn.execute("SELECT 1 FROM pr_review_self_improvement_runs WHERE status='running' LIMIT 1").fetchone()
            if running is not None:
                return None
            last_key = self._last_done_self_improvement_key()
            params: tuple[object, ...] = ()
            where = ""
            if last_key is not None:
                marker = conn.execute(
                    "SELECT submitted_at FROM pr_review_completed_reviews WHERE completed_review_key=?",
                    (last_key,),
                ).fetchone()
                where = "WHERE submitted_at > ?"
                params = ((marker["submitted_at"] if marker is not None else ""),)
            rows = conn.execute(
                f"SELECT * FROM pr_review_completed_reviews {where} ORDER BY submitted_at LIMIT ?",
                (*params, batch_size),
            ).fetchall()
            if len(rows) < batch_size:
                return None
            first_key = rows[0]["completed_review_key"]
            last_claim_key = rows[-1]["completed_review_key"]
            run_id = _hash_parts(first_key, last_claim_key, scanned_until)[:16]
            now = _utcnow()
            conn.execute(
                """
                INSERT INTO pr_review_self_improvement_runs
                  (run_id, started_at, status, trigger_count, review_count_since_last,
                   first_completed_review_key, last_completed_review_key, scanned_since, scanned_until, model)
                VALUES (?, ?, 'running', ?, ?, ?, ?, ?, ?, ?)
                """,
                (run_id, now, len(rows), len(rows), first_key, last_claim_key, scanned_since, scanned_until, model),
            )
            return run_id, [dict(row) for row in rows]

    def finish_pr_review_self_improvement_run(
        self, *, run_id: str, status: str, session_count: int, recommendation_count: int,
        files_changed: int = 0, commit_id: str | None = None, pushed_bookmark: str | None = None,
        report_path: str | None = None, quality_gate: Mapping[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        with self._lock:
            self._conn.execute(
                """
                UPDATE pr_review_self_improvement_runs
                SET finished_at=?, status=?, session_count=?, recommendation_count=?, files_changed=?,
                    commit_id=?, pushed_bookmark=?, report_path=?, quality_gate_json=?, error=?
                WHERE run_id=?
                """,
                (
                    _utcnow(), status, session_count, recommendation_count, files_changed, commit_id,
                    pushed_bookmark, report_path,
                    None if quality_gate is None else json.dumps(quality_gate, separators=(",", ":")),
                    error, run_id,
                ),
            )

    def record_pr_review_self_improvement_recommendations(self, *, run_id: str, recommendations: Sequence[Mapping[str, Any]]) -> int:
        now = _utcnow()
        inserted = 0
        with self._lock:
            for rec in recommendations:
                recommendation_id = _hash_parts(run_id, rec.get("severity"), rec.get("category"), rec.get("title"), rec.get("proposed_change"))
                cur = self._conn.execute(
                    """
                    INSERT OR IGNORE INTO pr_review_self_improvement_recommendations
                      (recommendation_id, run_id, severity, category, title, summary, evidence_json,
                       proposed_change, verification, status, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'candidate', ?)
                    """,
                    (
                        recommendation_id, run_id, rec.get("severity"), rec.get("category"), rec.get("title"),
                        rec.get("summary"), json.dumps(rec.get("evidence", []), separators=(",", ":")),
                        rec.get("proposed_change"), rec.get("verification"), now,
                    ),
                )
                inserted += int(cur.rowcount or 0)
        return inserted

    def latest_pr_review_self_improvement_run(self) -> Mapping[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM pr_review_self_improvement_runs ORDER BY started_at DESC LIMIT 1"
            ).fetchone()
        return None if row is None else dict(row)

    def list_pr_review_self_improvement_runs(self, *, limit: int = 20) -> list[Mapping[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM pr_review_self_improvement_runs ORDER BY started_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_pr_review_self_improvement_recommendations(self, *, status: str = "candidate", limit: int = 100) -> list[Mapping[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM pr_review_self_improvement_recommendations WHERE status=? ORDER BY created_at DESC LIMIT ?",
                (status, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_pr_review_issue_rows_for_self_improvement(self, *, since: str, until: str, limit: int) -> list[IssueRow]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT key, repo, number, branch, session_dir, pr_number, state, classification, updated_at
                FROM issues
                WHERE pr_number IS NOT NULL AND session_dir IS NOT NULL AND updated_at BETWEEN ? AND ?
                ORDER BY updated_at DESC LIMIT ?
                """,
                (since, until, limit),
            ).fetchall()
        return [_issue_row_from_db_row(row) for row in rows]

    def pr_review_tool_call_counts(self, issue_keys: Sequence[str]) -> dict[str, dict[str, int]]:
        keys = [key for key in issue_keys if key]
        if not keys:
            return {}
        placeholders = ",".join("?" for _ in keys)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT issue_key, tool, COUNT(*) AS n FROM tool_calls WHERE issue_key IN ({placeholders}) GROUP BY issue_key, tool",
                tuple(keys),
            ).fetchall()
        out: dict[str, dict[str, int]] = {key: {} for key in keys}
        for row in rows:
            out[row["issue_key"]][row["tool"]] = int(row["n"])
        return out


_DB_SINGLETON: Database | None = None
_DB_LOCK = threading.Lock()


def get_database(path: Path) -> Database:
    global _DB_SINGLETON
    with _DB_LOCK:
        if _DB_SINGLETON is None or _DB_SINGLETON.path != path:
            if _DB_SINGLETON is not None:
                _DB_SINGLETON.close()
            _DB_SINGLETON = Database(path)
        return _DB_SINGLETON


def close_database() -> None:
    global _DB_SINGLETON
    with _DB_LOCK:
        if _DB_SINGLETON is not None:
            _DB_SINGLETON.close()
            _DB_SINGLETON = None
