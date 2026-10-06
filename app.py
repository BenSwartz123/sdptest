"""RAT - Repository Analysis Tool.

A multi-repository web dashboard that computes the COMS3011A metrics for
authors, files, directories and commit sets of Git repositories.
"""
import calendar
import csv
import datetime
import io
import os
import shutil
import threading
import time
import uuid
from math import ceil
from urllib.parse import parse_qsl, urlencode, urlsplit

from flask import (Flask, Response, abort, flash, g, jsonify, redirect,
                   render_template, request, url_for)

from rat import db, gitutil, indexer, ingest, metrics

app = Flask(__name__)
app.secret_key = os.environ.get("RAT_SECRET", uuid.uuid4().hex)
app.jinja_env.filters["num"] = lambda v: "{:,}".format(int(v or 0))
app.jinja_env.filters["pct"] = lambda v: "%.1f%%" % (100.0 * (v or 0.0))
app.jinja_env.filters["dt"] = lambda ts: datetime.datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else ""
app.jinja_env.filters["dti"] = lambda ts: datetime.datetime.utcfromtimestamp(ts).strftime("%Y-%m-%dT%H:%M") if ts else ""

db.init_db()

# In-memory manual-commit-selection store: sel uuid -> {'repo_id': int, 'hashes': [..]}
SELECTIONS = {}
SELECTIONS_LOCK = threading.Lock()
MAX_SELECTION = 10000

PER_PAGE = 100


# ---------------------------------------------------------------------------
# request plumbing
# ---------------------------------------------------------------------------

@app.before_request
def _open_db():
    g.conn = db.connect()


@app.teardown_appcontext
def _close_db(_exc):
    conn = getattr(g, "conn", None)
    if conn is not None:
        conn.close()


@app.context_processor
def _helpers():
    def q(over=None, **kw):
        """URL-encode the current query string with overrides (empty removes)."""
        d = dict(request.args)
        if over:
            d.update(over)
        d.update(kw)
        clean = {k: v for k, v in d.items() if v not in (None, "")}
        return urlencode(clean)
    return {"q": q}


@app.errorhandler(404)
def _not_found(_e):
    return render_template("error.html", code=404,
                           message="The page or repository you requested does not exist."), 404


def repo_or_404(repo_id):
    repo = db.get_repo(g.conn, repo_id)
    if repo is None:
        abort(404)
    return repo


def require_ready(repo):
    if repo["status"] != "ready":
        return redirect(url_for("repo_status", repo_id=repo["id"]))
    return None


def parse_ts(s):
    if not s:
        return None
    s = s.strip()
    for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            return calendar.timegm(datetime.datetime.strptime(s, fmt).timetuple())
        except ValueError:
            continue
    return None


def build_filter(repo, notes):
    """Build the commit-set filter (H) from query args; validates ref/author."""
    args = request.args
    ref = gitutil.validate_ref(args.get("ref")) or "HEAD"
    try:
        indexer.ensure_ref_set(g.conn, repo, ref)
    except gitutil.GitError as exc:
        if ref != "HEAD":
            notes.append("Unknown reference '%s' (%s). Showing HEAD instead." % (ref, exc))
            ref = "HEAD"
            try:
                indexer.ensure_ref_set(g.conn, repo, ref)
            except gitutil.GitError:
                pass
        else:
            notes.append("Could not resolve HEAD: %s" % exc)

    ts_from = parse_ts(args.get("from"))
    ts_to = parse_ts(args.get("to"))
    if args.get("from") and ts_from is None:
        notes.append("Could not parse the 'from' date - ignoring it.")
    if args.get("to") and ts_to is None:
        notes.append("Could not parse the 'to' date - ignoring it.")

    mode = "all"
    manual_ids = []
    if ts_from is not None or ts_to is not None:
        mode = "range"
    elif args.get("sel"):
        with SELECTIONS_LOCK:
            entry = SELECTIONS.get(args["sel"])
        if entry and entry["repo_id"] == repo["id"]:
            mode = "manual"
            hashes = entry["hashes"]
            id_by_hash = dict(g.conn.execute(
                "SELECT hash, id FROM commits WHERE repo_id=?", (repo["id"],)).fetchall())
            manual_ids = [id_by_hash[h] for h in hashes if h in id_by_hash]
            if not manual_ids:
                notes.append("The manual selection contains no indexed commits.")
        else:
            notes.append("Your manual commit selection has expired; showing all commits.")

    author_id = None
    raw_author = args.get("author")
    if raw_author:
        try:
            author_id = int(raw_author)
        except ValueError:
            author_id = None
        if author_id is not None:
            found = g.conn.execute(
                "SELECT 1 FROM authors WHERE repo_id=? AND COALESCE(merged_into,id)=?",
                (repo["id"], author_id)).fetchone()
            if not found:
                notes.append("Unknown author filter - showing all authors.")
                author_id = None
    return metrics.CommitFilter(repo["id"], ref, mode=mode, ts_from=ts_from,
                                ts_to=ts_to, manual_ids=manual_ids), author_id, ref


