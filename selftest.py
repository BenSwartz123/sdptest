"""Self-test: verifies the metric engine against an exactly known repository.

Creates a synthetic repository with deterministic commits covering every
special case in the brief (root commit, edit, rename-only, rename+edit,
binary file, deletion, merge commit, mailmap, manual author merging) and
asserts the exact expected metric values, then smoke-tests every dashboard
route through Flask's test client.

Run:  python selftest.py
"""
import io
import os
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile

TMP = tempfile.mkdtemp(prefix="rat-selftest-")
os.environ["RAT_DATA"] = os.path.join(TMP, "data")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from rat import db, gitutil, indexer, metrics  # noqa: E402
import app as rat_app  # noqa: E402  (initialises the database in RAT_DATA)

FAILURES = []
CHECKS = [0]


def check(label, got, want):
    CHECKS[0] += 1
    if got != want:
        FAILURES.append("%s: got %r, want %r" % (label, got, want))
        print("FAIL  %s: got %r, want %r" % (label, got, want))
    else:
        print("ok    %s = %r" % (label, got))


def git(cwd, *args, env=None, check=True):
    e = dict(os.environ)
    if env:
        e.update(env)
    return subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True, env=e, check=check)


def commit_env(date, name="Alice", email="alice@example.com"):
    return {
        "GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": email,
        "GIT_AUTHOR_DATE": date + "T10:00:00+00:00",
        "GIT_COMMITTER_NAME": "Alice", "GIT_COMMITTER_EMAIL": "alice@example.com",
        "GIT_COMMITTER_DATE": date + "T10:00:00+00:00",
    }


def make_repo_a(path):
    """The main fixture: exercises renames, binaries, deletes, merges, mailmap."""
    os.makedirs(path)
    git(path, "init", "-q", "-b", "main")
    git(path, "config", "user.name", "Alice")
    git(path, "config", "user.email", "alice@example.com")

    def w(name, text, mode="w"):
        with open(os.path.join(path, name), mode) as fh:
            fh.write(text)

    w("foo.txt", "a\nb\nc\nd\ne\n")
    os.makedirs(os.path.join(path, "sub"))
    w("sub/inner.txt", "x\ny\n")
    git(path, "add", "-A")
    git(path, "commit", "-qm", "c1 root", env=commit_env("2024-01-01"))

    w("foo.txt", "a\nb\nC\nd\ne\nf\n")
    git(path, "commit", "-qam", "c2 modify foo",
        env=commit_env("2024-01-02", "Alice Old", "alice.old@example.com"))

    git(path, "mv", "foo.txt", "bar.txt")
    git(path, "commit", "-qm", "c3 rename only", env=commit_env("2024-01-03"))

    w("bar.txt", "a\nb\nC\nd\ne\nf\ng\n")
    git(path, "commit", "-qam", "c4 edit after rename", env=commit_env("2024-01-04"))

    with open(os.path.join(path, "blob.bin"), "wb") as fh:
        fh.write(b"\x00\x01\x02\x03binary")
    git(path, "add", "blob.bin")
    git(path, "commit", "-qm", "c5 add binary", env=commit_env("2024-01-05"))

    git(path, "rm", "-q", "sub/inner.txt")
    git(path, "commit", "-qm", "c6 delete inner", env=commit_env("2024-01-06"))

    git(path, "mv", "bar.txt", "baz.txt")
    w("baz.txt", "a\nb\nC\nd\ne\nf\ng\nh\n")
    git(path, "add", "-A")
    git(path, "commit", "-qm", "c7 rename+edit", env=commit_env("2024-01-07"))

    git(path, "checkout", "-qb", "side")
    w("side.txt", "s\n")
    git(path, "add", "side.txt")
    git(path, "commit", "-qm", "c8 side", env=commit_env("2024-01-08"))
    git(path, "checkout", "-q", "main")
    w("main.txt", "m\n")
    git(path, "add", "main.txt")
    git(path, "commit", "-qm", "c9 main", env=commit_env("2024-01-09"))
    env = commit_env("2024-01-10")
    env["GIT_AUTHOR_NAME"] = "Alice"
    subprocess.run(["git", "-C", path, "merge", "-q", "--no-edit", "side", "-m", "c10 merge"],
                   env=dict(os.environ, **env), check=True, capture_output=True)

    # .mailmap maps the second identity onto Alice (left uncommitted: it is a
    # checkout-level file, exactly as git expects it).
    with open(os.path.join(path, ".mailmap"), "w") as fh:
        fh.write("Alice <alice@example.com> <alice.old@example.com>\n")
    return path


