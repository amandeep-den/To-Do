#!/usr/bin/env python3
"""Taskly server: static files, email/password auth, per-user tasks.

Zero third-party dependencies. Run:

    python server.py

Then open http://127.0.0.1:8000
"""

import hashlib
import hmac
import json
import os
import queue
import re
import secrets
import sqlite3
import threading
import time
from http.cookies import SimpleCookie
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse

ROOT = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(ROOT, "taskly.db")
INTEL_PATH = os.path.join(ROOT, "task_intel.json")

PBKDF2_ITERATIONS = 210_000
SESSION_COOKIE = "taskly_session"
SESSION_TTL = 30 * 24 * 60 * 60  # 30 days
MAX_BODY = 2 * 1024 * 1024
MAX_TASKS = 500
MAX_SUBTASKS = 200
MAX_TEXT = 500
MAX_ID = 64
MAX_TEAM_NAME = 80
MAX_COMMENT = 2000
MAX_EVENTS = 40

PRIORITIES = ("Urgent", "High", "Medium", "Low")
SHIFT_REASONS = (
    "Scope Expansion",
    "Client Feedback Delay",
    "Technical Roadblock",
    "Dependency Block",
    "Resource Reallocation",
)

CATEGORIES = (
    "Data Engineering & AI",
    "DevOps & Infrastructure",
    "Finance & Compliance",
    "Marketing & Content",
    "Operations",
    "Product Design",
    "Quality Assurance",
    "Software Engineering",
)

# Keyword -> weight. Specific phrases outweigh generic words, and the first
# category in CATEGORIES wins a tie. Keep this table in sync with index.html.
CATEGORY_KEYWORDS = {
    "Data Engineering & AI": {
        "data": 2, "dataset": 3, "analytics": 3, "pipeline": 1, "etl": 3,
        "warehouse": 2, "sql": 3, "database": 2, "query": 1,
        "machine learning": 3, "ml": 2, "ai": 2, "model": 1,
        "training": 1, "dashboard": 2, "metric": 1, "report": 1,
        "python": 2, "notebook": 2, "prediction": 2, "forecast": 1,
        "statistics": 2, "spark": 2, "airflow": 2, "snowflake": 2,
        "bigquery": 2, "visualization": 1,
    },
    "DevOps & Infrastructure": {
        "deploy": 3, "deployment": 3, "server": 2, "infrastructure": 3,
        "infra": 3, "ci": 2, "cd": 1, "docker": 3, "kubernetes": 3,
        "k8s": 3, "aws": 2, "azure": 2, "gcp": 2, "cloud": 2,
        "monitoring": 2, "alerting": 1, "backup": 2, "uptime": 2,
        "outage": 2, "ssl": 2, "dns": 2, "vpn": 2, "firewall": 2,
        "security patch": 3, "migration": 1, "environment": 1, "logs": 1,
        "terraform": 3, "ansible": 2, "jenkins": 2, "load balancer": 2,
    },
    "Finance & Compliance": {
        "invoice": 3, "budget": 3, "finance": 3, "financial": 3, "tax": 3,
        "compliance": 3, "audit": 3, "legal": 3, "contract": 2,
        "payroll": 3, "expense": 3, "reimbursement": 3, "policy": 1,
        "gdpr": 3, "privacy": 2, "regulatory": 2, "procurement": 2,
        "purchase order": 2, "accounting": 3, "billing": 3, "vendor": 1,
        "risk assessment": 1,
    },
    "Marketing & Content": {
        "campaign": 3, "marketing": 3, "content": 2, "blog": 3,
        "social media": 3, "social": 1, "seo": 3, "newsletter": 3,
        "email blast": 3, "advertisement": 3, "ad campaign": 3,
        "brand": 1, "copywriting": 3, "creative": 1, "webinar": 2,
        "press release": 3, "landing page": 2, "engagement": 1,
        "audience": 1, "promotion": 2, "launch": 1,
    },
    "Operations": {
        "operations": 3, "process": 1, "workflow": 1, "schedule": 1,
        "logistics": 3, "inventory": 3, "procurement": 2, "onboarding": 2,
        "training": 1, "meeting": 1, "coordination": 1, "support": 1,
        "ticket": 1, "admin": 1, "facilities": 2, "supplier": 2,
        "shipment": 2, "order": 1, "documentation": 1, "sop": 2,
        "staffing": 2, "roster": 2,
    },
    "Product Design": {
        "design": 3, "ux": 3, "ui": 2, "wireframe": 3, "prototype": 3,
        "figma": 3, "mockup": 3, "usability": 3, "user research": 3,
        "persona": 2, "layout": 1, "visual": 1, "interaction": 1,
        "accessibility": 2, "style guide": 2, "sketch": 1,
        "product spec": 1,
    },
    "Quality Assurance": {
        "test": 2, "testing": 3, "qa": 3, "bug": 2, "defect": 3,
        "regression": 3, "validation": 2, "verify": 1, "quality": 2,
        "coverage": 1, "smoke test": 3, "test case": 3, "automation": 1,
        "release check": 1, "edge case": 1,
    },
    "Software Engineering": {
        "code": 2, "coding": 3, "develop": 1, "development": 2,
        "feature": 2, "api": 2, "endpoint": 3, "backend": 3,
        "frontend": 3, "refactor": 3, "bug fix": 2, "software": 2,
        "function": 1, "integration": 1, "github": 2, "commit": 1,
        "pull request": 3, "repository": 2, "library": 1, "framework": 1,
        "app": 1, "application": 2, "release": 2, "build": 1,
        "unit test": 1,
    },
}

CATEGORY_PATTERNS = {
    category: tuple(
        (re.compile(r"\b%s\b" % re.escape(word)), weight)
        for word, weight in words.items()
    )
    for category, words in CATEGORY_KEYWORDS.items()
}


def classify_category(text):
    """Pick the category whose keywords score highest in the task title."""
    if not isinstance(text, str):
        return "Operations"
    lowered = " " + re.sub(r"[^a-z0-9+#]+", " ", text.lower()).strip() + " "
    best = "Operations"
    best_score = 0
    for category in CATEGORIES:
        score = 0
        for pattern, weight in CATEGORY_PATTERNS.get(category, ()):
            if pattern.search(lowered):
                score += weight
        if score > best_score:
            best = category
            best_score = score
    return best

# Live updates: one queue per open EventSource connection, keyed by user id.
SUBSCRIBERS = {}
SUBSCRIBERS_LOCK = threading.Lock()

# Presence: user id -> {team_id, task_id, seen}. "seen" is refreshed by the
# client's viewing heartbeat; stale entries are ignored after PRESENCE_TTL.
PRESENCE = {}
PRESENCE_LOCK = threading.Lock()
PRESENCE_TTL = 120

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Basic in-memory login throttling: 10 failures per IP per 5 minutes.
FAILURES = {}
FAILURES_LOCK = threading.Lock()
MAX_FAILURES = 10
FAILURE_WINDOW = 300


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


# ---------------------------------------------------------------- database


