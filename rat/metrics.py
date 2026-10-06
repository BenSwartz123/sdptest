"""Metric queries.

Formulas follow the COMS3011A brief:

  l+_{h,f} / l-_{h,f}   per-commit line counts (indexed from git's own diff)
  delta = l+ - l-       growth
  lambda = l+ + l-      churn
  directory values      = recursive sums over every file below the directory
  repository metrics    = directory metrics of the root
  n_{H,o}               = # commits in H with lambda_{h,o} > 0   (modifications)
  eta   = n / |H|       modification frequency
  rho   = lambda_H / |H| churn rate
  author variants       = the same sums restricted to one effective author
  omega = lambda_{H,o,a} / lambda_{H,o}   ownership

A CommitFilter describes the commit set H: commits reachable from a reference
commit, optionally restricted by a committer-date range (H_t / H_i,j) or a
manually selected commit list.
"""


class CommitFilter:
    def __init__(self, repo_id, ref, mode="all", ts_from=None, ts_to=None, manual_ids=None):
        self.repo_id = repo_id
        self.ref = ref                        # reference commit h_r (usually HEAD)
        self.mode = mode                      # 'all' | 'range' | 'manual'
        self.ts_from = ts_from                # inclusive
        self.ts_to = ts_to                    # exclusive
        self.manual_ids = list(manual_ids or [])

    # -- SQL fragments ------------------------------------------------------
    def _common(self, alias, id_col):
        parts = ["%s.repo_id=?" % alias]
        params = [self.repo_id]
        if self.ref:
            parts.append(
                "%s.%s IN (SELECT commit_id FROM ref_commits WHERE repo_id=? AND ref=?)" % (alias, id_col))
            params += [self.repo_id, self.ref]
        if self.ts_from is not None:
            parts.append("%s.ts>=?" % alias)
            params.append(self.ts_from)
        if self.ts_to is not None:
            parts.append("%s.ts<?" % alias)
            params.append(self.ts_to)
        if self.mode == "manual":
            if not self.manual_ids:
                parts.append("1=0")
            else:
                parts.append("%s.%s IN (%s)" % (alias, id_col, ",".join("?" * len(self.manual_ids))))
                params += self.manual_ids
        return " AND ".join(parts), params

    def fs_where(self, alias="fs"):
        return self._common(alias, "commit_id")

    def c_where(self, alias="c"):
        return self._common(alias, "id")

    def author_cond(self, alias="fs", author_id=None):
        if author_id is None:
            return "", []
        return (
            "%s.author_id IN (SELECT id FROM authors WHERE repo_id=? AND COALESCE(merged_into,id)=?)" % alias,
            [self.repo_id, author_id],
        )

    # -- misc ---------------------------------------------------------------
    def size(self, conn):
        where, params = self.c_where()
        return conn.execute("SELECT COUNT(*) FROM commits c WHERE " + where, params).fetchone()[0]

    def describe(self):
        if self.mode == "manual":
            return "Manual selection (%d commits)" % len(self.manual_ids)
        if self.mode == "range":
            f = _fmt_ts(self.ts_from) if self.ts_from is not None else "beginning"
            t = _fmt_ts(self.ts_to) if self.ts_to is not None else "now"
            return "%s to %s" % (f, t)
        return "All history reachable from %s" % (self.ref or "HEAD")


def _fmt_ts(ts):
    import time
    return time.strftime("%Y-%m-%d %H:%M", time.gmtime(ts))


def _obj_cond(path, alias="fs", is_file=False):
    """WHERE fragment selecting one object (file or directory subtree)."""
    if path is None or path == "":
        return "", []
    if is_file:
        return "%s.path=?" % alias, [path]
    return "%s.path>=? AND %s.path<?" % (alias, alias), [path + "/", path + "0"]


def object_metrics(conn, f, path=None, is_file=False, author_id=None, size=None):
    """Metrics for one object (file, directory, or the whole repo when path is None)."""
    where, params = f.fs_where()
    acond, aparams = f.author_cond(author_id=author_id)
    if acond:
        where += " AND " + acond
        params += aparams
    ocond, oparams = _obj_cond(path, is_file=is_file)
    if ocond:
        where += " AND " + ocond
        params += oparams
    row = conn.execute(
        "SELECT COALESCE(SUM(fs.add_lines),0) AS addl, COALESCE(SUM(fs.del_lines),0) AS dell, "
        "COUNT(DISTINCT CASE WHEN fs.add_lines+fs.del_lines>0 THEN fs.commit_id END) AS mods "
        "FROM file_stats fs WHERE " + where,
        params,
    ).fetchone()
    addl, dell, mods = row["addl"], row["dell"], row["mods"]
    if size is None:
        size = f.size(conn)
    return {
        "add": addl, "rem": dell,
        "growth": addl - dell, "churn": addl + dell,
        "mods": mods,
        "size": size,  # |H|
        "freq": (mods / size) if size else 0.0,
        "churn_rate": ((addl + dell) / size) if size else 0.0,
    }


