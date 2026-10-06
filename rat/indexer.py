"""One-pass history indexer.

Streams `git log --all --no-merges --numstat -z --find-renames=50%` and stores
per-commit file statistics in SQLite.  Every statistic comes from git's own
diff machinery, so rename detection (50% threshold), binary detection, and
line counting follow git's definitions exactly:

  * binary files emit `-\t-\t<path>` and are skipped,
  * renames emit `0\t0\t` + old path + new path (stats attributed to new path),
  * deletions emit removed lines on the old path,
  * merge commits are excluded by `--no-merges`,
  * the root commit diffs against the empty tree (h[p] = h_empty).
"""
import re
import sys

from . import db, gitutil

HEADER_MARK = b"\x02"  # STX byte marks the start of a commit header token
STATS_RE = re.compile(rb"^\n?(-|\d+)\t(-|\d+)\t(.*)$", re.S)
RENAME_RE = re.compile(rb"^\n?(-|\d+)\t(-|\d+)\t$")
FIELD_SEP = b"\x1f"

FLUSH_EVERY_COMMITS = 200
FS_BATCH = 20000

LOG_ARGS = [
    "log", "--all", "--no-merges", "--numstat", "-z",
    "--find-renames=50%",
    "--format=%x02%H%x1f%P%x1f%ct%x1f%an%x1f%ae%x1f%s",
]


def _dec(raw):
    return raw.decode("utf-8", "backslashreplace")


def _iter_tokens(stream, chunk_size=1 << 20):
    """Yield NUL-separated tokens from a byte stream."""
    buf = b""
    while True:
        chunk = stream.read(chunk_size)
        if not chunk:
            break
        buf += chunk
        parts = buf.split(b"\x00")
        buf = parts.pop()
        for p in parts:
            yield p
    if buf:
        yield buf


class _State:
    def __init__(self):
        self.commit = None      # (hash, parent, ts, an, ae, subject)
        self.stats = []         # pending (path, add, del) for current commit
        self.expect = 0         # 0 idle, 1 expecting rename old path, 2 new path
        self.rename_add = 0
        self.rename_del = 0
        self.rename_old = ""