def make_repo_b(path):
    """Two distinct authors, no mailmap - used for manual merging."""
    os.makedirs(path)
    git(path, "init", "-q", "-b", "main")
    git(path, "config", "user.name", "Alice")
    git(path, "config", "user.email", "alice@example.com")

    def w(name, text):
        with open(os.path.join(path, name), "w") as fh:
            fh.write(text)

    w("file.txt", "1\n2\n3\n")
    git(path, "add", "-A")
    git(path, "commit", "-qm", "b1", env=commit_env("2024-02-01"))
    w("file.txt", "1\n2\n3\n4\n5\n")
    git(path, "commit", "-qam", "b2", env=commit_env("2024-02-02", "Bob", "bob@example.com"))
    w("other.txt", "x\n")
    git(path, "add", "other.txt")
    git(path, "commit", "-qm", "b3", env=commit_env("2024-02-03"))
    return path


def make_repo_c(path):
    """Binary rename: git emits '-\t-\t' + old + new (no line counts)."""
    os.makedirs(path)
    git(path, "init", "-q", "-b", "main")
    git(path, "config", "user.name", "Alice")
    git(path, "config", "user.email", "alice@example.com")
    with open(os.path.join(path, "blobby.bin"), "wb") as fh:
        fh.write(b"\x00\x01\x02binary")
    git(path, "add", "-A")
    git(path, "commit", "-qm", "r1 binary", env=commit_env("2024-03-01"))
    git(path, "mv", "blobby.bin", "blob2.bin")
    git(path, "commit", "-qm", "r2 binary rename", env=commit_env("2024-03-02"))
    return path


def index_path(name, path):
    conn = db.connect()
    repo_id = db.new_repo(conn, name, "zip", name + ".zip", work_dir=path)
    repo = db.get_repo(conn, repo_id)
    conn.close()
    indexer.index_repo(repo)
    conn = db.connect()
    repo = db.get_repo(conn, repo_id)
    check("%s status" % name, repo["status"], "ready")
    indexer.ensure_ref_set(conn, repo, "HEAD")
    return conn, repo