def file_rows(conn, f, search=None, sort="churn", limit=None, offset=0, author_id=None):
    """Per-file metrics over the commit set (one row per changed path)."""
    where, params = f.fs_where()
    acond, aparams = f.author_cond(author_id=author_id)
    if acond:
        where += " AND " + acond
        params += aparams
    if search:
        where += " AND fs.path LIKE ?"
        params.append("%" + search + "%")
    order = {
        "path": "fs.path",
        "add": "addl DESC",
        "rem": "dell DESC",
        "growth": "(addl-dell) DESC",
        "churn": "(addl+dell) DESC",
        "mods": "mods DESC",
    }.get(sort, "(addl+dell) DESC")
    sql = (
        "SELECT fs.path AS path, SUM(fs.add_lines) AS addl, SUM(fs.del_lines) AS dell, "
        "COUNT(DISTINCT CASE WHEN fs.add_lines+fs.del_lines>0 THEN fs.commit_id END) AS mods "
        "FROM file_stats fs WHERE " + where + " GROUP BY fs.path ORDER BY " + order
    )
    if limit is not None:
        sql += " LIMIT ? OFFSET ?"
        params = params + [limit, offset]
    rows = conn.execute(sql, params).fetchall()
    out = [dict(r) for r in rows]
    for r in out:  # convenience keys for templates/charts
        r["growth"] = r["addl"] - r["dell"]
        r["churn"] = r["addl"] + r["dell"]
    return out


def file_count(conn, f, search=None):
    where, params = f.fs_where()
    if search:
        where += " AND fs.path LIKE ?"
        params.append("%" + search + "%")
    return conn.execute(
        "SELECT COUNT(DISTINCT fs.path) FROM file_stats fs WHERE " + where, params).fetchone()[0]


def dir_paths(conn, f, dir_path):
    """Distinct changed paths below dir_path (used to derive children)."""
    where, params = f.fs_where()
    ocond, oparams = _obj_cond(dir_path)
    if ocond:
        where += " AND " + ocond
        params += oparams
    rows = conn.execute(
        "SELECT DISTINCT fs.path FROM file_stats fs WHERE " + where, params).fetchall()
    return [r["path"] for r in rows]


def dir_children(conn, f, dir_path, author_id=None, size=None):
    """Return (dirs, files) immediate children of dir_path with their metrics."""
    prefix = (dir_path + "/") if dir_path else ""
    children = {}
    for p in dir_paths(conn, f, dir_path):
        rest = p[len(prefix):]
        if not rest:
            continue
        seg, sep, tail = rest.partition("/")
        if sep:  # a directory child
            full = prefix + seg
            children.setdefault((full, True), None)
        else:
            children.setdefault((p, False), None)
    dirs, files = [], []
    for (path, is_dir) in children:
        m = object_metrics(conn, f, path=path, is_file=not is_dir, author_id=author_id, size=size)
        m["path"] = path
        m["name"] = path.rsplit("/", 1)[-1]
        (dirs if is_dir else files).append(m)
    dirs.sort(key=lambda x: (-x["churn"], x["path"]))
    files.sort(key=lambda x: (-x["churn"], x["path"]))
    return dirs, files


def object_authors(conn, f, path=None, is_file=False, author_id=None):
    """Per-author breakdown (author metrics) for one object."""
    where, params = f.fs_where()
    acond, aparams = f.author_cond(author_id=author_id)
    if acond:
        where += " AND " + acond
        params += aparams
    ocond, oparams = _obj_cond(path, is_file=is_file)
    if ocond:
        where += " AND " + ocond
        params += oparams
    rows = conn.execute(
        "SELECT COALESCE(a.merged_into, a.id) AS aid, MIN(a.name) AS name, MIN(a.email) AS email, "
        "SUM(fs.add_lines) AS addl, SUM(fs.del_lines) AS dell, "
        "COUNT(DISTINCT CASE WHEN fs.add_lines+fs.del_lines>0 THEN fs.commit_id END) AS mods, "
        "COUNT(DISTINCT fs.commit_id) AS touched "
        "FROM file_stats fs JOIN authors a ON a.id = fs.author_id "
        "WHERE " + where + " GROUP BY aid ORDER BY (SUM(fs.add_lines)+SUM(fs.del_lines)) DESC",
        params,
    ).fetchall()
    total_churn = sum(r["addl"] + r["dell"] for r in rows)
    out = []
    for r in rows:
        churn = r["addl"] + r["dell"]
        out.append({
            "aid": r["aid"], "name": r["name"], "email": r["email"],
            "add": r["addl"], "rem": r["dell"], "churn": churn, "mods": r["mods"],
            "touched": r["touched"],
            "ownership": (churn / total_churn) if total_churn else 0.0,
        })
    return out


