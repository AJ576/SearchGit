"""Throwaway console harness for poking at the index.

Usage: python main.py [repo.git ...]     (defaults to physics_engine.git)

Every repo given on the command line is indexed into one shared Store. One
repo is "current" at a time; files/blob/cat/changes/paths/log/stats and the
search commands all work on the current repo. Switch with `use <name>`.
"""

import inspect
import os
import sys
import time

from indexer import Indexer
from reader import Hit, Reader
from store import DELETED, Store, canonical
from trigram import TrigramIndex

DEFAULT_REPOS = ["physics_engine.git"]
TOP_N = 10
# commands whose argument is free text: keep the rest of the line exactly as
# typed, so spacing inside the query survives
TEXT_COMMANDS = {"search", "first", "died"}

HELP = """
commands (<commit> defaults to HEAD, short shas ok; all act on the current repo)

  repos                    indexed repos, * marks the current one
  use <repo>               switch current repo (name or unique prefix)

  search [-i] <text>       top 10 file versions containing text, oldest first
                           (-i = ignore case)
  first [-i] <text>        earliest commit that introduced the text
  died [-i] <text>         commits where the text disappeared from a path

  files [commit]           every path at that commit, read from git
  blob <path> [commit]     blob sha for one path at that commit
  cat <path> [commit]      the file's actual contents at that commit
  changes <path>           commits where that path changed, from the index
  paths                    every path the index has ever seen
  log [n]                  n most recent commits (default 10)
  stats                    index size
  help                     this
  quit                     done
"""


def short(sha):
    """Returns an 8-character prefix of a SHA hash for compact display."""
    return sha.decode()[:8] if isinstance(sha, bytes) else str(sha)[:8]


def subject(commit):
    """First line of a Git commit message, decoded with replacement characters."""
    lines = commit.message.decode(errors="replace").splitlines()
    return lines[0] if lines else ""


def show(path):
    """Safely decodes a raw filepath into a printable string."""
    return path.decode(errors="replace") if isinstance(path, bytes) else str(path)


def day(ts):
    """Unix time -> YYYY-MM-DD (UTC), or '?' if unknown."""
    return time.strftime("%Y-%m-%d", time.gmtime(ts)) if ts is not None else "?"


def when(occ):
    """Sort key for an Occurrence: commit time, unknown times last."""
    return occ.commit_time if occ.commit_time is not None else float("inf")


