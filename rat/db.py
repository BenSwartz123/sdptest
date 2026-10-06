"""SQLite storage layer for the Repo Analysis Tool (RAT)."""
import os
import sqlite3
import time

DATA_DIR = os.environ.get("RAT_DATA", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data"))
REPOS_DIR = os.path.join(DATA_DIR, "repos")
UPLOADS_DIR = os.path.join(DATA_DIR, "uploads")
DB_PATH = os.path.join(DATA_DIR, "rat.db")

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS repos(
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  source_type TEXT NOT NULL,            -- 'zip' | 'url'
  source TEXT NOT NULL,                 -- original url / file name
  work_dir TEXT NOT NULL,               -- directory a worktree-root or bare repo lives in
  git_dir TEXT,                         -- set when git needs an explicit --git-dir (bare repo)
  created_at TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'new',   -- new | working | ready | error
  stage TEXT DEFAULT '',                -- human-readable progress/error detail
  progress_done INTEGER NOT NULL DEFAULT 0,
  progress_total INTEGER NOT NULL DEFAULT 0,
  n_commits INTEGER NOT NULL DEFAULT 0,
  n_files INTEGER NOT NULL DEFAULT 0,
  n_authors INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS commits(
  id INTEGER PRIMARY KEY,
  repo_id INTEGER NOT NULL REFERENCES repos(id) ON DELETE CASCADE,
  hash TEXT NOT NULL,
  parent TEXT NOT NULL DEFAULT '',
  author_id INTEGER NOT NULL,
  ts INTEGER NOT NULL,                  -- committer date (unix seconds)
  subject TEXT NOT NULL DEFAULT '',
  add_lines INTEGER NOT NULL DEFAULT 0, -- totals across files (denormalised)
  del_lines INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_commits_repo_hash ON commits(repo_id, hash);
CREATE INDEX IF NOT EXISTS ix_commits_repo_ts ON commits(repo_id, ts);

CREATE TABLE IF NOT EXISTS authors(
  id INTEGER PRIMARY KEY,
  repo_id INTEGER NOT NULL REFERENCES repos(id) ON DELETE CASCADE,
  name TEXT NOT NULL,                   -- identity after .mailmap resolution
  email TEXT NOT NULL,
  merged_into INTEGER,                  -- effective author (path-compressed), NULL = this row
  UNIQUE(repo_id, email, name)
);

CREATE TABLE IF NOT EXISTS author_merges(
  id INTEGER PRIMARY KEY,
  repo_id INTEGER NOT NULL REFERENCES repos(id) ON DELETE CASCADE,
  from_id INTEGER NOT NULL,
  to_id INTEGER NOT NULL,
  created_at TEXT NOT NULL
);

-- Raw identities exactly as git reports them, resolved to an effective author
-- row (e.g. several emails collapsed by .mailmap at index time).
CREATE TABLE IF NOT EXISTS author_identities(
  repo_id INTEGER NOT NULL,
  author_id INTEGER NOT NULL,
  name TEXT NOT NULL,
  email TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_ai_repo_author ON author_identities(repo_id, author_id);

-- Persistent id counters: id columns are global primary keys, so indexers
-- reserve id blocks up-front instead of racing on MAX(id).
CREATE TABLE IF NOT EXISTS id_counters(
  kind TEXT PRIMARY KEY,
  value INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS file_stats(
  repo_id INTEGER NOT NULL,
  commit_id INTEGER NOT NULL,
  ts INTEGER NOT NULL,                  -- denormalised from commits
  author_id INTEGER NOT NULL,           -- denormalised from commits
  path TEXT NOT NULL,                   -- path the change is attributed to (new path on rename)
  add_lines INTEGER NOT NULL,
  del_lines INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_fs_repo_commit ON file_stats(repo_id, commit_id);
CREATE INDEX IF NOT EXISTS ix_fs_repo_path ON file_stats(repo_id, path);
CREATE INDEX IF NOT EXISTS ix_fs_repo_ts ON file_stats(repo_id, ts);

-- Materialised reachability: which indexed commits are reachable from a given ref.
CREATE TABLE IF NOT EXISTS ref_commits(
  repo_id INTEGER NOT NULL,
  ref TEXT NOT NULL,
  commit_id INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_refc ON ref_commits(repo_id, ref);
"""


def ensure_dirs():
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(REPOS_DIR, exist_ok=True)
    os.makedirs(UPLOADS_DIR, exist_ok=True)


def connect():
    ensure_dirs()
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    conn = connect()
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


def get_repo(conn, repo_id):
    return conn.execute("SELECT * FROM repos WHERE id=?", (repo_id,)).fetchone()


def list_repos(conn):
    return conn.execute("SELECT * FROM repos ORDER BY id").fetchall()


def set_status(conn, repo_id, status, stage="", commit=True):
    conn.execute("UPDATE repos SET status=?, stage=? WHERE id=?", (status, stage, repo_id))
    if commit:
        conn.commit()


def set_progress(conn, repo_id, done, total, stage=None):
    if stage is None:
        conn.execute("UPDATE repos SET progress_done=?, progress_total=? WHERE id=?", (done, total, repo_id))
    else:
        conn.execute("UPDATE repos SET progress_done=?, progress_total=?, stage=? WHERE id=?", (done, total, stage, repo_id))
    conn.commit()


def reserve_ids(conn, kind, count):
    """Atomically reserve a block of `count` ids; returns the first id.

    The id columns are global primary keys; reserving from a shared counter
    with a single UPDATE keeps concurrent indexing runs collision-free.
    """
    conn.execute("INSERT OR IGNORE INTO id_counters(kind, value) VALUES(?, 0)", (kind,))
    row = conn.execute(
        "UPDATE id_counters SET value = value + ? WHERE kind = ? RETURNING value",
        (count, kind),
    ).fetchone()
    conn.commit()
    return row[0] - count


def clear_repo_data(conn, repo_id):
    """Remove all indexed data for a repo (used before (re)indexing)."""
    for sql in (
        "DELETE FROM file_stats WHERE repo_id=?",
        "DELETE FROM commits WHERE repo_id=?",
        "DELETE FROM ref_commits WHERE repo_id=?",
        "DELETE FROM author_merges WHERE repo_id=?",
        "DELETE FROM author_identities WHERE repo_id=?",
        "DELETE FROM authors WHERE repo_id=?",
    ):
        conn.execute(sql, (repo_id,))
    conn.commit()


def delete_repo(conn, repo_id):
    clear_repo_data(conn, repo_id)
    conn.execute("DELETE FROM repos WHERE id=?", (repo_id,))
    conn.commit()


def new_repo(conn, name, source_type, source, work_dir, git_dir=None):
    cur = conn.execute(
        "INSERT INTO repos(name, source_type, source, work_dir, git_dir, created_at, status, stage) "
        "VALUES(?,?,?,?,?,?, 'new', '')",
        (name, source_type, source, work_dir, git_dir, time.strftime("%Y-%m-%d %H:%M:%S")),
    )
    conn.commit()
    return cur.lastrowid
