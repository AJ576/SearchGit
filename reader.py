"""Read path: answers queries from git trees and from the Store's delta index."""

from store import DELETED, MODE_SUBMODULE, MODE_TREE, Store


def _b(value) -> bytes:
    """Accepts str or bytes, returns bytes."""
    return value.encode() if isinstance(value, str) else value


class Reader:
    """Answers queries without replaying deltas.

    Snapshots and single-file lookups go straight to git: a commit's tree
    already IS the full snapshot. File history and the list of known paths
    come from the delta index in the Store.
    """

    def __init__(self, store: Store):
        """Initializes the reader over a shared Store."""
        self.store = store

    def _repo(self, commit_sha: bytes, repo_path):
        """The repo to read from: the one given, else the one that owns the commit."""
        if repo_path:
            return self.store.get_repo(repo_path)
        return self.store.repo_for_commit(commit_sha)

    # --- reads answered by git ---------------------------------------------

    def snapshot_at(self, commit_sha, repo_path=None) -> dict[bytes, bytes]:
        """Full {path: blob_sha} at a commit, read straight from its tree.

        Args:
            commit_sha (bytes | str): The commit SHA to snapshot.
            repo_path (str, optional): Repository path (inferred from commit if omitted).
        """
        commit_sha = _b(commit_sha)
        repo = self._repo(commit_sha, repo_path)
        return dict(self.store.flatten(repo, repo[commit_sha].tree))

    def blob_at(self, commit_sha, filepath, repo_path=None):
        """Blob SHA for one path at one commit, or DELETED if absent / not a file.

        Walks only the trees along the path -- O(depth), not O(repo).

        Args:
            commit_sha (bytes | str): The commit SHA to inspect.
            filepath (bytes | str): Relative path to the file.
            repo_path (str, optional): Repository path (inferred from commit if omitted).
        """
        commit_sha = _b(commit_sha)
        repo = self._repo(commit_sha, repo_path)

        parts = [p for p in _b(filepath).split(b"/") if p]
        if not parts:
            return DELETED

        sha = repo[commit_sha].tree
        for i, part in enumerate(parts):
            try:
                mode, sha = repo[sha][part]
            except KeyError:
                return DELETED  # path does not exist at this commit

            if i == len(parts) - 1:
                # a directory or submodule is not a blob
                return DELETED if mode in (MODE_TREE, MODE_SUBMODULE) else sha
            if mode != MODE_TREE:
                return DELETED  # tried to descend through a file

        return DELETED

    # --- reads answered by the delta index ---------------------------------

    def changes_to(self, filepath, repo_path=None):
        """[(commit_sha, blob_sha | DELETED)] for commits where this path changed.

        In ingestion order, which get_walker() gives roughly newest-first --
        it is date order, not a strict chain. repo_path may be omitted only
        while a single repo is indexed.
        """
        key = self.store.one_repo(repo_path)
        return self.store.path_changes.get((key, _b(filepath)), [])

    def paths(self, repo_path=None) -> list[bytes]:
        """Every path this repo's index has ever seen."""
        key = self.store.one_repo(repo_path)
        return [p for (r, p) in self.store.path_changes if r == key]
