"""Shared state for SearchGit: open repo handles, the delta index, blob tables, the tree cache.

The store holds the data and the low-level git reads both paths need
(opening repos, flattening trees, reading blobs). It does not decide what
to index (that's the Indexer) or how to answer queries (that's the Reader).
Both of those take a Store and work through it.
"""

import os

from dulwich.repo import Repo
from db import Database

# git object modes (octal in git, ints here)
MODE_TREE = 0o040000       # 16384 -> directory / subtree
MODE_SUBMODULE = 0o160000  # 57344 -> gitlink, points at another repo's commit

DELETED = None  # tombstone: path existed in the parent, not in this commit


def canonical(path: str) -> str:
    """Canonical repo key: "x.git" and "./x.git" are the same repo."""
    return os.path.realpath(path)


class Store:
    """In-memory delta index and blob tables, plus the git access both paths share.

    The delta index records a (commit, path) entry only where the blob at that
    path changed relative to the commit's first parent. It is an index, not a
    source of truth -- full snapshots and blob contents are read straight from
    git, through the handles and helpers here (get_repo, flatten, read_blob).
    The store does not decide what to index or how to answer queries; that's
    the Indexer and the Reader.
    """

    def __init__(self, db: Database | None = None, db_path: str | None = None):
        """Initializes empty index tables, repo handles, tree cache, and optional SQLite DB."""
        if db is not None:
            self.db = db
        elif db_path is not None:
            self.db = Database(db_path)
        else:
            self.db = None

        # (repo_path, commit_sha, filepath) -> blob_sha, or DELETED
        # only present when the path changed at that commit. keyed by repo
        # because the same commit can diff differently in different copies
        # (a shallow clone's boundary commit records every file)
        self.file_index = {}
        # (repo_path, filepath) -> [(commit_sha, blob_sha | DELETED), ...]
        # inverted view of file_index so "where did this path change?" is a
        # lookup, not a scan. keyed by repo too: two repos both having a
        # README.md must not end up sharing one history
        self.path_changes = {}
        # canonical repo path -> {commit shas indexed in it}. repo first, like
        # every other table: forks, clones and mirrors share commit shas, so
        # a commit sha alone does not identify which repo to read from
        self.repo_commits = {}
        # canonical repo paths that have been fully indexed
        self.indexed_repos = set()
        # repo path -> open Repo. the read path needs live handles
        self.repos = {}
        # tree_sha -> [(relative_path, blob_sha), ...]
        self.tree_cache = {}

        # --- blob content tables, global across repos: identical content
        # gets one id no matter how many repos, commits or paths hold it
        # blob_sha -> int id
        self.blob_ids = {}
        # int id -> blob_sha
        self.blob_shas = []
        # int id -> canonical path of a repo the blob can be read from
        self.blob_repo = {}
        # new int ids not yet run through the trigram pass
        self.pending = []
        # blob_sha -> [(repo_path, commit_sha, filepath), ...] for every
        # commit that wrote this blob to a path
        self.blob_occurrences = {}
        # (repo_path, commit_sha) -> commit time, unix seconds
        self.commit_times = {}

        if self.db is not None:
            self._load_from_db()

    def _load_from_db(self):
        """Restores indexed state, commits, blobs, and deltas from SQLite."""
        self.indexed_repos = self.db.get_indexed_repos()
        for repo_key in self.indexed_repos:
            self.repo_commits[repo_key] = self.db.get_all_commits_in(repo_key)
        self.commit_times.update(self.db.get_all_commit_times())

        blob_ids, blob_shas, blob_repo = self.db.get_all_blobs()
        self.blob_ids.update(blob_ids)
        self.blob_shas = blob_shas
        self.blob_repo.update(blob_repo)

        path_changes = self.db.get_all_path_changes()
        self.path_changes.update(path_changes)
        for (repo_key, filepath), changes in path_changes.items():
            for commit_sha, blob_sha in changes:
                self.file_index[(repo_key, commit_sha, filepath)] = blob_sha

        self.blob_occurrences.update(self.db.get_all_blob_occurrences())

    # --- repo handles ------------------------------------------------------

    def get_repo(self, path: str) -> Repo:
        """Opens a Git repo once and keeps the handle.

        Args:
            path (str): File system path to the Git repository.

        Returns:
            Repo: Open Dulwich repository instance.
        """
        key = canonical(path)
        repo = self.repos.get(key)
        if repo is None:
            repo = Repo(key)
            self.repos[key] = repo
        return repo

    def repo_for_commit(self, commit_sha: bytes, repo_path: str | None = None) -> Repo:
        """Looks up the open Repo to read an indexed commit from.

        The repo is resolved first (given, or the only one indexed), then the
        commit is checked against that repo -- never the other way round.

        Raises:
            ValueError: If repo_path is omitted and several repos are indexed.
            KeyError: If the commit was never indexed in that repo.
        """
        key = self.one_repo(repo_path)
        if commit_sha not in self.repo_commits.get(key, ()):
            raise KeyError(f"commit {commit_sha!r} is not indexed in {key}")
        return self.get_repo(key)

    def commits_in(self, repo_key: str) -> frozenset[bytes]:
        """Every indexed commit sha in this canonical repo path (a read-only copy)."""
        return frozenset(self.repo_commits.get(repo_key, ()))

    def one_repo(self, repo_path: str | None = None) -> str:
        """Resolves a repo key, allowing it to be omitted while only one repo is indexed.

        Raises:
            ValueError: If repo_path is omitted and several repos are indexed.
        """
        if repo_path is not None:
            return canonical(repo_path)
        if len(self.indexed_repos) == 1:
            return next(iter(self.indexed_repos))
        if not self.indexed_repos:
            raise ValueError("no repos are fully indexed yet")
        raise ValueError(
            f"repo_path is required with {len(self.indexed_repos)} repos indexed"
        )

    # --- delta index writes ------------------------------------------------

    def is_indexed(self, repo_key: str) -> bool:
        """True if this canonical repo path has already been indexed."""
        return repo_key in self.indexed_repos

    def add_commit(self, repo_key: str, commit_sha: bytes, commit_time: int | None = None):
        """Records which repo a commit belongs to."""
        self.repo_commits.setdefault(repo_key, set()).add(commit_sha)
        if commit_time is not None:
            self.commit_times[(repo_key, commit_sha)] = commit_time
        if self.db is not None:
            self.db.record_commit(repo_key, commit_sha, commit_time)

    def add_delta(self, repo_key: str, commit_sha: bytes, filepath: bytes, blob_sha):
        """Records that `filepath` changed to `blob_sha` (or DELETED) at `commit_sha`.

        If blob_sha is not DELETED: assigns id if new (append to pending),
        and ALWAYS appends occurrence to blob_occurrences.
        """
        self.file_index[(repo_key, commit_sha, filepath)] = blob_sha
        self.path_changes.setdefault((repo_key, filepath), []).append(
            (commit_sha, blob_sha)
        )
        if blob_sha is not DELETED:
            self._blob_id(blob_sha, repo_key)
            self.blob_occurrences.setdefault(blob_sha, []).append(
                (repo_key, commit_sha, filepath)
            )
        if self.db is not None:
            self.db.insert_delta(repo_key, commit_sha, filepath, blob_sha)

    def _blob_id(self, blob_sha: bytes, repo_key: str):
        """Assigns an id to a blob seen for the first time; no-op if it has one.

        A new id is queued in `pending` for the trigram pass, and remembers
        `repo_key` as the repo to read the blob's contents from.
        """
        if blob_sha in self.blob_ids:
            return
        blob_id = len(self.blob_shas)
        self.blob_ids[blob_sha] = blob_id
        self.blob_shas.append(blob_sha)
        self.pending.append(blob_id)
        self.blob_repo[blob_id] = repo_key
        if self.db is not None:
            self.db.record_blob(blob_id, blob_sha, repo_key)

    def take_pending(self) -> list[int]:
        """Return pending ids and clear the list."""
        pending = self.pending
        self.pending = []
        return pending

    def read_blob(self, blob_id: int) -> bytes | None:
        """Raw contents of a blob, by id, read from git.

        Returns:
            bytes | None: The blob's bytes, or None if the id is unknown or the
                blob is missing from the repo it was recorded in.
        """
        try:
            repo_key = self.blob_repo[blob_id]
            blob_sha = self.blob_shas[blob_id]
            repo = self.get_repo(repo_key)
            return repo[blob_sha].data
        except (KeyError, IndexError):
            return None

    def mark_indexed(self, repo_key: str):
        """Marks a repo as fully indexed."""
        self.indexed_repos.add(repo_key)
        if self.db is not None:
            self.db.record_repo(repo_key)
            self.db.commit()

    # --- git tree helpers (shared by indexer and reader) -------------------

    def flatten(self, repo: Repo, tree_sha: bytes, prefix: bytes = b""):
        """Flattens a tree into every (relative_path, blob_sha) under it.

        Memoized by tree_sha, so commits that share subtrees share the work.
        Submodules are skipped -- a gitlink is a commit elsewhere, not a blob.

        Args:
            repo (Repo): An open Dulwich repository object.
            tree_sha (bytes): SHA of the Git tree to flatten.
            prefix (bytes): Prefix prepended to every returned path.

        Returns:
            list[tuple[bytes, bytes]]: `(path, blob_sha)` for all blobs in the tree.
                Treat as read-only: with an empty prefix this is the cached list.
        """
        cached = self.tree_cache.get(tree_sha)
        if cached is None:
            cached = []
            for name, mode, obj_sha in repo[tree_sha].iteritems():
                if mode == MODE_TREE:
                    cached.extend(self.flatten(repo, obj_sha, name + b"/"))
                elif mode == MODE_SUBMODULE:
                    continue
                else:
                    cached.append((name, obj_sha))
            self.tree_cache[tree_sha] = cached
        if not prefix:
            return cached
        return [(prefix + p, sha) for p, sha in cached]

    def close(self):
        """Closes all open repository handles and the database connection."""
        for repo in self.repos.values():
            repo.close()
        self.repos.clear()
        if self.db is not None:
            self.db.close()