def authors_summary(conn, f):
    """Per effective author totals over the commit set."""
    where, params = f.fs_where()
    rows = conn.execute(
        "SELECT COALESCE(a.merged_into, a.id) AS aid, MIN(a.name) AS name, MIN(a.email) AS email, "
        "SUM(fs.add_lines) AS addl, SUM(fs.del_lines) AS dell, "
        "COUNT(DISTINCT CASE WHEN fs.add_lines+fs.del_lines>0 THEN fs.commit_id END) AS mods "
        "FROM file_stats fs JOIN authors a ON a.id = fs.author_id "
        "WHERE " + where + " GROUP BY aid",
        params,
    ).fetchall()
    summary = {}
    for r in rows:
        summary[r["aid"]] = {
            "aid": r["aid"], "name": r["name"], "email": r["email"],
            "add": r["addl"], "rem": r["dell"], "churn": r["addl"] + r["dell"],
            "mods": r["mods"], "commits": 0,
        }
    cwhere, cparams = f.c_where()
    for r in conn.execute(
        "SELECT COALESCE(a.merged_into, a.id) AS aid, MIN(a.name) AS name, MIN(a.email) AS email, COUNT(*) AS n "
        "FROM commits c JOIN authors a ON a.id = c.author_id WHERE " + cwhere + " GROUP BY aid",
        cparams,
    ).fetchall():
        e = summary.setdefault(r["aid"], {
            "aid": r["aid"], "name": r["name"], "email": r["email"],
            "add": 0, "rem": 0, "churn": 0, "mods": 0, "commits": 0,
        })
        e["commits"] = r["n"]
    out = sorted(summary.values(), key=lambda x: -x["churn"])
    total = sum(x["churn"] for x in out)
    for x in out:
        x["ownership"] = (x["churn"] / total) if total else 0.0
    return out


def timeseries(conn, f, bucket=604800, path=None, is_file=False, author_id=None):
    """(churn/growth per time bucket, commits per bucket) over the commit set."""
    where, params = f.fs_where()
    acond, aparams = f.author_cond(author_id=author_id)
    if acond:
        where += " AND " + acond
        params += aparams
    ocond, oparams = _obj_cond(path, is_file=is_file)
    if ocond:
        where += " AND " + ocond
        params += oparams
    rows = conn.execute(
        "SELECT fs.ts/? AS b, SUM(fs.add_lines) AS addl, SUM(fs.del_lines) AS dell "
        "FROM file_stats fs WHERE " + where + " GROUP BY b ORDER BY b",
        [bucket] + params,
    ).fetchall()
    per_bucket = {r["b"]: {"add": r["addl"], "rem": r["dell"], "commits": 0} for r in rows}
    cwhere, cparams = f.c_where()
    for r in conn.execute(
        "SELECT c.ts/? AS b, COUNT(*) AS n FROM commits c WHERE " + cwhere + " GROUP BY b ORDER BY b",
        [bucket] + cparams,
    ).fetchall():
        e = per_bucket.setdefault(r["b"], {"add": 0, "rem": 0, "commits": 0})
        e["commits"] = r["n"]
    points = []
    for b in sorted(per_bucket):
        e = per_bucket[b]
        points.append({
            "ts": b * bucket, "add": e["add"], "rem": e["rem"],
            "churn": e["add"] + e["rem"], "growth": e["add"] - e["rem"],
            "commits": e["commits"],
        })
    return points


def repo_metric_rows(conn, f):
    """Repository + root metric summary (used by the overview)."""
    total = object_metrics(conn, f)
    return total


def list_authors(conn, repo_id):
    rows = conn.execute(
        "SELECT COALESCE(a.merged_into, a.id) AS aid, MIN(a.name) AS name, MIN(a.email) AS email, "
        "COUNT(i.name) AS n_identities "
        "FROM authors a LEFT JOIN author_identities i "
        "ON i.repo_id = a.repo_id AND i.author_id = a.id "
        "WHERE a.repo_id=? GROUP BY aid ORDER BY LOWER(MIN(a.name)), email",
        (repo_id,),
    ).fetchall()
    # Commit counts per effective author, across the whole repo.
    counts = dict(conn.execute(
        "SELECT COALESCE(a.merged_into, a.id) AS aid, COUNT(*) FROM commits c JOIN authors a ON a.id=c.author_id "
        "WHERE c.repo_id=? GROUP BY aid", (repo_id,)).fetchall())
    return [{
        "aid": r["aid"], "name": r["name"], "email": r["email"],
        "commits": counts.get(r["aid"], 0), "identities": r["n_identities"],
    } for r in rows]
