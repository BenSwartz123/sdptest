"""Repository ingestion: remote URL (deep clone) and zip upload.

Both paths run in a background thread: the request returns immediately and the
repository page polls status/progress until indexing finishes.
"""
import os
import shutil
import threading
import zipfile

from . import db, gitutil, indexer


def name_from_url(url):
    name = url.rstrip("/").rsplit("/", 1)[-1]
    if name.endswith(".git"):
        name = name[:-4]
    return name or "repository"


def validate_url(url):
    url = (url or "").strip()
    for scheme in ("https://", "http://", "git://", "ssh://", "git@"):
        if url.startswith(scheme):
            return url
    raise ValueError(
        "Unsupported repository URL. Use https://, http://, git://, ssh:// or git@host:path")


def start_url_ingest(url):
    """Create the repo row and start clone + index in the background."""
    url = validate_url(url)
    conn = db.connect()
    try:
        repo_id = db.new_repo(conn, name_from_url(url), "url", url, work_dir="")
    finally:
        conn.close()
    t = threading.Thread(target=_ingest_url, args=(repo_id, url), daemon=True)
    t.start()
    return repo_id


def start_zip_ingest(file_storage):
    """Create the repo row and start extract + index in the background."""
    filename = os.path.basename(file_storage.filename or "upload.zip")
    if not filename.lower().endswith(".zip"):
        raise ValueError("Please upload a .zip file of the repository (including its .git data)")
    conn = db.connect()
    try:
        repo_id = db.new_repo(conn, filename[:-4] or "repository", "zip", filename, work_dir="")
    finally:
        conn.close()
    upload_path = os.path.join(db.UPLOADS_DIR, "repo-%d.zip" % repo_id)
    file_storage.save(upload_path)
    t = threading.Thread(target=_ingest_zip, args=(repo_id, upload_path), daemon=True)
    t.start()
    return repo_id


# ---------------------------------------------------------------------------

def _ingest_url(repo_id, url):
    conn = db.connect()
    try:
        target = os.path.join(db.REPOS_DIR, "repo-%d" % repo_id)
        if os.path.exists(target):
            shutil.rmtree(target)
        db.set_status(conn, repo_id, "working", "Cloning %s (deep clone)" % url)
        # A full mirror clone keeps every ref and the complete history.
        gitutil.run_git(os.path.dirname(target), None, ["clone", "--mirror", url, target], timeout=3600)
        conn.execute("UPDATE repos SET work_dir=?, git_dir=? WHERE id=?", (target, target, repo_id))
        conn.commit()
    except Exception as exc:  # noqa: BLE001
        db.set_status(conn, repo_id, "error", "Clone failed: %s" % exc)
        conn.close()
        return
    conn.close()
    repo = db.connect().execute("SELECT * FROM repos WHERE id=?", (repo_id,)).fetchone()
    indexer.index_repo(repo)


def _ingest_zip(repo_id, upload_path):
    conn = db.connect()
    try:
        extract_dir = os.path.join(db.REPOS_DIR, "repo-%d" % repo_id, "extract")
        if os.path.exists(os.path.dirname(extract_dir)):
            shutil.rmtree(os.path.dirname(extract_dir))
        db.set_status(conn, repo_id, "working", "Extracting archive")
        if not zipfile.is_zipfile(upload_path):
            raise ValueError("the uploaded file is not a valid zip archive")
        os.makedirs(extract_dir, exist_ok=True)
        with zipfile.ZipFile(upload_path) as zf:
            _safe_extract(zf, extract_dir)
        work_dir, git_dir = gitutil.detect_repo_root(extract_dir)
        conn.execute("UPDATE repos SET work_dir=?, git_dir=? WHERE id=?", (work_dir, git_dir, repo_id))
        conn.commit()
    except Exception as exc:  # noqa: BLE001
        db.set_status(conn, repo_id, "error", "Zip import failed: %s" % exc)
        conn.close()
        return
    conn.close()
    repo = db.connect().execute("SELECT * FROM repos WHERE id=?", (repo_id,)).fetchone()
    indexer.index_repo(repo)


def _safe_extract(zf, dest):
    """Extract while refusing absolute paths and parent-directory traversal."""
    base = os.path.abspath(dest)
    for member in zf.infolist():
        name = member.filename.replace("\\", "/")
        parts = [p for p in name.split("/") if p not in ("", ".")]
        if name.startswith("/") or any(p == ".." for p in parts):
            raise ValueError("unsafe path in zip archive: %s" % member.filename)
        target = os.path.abspath(os.path.join(base, *parts))
        if not target.startswith(base + os.sep) and target != base:
            raise ValueError("unsafe path in zip archive: %s" % member.filename)
    zf.extractall(base)
