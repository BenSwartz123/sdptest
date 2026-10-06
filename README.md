# RAT - Repository Analysis Tool

A multi-repository web dashboard that computes the COMS3011A metrics for the
**authors, files, directories and commit sets** of Git repositories, with
filtering, author merging and multi-repo support.

All statistics come from git's own diff machinery (`git log --numstat` with
rename detection at 50%), so line counts, binary detection and rename
attribution follow git's definitions exactly.

## Requirements

- Python 3.10+ (tested on 3.12) and `pip`
- Git 2.30+ (tested on 2.43), available on `PATH`

Only one Python dependency (Flask) is needed; charts are hand-rolled SVG with
no external assets, and all storage is SQLite from the standard library.

## Quick start (fresh clone)

```bash
git clone <this-repo-url>
cd sdptest
python3 -m pip install -r requirements.txt   # installs Flask
python3 app.py
```

Then open <http://127.0.0.1:5000> in a browser.

## Adding repositories

On the home page, either:

- **Clone from URL** - paste an `https://`, `git://` or `ssh://` URL and the
  server makes a full mirror clone (every ref and the complete history) and
  indexes it in the background, or
- **Upload a zip** - a `.zip` of a repository *including* its `.git`
  directory (the archive root may be the repo itself or contain a single
  top-level folder).

Progress is shown live and the repository page opens automatically when
indexing finishes. Multiple repositories can be ingested, switched between
and deleted independently.

## Features

- **Metric categories**: file, directory (recursive sums), repository
  (= root directory) and commit set (whole history, time period `H_i,j`, or a
  manually selected commit list).
- **Metrics**: added/removed lines `l+`/`l-`, growth `delta = l+ - l-`,
  churn `lambda = l+ + l-`, modifications `n` (commits with `lambda > 0`),
  modification frequency `eta = n / |H|`, churn rate `rho = lambda / |H|`,
  and per-author ownership `omega = lambda_{H,o,a} / lambda_{H,o}`.
- **Reference commit selection**: metrics are measured over all non-merge
  commits reachable from a reference commit (default `HEAD`); pick any
  branch, tag or paste a commit hash.
- **Filters**: repository, author, file/directory and commit set
  (quick ranges, custom `from`/`to` dates, or hand-picked commits).
- **Author merging**: `.mailmap` files are applied automatically at index
  time; identities can also be merged manually in the UI, with undo.
- **Visualisation**: activity-over-time chart (churn/growth/commits per
  week), top-authors donut, most-changed-files bars, per-object drill-down.
- **Quality of life**: file search, sortable columns, pagination, CSV export,
  persistent filter links, quick ranges, breadcrumb navigation.

## Metric semantics

- `l+`/`l-` for a commit are taken against its previous state using git's
  diff (root commits diff against the empty tree).
- Binary files are not measured; rename detection is at 50% and statistics
  are attributed to the **new** path; deletions count removed lines on the
  old path; merge commits are excluded from `H`.
- Time filters use the **committer date**, `from` inclusive and `to`
  exclusive.

## Self-test

```bash
python3 selftest.py
```

Builds synthetic repositories with deterministic dates and asserts exact
metric values (renames, binary files, deletions, merges, mailmap, manual
author merging, time ranges, manual commit sets, alternate refs), exercises
zip ingestion through the real upload endpoint, and smoke-tests every HTTP
route. Prints `All 62 checks passed.` when everything is correct.

## Notes

- Indexed data and cloned repositories live in `./data/` (git-ignored).
  Set `RAT_DATA=/some/dir` to store them elsewhere.
- The app listens on `0.0.0.0:5000`; override with `RAT_HOST` / `RAT_PORT`.
- No credentials or secrets are required or stored.