def _strip_param(url, name):
    parts = urlsplit(url)
    qsl = [(k, v) for k, v in parse_qsl(parts.query) if k != name]
    return parts.path + (("?" + urlencode(qsl)) if qsl else "")


def page_ctx(repo, f, author_id, ref, notes, **kw):
    refs = []
    if repo["status"] == "ready":
        try:
            refs = gitutil.list_refs(repo["work_dir"], repo["git_dir"])
        except gitutil.GitError:
            refs = ["HEAD"]
    now = int(time.time())
    quick_ranges = [("30 days", now - 30 * 86400), ("90 days", now - 90 * 86400),
                    ("1 year", now - 365 * 86400)]
    return dict(repo=repo, f=f, author_filter=author_id, current_ref=ref, notes=notes,
                refs=refs, quick_ranges=quick_ranges, repos=db.list_repos(g.conn),
                authors_list=metrics.list_authors(g.conn, repo["id"]) if repo["status"] == "ready" else [],
                **kw)


# ---------------------------------------------------------------------------
# repository management (multiple repository support)
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("repos.html", repos=db.list_repos(g.conn))


@app.route("/help")
def help_page():
    """Plain-English guide and glossary for non-technical readers."""
    return render_template("help.html", repos=db.list_repos(g.conn))


@app.route("/repos/clone", methods=["POST"])
def clone_repo():
    url = (request.form.get("url") or "").strip()
    try:
        repo_id = ingest.start_url_ingest(url)
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("index"))
    flash("Cloning %s in the background - indexing starts automatically." % url, "info")
    return redirect(url_for("repo_status", repo_id=repo_id))


@app.route("/repos/upload", methods=["POST"])
def upload_repo():
    file = request.files.get("zipfile")
    if file is None or not file.filename:
        flash("Choose a .zip file to upload.", "error")
        return redirect(url_for("index"))
    try:
        repo_id = ingest.start_zip_ingest(file)
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("index"))
    flash("Archive uploaded - extracting and indexing in the background.", "info")
    return redirect(url_for("repo_status", repo_id=repo_id))


@app.route("/r/<int:repo_id>/delete", methods=["POST"])
def delete_repo(repo_id):
    repo = repo_or_404(repo_id)
    db.delete_repo(g.conn, repo_id)
    for path in (os.path.join(db.REPOS_DIR, "repo-%d" % repo_id),
                 os.path.join(db.UPLOADS_DIR, "repo-%d.zip" % repo_id)):
        if os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)
        elif os.path.isfile(path):
            try:
                os.remove(path)
            except OSError:
                pass
    flash("Repository '%s' deleted." % repo["name"], "info")
    return redirect(url_for("index"))