def index_repo(repo):
    """Index a repo row. Runs in a background thread; updates status/progress."""
    conn = db.connect()
    try:
        work_dir, git_dir = repo["work_dir"], repo["git_dir"]
        db.set_status(conn, repo["id"], "working", "Reading repository metadata")
        total_proc = gitutil.run_git(work_dir, git_dir, ["rev-list", "--all", "--no-merges", "--count"], timeout=300)
        total = int(total_proc.stdout.strip() or "0")
        if total == 0:
            conn.execute("UPDATE repos SET n_commits=0, n_files=0, n_authors=0 WHERE id=?", (repo["id"],))
            db.set_status(conn, repo["id"], "ready", "Repository has no commits")
            return

        db.set_progress(conn, repo["id"], 0, total, "Indexing history")

        # Globally unique ids: id columns are global primary keys, so reserve
        # blocks up-front from the persistent counters (safe under concurrency).
        next_commit_id = db.reserve_ids(conn, "commits", total + 16) + 1
        base_author = db.reserve_ids(conn, "authors", 2 * total + 16)
        author_ids = {}          # (canonical name, email) -> id
        mailmap_cache = {}       # (raw name, email) -> (cname, cemail)
        seen_identities = set()  # raw identities already recorded
        author_rows = []
        identity_rows = []
        commit_rows = []
        fs_rows = []

        def get_author(raw_name, raw_email):
            key = (raw_name, raw_email)
            canon = mailmap_cache.get(key)
            if canon is None:
                canon = gitutil.check_mailmap(work_dir, git_dir, raw_name, raw_email)
                mailmap_cache[key] = canon
            aid = author_ids.get(canon)
            if aid is None:
                aid = base_author + len(author_ids) + 1
                author_ids[canon] = aid
                author_rows.append((aid, repo["id"], canon[0], canon[1]))
            if key not in seen_identities:
                seen_identities.add(key)
                identity_rows.append((repo["id"], aid, raw_name, raw_email))
            return aid

        proc = gitutil.spawn_git_stream(work_dir, git_dir, LOG_ARGS)
        st = _State()
        done = 0

        def flush_commit():
            nonlocal next_commit_id
            if st.commit is None:
                return
            h, parent, ts, an, ae, subject = st.commit
            aid = get_author(an, ae)
            add_total = 0
            del_total = 0
            for path, add, dele in st.stats:
                add_total += add
                del_total += dele
                fs_rows.append((repo["id"], next_commit_id, ts, aid, path, add, dele))
            commit_rows.append((next_commit_id, repo["id"], h, parent, aid, ts, subject, add_total, del_total))
            next_commit_id += 1
            st.commit = None
            st.stats = []

        for tok in _iter_tokens(proc.stdout):
            if tok == b"":
                continue
            if tok.startswith(HEADER_MARK):
                flush_commit()
                fields = tok[1:].split(FIELD_SEP, 5)
                if len(fields) < 6:
                    continue
                st.commit = (
                    _dec(fields[0]),
                    _dec(fields[1]).split()[0] if fields[1] else "",
                    int(fields[2] or b"0"),
                    _dec(fields[3]),
                    _dec(fields[4]),
                    _dec(fields[5]).rstrip("\n"),
                )
                done += 1
                if done % FLUSH_EVERY_COMMITS == 0:
                    _write_batches(conn, author_rows, identity_rows, commit_rows, fs_rows)
                    author_rows.clear(); identity_rows.clear()
                    commit_rows.clear(); fs_rows.clear()
                    db.set_progress(conn, repo["id"], done, total)
                continue
            # Rename support: "add\tdel\t" token is followed by old then new path.
            if st.expect == 1:
                st.rename_old = _dec(tok)
                st.expect = 2
                continue
            if st.expect == 2:
                new_path = _dec(tok)
                if st.rename_add is not None and st.rename_del is not None:
                    st.stats.append((new_path, st.rename_add, st.rename_del))
                # binary renames ("-\t-\t" + old + new) carry no line counts
                st.expect = 0
                continue
            m = RENAME_RE.match(tok)
            if m and st.commit is not None:
                st.rename_add, st.rename_del = _as_int(m.group(1)), _as_int(m.group(2))
                st.expect = 1
                continue
            m = STATS_RE.match(tok)
            if m and st.commit is not None:
                add, dele = _as_int(m.group(1)), _as_int(m.group(2))
                if add is None or dele is None:
                    continue  # binary file: not measured
                st.stats.append((_dec(m.group(3)), add, dele))
                continue
            # Anything else (stray newlines) is ignored.

        flush_commit()
        _write_batches(conn, author_rows, identity_rows, commit_rows, fs_rows)

        err = proc.stderr.read()
        rc = proc.wait()
        if rc != 0:
            raise gitutil.GitError("git log failed: %s" % _dec(err).strip().splitlines()[-1] if err else "exit %d" % rc)

        # Finalise counters.
        n_commits = conn.execute("SELECT COUNT(*) FROM commits WHERE repo_id=?", (repo["id"],)).fetchone()[0]
        n_files = conn.execute("SELECT COUNT(DISTINCT path) FROM file_stats WHERE repo_id=?", (repo["id"],)).fetchone()[0]
        n_authors = conn.execute("SELECT COUNT(*) FROM authors WHERE repo_id=?", (repo["id"],)).fetchone()[0]
        conn.execute(
            "UPDATE repos SET n_commits=?, n_files=?, n_authors=?, progress_done=?, progress_total=? WHERE id=?",
            (n_commits, n_files, n_authors, done, total, repo["id"]),
        )
        db.set_status(conn, repo["id"], "ready", "Indexed %d commits" % n_commits)
    except Exception as exc:  # noqa: BLE001 - surface any failure on the repo row
        db.set_status(conn, repo["id"], "error", str(exc))
    finally:
        conn.close()


def _as_int(raw):
    if raw == b"-":
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _write_batches(conn, author_rows, identity_rows, commit_rows, fs_rows):
    if author_rows:
        conn.executemany(
            "INSERT OR IGNORE INTO authors(id, repo_id, name, email) VALUES(?,?,?,?)", author_rows)
    if identity_rows:
        conn.executemany(
            "INSERT INTO author_identities(repo_id, author_id, name, email) VALUES(?,?,?,?)", identity_rows)
    if commit_rows:
        conn.executemany(
            "INSERT INTO commits(id, repo_id, hash, parent, author_id, ts, subject, add_lines, del_lines) "
            "VALUES(?,?,?,?,?,?,?,?,?)", commit_rows)
    if fs_rows:
        conn.executemany(
            "INSERT INTO file_stats(repo_id, commit_id, ts, author_id, path, add_lines, del_lines) "
            "VALUES(?,?,?,?,?,?,?)", fs_rows)
    conn.commit()


# ---------------------------------------------------------------------------
# Reachability materialisation (H_bar for a given reference commit)
# ---------------------------------------------------------------------------

def ensure_ref_set(conn, repo, ref):
    """Materialise the commit ids reachable from `ref` into ref_commits.

    Returns the number of reachable commits that exist in our index.
    """
    existing = conn.execute(
        "SELECT 1 FROM ref_commits WHERE repo_id=? AND ref=? LIMIT 1", (repo["id"], ref)
    ).fetchone()
    if existing:
        return
    hashes = gitutil.rev_list(repo["work_dir"], repo["git_dir"], ref)
    id_by_hash = dict(conn.execute(
        "SELECT hash, id FROM commits WHERE repo_id=?", (repo["id"],)).fetchall())
    rows = [(repo["id"], ref, id_by_hash[h]) for h in hashes if h in id_by_hash]
    conn.execute("DELETE FROM ref_commits WHERE repo_id=? AND ref=?", (repo["id"], ref))
    conn.executemany("INSERT INTO ref_commits(repo_id, ref, commit_id) VALUES(?,?,?)", rows)
    conn.commit()
