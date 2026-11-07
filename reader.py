"""Read path: answers queries from git trees and from the Store's delta index."""

from dataclasses import dataclass

from store import DELETED, MODE_SUBMODULE, MODE_TREE, Store
from trigram import TrigramIndex


def _b(value) -> bytes:
    """Accepts str or bytes, returns bytes."""
    return value.encode() if isinstance(value, str) else value


@dataclass
class Occurrence:
    repo: str
    commit: bytes
    path: bytes
    commit_time: int | None


@dataclass
class Hit:
    blob_sha: bytes
    occurrences: list[Occurrence]      # oldest first

    @property
    def first_seen(self) -> Occurrence:
        return self.occurrences[0]


class Reader:
    """Answers queries without replaying deltas.

    Snapshots and single-file lookups go straight to git: a commit's tree
    already IS the full snapshot. File history and the list of known paths
    come from the delta index in the Store.
    """

    def __init__(self, store: Store, trigrams: TrigramIndex):
        """Initializes the reader over a shared Store and TrigramIndex."""
        self.store = store
        self.trigrams = trigrams

    def _repo(self, commit_sha: bytes, repo_path):
        """The repo to read from: the one given, else the only one indexed; the commit must be in it."""
        return self.store.repo_for_commit(commit_sha, repo_path)

    # --- reads answered by git ---------------------------------------------

    def snapshot_at(self, commit_sha, repo_path=None) -> dict[bytes, bytes]:
        """Full {path: blob_sha} at a commit, read straight from its tree.

        Args:
            commit_sha (bytes | str): The commit SHA to snapshot.
            repo_path (str, optional): Repository path (required if several repos are indexed).
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
            repo_path (str, optional): Repository path (required if several repos are indexed).
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

    # --- reads answered by trigrams and store ------------------------------

    def search(self, query, case_sensitive: bool = True) -> list[Hit]:
        """Every blob, in any indexed repo and any commit, whose contents contain `query`.

        Searches all history, not one snapshot: each version of a file that
        matched is its own Hit. Each Hit lists where that blob was written
        (oldest first), and hits are ordered by when they were first seen.
        Commits with no recorded time sort last.

        Args:
            query (str | bytes): Literal text, at least 3 bytes.
            case_sensitive (bool): If False, ASCII letters match either case.

        Returns:
            list[Hit]: Matching blobs, oldest first-seen first.

        Raises:
            ValueError: If the query is shorter than 3 bytes.
        """
        query_bytes = _b(query)
        blob_ids = self.trigrams.search(
            query_bytes, self.store.read_blob, case_sensitive=case_sensitive
        )
        hits = []
        for blob_id in blob_ids:
            blob_sha = self.store.blob_shas[blob_id]
            raw_occs = self.store.blob_occurrences.get(blob_sha, [])
            occurrences = [
                Occurrence(
                    repo=repo,
                    commit=commit,
                    path=path,
                    commit_time=self.store.commit_times.get((repo, commit)),
                )
                for repo, commit, path in raw_occs
            ]
            occurrences.sort(
                key=lambda occ: (
                    occ.commit_time
                    if occ.commit_time is not None
                    else float("inf")
                )
            )
            hits.append(Hit(blob_sha=blob_sha, occurrences=occurrences))

        hits.sort(
            key=lambda hit: (
                hit.first_seen.commit_time
                if hit.first_seen.commit_time is not None
                else float("inf")
            )
        )
        return hits

    def read(self, blob_sha: bytes) -> bytes | None:
        """Full file text for a blob sha."""
        blob_id = self.store.blob_ids.get(_b(blob_sha))
        if blob_id is None:
            return None
        return self.store.read_blob(blob_id)