@app.route("/r/<int:repo_id>/reindex", methods=["POST"])
def reindex_repo(repo_id):
    repo = repo_or_404(repo_id)
    if not repo["work_dir"] or not os.path.exists(repo["work_dir"]):
        flash("The repository data is no longer available on disk; please re-import it.", "error")
        return redirect(url_for("repo_status", repo_id=repo_id))
    db.clear_repo_data(g.conn, repo_id)
    db.set_status(g.conn, repo_id, "working", "Re-indexing")

    def _run():
        conn = db.connect()
        try:
            row = conn.execute("SELECT * FROM repos WHERE id=?", (repo_id,)).fetchone()
            indexer.index_repo(row)
        finally:
            conn.close()

    threading.Thread(target=_run, daemon=True).start()
    return redirect(url_for("repo_status", repo_id=repo_id))


@app.route("/r/<int:repo_id>/status")
def repo_status(repo_id):
    repo = repo_or_404(repo_id)
    if repo["status"] == "ready":
        return redirect(url_for("overview", repo_id=repo_id))
    return render_template("status.html", repo=repo, repos=db.list_repos(g.conn))


@app.route("/r/<int:repo_id>/status.json")
def repo_status_json(repo_id):
    repo = repo_or_404(repo_id)
    return jsonify({
        "status": repo["status"], "stage": repo["stage"],
        "done": repo["progress_done"], "total": repo["progress_total"],
        "commits": repo["n_commits"], "files": repo["n_files"], "authors": repo["n_authors"],
    })


# ---------------------------------------------------------------------------
# dashboards
# ---------------------------------------------------------------------------

@app.route("/r/<int:repo_id>/")
def overview(repo_id):
    repo = repo_or_404(repo_id)
    if repo["status"] != "ready":
        return render_template("status.html", repo=repo, repos=db.list_repos(g.conn))
    notes = []
    f, author_id, ref = build_filter(repo, notes)
    size = f.size(g.conn)
    totals = metrics.object_metrics(g.conn, f, author_id=author_id, size=size)
    authors_all = metrics.authors_summary(g.conn, f)
    author_count = len(authors_all)
    author_rows = authors_all[:12]
    top_files = metrics.file_rows(g.conn, f, limit=10, author_id=author_id)
    series = metrics.timeseries(g.conn, f, author_id=author_id)
    total_files = metrics.file_count(g.conn, f)
    return render_template(
        "overview.html",
        **page_ctx(repo, f, author_id, ref, notes, totals=totals, author_count=author_count,
                   author_rows=author_rows, top_files=top_files, series=series, size=size,
                   total_files=total_files))


@app.route("/r/<int:repo_id>/files")
def files(repo_id):
    repo = repo_or_404(repo_id)
    redirect_resp = require_ready(repo)
    if redirect_resp:
        return redirect_resp
    notes = []
    f, author_id, ref = build_filter(repo, notes)
    search = (request.args.get("search") or "").strip()
    sort = request.args.get("sort") or "churn"
    try:
        page = max(1, int(request.args.get("page", "1")))
    except ValueError:
        page = 1
    size = f.size(g.conn)
    total = metrics.file_count(g.conn, f, search=search)
    rows = metrics.file_rows(g.conn, f, search=search, sort=sort,
                             limit=PER_PAGE, offset=(page - 1) * PER_PAGE, author_id=author_id)
    totals = metrics.object_metrics(g.conn, f, author_id=author_id, size=size)
    pages = max(1, ceil(total / PER_PAGE))
    return render_template(
        "files.html",
        **page_ctx(repo, f, author_id, ref, notes, rows=rows, totals=totals, search=search,
                   sort=sort, page=page, pages=pages, total=total, size=size))


