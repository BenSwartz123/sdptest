"""Thin wrappers around the git CLI.

All metric semantics (rename detection, binary detection, line counts) are
delegated to git itself so the numbers match git's own definitions.
"""
import os
import subprocess

GIT = "git"

# Max bytes of a single ref name we accept from the user (defensive).
_MAX_REF_LEN = 200


class GitError(RuntimeError):
    pass


def _base_args(work_dir, git_dir=None):
    if git_dir:
        return [GIT, "--git-dir", git_dir]
    return [GIT, "-C", work_dir]


def run_git(work_dir, git_dir=None, args=(), timeout=None, check=True, env=None):
    """Run a git command; returns CompletedProcess with text output."""
    cmd = _base_args(work_dir, git_dir) + list(args)
    e = dict(os.environ)
    if env:
        e.update(env)
    # Never prompt for credentials on clones of private repos: fail fast instead.
    e.setdefault("GIT_TERMINAL_PROMPT", "0")
    e.setdefault("GIT_ASKPASS", "true")
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, env=e,
            errors="surrogateescape",
        )
    except FileNotFoundError as exc:
        raise GitError("git executable not found on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise GitError("git command timed out: %s" % " ".join(cmd)) from exc
    if check and proc.returncode != 0:
        msg = (proc.stderr or proc.stdout or "").strip().splitlines()
        raise GitError("git %s failed: %s" % (args[0] if args else "?", msg[-1] if msg else "exit %d" % proc.returncode))
    return proc


def spawn_git_stream(work_dir, git_dir=None, args=()):
    """Start a git command and return the Popen object (stdout bytes stream)."""
    cmd = _base_args(work_dir, git_dir) + list(args)
    env = dict(os.environ)
    env.setdefault("GIT_TERMINAL_PROMPT", "0")
    try:
        return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    except FileNotFoundError as exc:
        raise GitError("git executable not found on PATH") from exc


# ---------------------------------------------------------------------------
# Repository layout detection (for uploaded zips)
# ---------------------------------------------------------------------------

def _looks_bare(path):
    return (
        os.path.isfile(os.path.join(path, "HEAD"))
        and os.path.isdir(os.path.join(path, "objects"))
        and os.path.isdir(os.path.join(path, "refs"))
    )


def detect_repo_root(extract_dir):
    """Find the git repository inside an extracted zip.

    Returns (work_dir, git_dir) where git_dir is None when the repository can
    simply be addressed with `git -C work_dir`, or a path when an explicit
    --git-dir is required (bare repository).
    """
    # Check the extract root and one/two levels below (zips often contain a
    # single top-level folder).
    candidates = [extract_dir]
    for entry in sorted(os.listdir(extract_dir)):
        p = os.path.join(extract_dir, entry)
        if os.path.isdir(p):
            candidates.append(p)
            for entry2 in sorted(os.listdir(p)):
                p2 = os.path.join(p, entry2)
                if os.path.isdir(p2):
                    candidates.append(p2)
    # Prefer a directory that has a .git entry (normal worktree clone).
    for cand in candidates:
        git_entry = os.path.join(cand, ".git")
        if os.path.isdir(git_entry):
            return cand, None
        if os.path.isfile(git_entry):
            # Worktree / submodule style pointer file: "gitdir: <path>".
            try:
                with open(git_entry, "r", encoding="utf-8", errors="replace") as fh:
                    line = fh.readline().strip()
            except OSError:
                continue
            if line.lower().startswith("gitdir:"):
                target = line.split(":", 1)[1].strip()
                if not os.path.isabs(target):
                    target = os.path.normpath(os.path.join(cand, target))
                if os.path.isdir(target):
                    return cand, target
    # Bare repository fallback.
    for cand in candidates:
        if _looks_bare(cand):
            return cand, cand
    raise GitError(
        "no git repository found in the uploaded zip (expected a .git directory or a bare repository)"
    )


# ---------------------------------------------------------------------------
# Ref handling
# ---------------------------------------------------------------------------

def validate_ref(ref):
    ref = (ref or "").strip()
    if not ref or len(ref) > _MAX_REF_LEN or ref.startswith("-"):
        return None
    # Allow branch/tag names, HEAD, short and full hashes; reject whitespace.
    if any(ch.isspace() for ch in ref):
        return None
    if not all(ch.isalnum() or ch in "./_~^@{}-" for ch in ref):
        return None
    return ref


def list_refs(work_dir, git_dir=None):
    """Return a list of selectable refs (HEAD first, then branches and tags)."""
    refs = []
    head = run_git(work_dir, git_dir, ["symbolic-ref", "-q", "HEAD"], check=False)
    head_target = head.stdout.strip() if head.returncode == 0 else "HEAD"
    out = run_git(
        work_dir, git_dir,
        ["for-each-ref", "--format=%(refname:short)", "--sort=-committerdate",
         "refs/heads", "refs/tags", "refs/remotes"],
    )
    for line in out.stdout.splitlines():
        line = line.strip()
        if line and line not in refs:
            refs.append(line)
    result = []
    if head_target and head_target != "HEAD":
        result.append(head_target)
    result.append("HEAD")
    for r in refs:
        if r not in result and not r.endswith("/HEAD"):
            result.append(r)
    return result


def rev_list(work_dir, git_dir, ref):
    """Commit hashes reachable from ref (all, including merges)."""
    out = run_git(work_dir, git_dir, ["rev-list", ref], timeout=300)
    return out.stdout.split()


def head_commit(work_dir, git_dir=None):
    proc = run_git(work_dir, git_dir, ["rev-parse", "--verify", "-q", "HEAD"], check=False)
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


# ---------------------------------------------------------------------------
# Author / mailmap resolution
# ---------------------------------------------------------------------------

def check_mailmap(work_dir, git_dir, name, email):
    """Resolve one author identity through the repository .mailmap (via git)."""
    ident = "%s <%s>" % (name, email)
    proc = run_git(work_dir, git_dir, ["check-mailmap", ident], check=False, timeout=30)
    if proc.returncode != 0:
        return name, email
    line = proc.stdout.strip()
    if line.endswith(">") and "<" in line:
        cname, rest = line.rsplit("<", 1)
        cemail = rest[:-1].strip()
        return cname.strip(), cemail
    return name, email
