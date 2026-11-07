"""Throwaway console harness for poking at the index.

Hardcoded to physics_engine.git for now -- the indexer itself takes any path.
"""

import inspect
import sys

from indexer import Indexer
from reader import Reader
from store import DELETED, Store, canonical

REPO = "physics_engine.git"

HELP = """
commands (<commit> defaults to HEAD, short shas ok)

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
    """Returns an 8-character prefix of a SHA hash for compact display.

    Args:
        sha (bytes | str): The Git object SHA (either binary or string).

    Returns:
        str: The first 8 characters of the hex SHA.
    """
    return sha.decode()[:8] if isinstance(sha, bytes) else str(sha)[:8]


def subject(commit):
    """Extracts the first line (subject line) of a Git commit message.

    Args:
        commit (Commit): Dulwich Commit object.

    Returns:
        str: First line of the commit message, decoded with replacement characters.
    """
    lines = commit.message.decode(errors="replace").splitlines()
    return lines[0] if lines else ""


def show(path):
    """Safely decodes a raw filepath into a printable string.

    Args:
        path (bytes | str): File path as bytes or str.

    Returns:
        str: Decoded string representation of the path.
    """
    return path.decode(errors="replace") if isinstance(path, bytes) else str(path)


class Harness:
    """Console test harness that provides interactive commands over the index."""

    def __init__(self, path):
        """Initializes the harness, indexes the repository, and caches HEAD.

        Args:
            path (str): File system path to the Git repository to index.
        """
        self.store = Store()
        self.indexer = Indexer(self.store)
        self.reader = Reader(self.store)
        self.indexer.index_repo(path)
        # every read names this repo explicitly -- other indexed repos may
        # share commit shas with it (forks, clones)
        self.repo_path = canonical(path)
        self.repo = self.store.get_repo(path)
        self.head = self.repo.head()

    # --- helpers ----------------------------------------------------------

    def resolve(self, ref):
        """Resolves a user-provided commit reference to a full commit SHA in bytes.

        Accepts None, 'HEAD', a short SHA prefix, or a full SHA. Searches
        indexed commits for a matching prefix.

        Args:
            ref (str | None): Commit reference entered by the user.

        Returns:
            bytes: Full 40-character commit SHA in bytes.

        Raises:
            ValueError: If no commit matches or if the prefix is ambiguous.
        """
        if ref is None or ref.upper() == "HEAD":
            return self.head
        want = ref.lower().encode()
        hits = [c for c in self.store.commits_in(self.repo_path) if c.startswith(want)]
        if len(hits) == 1:
            return hits[0]
        if not hits:
            raise ValueError(f"no indexed commit starts with {ref!r}")
        raise ValueError(f"{ref!r} is ambiguous: {', '.join(short(h) for h in hits)}")

    # --- commands ---------------------------------------------------------

    def cmd_files(self, commit=None):
        """Command 'files [commit]': prints all file paths and blob SHAs at a commit.

        Args:
            commit (str, optional): Commit reference or None (defaults to HEAD).
        """
        sha = self.resolve(commit)
        snap = self.reader.snapshot_at(sha, repo_path=self.repo_path)
        for path in sorted(snap):
            print(f"  {short(snap[path])}  {show(path)}")
        print(f"{len(snap)} files at {short(sha)}")

    def cmd_blob(self, path, commit=None):
        """Command 'blob <path> [commit]': prints the blob SHA of a specific file.

        Args:
            path (str): Relative file path within the repository.
            commit (str, optional): Commit reference or None (defaults to HEAD).
        """
        sha = self.resolve(commit)
        blob = self.reader.blob_at(sha, path, repo_path=self.repo_path)
        if blob is DELETED:
            # V0 cannot tell apart absent / directory / submodule
            print(f"  {path} is not a file at {short(sha)}")
        else:
            print(f"  {blob.decode()}")

    def cmd_cat(self, path, commit=None):
        """Command 'cat <path> [commit]': displays file contents at a given commit.

        Args:
            path (str): Relative file path to display.
            commit (str, optional): Commit reference or None (defaults to HEAD).
        """
        sha = self.resolve(commit)
        blob = self.reader.blob_at(sha, path, repo_path=self.repo_path)
        if blob is DELETED:
            print(f"  {path} is not a file at {short(sha)}")
            return
        print(self.repo[blob].data.decode(errors="replace"))

    def cmd_changes(self, path):
        """Command 'changes <path>': lists commits where a specific file was modified or deleted.

        Args:
            path (str): Relative file path whose change history to look up.
        """
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
        """Command 'paths': lists all paths ever indexed and the count of commits touching each."""
        rows = sorted(self.reader.paths(repo_path=self.repo_path))
        for path in rows:
            n = len(self.reader.changes_to(path, repo_path=self.repo_path))
            print(f"  {n:4}  {show(path)}")
        print(f"{len(rows)} paths")

    def cmd_log(self, n="10"):
        """Command 'log [n]': prints the N most recent commits (default 10) in reverse date order.

        Args:
            n (str): Number of commits to show (default '10').
        """
        for entry in self.repo.get_walker(max_entries=int(n)):
            print(f"  {short(entry.commit.id)}  {subject(entry.commit)[:60]}")

    def cmd_stats(self):
        """Command 'stats': displays index size metrics and cache counts."""
        print(f"  repos indexed:  {len(self.store.indexed_repos)}")
        print(f"  commits:        {len(self.store.commits_in(self.repo_path))}")
        print(f"  delta entries:  {sum(1 for k in self.store.file_index if k[0] == self.repo_path)}")
        print(f"  paths tracked:  {len(self.reader.paths(repo_path=self.repo_path))}")
        print(f"  trees cached:   {len(self.store.tree_cache)}")


def main():
    """Runs the interactive REPL loop, parsing commands and dispatching them to Harness."""
    h = Harness(REPO)
    print(HELP)

    table = {
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
            line = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not line:
            continue

        cmd, *args = line.split()
        if cmd in ("quit", "exit", "q"):
            break
        if cmd in ("help", "?"):
            print(HELP)
            continue

        fn = table.get(cmd)
        if fn is None:
            print(f"  unknown command {cmd!r} -- try 'help'")
            continue
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