@app.route("/r/<int:repo_id>/files.csv")
def files_csv(repo_id):
    repo = repo_or_404(repo_id)
    if repo["status"] != "ready":
        return redirect(url_for("repo_status", repo_id=repo_id))
    notes = []
    f, author_id, ref = build_filter(repo, notes)
    search = (request.args.get("search") or "").strip()
    sort = request.args.get("sort") or "churn"
    size = f.size(g.conn) or 1
    rows = metrics.file_rows(g.conn, f, search=search, sort=sort, author_id=author_id)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["path", "added_lines", "removed_lines", "growth", "churn", "modifications",
                "modification_frequency", "churn_rate"])
    for r in rows:
        addl, dell, mods = r["addl"], r["dell"], r["mods"]
        w.writerow([r["path"], addl, dell, addl - dell, addl + dell, mods,
                    "%.6f" % (mods / size), "%.6f" % ((addl + dell) / size)])
    return Response(
        buf.getvalue(), mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=%s-files.csv" % repo["name"]})


def _norm_dir(sub):
    sub = (sub or "").strip("/")
    while sub.startswith("./"):
        sub = sub[2:]
    return sub


@app.route("/r/<int:repo_id>/dir/", defaults={"sub": ""})
@app.route("/r/<int:repo_id>/dir/<path:sub>")
def directory(repo_id, sub):
    repo = repo_or_404(repo_id)
    redirect_resp = require_ready(repo)
    if redirect_resp:
        return redirect_resp
    notes = []
    f, author_id, ref = build_filter(repo, notes)
    sub = _norm_dir(sub)
    size = f.size(g.conn)
    totals = metrics.object_metrics(g.conn, f, path=sub, size=size)
    subdirs, dir_files = metrics.dir_children(g.conn, f, sub, author_id=author_id, size=size)
    obj_authors = metrics.object_authors(g.conn, f, path=sub, author_id=author_id)
    series = metrics.timeseries(g.conn, f, path=sub, author_id=author_id)
    crumbs = []
    acc = ""
    for seg in sub.split("/") if sub else []:
        acc = acc + "/" + seg if acc else seg
        crumbs.append({"name": seg, "path": acc})
    return render_template(
        "directory.html",
        **page_ctx(repo, f, author_id, ref, notes, sub=sub, is_file=False, totals=totals,
                   subdirs=subdirs, dir_files=dir_files, obj_authors=obj_authors, series=series,
                   crumbs=crumbs, size=size))


@app.route("/r/<int:repo_id>/file/<path:sub>")
def file_detail(repo_id, sub):
    repo = repo_or_404(repo_id)
    redirect_resp = require_ready(repo)
    if redirect_resp:
        return redirect_resp
    notes = []
    f, author_id, ref = build_filter(repo, notes)
    sub = _norm_dir(sub)
    size = f.size(g.conn)
    totals = metrics.object_metrics(g.conn, f, path=sub, is_file=True, size=size)
    obj_authors = metrics.object_authors(g.conn, f, path=sub, is_file=True, author_id=author_id)
    series = metrics.timeseries(g.conn, f, path=sub, is_file=True, author_id=author_id)
    parent = sub.rsplit("/", 1)[0] if "/" in sub else ""
    return render_template(
        "directory.html",
        **page_ctx(repo, f, author_id, ref, notes, sub=sub, is_file=True, totals=totals,
                   subdirs=[], dir_files=[], obj_authors=obj_authors, series=series,
                   parent=parent, crumbs=[], size=size))


# ---------------------------------------------------------------------------
# commits & manual commit sets
# ---------------------------------------------------------------------------

@app.route("/r/<int:repo_id>/commits")
def commits(repo_id):
    repo = repo_or_404(repo_id)
    redirect_resp = require_ready(repo)
    if redirect_resp:
        return redirect_resp
    notes = []
    f, author_id, ref = build_filter(repo, notes)
    search = (request.args.get("search") or "").strip()
    try:
        page = max(1, int(request.args.get("page", "1")))
    except ValueError:
        page = 1
    where, params = f.c_where()
    if search:
        where += " AND (c.hash LIKE ? OR c.subject LIKE ?)"
        params += ["%" + search + "%", "%" + search + "%"]
    total = g.conn.execute("SELECT COUNT(*) FROM commits c WHERE " + where, params).fetchone()[0]
    rows = g.conn.execute(
        "SELECT c.id, c.hash, c.ts, c.subject, c.add_lines, c.del_lines, a.name AS author "
        "FROM commits c JOIN authors a ON a.id=c.author_id WHERE " + where +
        " ORDER BY c.ts DESC, c.id DESC LIMIT ? OFFSET ?",
        params + [PER_PAGE, (page - 1) * PER_PAGE]).fetchall()
    pages = max(1, ceil(total / PER_PAGE))
    sel_info = None
    if f.mode == "manual":
        sel_info = {"sel": request.args.get("sel"), "count": len(f.manual_ids)}
    size = f.size(g.conn)
    return render_template(
        "commits.html",
        **page_ctx(repo, f, author_id, ref, notes, rows=rows, search=search, page=page,
                   pages=pages, total=total, sel_info=sel_info, size=size))


@app.route("/r/<int:repo_id>/commits/select", methods=["POST"])
def commits_select(repo_id):
    repo = repo_or_404(repo_id)
    if repo["status"] != "ready":
        return redirect(url_for("repo_status", repo_id=repo_id))
    notes = []
    f, author_id, ref = build_filter(repo, notes)
    target = _strip_param(request.form.get("next") or url_for("commits", repo_id=repo_id), "sel")

    if request.form.get("clear") == "1":
        return redirect(target)

    if request.form.get("select_all") == "1":
        where, params = f.c_where()
        search = (request.form.get("search") or "").strip()
        if search:
            where += " AND (c.hash LIKE ? OR c.subject LIKE ?)"
            params += ["%" + search + "%", "%" + search + "%"]
        hashes = [r[0] for r in g.conn.execute(
            "SELECT c.hash FROM commits c WHERE " + where + " LIMIT ?",
            params + [MAX_SELECTION + 1]).fetchall()]
    else:
        hashes = request.form.getlist("commit")

    if len(hashes) > MAX_SELECTION:
        flash("That selection is too large (limit %d commits). Narrow it with the time range or search." % MAX_SELECTION, "error")
        return redirect(target)
    if not hashes:
        flash("No commits selected.", "error")
        return redirect(target)

    sel = uuid.uuid4().hex
    with SELECTIONS_LOCK:
        SELECTIONS[sel] = {"repo_id": repo_id, "hashes": set(hashes)}
        # Keep the store small: drop the oldest entries beyond 32.
        if len(SELECTIONS) > 32:
            for old in list(SELECTIONS)[:len(SELECTIONS) - 32]:
                SELECTIONS.pop(old, None)
    flash("Manual commit set applied: %d commits." % len(hashes), "info")
    sep = "&" if "?" in target else "?"
    return redirect(target + sep + "sel=" + sel)


# ---------------------------------------------------------------------------
# authors & merging
# ---------------------------------------------------------------------------

@app.route("/r/<int:repo_id>/authors")
def authors(repo_id):
    repo = repo_or_404(repo_id)
    redirect_resp = require_ready(repo)
    if redirect_resp:
        return redirect_resp
    notes = []
    f, author_id, ref = build_filter(repo, notes)
    summary = {a["aid"]: a for a in metrics.authors_summary(g.conn, f)}
    all_authors = metrics.list_authors(g.conn, repo_id)
    for a in all_authors:
        s = summary.get(a["aid"])
        a["in_set"] = s or {"commits": 0, "add": 0, "rem": 0, "churn": 0, "mods": 0, "ownership": 0.0}
    merges = g.conn.execute(
        "SELECT m.id, m.created_at, "
        "fa.name AS from_name, fa.email AS from_email, "
        "ta.name AS to_name, ta.email AS to_email "
        "FROM author_merges m "
        "JOIN authors fa ON fa.id = m.from_id JOIN authors ta ON ta.id = m.to_id "
        "WHERE m.repo_id=? ORDER BY m.id DESC", (repo_id,)).fetchall()
    return render_template(
        "authors.html",
        **page_ctx(repo, f, author_id, ref, notes, all_authors=all_authors, merges=merges,
                   size=f.size(g.conn)))


def _resolve_root(conn, repo_id, aid):
    """Follow merged_into links to the effective author id (cycle-guarded)."""
    seen = set()
    cur = aid
    while cur is not None and cur not in seen:
        seen.add(cur)
        row = conn.execute("SELECT merged_into FROM authors WHERE id=? AND repo_id=?",
                           (cur, repo_id)).fetchone()
        if row is None or row["merged_into"] is None:
            return cur
        cur = row["merged_into"]
    return cur


@app.route("/r/<int:repo_id>/authors/merge", methods=["POST"])
def merge_authors(repo_id):
    repo_or_404(repo_id)
    try:
        from_id = int(request.form.get("from_id", ""))
        to_id = int(request.form.get("to_id", ""))
    except ValueError:
        flash("Select two authors to merge.", "error")
        return redirect(url_for("authors", repo_id=repo_id))
    if from_id == to_id:
        flash("Cannot merge an author into themselves.", "error")
        return redirect(url_for("authors", repo_id=repo_id))
    exists = g.conn.execute(
        "SELECT COUNT(*) FROM authors WHERE repo_id=? AND id IN (?,?)",
        (repo_id, from_id, to_id)).fetchone()[0]
    if exists != 2:
        flash("Unknown author selected.", "error")
        return redirect(url_for("authors", repo_id=repo_id))
    root_to = _resolve_root(g.conn, repo_id, to_id)
    root_from = _resolve_root(g.conn, repo_id, from_id)
    if root_to == root_from:
        flash("Those authors are already merged.", "info")
        return redirect(url_for("authors", repo_id=repo_id))
    # Path-compress: move the whole group of `from` onto the root of `to`.
    g.conn.execute(
        "UPDATE authors SET merged_into=? WHERE repo_id=? AND (id=? OR merged_into=?)",
        (root_to, repo_id, root_from, root_from))
    g.conn.execute(
        "INSERT INTO author_merges(repo_id, from_id, to_id, created_at) VALUES(?,?,?,?)",
        (repo_id, from_id, root_to, datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")))
    g.conn.commit()
    flash("Authors merged.", "info")
    return redirect(url_for("authors", repo_id=repo_id))


@app.route("/r/<int:repo_id>/authors/unmerge/<int:merge_id>", methods=["POST"])
def unmerge_authors(repo_id, merge_id):
    repo_or_404(repo_id)
    g.conn.execute("DELETE FROM author_merges WHERE id=? AND repo_id=?", (merge_id, repo_id))
    _rebuild_merges(repo_id)
    flash("Merge undone.", "info")
    return redirect(url_for("authors", repo_id=repo_id))


@app.route("/r/<int:repo_id>/authors/unmerge_all", methods=["POST"])
def unmerge_all(repo_id):
    repo_or_404(repo_id)
    g.conn.execute("DELETE FROM author_merges WHERE repo_id=?", (repo_id,))
    _rebuild_merges(repo_id)
    flash("All manual merges cleared (mailmap resolution is kept).", "info")
    return redirect(url_for("authors", repo_id=repo_id))


def _rebuild_merges(repo_id):
    """Recompute merged_into from scratch by replaying the merge log."""
    g.conn.execute("UPDATE authors SET merged_into=NULL WHERE repo_id=?", (repo_id,))
    merges = g.conn.execute(
        "SELECT id, from_id, to_id FROM author_merges WHERE repo_id=? ORDER BY id",
        (repo_id,)).fetchall()
    for m in merges:
        root_to = _resolve_root(g.conn, repo_id, m["to_id"])
        g.conn.execute(
            "UPDATE authors SET merged_into=? WHERE repo_id=? AND (id=? OR merged_into=?)",
            (root_to, repo_id, m["from_id"], m["from_id"]))
    g.conn.commit()


if __name__ == "__main__":
    port = int(os.environ.get("RAT_PORT", "5000"))
    host = os.environ.get("RAT_HOST", "0.0.0.0")
    app.run(host=host, port=port, debug=False, threaded=True)