def main():
    # --- fixture A -------------------------------------------------------
    path_a = make_repo_a(os.path.join(TMP, "repoA"))
    conn, repo_a = index_path("repoA", path_a)
    rid = repo_a["id"]

    # Exact per-commit file statistics (the core correctness check).
    rows = conn.execute(
        "SELECT c.subject, fs.path, fs.add_lines, fs.del_lines FROM file_stats fs "
        "JOIN commits c ON c.id = fs.commit_id WHERE fs.repo_id=? ORDER BY c.ts, fs.path",
        (rid,)).fetchall()
    got = {(r["subject"], r["path"]): (r["add_lines"], r["del_lines"]) for r in rows}
    want = {
        ("c1 root", "foo.txt"): (5, 0),
        ("c1 root", "sub/inner.txt"): (2, 0),
        ("c2 modify foo", "foo.txt"): (2, 1),
        ("c3 rename only", "bar.txt"): (0, 0),          # stat attributed to new path
        ("c4 edit after rename", "bar.txt"): (1, 0),
        ("c6 delete inner", "sub/inner.txt"): (0, 2),   # deletion -> removed lines on old path
        ("c7 rename+edit", "baz.txt"): (1, 0),          # only the edit counts, on the new path
        ("c8 side", "side.txt"): (1, 0),
        ("c9 main", "main.txt"): (1, 0),
    }
    check("exact per-commit rows (binary excluded, merge excluded)", got, want)

    # --- commit set: all history from HEAD -------------------------------
    f = metrics.CommitFilter(rid, "HEAD")
    check("|H| all (non-merge, reachable)", f.size(conn), 9)
    root = metrics.object_metrics(conn, f)
    check("root added lines", root["add"], 13)
    check("root removed lines", root["rem"], 3)
    check("root growth", root["growth"], 10)
    check("root churn", root["churn"], 16)
    check("root modifications (rename-only & binary commits excluded)", root["mods"], 7)

    sub = metrics.object_metrics(conn, f, path="sub")
    check("directory 'sub' churn (recursive)", sub["churn"], 4)
    check("directory 'sub' modifications", sub["mods"], 2)

    fr = {r["path"]: (r["addl"], r["dell"], r["mods"]) for r in metrics.file_rows(conn, f)}
    check("file foo.txt (follows rename chain)", fr.get("foo.txt"), (7, 1, 2))
    check("file bar.txt (mods excludes pure rename)", fr.get("bar.txt"), (1, 0, 1))
    check("file baz.txt", fr.get("baz.txt"), (1, 0, 1))
    check("file sub/inner.txt (delete attributed)", fr.get("sub/inner.txt"), (2, 2, 2))
    check("binary file absent", "blob.bin" in fr, False)

    # --- author metrics with mailmap -------------------------------------
    authors = metrics.authors_summary(conn, f)
    check("mailmap merged identities", len(authors), 1)
    check("author churn", authors[0]["churn"], 16)
    check("author ownership", round(authors[0]["ownership"], 6), 1.0)
    listed = metrics.list_authors(conn, rid)
    check("raw identities merged into one", listed[0]["identities"], 2)

    # --- commit sets: time range H_i,j -----------------------------------
    f_range = metrics.CommitFilter(rid, "HEAD", mode="range",
                                   ts_from=1704448800, ts_to=1704873600)  # Jan 5 -> Jan 10
    check("|H| range Jan5-Jan10", f_range.size(conn), 5)
    rng = metrics.object_metrics(conn, f_range)
    check("range added", rng["add"], 3)
    check("range removed", rng["rem"], 2)
    check("range modifications", rng["mods"], 4)

    f_range2 = metrics.CommitFilter(rid, "HEAD", mode="range",
                                    ts_from=1704067200, ts_to=1704326400)  # Jan 1 -> Jan 4
    check("|H| range Jan1-Jan4", f_range2.size(conn), 3)
    rng2 = metrics.object_metrics(conn, f_range2)
    check("range2 churn", rng2["churn"], 10)

    # --- commit sets: manual selection -----------------------------------
    rows = conn.execute(
        "SELECT id, subject FROM commits WHERE repo_id=? AND subject IN ('c1 root','c2 modify foo')",
        (rid,)).fetchall()
    manual = metrics.CommitFilter(rid, "HEAD", mode="manual", manual_ids=[r["id"] for r in rows])
    check("|H| manual", manual.size(conn), 2)
    man = metrics.object_metrics(conn, manual)
    check("manual added", man["add"], 9)   # c1: 5 + 2, c2: +2

    # --- reference commit (reachability from a specific commit) ----------
    f_side = metrics.CommitFilter(rid, "side")
    indexer.ensure_ref_set(conn, repo_a, "side")
    check("|H| from side ref", f_side.size(conn), 8)
    side_files = {r["path"] for r in metrics.file_rows(conn, f_side)}
    check("main.txt not reachable from side", "main.txt" in side_files, False)
    check("side.txt reachable from side", "side.txt" in side_files, True)

    # --- timeseries -------------------------------------------------------
    series = metrics.timeseries(conn, f)
    check("timeseries total churn", sum(p["churn"] for p in series), 16)
    check("timeseries weekly buckets", len(series) >= 2, True)

    # --- fixture B: manual author merging --------------------------------
    path_b = make_repo_b(os.path.join(TMP, "repoB"))
    conn_b, repo_b = index_path("repoB", path_b)
    rid_b = repo_b["id"]
    fb = metrics.CommitFilter(rid_b, "HEAD")
    check("repoB authors before merge", len(metrics.authors_summary(conn_b, fb)), 2)

    client = rat_app.app.test_client()
    listed_b = metrics.list_authors(conn_b, rid_b)
    alice = next(a for a in listed_b if a["email"] == "alice@example.com")
    bob = next(a for a in listed_b if a["email"] == "bob@example.com")
    resp = client.post("/r/%d/authors/merge" % rid_b,
                       data={"from_id": bob["aid"], "to_id": alice["aid"]})
    check("merge POST redirects", resp.status_code, 302)
    merged = metrics.authors_summary(conn_b, fb)
    check("authors after merge", len(merged), 1)
    check("merged churn", merged[0]["churn"], 6)
    check("merged name", merged[0]["name"], "Alice")

    # ownership on a single file after merging: file.txt churn 5 -> Alice owns it
    auth_file = metrics.object_authors(conn_b, fb, path="file.txt", is_file=True)
    check("file.txt ownership after merge", round(auth_file[0]["ownership"], 6), 1.0)
    check("file.txt author modifications", auth_file[0]["mods"], 2)

    # undo the merge
    merge_id = conn_b.execute("SELECT id FROM author_merges WHERE repo_id=?", (rid_b,)).fetchone()["id"]
    resp = client.post("/r/%d/authors/unmerge/%d" % (rid_b, merge_id))
    check("unmerge POST redirects", resp.status_code, 302)
    after = metrics.authors_summary(conn_b, fb)
    check("authors after unmerge", len(after), 2)

    # --- fixture C: binary rename (regression: '-\t-\t' + old + new) -----
    path_c = make_repo_c(os.path.join(TMP, "repoC"))
    conn_c, repo_c = index_path("repoC", path_c)
    rid_c = repo_c["id"]
    n_rows = conn_c.execute(
        "SELECT COUNT(*) FROM file_stats WHERE repo_id=?", (rid_c,)).fetchone()[0]
    check("binary rename produces no file rows", n_rows, 0)
    check("binary rename commits still counted", repo_c["n_commits"], 2)

    # --- end-to-end zip ingestion through the real upload endpoint -------
    zip_buf = io.BytesIO()
    with zipfile.ZipFile(zip_buf, "w") as zf:
        for root_dir, _dirs, names in os.walk(path_c):
            for fn in names:
                full = os.path.join(root_dir, fn)
                zf.write(full, os.path.relpath(full, path_c))
    zip_buf.seek(0)
    resp = client.post("/repos/upload",
                       data={"zipfile": (zip_buf, "repoC_zip.zip")},
                       content_type="multipart/form-data")
    check("zip upload POST redirects", resp.status_code, 302)
    conn_z = db.connect()
    deadline = time.time() + 120
    repo_z = None
    while time.time() < deadline:
        repo_z = conn_z.execute(
            "SELECT * FROM repos WHERE name='repoC_zip'").fetchone()
        if repo_z and repo_z["status"] in ("ready", "error"):
            break
        time.sleep(0.5)
    check("zip ingest status", repo_z["status"], "ready")
    z_commits = conn_z.execute(
        "SELECT COUNT(*) FROM commits WHERE repo_id=?", (repo_z["id"],)).fetchone()[0]
    check("zip ingest indexed commits", z_commits, 2)

    # --- dashboard smoke tests (HTTP 200 for every route) ----------------
    for url in ("/", "/r/%d/" % rid, "/r/%d/files" % rid, "/r/%d/dir/" % rid,
                "/r/%d/dir/sub" % rid, "/r/%d/file/sub/inner.txt" % rid,
                "/r/%d/commits" % rid, "/r/%d/authors" % rid,
                "/r/%d/status.json" % rid, "/r/%d/files.csv" % rid):
        resp = client.get(url)
        check("GET %s" % url, resp.status_code, 200)

    # range-filtered pages
    resp = client.get("/r/%d/?from=2024-01-05T10:00&to=2024-01-10T10:00" % rid)
    check("range-filtered overview", resp.status_code, 200)
    resp = client.get("/r/%d/?ref=nope-does-not-exist" % rid)
    check("invalid ref falls back gracefully", resp.status_code, 200)

    # manual selection through the real endpoint
    c1 = conn.execute("SELECT hash FROM commits WHERE repo_id=? AND subject='c1 root'", (rid,)).fetchone()["hash"]
    resp = client.post("/r/%d/commits/select" % rid, data={"commit": [c1], "next": "/r/%d/commits" % rid})
    check("manual selection POST", resp.status_code, 302)
    loc = resp.headers.get("Location", "")
    check("manual selection link has sel", "sel=" in loc, True)

    conn.close()
    conn_b.close()
    conn_c.close()
    conn_z.close()

    print()
    print("=" * 62)
    if FAILURES:
        print("%d/%d checks FAILED:" % (len(FAILURES), CHECKS[0]))
        for f_ in FAILURES:
            print("  -", f_)
        return 1
    print("All %d checks passed." % CHECKS[0])
    return 0


if __name__ == "__main__":
    try:
        code = main()
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    sys.exit(code)