def connect():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def ensure_columns(conn):
    """Add newer task columns to databases created by older versions."""
    for table in ("tasks", "team_tasks"):
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(%s)" % table)}
        if "priority" not in columns:
            conn.execute(
                "ALTER TABLE %s ADD COLUMN priority TEXT NOT NULL DEFAULT 'Medium'" % table
            )
        if "reason" not in columns:
            conn.execute("ALTER TABLE %s ADD COLUMN reason TEXT" % table)
        if "category" not in columns:
            conn.execute(
                "ALTER TABLE %s ADD COLUMN category TEXT NOT NULL DEFAULT ''" % table
            )
        # Backfill categories for rows written before categories existed.
        rows = conn.execute(
            "SELECT id, text FROM %s WHERE category IS NULL OR category = ''" % table
        ).fetchall()
        for row in rows:
            conn.execute(
                "UPDATE %s SET category = ? WHERE id = ?" % table,
                (classify_category(row["text"]), row["id"]),
            )


def init_db():
    conn = connect()
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                email         TEXT NOT NULL UNIQUE COLLATE NOCASE,
                salt          TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                created_at    INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS sessions (
                token_hash TEXT PRIMARY KEY,
                user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                created_at INTEGER NOT NULL,
                expires_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS tasks (
                row_id     INTEGER PRIMARY KEY AUTOINCREMENT,
                id         TEXT NOT NULL,
                user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                parent_id  TEXT,
                text       TEXT NOT NULL,
                done       INTEGER NOT NULL DEFAULT 0,
                due        TEXT,
                priority   TEXT NOT NULL DEFAULT 'Medium',
                reason     TEXT,
                category   TEXT NOT NULL DEFAULT '',
                position   INTEGER NOT NULL DEFAULT 0,
                created_at INTEGER NOT NULL,
                UNIQUE (user_id, id)
            );

            CREATE INDEX IF NOT EXISTS idx_tasks_user ON tasks(user_id);

            CREATE TABLE IF NOT EXISTS teams (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                name       TEXT NOT NULL,
                owner_id   INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                created_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS team_members (
                team_id    INTEGER NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
                user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                role       TEXT NOT NULL DEFAULT 'member',
                status     TEXT NOT NULL DEFAULT 'active',
                invited_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
                created_at INTEGER NOT NULL,
                PRIMARY KEY (team_id, user_id)
            );

            CREATE TABLE IF NOT EXISTS team_tasks (
                row_id      INTEGER PRIMARY KEY AUTOINCREMENT,
                id          TEXT NOT NULL,
                team_id     INTEGER NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
                text        TEXT NOT NULL,
                done        INTEGER NOT NULL DEFAULT 0,
                due         TEXT,
                assignee_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                created_by  INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                priority    TEXT NOT NULL DEFAULT 'Medium',
                reason      TEXT,
                category    TEXT NOT NULL DEFAULT '',
                position    INTEGER NOT NULL DEFAULT 0,
                created_at  INTEGER NOT NULL,
                updated_at  INTEGER NOT NULL,
                UNIQUE (team_id, id)
            );

            CREATE TABLE IF NOT EXISTS team_task_events (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                team_id    INTEGER NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
                task_id    TEXT,
                user_id    INTEGER REFERENCES users(id) ON DELETE SET NULL,
                type       TEXT NOT NULL,
                detail     TEXT NOT NULL,
                created_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS team_task_comments (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                team_id    INTEGER NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
                task_id    TEXT NOT NULL,
                user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                parent_id  INTEGER REFERENCES team_task_comments(id) ON DELETE CASCADE,
                body       TEXT NOT NULL,
                created_at INTEGER NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_team_members_user ON team_members(user_id);
            CREATE INDEX IF NOT EXISTS idx_team_tasks_team ON team_tasks(team_id);
            CREATE INDEX IF NOT EXISTS idx_team_events_team ON team_task_events(team_id);
            CREATE INDEX IF NOT EXISTS idx_team_comments_task ON team_task_comments(team_id, task_id);
            """
        )
        ensure_columns(conn)
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------- helpers


def hash_password(password, salt_hex):
    return hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        bytes.fromhex(salt_hex),
        PBKDF2_ITERATIONS,
    ).hex()


def token_hash(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def normalize_email(email):
    return email.strip().lower() if isinstance(email, str) else ""


def failure_key(ip):
    return ip or "unknown"


def record_failure(ip):
    now = time.time()
    key = failure_key(ip)
    with FAILURES_LOCK:
        count, reset = FAILURES.get(key, (0, now + FAILURE_WINDOW))
        if now > reset:
            count, reset = 0, now + FAILURE_WINDOW
        FAILURES[key] = (count + 1, reset)


def clear_failures(ip):
    with FAILURES_LOCK:
        FAILURES.pop(failure_key(ip), None)


def too_many_failures(ip):
    now = time.time()
    key = failure_key(ip)
    with FAILURES_LOCK:
        count, reset = FAILURES.get(key, (0, 0))
        if now > reset:
            FAILURES.pop(key, None)
            return False
        return count >= MAX_FAILURES


# ---------------------------------------------------------------- intel

INTEL = None
INTEL_LOCK = threading.Lock()


def load_intel():
    """Load the precomputed forecast model once; False means unavailable."""
    global INTEL
    if INTEL is not None:
        return INTEL
    with INTEL_LOCK:
        if INTEL is None:
            try:
                with open(INTEL_PATH, "r", encoding="utf-8") as handle:
                    INTEL = json.load(handle)
            except (OSError, ValueError):
                INTEL = False
    return INTEL


# ---------------------------------------------------------------- tasks


def clean_priority(value):
    if value in (None, ""):
        return "Medium"
    if not isinstance(value, str) or value not in PRIORITIES:
        raise ApiError(400, "Priority must be Urgent, High, Medium, or Low")
    return value


def clean_reason(value):
    if value in (None, "", "None"):
        return None
    if not isinstance(value, str) or value not in SHIFT_REASONS:
        raise ApiError(400, "Unknown shift reason")
    return value


def clean_tasks(payload):
    if not isinstance(payload, list):
        raise ApiError(400, "tasks must be a list")
    if len(payload) > MAX_TASKS:
        raise ApiError(400, "Too many tasks")

    cleaned = []
    seen = set()
    total = 0

    for task in payload:
        if not isinstance(task, dict):
            raise ApiError(400, "Invalid task")
        task_id = task.get("id")
        text = task.get("text")
        if not isinstance(task_id, str) or not (1 <= len(task_id) <= MAX_ID):
            raise ApiError(400, "Invalid task id")
        if task_id in seen:
            raise ApiError(400, "Duplicate task id")
        if not isinstance(text, str) or not (1 <= len(text.strip()) <= MAX_TEXT):
            raise ApiError(400, "Task text must be 1-500 characters")
        seen.add(task_id)

        due = task.get("due")
        if due is not None and (not isinstance(due, str) or not DATE_RE.match(due)):
            raise ApiError(400, "Invalid deadline")

        priority = clean_priority(task.get("priority"))
        reason = clean_reason(task.get("reason"))
        category = classify_category(text.strip())

        created = task.get("createdAt")
        if not isinstance(created, (int, float)):
            created = int(time.time() * 1000)

        subtasks = task.get("subtasks") or []
        if not isinstance(subtasks, list):
            raise ApiError(400, "Invalid subtasks")
        if len(subtasks) > MAX_SUBTASKS:
            raise ApiError(400, "Too many subtasks")

        clean_subs = []
        for sub in subtasks:
            if not isinstance(sub, dict):
                raise ApiError(400, "Invalid subtask")
            sub_id = sub.get("id")
            sub_text = sub.get("text")
            if not isinstance(sub_id, str) or not (1 <= len(sub_id) <= MAX_ID):
                raise ApiError(400, "Invalid subtask id")
            if sub_id in seen:
                raise ApiError(400, "Duplicate subtask id")
            if not isinstance(sub_text, str) or not (1 <= len(sub_text.strip()) <= MAX_TEXT):
                raise ApiError(400, "Subtask text must be 1-500 characters")
            seen.add(sub_id)
            clean_subs.append(
                {"id": sub_id, "text": sub_text.strip(), "done": bool(sub.get("done"))}
            )

        cleaned.append(
            {
                "id": task_id,
                "text": text.strip(),
                "done": bool(task.get("done")),
                "due": due,
                "priority": priority,
                "reason": reason,
                "category": category,
                "createdAt": int(created),
                "subtasks": clean_subs,
            }
        )
        total += 1 + len(clean_subs)
        if total > 2000:
            raise ApiError(400, "Too many items")

    return cleaned


def save_tasks(conn, user_id, cleaned):
    conn.execute("DELETE FROM tasks WHERE user_id = ?", (user_id,))
    for index, task in enumerate(cleaned):
        conn.execute(
            """
            INSERT INTO tasks (id, user_id, parent_id, text, done, due, priority, reason, category, position, created_at)
            VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                task["id"],
                user_id,
                task["text"],
                1 if task["done"] else 0,
                task["due"],
                task["priority"],
                task["reason"],
                task["category"],
                index,
                task["createdAt"],
            ),
        )
        for sub_index, sub in enumerate(task["subtasks"]):
            conn.execute(
                """
                INSERT INTO tasks (id, user_id, parent_id, text, done, due, priority, reason, category, position, created_at)
                VALUES (?, ?, ?, ?, ?, NULL, ?, NULL, ?, ?, ?)
                """,
                (
                    sub["id"],
                    user_id,
                    task["id"],
                    sub["text"],
                    1 if sub["done"] else 0,
                    task["priority"],
                    task["category"],
                    sub_index,
                    task["createdAt"],
                ),
            )
    conn.commit()


def load_tasks(conn, user_id):
    rows = conn.execute(
        "SELECT * FROM tasks WHERE user_id = ? ORDER BY position ASC", (user_id,)
    ).fetchall()

    parents = {}
    ordered = []
    for row in rows:
        if row["parent_id"] is None:
            task = {
                "id": row["id"],
                "text": row["text"],
                "done": bool(row["done"]),
                "due": row["due"],
                "priority": row["priority"] or "Medium",
                "reason": row["reason"],
                "category": row["category"] or classify_category(row["text"]),
                "createdAt": row["created_at"],
                "subtasks": [],
            }
            parents[row["id"]] = task
            ordered.append((row["position"], task))

    for row in rows:
        if row["parent_id"] is not None:
            parent = parents.get(row["parent_id"])
            if parent is not None:
                parent["subtasks"].append(
                    {"id": row["id"], "text": row["text"], "done": bool(row["done"])}
                )

    ordered.sort(key=lambda pair: pair[0])
    return [task for _, task in ordered]


# ---------------------------------------------------------------- live updates


def subscribe(user_id):
    q = queue.Queue()
    with SUBSCRIBERS_LOCK:
        SUBSCRIBERS.setdefault(user_id, set()).add(q)
    return q


def unsubscribe(user_id, q):
    with SUBSCRIBERS_LOCK:
        listeners = SUBSCRIBERS.get(user_id)
        if listeners is not None:
            listeners.discard(q)
            if not listeners:
                SUBSCRIBERS.pop(user_id, None)


def publish(user_ids, payload):
    data = dict(payload)
    with SUBSCRIBERS_LOCK:
        for user_id in user_ids:
            for q in list(SUBSCRIBERS.get(user_id, ())):
                if q.qsize() < 100:
                    q.put_nowait(data)


def team_member_ids(conn, team_id, active_only=True):
    sql = "SELECT user_id FROM team_members WHERE team_id = ?"
    if active_only:
        sql += " AND status = 'active'"
    return [row["user_id"] for row in conn.execute(sql, (team_id,)).fetchall()]


def publish_team(conn, team_id, payload, extra_user_ids=()):
    ids = set(team_member_ids(conn, team_id))
    ids.update(extra_user_ids)
    publish(ids, payload)


def set_presence(user_id, team_id, task_id=None):
    """Record presence and report whether anything visible actually changed."""
    with PRESENCE_LOCK:
        previous = PRESENCE.get(user_id)
        PRESENCE[user_id] = {
            "team_id": team_id,
            "task_id": task_id,
            "seen": int(time.time()),
        }
        return (
            previous is None
            or previous["team_id"] != team_id
            or previous["task_id"] != task_id
        )


def clear_presence(user_id):
    """Drop presence and return the team the user was last viewing, if any."""
    with PRESENCE_LOCK:
        info = PRESENCE.pop(user_id, None)
        return info["team_id"] if info else None


def presence_snapshot(team_id):
    now = int(time.time())
    snapshot = {}
    with PRESENCE_LOCK:
        for user_id, info in list(PRESENCE.items()):
            if info["seen"] < now - PRESENCE_TTL:
                PRESENCE.pop(user_id, None)
                continue
            if info["team_id"] == team_id:
                snapshot[user_id] = {"task_id": info["task_id"]}
    return snapshot


def presence_payload(team_id, user, online, task_id=None):
    return {
        "type": "team.presence",
        "team_id": team_id,
        "user_id": user["id"],
        "email": user["email"],
        "online": bool(online),
        "task_id": task_id if online else None,
    }



# ---------------------------------------------------------------- teams


def team_membership(conn, team_id, user_id):
    return conn.execute(
        "SELECT team_id, user_id, role, status FROM team_members WHERE team_id = ? AND user_id = ?",
        (team_id, user_id),
    ).fetchone()


def require_team(conn, team_id, user_id, allow_pending=False):
    team = conn.execute(
        "SELECT id, name, owner_id, created_at FROM teams WHERE id = ?", (team_id,)
    ).fetchone()
    if team is None:
        raise ApiError(404, "Team not found")

    membership = team_membership(conn, team_id, user_id)
    if membership is None:
        raise ApiError(404, "Team not found")
    if membership["status"] != "active" and not allow_pending:
        raise ApiError(403, "Accept your invitation before opening this team")
    return team, membership


def log_event(conn, team_id, task_id, user_id, kind, detail):
    conn.execute(
        """
        INSERT INTO team_task_events (team_id, task_id, user_id, type, detail, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (team_id, task_id, user_id, kind, detail, int(time.time())),
    )


TEAM_TASK_SELECT = """
SELECT tt.*, assignee.email AS assignee_email, creator.email AS created_by_email,
       (SELECT COUNT(*) FROM team_task_comments cm
        WHERE cm.team_id = tt.team_id AND cm.task_id = tt.id) AS comment_count,
       (SELECT COALESCE(MAX(cm.id), 0) FROM team_task_comments cm
        WHERE cm.team_id = tt.team_id AND cm.task_id = tt.id) AS last_comment_id
FROM team_tasks tt
LEFT JOIN users assignee ON assignee.id = tt.assignee_id
JOIN users creator ON creator.id = tt.created_by
"""


def team_task_row(conn, team_id, task_id):
    return conn.execute(
        TEAM_TASK_SELECT + " WHERE tt.team_id = ? AND tt.id = ?", (team_id, task_id)
    ).fetchone()


def team_task_dict(row):
    return {
        "id": row["id"],
        "text": row["text"],
        "done": bool(row["done"]),
        "due": row["due"],
        "assignee_id": row["assignee_id"],
        "assignee_email": row["assignee_email"],
        "created_by_email": row["created_by_email"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "priority": row["priority"] or "Medium",
        "reason": row["reason"],
        "category": row["category"] or classify_category(row["text"]),
        "comment_count": row["comment_count"],
        "last_comment_id": row["last_comment_id"],
    }


def load_team_tasks(conn, team_id):
    rows = conn.execute(
        TEAM_TASK_SELECT + " WHERE tt.team_id = ? ORDER BY tt.created_at DESC", (team_id,)
    ).fetchall()
    return [team_task_dict(row) for row in rows]


def load_team_members(conn, team_id):
    rows = conn.execute(
        """
        SELECT m.user_id, u.email, m.role, m.status
        FROM team_members m JOIN users u ON u.id = m.user_id
        WHERE m.team_id = ?
        ORDER BY CASE m.role WHEN 'owner' THEN 0 ELSE 1 END, u.email
        """,
        (team_id,),
    ).fetchall()
    return [
        {
            "id": row["user_id"],
            "email": row["email"],
            "role": row["role"],
            "status": row["status"],
        }
        for row in rows
    ]


def load_team_events(conn, team_id, task_id=None, limit=MAX_EVENTS):
    sql = """
        SELECT e.id, e.task_id, e.type, e.detail, e.created_at, u.email AS actor
        FROM team_task_events e LEFT JOIN users u ON u.id = e.user_id
        WHERE e.team_id = ?
    """
    params = [team_id]
    if task_id is not None:
        sql += " AND e.task_id = ?"
        params.append(task_id)
    sql += " ORDER BY e.id DESC LIMIT ?"
    params.append(limit)
    return [
        {
            "id": row["id"],
            "task_id": row["task_id"],
            "type": row["type"],
            "detail": row["detail"],
            "created_at": row["created_at"],
            "actor": row["actor"],
        }
        for row in conn.execute(sql, params).fetchall()
    ]


def load_thread_comments(conn, team_id, task_id):
    rows = conn.execute(
        """
        SELECT c.id, c.parent_id, c.body, c.created_at, u.email AS author
        FROM team_task_comments c JOIN users u ON u.id = c.user_id
        WHERE c.team_id = ? AND c.task_id = ?
        ORDER BY c.id ASC
        """,
        (team_id, task_id),
    ).fetchall()
    return [dict(row) for row in rows]


def clean_team_name(value):
    if not isinstance(value, str):
        raise ApiError(400, "Team name is required")
    name = " ".join(value.split())
    if not (1 <= len(name) <= MAX_TEAM_NAME):
        raise ApiError(400, "Team name must be 1-80 characters")
    return name


def clean_due(value):
    if value in (None, ""):
        return None
    if not isinstance(value, str) or not DATE_RE.match(value):
        raise ApiError(400, "Invalid deadline")
    return value


def active_member_by_email(conn, team_id, email):
    row = conn.execute(
        """
        SELECT m.user_id, u.email FROM team_members m JOIN users u ON u.id = m.user_id
        WHERE m.team_id = ? AND m.status = 'active' AND u.email = ?
        """,
        (team_id, email),
    ).fetchone()
    return row


def clean_assignee(conn, team_id, value):
    """Accept an email (or empty/None) and return the member's user id."""
    email = normalize_email(value)
    if not email:
        return None
    member = active_member_by_email(conn, team_id, email)
    if member is None:
        raise ApiError(400, "Assign tasks to an active team member")
    return member["user_id"]


# ---------------------------------------------------------------- handler


class Handler(SimpleHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=ROOT, **kwargs)

    # ---- plumbing

    def log_message(self, fmt, *args):
        print("%s - %s" % (self.address_string(), fmt % args))

    def end_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
            "connect-src 'self'; base-uri 'none'; form-action 'self'",
        )
        if self.path.endswith(".html") or self.path == "/":
            self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def send_json(self, status, payload, cookie=None):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if cookie is not None:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(body)

    def read_json(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise ApiError(400, "Invalid request")
        if length <= 0:
            return {}
        if length > MAX_BODY:
            raise ApiError(413, "Request too large")
        if "application/json" not in (self.headers.get("Content-Type") or ""):
            raise ApiError(415, "Expected JSON")
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            raise ApiError(400, "Invalid JSON")
        if not isinstance(data, dict):
            raise ApiError(400, "Invalid request")
        return data

    def cookie_token(self):
        raw = self.headers.get("Cookie")
        if not raw:
            return None
        jar = SimpleCookie()
        try:
            jar.load(raw)
        except Exception:
            return None
        morsel = jar.get(SESSION_COOKIE)
        return morsel.value if morsel else None

    def current_user(self, conn):
        token = self.cookie_token()
        if not token:
            return None
        digest = token_hash(token)
        row = conn.execute(
            """
            SELECT u.id, u.email, s.expires_at
            FROM sessions s JOIN users u ON u.id = s.user_id
            WHERE s.token_hash = ?
            """,
            (digest,),
        ).fetchone()
        if row is None:
            return None
        if row["expires_at"] < int(time.time()):
            conn.execute("DELETE FROM sessions WHERE token_hash = ?", (digest,))
            conn.commit()
            return None
        return {"id": row["id"], "email": row["email"]}

    def require_user(self, conn):
        user = self.current_user(conn)
        if user is None:
            raise ApiError(401, "Please sign in")
        return user

    def start_session(self, conn, user_id):
        token = secrets.token_urlsafe(32)
        now = int(time.time())
        conn.execute(
            "INSERT INTO sessions (token_hash, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
            (token_hash(token), user_id, now, now + SESSION_TTL),
        )
        # Opportunistic cleanup of expired sessions.
        conn.execute("DELETE FROM sessions WHERE expires_at < ?", (now,))
        conn.commit()
        return "%s=%s; Path=/; HttpOnly; SameSite=Lax; Max-Age=%d" % (
            SESSION_COOKIE,
            token,
            SESSION_TTL,
        )

    # ---- routing

    @staticmethod
    def api_segments(path):
        if not path.startswith("/api/"):
            return None
        return [unquote(part) for part in path.strip("/").split("/") if part]

    @staticmethod
    def parse_id(value):
        try:
            number = int(value)
        except (TypeError, ValueError):
            raise ApiError(404, "Not found")
        if number <= 0:
            raise ApiError(404, "Not found")
        return number

    def do_GET(self):
        path = urlparse(self.path).path
        parts = self.api_segments(path)
        try:
            if parts is not None:
                if parts == ["api", "me"]:
                    return self.api_me()
                if parts == ["api", "tasks"]:
                    return self.api_get_tasks()
                if parts == ["api", "intel"]:
                    return self.api_get_intel()
                if parts == ["api", "events"]:
                    return self.api_events()
                if parts == ["api", "teams"]:
                    return self.api_get_teams()
                if len(parts) == 3 and parts[:2] == ["api", "teams"]:
                    return self.api_get_team(self.parse_id(parts[2]))
                if (
                    len(parts) == 6
                    and parts[:2] == ["api", "teams"]
                    and parts[3] == "tasks"
                    and parts[5] == "thread"
                ):
                    return self.api_get_thread(
                        self.parse_id(parts[2]), parts[4]
                    )
                return self.send_json(404, {"error": "Not found"})
        except ApiError as err:
            return self.send_json(err.status, {"error": err.message})
        if not self.is_public_file(path):
            return self.send_json(404, {"error": "Not found"})
        return super().do_GET()

    @staticmethod
    def is_public_file(path):
        """Serve the app and its assets only — never the database or source."""
        if ".." in path or path.startswith("/."):
            return False
        blocked = (".py", ".db", ".sqlite", ".sqlite3", ".log", ".md", ".bak", ".json")
        return not path.lower().endswith(blocked)

    def do_POST(self):
        path = urlparse(self.path).path
        parts = self.api_segments(path)
        try:
            if parts == ["api", "register"]:
                return self.api_register()
            if parts == ["api", "login"]:
                return self.api_login()
            if parts == ["api", "logout"]:
                return self.api_logout()
            if parts == ["api", "teams"]:
                return self.api_create_team()
            if parts is not None and len(parts) == 4 and parts[:2] == ["api", "teams"]:
                team_id = self.parse_id(parts[2])
                if parts[3] == "invite":
                    return self.api_invite(team_id)
                if parts[3] == "leave":
                    return self.api_leave_team(team_id)
                if parts[3] == "viewing":
                    return self.api_set_viewing(team_id)
            if (
                parts is not None
                and len(parts) == 5
                and parts[:2] == ["api", "teams"]
                and parts[3] == "invitations"
            ):
                team_id = self.parse_id(parts[2])
                if parts[4] == "accept":
                    return self.api_respond_invite(team_id, True)
                if parts[4] == "decline":
                    return self.api_respond_invite(team_id, False)
            if (
                parts is not None
                and len(parts) == 4
                and parts[:2] == ["api", "teams"]
                and parts[3] == "tasks"
            ):
                return self.api_create_team_task(self.parse_id(parts[2]))
            if (
                parts is not None
                and len(parts) == 6
                and parts[:2] == ["api", "teams"]
                and parts[3] == "tasks"
                and parts[5] == "comments"
            ):
                return self.api_add_comment(self.parse_id(parts[2]), parts[4])
        except ApiError as err:
            return self.send_json(err.status, {"error": err.message})
        return self.send_json(404, {"error": "Not found"})

    def do_PUT(self):
        path = urlparse(self.path).path
        parts = self.api_segments(path)
        try:
            if parts == ["api", "tasks"]:
                return self.api_put_tasks()
            if (
                parts is not None
                and len(parts) == 5
                and parts[:2] == ["api", "teams"]
                and parts[3] == "tasks"
            ):
                return self.api_update_team_task(self.parse_id(parts[2]), parts[4])
        except ApiError as err:
            return self.send_json(err.status, {"error": err.message})
        return self.send_json(404, {"error": "Not found"})

    def do_DELETE(self):
        path = urlparse(self.path).path
        parts = self.api_segments(path)
        try:
            if parts is not None and len(parts) == 5 and parts[:2] == ["api", "teams"]:
                team_id = self.parse_id(parts[2])
                if parts[3] == "tasks":
                    return self.api_delete_team_task(team_id, parts[4])
                if parts[3] == "members":
                    return self.api_remove_member(team_id, self.parse_id(parts[4]))
        except ApiError as err:
            return self.send_json(err.status, {"error": err.message})
        return self.send_json(404, {"error": "Not found"})

    # ---- api

    def api_me(self):
        conn = connect()
        try:
            user = self.current_user(conn)
            if user is None:
                raise ApiError(401, "Please sign in")
            return self.send_json(200, {"user": user})
        finally:
            conn.close()

    def api_register(self):
        data = self.read_json()
        email = normalize_email(data.get("email"))
        password = data.get("password")

        if not EMAIL_RE.match(email) or len(email) > 254:
            raise ApiError(400, "Enter a valid email address.")
        if not isinstance(password, str) or not (8 <= len(password) <= 200):
            raise ApiError(400, "Password must be at least 8 characters.")

        salt = secrets.token_hex(16)
        password_hash = hash_password(password, salt)

        conn = connect()
        try:
            try:
                cursor = conn.execute(
                    "INSERT INTO users (email, salt, password_hash, created_at) VALUES (?, ?, ?, ?)",
                    (email, salt, password_hash, int(time.time())),
                )
            except sqlite3.IntegrityError:
                raise ApiError(409, "An account with that email already exists.")
            user_id = cursor.lastrowid
            cookie = self.start_session(conn, user_id)
            return self.send_json(
                201, {"user": {"id": user_id, "email": email}}, cookie=cookie
            )
        finally:
            conn.close()

    def api_login(self):
        ip = self.client_address[0] if self.client_address else "unknown"
        if too_many_failures(ip):
            raise ApiError(429, "Too many attempts. Try again in a few minutes.")

        data = self.read_json()
        email = normalize_email(data.get("email"))
        password = data.get("password")

        if not email or not isinstance(password, str):
            raise ApiError(400, "Email and password are required.")

        conn = connect()
        try:
            row = conn.execute(
                "SELECT id, email, salt, password_hash FROM users WHERE email = ?",
                (email,),
            ).fetchone()

            if row is None:
                # Spend comparable time on unknown accounts.
                hash_password(password, secrets.token_hex(16))
                record_failure(ip)
                raise ApiError(401, "Incorrect email or password.")

            candidate = hash_password(password, row["salt"])
            if not hmac.compare_digest(candidate, row["password_hash"]):
                record_failure(ip)
                raise ApiError(401, "Incorrect email or password.")

            clear_failures(ip)
            cookie = self.start_session(conn, row["id"])
            return self.send_json(
                200, {"user": {"id": row["id"], "email": row["email"]}}, cookie=cookie
            )
        finally:
            conn.close()

    def api_logout(self):
        token = self.cookie_token()
        if token:
            conn = connect()
            try:
                row = conn.execute(
                    "SELECT user_id FROM sessions WHERE token_hash = ?",
                    (token_hash(token),),
                ).fetchone()
                conn.execute(
                    "DELETE FROM sessions WHERE token_hash = ?", (token_hash(token),)
                )
                conn.commit()
                if row is not None:
                    team_id = clear_presence(row["user_id"])
                    if team_id is not None:
                        email = conn.execute(
                            "SELECT email FROM users WHERE id = ?", (row["user_id"],)
                        ).fetchone()
                        if email is not None:
                            publish_team(
                                conn,
                                team_id,
                                presence_payload(
                                    team_id,
                                    {"id": row["user_id"], "email": email["email"]},
                                    False,
                                ),
                            )
            finally:
                conn.close()
        clear = "%s=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0" % SESSION_COOKIE
        return self.send_json(200, {"ok": True}, cookie=clear)

    def api_get_tasks(self):
        conn = connect()
        try:
            user = self.require_user(conn)
            return self.send_json(200, {"tasks": load_tasks(conn, user["id"])})
        finally:
            conn.close()

    def api_get_intel(self):
        conn = connect()
        try:
            self.require_user(conn)
        finally:
            conn.close()
        data = load_intel()
        if not data:
            raise ApiError(503, "Forecast data is not available")
        return self.send_json(200, data)

    def api_put_tasks(self):
        conn = connect()
        try:
            user = self.require_user(conn)
            data = self.read_json()
            cleaned = clean_tasks(data.get("tasks"))
            save_tasks(conn, user["id"], cleaned)
            count = len(cleaned) + sum(len(t["subtasks"]) for t in cleaned)
            return self.send_json(200, {"ok": True, "count": count})
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


    # ---- teams

    def api_get_teams(self):
        conn = connect()
        try:
            user = self.require_user(conn)
            rows = conn.execute(
                """
                SELECT t.id, t.name, t.owner_id, owner.email AS owner_email, m.role,
                       (SELECT COUNT(*) FROM team_members WHERE team_id = t.id AND status = 'active') AS member_count,
                       (SELECT COUNT(*) FROM team_members WHERE team_id = t.id AND status = 'pending') AS pending_count,
                       (SELECT COUNT(*) FROM team_tasks WHERE team_id = t.id) AS task_count,
                       (SELECT COUNT(*) FROM team_tasks WHERE team_id = t.id AND done = 0) AS open_count,
                       (SELECT COALESCE(MAX(c.id), 0) FROM team_task_comments c WHERE c.team_id = t.id) AS last_comment_id
                FROM teams t
                JOIN team_members m ON m.team_id = t.id
                JOIN users owner ON owner.id = t.owner_id
                WHERE m.user_id = ? AND m.status = 'active'
                ORDER BY t.created_at DESC, t.id DESC
                """,
                (user["id"],),
            ).fetchall()

            invitations = conn.execute(
                """
                SELECT t.id AS team_id, t.name AS team_name, inviter.email AS invited_by_email,
                       m.created_at
                FROM team_members m
                JOIN teams t ON t.id = m.team_id
                LEFT JOIN users inviter ON inviter.id = m.invited_by
                WHERE m.user_id = ? AND m.status = 'pending'
                ORDER BY m.created_at DESC
                """,
                (user["id"],),
            ).fetchall()

            return self.send_json(
                200,
                {
                    "teams": [dict(row) for row in rows],
                    "invitations": [dict(row) for row in invitations],
                },
            )
        finally:
            conn.close()

    def api_create_team(self):
        data = self.read_json()
        name = clean_team_name(data.get("name"))

        conn = connect()
        try:
            user = self.require_user(conn)
            now = int(time.time())
            cursor = conn.execute(
                "INSERT INTO teams (name, owner_id, created_at) VALUES (?, ?, ?)",
                (name, user["id"], now),
            )
            team_id = cursor.lastrowid
            conn.execute(
                """
                INSERT INTO team_members (team_id, user_id, role, status, invited_by, created_at)
                VALUES (?, ?, 'owner', 'active', ?, ?)
                """,
                (team_id, user["id"], user["id"], now),
            )
            log_event(conn, team_id, None, user["id"], "team_created", "created the team")
            conn.commit()
            publish_team(
                conn,
                team_id,
                {
                    "type": "team.changed",
                    "team_id": team_id,
                    "task_id": None,
                    "kind": "team_created",
                    "actor": user["email"],
                    "subject": name,
                },
            )
            return self.send_json(
                201,
                {
                    "team": {
                        "id": team_id,
                        "name": name,
                        "owner_id": user["id"],
                        "role": "owner",
                        "created_at": now,
                    }
                },
            )
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def api_get_team(self, team_id):
        conn = connect()
        try:
            user = self.require_user(conn)
            team, membership = require_team(conn, team_id, user["id"])
            members = load_team_members(conn, team_id)
            presence = presence_snapshot(team_id)
            for member in members:
                info = presence.get(member["id"])
                member["online"] = info is not None
                member["viewing_task_id"] = info["task_id"] if info else None
            return self.send_json(
                200,
                {
                    "team": {
                        "id": team["id"],
                        "name": team["name"],
                        "owner_id": team["owner_id"],
                        "role": membership["role"],
                        "created_at": team["created_at"],
                    },
                    "members": members,
                    "tasks": load_team_tasks(conn, team_id),
                    "events": load_team_events(conn, team_id),
                },
            )
        finally:
            conn.close()

    def api_set_viewing(self, team_id):
        conn = connect()
        try:
            user = self.require_user(conn)
            require_team(conn, team_id, user["id"])
            data = self.read_json()
            active = data.get("active", True) is not False

            if not active:
                was_team = clear_presence(user["id"])
                if was_team is not None:
                    publish_team(
                        conn, was_team, presence_payload(was_team, user, False)
                    )
                return self.send_json(200, {"ok": True, "online": False})

            task_id = data.get("task_id")
            if task_id in (None, ""):
                task_id = None
            elif not isinstance(task_id, str) or len(task_id) > MAX_ID:
                raise ApiError(400, "Invalid task")
            elif conn.execute(
                "SELECT id FROM team_tasks WHERE team_id = ? AND id = ?",
                (team_id, task_id),
            ).fetchone() is None:
                task_id = None

            if set_presence(user["id"], team_id, task_id):
                publish_team(conn, team_id, presence_payload(team_id, user, True, task_id))
            return self.send_json(
                200, {"ok": True, "online": True, "task_id": task_id}
            )
        finally:
            conn.close()

    def api_invite(self, team_id):
        conn = connect()
        try:
            user = self.require_user(conn)
            team, membership = require_team(conn, team_id, user["id"])
            if membership["role"] != "owner":
                raise ApiError(403, "Only the team owner can invite people")

            data = self.read_json()
            email = normalize_email(data.get("email"))
            if not EMAIL_RE.match(email) or len(email) > 254:
                raise ApiError(400, "Enter a valid email address")

            target = conn.execute(
                "SELECT id, email FROM users WHERE email = ?", (email,)
            ).fetchone()
            if target is None:
                raise ApiError(
                    404, "No Taskly account uses that email. Ask them to sign up first."
                )
            if target["id"] == user["id"]:
                raise ApiError(400, "You're already on this team")

            existing = team_membership(conn, team_id, target["id"])
            if existing is not None:
                if existing["status"] == "active":
                    raise ApiError(409, "That person is already a member")
                raise ApiError(409, "That person has already been invited")

            now = int(time.time())
            conn.execute(
                """
                INSERT INTO team_members (team_id, user_id, role, status, invited_by, created_at)
                VALUES (?, ?, 'member', 'pending', ?, ?)
                """,
                (team_id, target["id"], user["id"], now),
            )
            log_event(
                conn,
                team_id,
                None,
                user["id"],
                "member_invited",
                "invited %s" % target["email"],
            )
            conn.commit()
            publish_team(
                conn,
                team_id,
                {
                    "type": "team.changed",
                    "team_id": team_id,
                    "task_id": None,
                    "kind": "member_invited",
                    "actor": user["email"],
                    "subject": target["email"],
                },
                extra_user_ids=[target["id"]],
            )
            return self.send_json(
                200,
                {
                    "member": {
                        "id": target["id"],
                        "email": target["email"],
                        "role": "member",
                        "status": "pending",
                    }
                },
            )
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def api_respond_invite(self, team_id, accept):
        conn = connect()
        try:
            user = self.require_user(conn)
            membership = team_membership(conn, team_id, user["id"])
            if membership is None:
                raise ApiError(404, "Invitation not found")
            if membership["status"] != "pending":
                raise ApiError(400, "That invitation is no longer pending")

            team = conn.execute(
                "SELECT id, name FROM teams WHERE id = ?", (team_id,)
            ).fetchone()
            if team is None:
                raise ApiError(404, "Team not found")

            if accept:
                conn.execute(
                    "UPDATE team_members SET status = 'active' WHERE team_id = ? AND user_id = ?",
                    (team_id, user["id"]),
                )
                log_event(
                    conn, team_id, None, user["id"], "member_joined", "joined the team"
                )
            else:
                conn.execute(
                    "DELETE FROM team_members WHERE team_id = ? AND user_id = ?",
                    (team_id, user["id"]),
                )
                log_event(
                    conn,
                    team_id,
                    None,
                    user["id"],
                    "member_declined",
                    "declined the invitation",
                )
            conn.commit()
            publish_team(
                conn,
                team_id,
                {
                    "type": "team.changed",
                    "team_id": team_id,
                    "task_id": None,
                    "kind": "member_joined" if accept else "member_declined",
                    "actor": user["email"],
                    "subject": user["email"],
                },
                extra_user_ids=[user["id"]],
            )
            return self.send_json(200, {"ok": True, "accepted": accept})
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def api_leave_team(self, team_id):
        conn = connect()
        try:
            user = self.require_user(conn)
            team, membership = require_team(conn, team_id, user["id"])
            if membership["role"] == "owner":
                raise ApiError(400, "Owners can't leave their own team")

            conn.execute(
                "DELETE FROM team_members WHERE team_id = ? AND user_id = ?",
                (team_id, user["id"]),
            )
            clear_presence(user["id"])
            log_event(conn, team_id, None, user["id"], "member_left", "left the team")
            conn.commit()
            publish_team(
                conn,
                team_id,
                {
                    "type": "team.changed",
                    "team_id": team_id,
                    "task_id": None,
                    "kind": "member_left",
                    "actor": user["email"],
                    "subject": user["email"],
                },
                extra_user_ids=[user["id"]],
            )
            return self.send_json(200, {"ok": True})
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def api_remove_member(self, team_id, member_id):
        conn = connect()
        try:
            user = self.require_user(conn)
            team, membership = require_team(conn, team_id, user["id"])
            if membership["role"] != "owner":
                raise ApiError(403, "Only the team owner can remove members")
            if member_id == user["id"]:
                raise ApiError(400, "You can't remove yourself")

            target = team_membership(conn, team_id, member_id)
            if target is None:
                raise ApiError(404, "Member not found")
            if target["role"] == "owner":
                raise ApiError(400, "The owner can't be removed")

            row = conn.execute(
                "SELECT email FROM users WHERE id = ?", (member_id,)
            ).fetchone()
            email = row["email"] if row else "that member"
            conn.execute(
                "DELETE FROM team_members WHERE team_id = ? AND user_id = ?",
                (team_id, member_id),
            )
            clear_presence(member_id)
            log_event(
                conn,
                team_id,
                None,
                user["id"],
                "member_removed",
                "removed %s" % email,
            )
            conn.commit()
            publish_team(
                conn,
                team_id,
                {
                    "type": "team.changed",
                    "team_id": team_id,
                    "task_id": None,
                    "kind": "member_removed",
                    "actor": user["email"],
                    "subject": email,
                },
                extra_user_ids=[member_id],
            )
            return self.send_json(200, {"ok": True})
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def api_create_team_task(self, team_id):
        conn = connect()
        try:
            user = self.require_user(conn)
            team, membership = require_team(conn, team_id, user["id"])
            data = self.read_json()

            text = data.get("text")
            if not isinstance(text, str) or not (1 <= len(text.strip()) <= MAX_TEXT):
                raise ApiError(400, "Task text must be 1-500 characters")
            text = text.strip()
            due = clean_due(data.get("due"))
            assignee_id = clean_assignee(conn, team_id, data.get("assignee_email"))
            priority = clean_priority(data.get("priority"))
            reason = clean_reason(data.get("reason"))
            category = classify_category(text)

            task_id = "g" + secrets.token_hex(8)
            now = int(time.time())
            position = conn.execute(
                "SELECT COALESCE(MAX(position), -1) + 1 FROM team_tasks WHERE team_id = ?",
                (team_id,),
            ).fetchone()[0]

            conn.execute(
                """
                INSERT INTO team_tasks
                    (id, team_id, text, done, due, assignee_id, created_by, priority, reason, category, position, created_at, updated_at)
                VALUES (?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    team_id,
                    text,
                    due,
                    assignee_id,
                    user["id"],
                    priority,
                    reason,
                    category,
                    position,
                    now,
                    now,
                ),
            )

            detail = 'created "%s"' % text
            if assignee_id is not None:
                email = conn.execute(
                    "SELECT email FROM users WHERE id = ?", (assignee_id,)
                ).fetchone()["email"]
                detail += " and assigned it to %s" % email
            if priority != "Medium":
                detail += " at %s priority" % priority
            if reason:
                detail += " (reason: %s)" % reason
            log_event(conn, team_id, task_id, user["id"], "task_created", detail)
            conn.commit()

            publish_team(
                conn,
                team_id,
                {
                    "type": "team.changed",
                    "team_id": team_id,
                    "task_id": task_id,
                    "kind": "task_created",
                    "actor": user["email"],
                    "subject": text,
                },
            )
            row = team_task_row(conn, team_id, task_id)
            return self.send_json(201, {"task": team_task_dict(row)})
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def api_update_team_task(self, team_id, task_id):
        conn = connect()
        try:
            user = self.require_user(conn)
            team, membership = require_team(conn, team_id, user["id"])
            row = team_task_row(conn, team_id, task_id)
            if row is None:
                raise ApiError(404, "Task not found")

            data = self.read_json()
            new_text = row["text"]
            new_done = bool(row["done"])
            new_due = row["due"]
            new_assignee = row["assignee_id"]
            new_priority = row["priority"] or "Medium"
            new_reason = row["reason"]
            new_category = row["category"] or classify_category(row["text"])
            changes = []

            if "text" in data:
                text = data.get("text")
                if not isinstance(text, str) or not (1 <= len(text.strip()) <= MAX_TEXT):
                    raise ApiError(400, "Task text must be 1-500 characters")
                text = text.strip()
                if text != row["text"]:
                    changes.append(
                        ("task_renamed", 'renamed "%s" to "%s"' % (row["text"], text))
                    )
                    new_text = text
                    new_category = classify_category(text)

            if "done" in data:
                done = bool(data.get("done"))
                if done != bool(row["done"]):
                    changes.append(
                        (
                            "task_completed" if done else "task_reopened",
                            ('completed "%s"' if done else 'reopened "%s"') % new_text,
                        )
                    )
                    new_done = done

            if "due" in data:
                due = clean_due(data.get("due"))
                if due != row["due"]:
                    if due:
                        detail = 'set the deadline of "%s" to %s' % (new_text, due)
                    else:
                        detail = 'cleared the deadline of "%s"' % new_text
                    changes.append(("deadline_changed", detail))
                    new_due = due

            if "assignee_email" in data:
                assignee_id = clean_assignee(conn, team_id, data.get("assignee_email"))
                if assignee_id != row["assignee_id"]:
                    if assignee_id is None:
                        detail = 'unassigned "%s"' % new_text
                    else:
                        email = conn.execute(
                            "SELECT email FROM users WHERE id = ?", (assignee_id,)
                        ).fetchone()["email"]
                        detail = 'assigned "%s" to %s' % (new_text, email)
                    changes.append(
                        (
                            "task_assigned" if assignee_id else "task_unassigned",
                            detail,
                        )
                    )
                    new_assignee = assignee_id

            if "priority" in data:
                priority = clean_priority(data.get("priority"))
                if priority != new_priority:
                    changes.append(
                        (
                            "priority_changed",
                            'set the priority of "%s" to %s' % (new_text, priority),
                        )
                    )
                    new_priority = priority

            if "reason" in data:
                reason = clean_reason(data.get("reason"))
                if reason != new_reason:
                    if reason:
                        detail = 'recorded "%s" as the reason for "%s"' % (
                            reason,
                            new_text,
                        )
                    else:
                        detail = 'cleared the shift reason for "%s"' % new_text
                    changes.append(("reason_changed", detail))
                    new_reason = reason

            if changes:
                conn.execute(
                    """
                    UPDATE team_tasks
                    SET text = ?, done = ?, due = ?, assignee_id = ?, priority = ?,
                        reason = ?, category = ?, updated_at = ?
                    WHERE team_id = ? AND id = ?
                    """,
                    (
                        new_text,
                        1 if new_done else 0,
                        new_due,
                        new_assignee,
                        new_priority,
                        new_reason,
                        new_category,
                        int(time.time()),
                        team_id,
                        task_id,
                    ),
                )
                for kind, detail in changes:
                    log_event(conn, team_id, task_id, user["id"], kind, detail)
                conn.commit()

                publish_team(
                    conn,
                    team_id,
                    {
                        "type": "team.changed",
                        "team_id": team_id,
                        "task_id": task_id,
                        "kind": changes[-1][0],
                        "actor": user["email"],
                        "subject": new_text,
                    },
                )

            row = team_task_row(conn, team_id, task_id)
            return self.send_json(200, {"task": team_task_dict(row)})
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def api_delete_team_task(self, team_id, task_id):
        conn = connect()
        try:
            user = self.require_user(conn)
            team, membership = require_team(conn, team_id, user["id"])
            row = team_task_row(conn, team_id, task_id)
            if row is None:
                raise ApiError(404, "Task not found")
            if membership["role"] != "owner" and row["created_by"] != user["id"]:
                raise ApiError(403, "Only the owner or the person who created it can delete this task")

            conn.execute(
                "DELETE FROM team_task_comments WHERE team_id = ? AND task_id = ?",
                (team_id, task_id),
            )
            conn.execute(
                "DELETE FROM team_task_events WHERE team_id = ? AND task_id = ?",
                (team_id, task_id),
            )
            conn.execute(
                "DELETE FROM team_tasks WHERE team_id = ? AND id = ?", (team_id, task_id)
            )
            log_event(
                conn,
                team_id,
                None,
                user["id"],
                "task_deleted",
                'deleted "%s"' % row["text"],
            )
            conn.commit()
            publish_team(
                conn,
                team_id,
                {
                    "type": "team.changed",
                    "team_id": team_id,
                    "task_id": task_id,
                    "kind": "task_deleted",
                    "actor": user["email"],
                    "subject": row["text"],
                },
            )
            return self.send_json(200, {"ok": True})
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def api_get_thread(self, team_id, task_id):
        conn = connect()
        try:
            user = self.require_user(conn)
            require_team(conn, team_id, user["id"])
            row = team_task_row(conn, team_id, task_id)
            if row is None:
                raise ApiError(404, "Task not found")
            return self.send_json(
                200,
                {
                    "task": team_task_dict(row),
                    "comments": load_thread_comments(conn, team_id, task_id),
                    "events": load_team_events(conn, team_id, task_id, limit=100),
                },
            )
        finally:
            conn.close()

    def api_add_comment(self, team_id, task_id):
        conn = connect()
        try:
            user = self.require_user(conn)
            require_team(conn, team_id, user["id"])
            row = team_task_row(conn, team_id, task_id)
            if row is None:
                raise ApiError(404, "Task not found")

            data = self.read_json()
            body = data.get("body")
            if not isinstance(body, str) or not (1 <= len(body.strip()) <= MAX_COMMENT):
                raise ApiError(400, "Comment must be 1-2000 characters")
            body = body.strip()

            parent_id = data.get("parent_id")
            if parent_id not in (None, ""):
                try:
                    parent_id = int(parent_id)
                except (TypeError, ValueError):
                    raise ApiError(400, "Invalid reply target")
                parent = conn.execute(
                    """
                    SELECT id FROM team_task_comments
                    WHERE id = ? AND team_id = ? AND task_id = ?
                    """,
                    (parent_id, team_id, task_id),
                ).fetchone()
                if parent is None:
                    raise ApiError(400, "Invalid reply target")
            else:
                parent_id = None

            now = int(time.time())
            cursor = conn.execute(
                """
                INSERT INTO team_task_comments (team_id, task_id, user_id, parent_id, body, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (team_id, task_id, user["id"], parent_id, body, now),
            )
            conn.commit()
            publish_team(
                conn,
                team_id,
                {
                    "type": "team.changed",
                    "team_id": team_id,
                    "task_id": task_id,
                    "kind": "comment_added",
                    "actor": user["email"],
                    "subject": body[:140],
                },
            )
            return self.send_json(
                201,
                {
                    "comment": {
                        "id": cursor.lastrowid,
                        "parent_id": parent_id,
                        "body": body,
                        "created_at": now,
                        "author": user["email"],
                    }
                },
            )
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def api_events(self):
        conn = connect()
        try:
            user = self.require_user(conn)
        finally:
            conn.close()

        listener = subscribe(user["id"])
        self.close_connection = True
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache, no-store")
            self.send_header("Connection", "close")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            self.wfile.write(b"retry: 3000\n\n")
            self.wfile.flush()

            while True:
                try:
                    payload = listener.get(timeout=20)
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    continue
                line = "data: " + json.dumps(payload) + "\n\n"
                self.wfile.write(line.encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            unsubscribe(user["id"], listener)
            with SUBSCRIBERS_LOCK:
                still_open = bool(SUBSCRIBERS.get(user["id"]))
            if not still_open:
                team_id = clear_presence(user["id"])
                if team_id is not None:
                    conn = connect()
                    try:
                        publish_team(
                            conn, team_id, presence_payload(team_id, user, False)
                        )
                    finally:
                        conn.close()


class Server(ThreadingHTTPServer):
    daemon_threads = True


def main():
    init_db()
    port = int(os.environ.get("PORT", "8000"))
    server = Server(("127.0.0.1", port), Handler)
    print("Taskly running at http://127.0.0.1:%d" % port)
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