class Harness:
    """Console test harness that provides interactive commands over the index."""

    def __init__(self, paths):
        """Indexes every repo in `paths` into one shared store; first is current."""
        self.store = Store()
        self.trigrams = TrigramIndex()
        self.indexer = Indexer(self.store, self.trigrams)
        self.reader = Reader(self.store, self.trigrams)

        self.names = {}  # display name -> canonical repo path
        for path in paths:
            key = canonical(path)
            if key in self.names.values():
                continue
            self.indexer.index_repo(path)
            base = os.path.basename(key.rstrip("/")) or key
            name, n = base, 2
            while name in self.names:
                name, n = f"{base}-{n}", n + 1
            self.names[name] = key
        self.current = next(iter(self.names))

    # --- current repo -----------------------------------------------------

    @property
    def repo_path(self):
        """Canonical path of the current repo. Every read names it explicitly."""
        return self.names[self.current]

    @property
    def repo(self):
        """Open Dulwich handle for the current repo."""
        return self.store.get_repo(self.repo_path)

    @property
    def head(self):
        """HEAD commit sha of the current repo."""
        return self.repo.head()

    # --- helpers ----------------------------------------------------------

    def resolve(self, ref):
        """Resolves None / 'HEAD' / a short or full sha to a full commit sha (bytes)."""
        if ref is None or ref.upper() == "HEAD":
            return self.head
        want = ref.lower().encode()
        hits = [c for c in self.store.commits_in(self.repo_path) if c.startswith(want)]
        if len(hits) == 1:
            return hits[0]
        if not hits:
            raise ValueError(f"no indexed commit starts with {ref!r}")
        raise ValueError(f"{ref!r} is ambiguous: {', '.join(short(h) for h in hits)}")

    def _search(self, query):
        """Search scoped to the current repo.

        Reader.search is global (every repo, all history). We keep only the
        occurrences in the current repo, drop blobs that never appeared here,
        and re-sort by when each blob first appeared *in this repo*.
        A leading `-i ` makes the match case-insensitive.
        """
        case_sensitive = True
        if query.startswith("-i "):
            case_sensitive, query = False, query[3:]
        hits = []
        for hit in self.reader.search(query, case_sensitive=case_sensitive):
            occs = [o for o in hit.occurrences if o.repo == self.repo_path]
            if occs:
                hits.append(Hit(hit.blob_sha, occs))
        hits.sort(key=lambda h: when(h.first_seen))
        return hits

    def _timeline(self, hits):
        """Per-path births and deaths of a string, from the delta index.

        A path "has" the string at a commit if the blob it changed to is one
        of the hits. Walks each path's changes oldest -> newest, recording
        births (absent -> present) and deaths (present -> absent).

        Returns (events, alive): events are (time, kind, path, commit, how),
        alive is the paths still containing the string at their last change.
        """
        matching = {h.blob_sha for h in hits}
        paths = {o.path for h in hits for o in h.occurrences}

        events, alive = [], []
        for path in paths:
            rows = self.reader.changes_to(path, repo_path=self.repo_path)
            # changes_to is roughly newest-first; reverse, then stable-sort by time
            rows = sorted(
                reversed(rows),
                key=lambda r: self.store.commit_times.get((self.repo_path, r[0]), 0),
            )
            had = False
            for commit, blob in rows:
                has = blob is not DELETED and blob in matching
                t = self.store.commit_times.get((self.repo_path, commit))
                if has and not had:
                    events.append((t, "born", path, commit, ""))
                elif had and not has:
                    how = "file deleted" if blob is DELETED else "edited out"
                    events.append((t, "died", path, commit, how))
                had = has
            if had:
                alive.append(path)
        events.sort(key=lambda e: e[0] or 0)
        return events, alive

    # --- repo commands ----------------------------------------------------

    def cmd_repos(self):
        """Command 'repos': lists indexed repos."""
        for name, key in self.names.items():
            mark = "*" if name == self.current else " "
            n = len(self.store.commits_in(key))
            print(f"  {mark} {name:24} {n:6} commits  {key}")

    def cmd_use(self, name):
        """Command 'use <repo>': switches the current repo (exact name or unique prefix)."""
        if name in self.names:
            self.current = name
        else:
            matches = [n for n in self.names if n.startswith(name)]
            if len(matches) != 1:
                raise ValueError(
                    f"no repo named {name!r}" if not matches
                    else f"{name!r} is ambiguous: {', '.join(matches)}"
                )
            self.current = matches[0]
        print(f"  now in {self.current}")

    # --- search commands --------------------------------------------------

    def cmd_search(self, query):
        """Command 'search [-i] <text>': top file versions containing text, oldest first."""
        hits = self._search(query)
        for hit in hits[:TOP_N]:
            o = hit.first_seen
            more = len(hit.occurrences) - 1
            extra = f"  (+{more} more places)" if more else ""
            print(f"  {short(hit.blob_sha)}  {day(o.commit_time)}  {short(o.commit)}  "
                  f"{show(o.path)}{extra}")
        print(f"{len(hits)} matching file versions in {self.current}, "
              f"showing {min(TOP_N, len(hits))}")

    def cmd_first(self, query):
        """Command 'first [-i] <text>': the earliest commit that introduced the text."""
        events, _ = self._timeline(self._search(query))
        births = [e for e in events if e[1] == "born"]
        if not births:
            print("  not found")
            return
        t, _, path, commit, _ = births[0]
        print(f"  {day(t)}  {short(commit)}  {show(path)}  "
              f"{subject(self.repo[commit])[:46]}")

    def cmd_died(self, query):
        """Command 'died [-i] <text>': commits where the text disappeared from a path."""
        events, alive = self._timeline(self._search(query))
        deaths = [e for e in events if e[1] == "died"]
        for t, _, path, commit, how in deaths:
            print(f"  {day(t)}  {short(commit)}  {show(path):30}  {how:12}  "
                  f"{subject(self.repo[commit])[:36]}")
        print(f"{len(deaths)} deaths; still present in {len(alive)} path(s)")
        for p in sorted(alive):
            print(f"    alive: {show(p)}")

    # --- git / index commands --------------------------------------------

    def cmd_files(self, commit=None):
        """Command 'files [commit]': all file paths and blob SHAs at a commit."""
        sha = self.resolve(commit)
        snap = self.reader.snapshot_at(sha, repo_path=self.repo_path)
        for path in sorted(snap):
            print(f"  {short(snap[path])}  {show(path)}")
        print(f"{len(snap)} files at {short(sha)}")

    def cmd_blob(self, path, commit=None):
        """Command 'blob <path> [commit]': the blob SHA of a specific file."""
        sha = self.resolve(commit)
        blob = self.reader.blob_at(sha, path, repo_path=self.repo_path)
        if blob is DELETED:
            # V0 cannot tell apart absent / directory / submodule
            print(f"  {path} is not a file at {short(sha)}")
        else:
            print(f"  {blob.decode()}")

    def cmd_cat(self, path, commit=None):
        """Command 'cat <path> [commit]': file contents at a given commit."""
        sha = self.resolve(commit)
        blob = self.reader.blob_at(sha, path, repo_path=self.repo_path)
        if blob is DELETED:
            print(f"  {path} is not a file at {short(sha)}")
            return
        print(self.repo[blob].data.decode(errors="replace"))

    def cmd_changes(self, path):
        """Command 'changes <path>': commits where a specific file was modified or deleted."""
        rows = self.reader.changes_to(path, repo_path=self.repo_path)
        if not rows:
            print(f"  no recorded changes to {path}")
            return
        for commit_sha, blob_sha in rows:
            what = "DELETED " if blob_sha is DELETED else short(blob_sha) + " "
            commit = self.repo[commit_sha]
            # merges are not flagged yet, so some of these rows are a merge
            # differing from its first parent rather than a real edit
            print(f"  {short(commit_sha)}  {what} {subject(commit)[:46]}")
        print(f"{len(rows)} changes to {path}")

    def cmd_paths(self):
        """Command 'paths': every path ever indexed, with how many commits touched it."""
        rows = sorted(self.reader.paths(repo_path=self.repo_path))
        for path in rows:
            n = len(self.reader.changes_to(path, repo_path=self.repo_path))
            print(f"  {n:4}  {show(path)}")
        print(f"{len(rows)} paths")

    def cmd_log(self, n="10"):
        """Command 'log [n]': the N most recent commits (default 10)."""
        for entry in self.repo.get_walker(max_entries=int(n)):
            print(f"  {short(entry.commit.id)}  {subject(entry.commit)[:60]}")

    def cmd_stats(self):
        """Command 'stats': index size for the current repo, plus global trigram counts."""
        print(f"  repo:           {self.current}")
        print(f"  commits:        {len(self.store.commits_in(self.repo_path))}")
        print(f"  delta entries:  {sum(1 for k in self.store.file_index if k[0] == self.repo_path)}")
        print(f"  paths tracked:  {len(self.reader.paths(repo_path=self.repo_path))}")
        print(f"  trees cached:   {len(self.store.tree_cache)}")
        print(f"  -- all repos --")
        print(f"  unique blobs:   {len(self.store.blob_shas)}")
        print(f"  trigrammed:     {len(self.trigrams.indexed)}")
        print(f"  skipped:        {len(self.trigrams.skipped)}")
        print(f"  distinct trigrams: {len(self.trigrams.postings)}")


