"""Shared state for SearchGit: open repo handles, the delta index, the tree cache.

The store holds data and nothing else -- it does not decide what to index
(that's the Indexer) or how to answer queries (that's the Reader). Both of
those take a Store and work through it.
"""

import os

from dulwich.repo import Repo

# git object modes (octal in git, ints here)
MODE_TREE = 0o040000       # 16384 -> directory / subtree
MODE_SUBMODULE = 0o160000  # 57344 -> gitlink, points at another repo's commit

DELETED = None  # tombstone: path existed in the parent, not in this commit


def canonical(path: str) -> str:
    """Canonical repo key: "x.git" and "./x.git" are the same repo."""
    return os.path.realpath(path)


class Store:
    """In-memory delta index plus live git handles.

    The delta index records a (commit, path) entry only where the blob at that
    path changed relative to the commit's first parent. It is an index, not a
    source of truth -- full snapshots are read straight from git.
    """

    def __init__(self):
        """Initializes empty index tables, repo handles and tree cache."""
        # (commit_sha, filepath) -> blob_sha, or DELETED
        # only present when the path changed at that commit
        self.file_index = {}
        # (repo_path, filepath) -> [(commit_sha, blob_sha | DELETED), ...]
        # inverted view of file_index so "where did this path change?" is a
        # lookup, not a scan. keyed by repo too: two repos both having a
        # README.md must not end up sharing one history
        self.path_changes = {}
        # commit_sha -> repo path, so a read knows which repo to ask
        self.commit_repo = {}
        # canonical repo paths that have been fully indexed
        self.indexed_repos = set()
        # repo path -> open Repo. the read path needs live handles
        self.repos = {}
        # tree_sha -> [(relative_path, blob_sha), ...]
        self.tree_cache = {}

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

    def repo_for_commit(self, commit_sha: bytes) -> Repo:
        """Looks up the open Repo that owns an indexed commit.

        Raises:
            KeyError: If the commit SHA was never indexed.
        """
        path = self.commit_repo.get(commit_sha)
        if path is None:
            raise KeyError(f"commit {commit_sha!r} is not in the index")
        return self.get_repo(path)

    def one_repo(self, repo_path: str | None = None) -> str:
        """Resolves a repo key, allowing it to be omitted while only one repo is indexed.

        Raises:
            ValueError: If repo_path is omitted and several repos are indexed.
        """
        if repo_path is not None:
            return canonical(repo_path)
        if len(self.indexed_repos) == 1:
            return next(iter(self.indexed_repos))
        raise ValueError(
            f"repo_path is required with {len(self.indexed_repos)} repos indexed"
        )

    # --- delta index writes ------------------------------------------------

    def is_indexed(self, repo_key: str) -> bool:
        """True if this canonical repo path has already been indexed."""
        return repo_key in self.indexed_repos

    def add_commit(self, repo_key: str, commit_sha: bytes):
        """Records which repo a commit belongs to."""
        self.commit_repo[commit_sha] = repo_key

    def add_delta(self, repo_key: str, commit_sha: bytes, filepath: bytes, blob_sha):
        """Records that `filepath` changed to `blob_sha` (or DELETED) at `commit_sha`."""
        self.file_index[(commit_sha, filepath)] = blob_sha
        self.path_changes.setdefault((repo_key, filepath), []).append(
            (commit_sha, blob_sha)
        )

    def mark_indexed(self, repo_key: str):
        """Marks a repo as fully indexed."""
        self.indexed_repos.add(repo_key)

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
        """Closes all open repository handles."""
        for repo in self.repos.values():
            repo.close()
        self.repos.clear()