def main(argv=None):
    """Runs the interactive REPL loop, parsing commands and dispatching them to Harness."""
    paths = (sys.argv[1:] if argv is None else argv) or DEFAULT_REPOS
    h = Harness(paths)
    print(HELP)

    table = {
        "repos": h.cmd_repos,
        "use": h.cmd_use,
        "search": h.cmd_search,
        "first": h.cmd_first,
        "died": h.cmd_died,
        "files": h.cmd_files,
        "blob": h.cmd_blob,
        "cat": h.cmd_cat,
        "changes": h.cmd_changes,
        "paths": h.cmd_paths,
        "log": h.cmd_log,
        "stats": h.cmd_stats,
    }

    while True:
        try:
            line = input(f"{h.current}> ")
        except (EOFError, KeyboardInterrupt):
            print()
            break
        # split off only the command word; the rest of the line is untouched
        cmd, _, rest = line.strip().partition(" ")
        if not cmd:
            continue
        if cmd in ("quit", "exit", "q"):
            break
        if cmd in ("help", "?"):
            print(HELP)
            continue

        fn = table.get(cmd)
        if fn is None:
            print(f"  unknown command {cmd!r} -- try 'help'")
            continue
        # free-text commands get the rest of the line as one string; the
        # others get whitespace-split arguments
        args = [rest] if cmd in TEXT_COMMANDS else rest.split()
        # check arity up front so a TypeError raised *inside* a command
        # surfaces as a real traceback instead of a usage message
        try:
            inspect.signature(fn).bind(*args)
        except TypeError:
            print(f"  wrong number of arguments for {cmd!r} -- try 'help'")
            continue
        try:
            fn(*args)
        except (ValueError, KeyError) as e:
            print(f"  {e}")

    h.store.close()


if __name__ == "__main__":
    sys.exit(main())