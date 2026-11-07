"""Write path: walks a repo's commits and records per-commit file deltas in a Store."""

from store import DELETED, MODE_SUBMODULE, MODE_TREE, Store, canonical


class Indexer:
    """Indexes repos as a *delta* store.

    We only record a (commit, path) entry when the blob at that path actually
    changed relative to the commit's first parent. If a subtree's sha is
    identical to the parent's, nothing underneath it can have changed, so we
    don't even open it. That gives the search engine what it needs -- where
    each blob is born and where it dies.
    """

    def __init__(self, store: Store):
        """Initializes the indexer over a shared Store."""
        self.store = store

    def index_repo(self, path: str):
        """Indexes every commit reachable from HEAD into the store.

        For each commit (in get_walker()'s date order, roughly newest first):
          1. Finds the root tree of its first parent (if any).
          2. Diffs the parent tree against the commit's tree.
          3. Records each changed path in the store.

        Indexing a repo that is already indexed is a no-op: the store's
        history lists are append-only, so a second pass would duplicate them.

        Args:
            path (str): File system path to the bare or non-bare Git repository.
        """
        key = canonical(path)
        if self.store.is_indexed(key):
            return
        repo = self.store.get_repo(key)
        commit_count = 0
        entry_count = 0

        for entry in repo.get_walker():
            commit = entry.commit
            sha = commit.id

            self.store.add_commit(key, sha)
            parent_tree = self._parent_tree(repo, commit)

            for filepath, blob_sha in self._diff_trees(
                repo, parent_tree, commit.tree, b""
            ):
                self.store.add_delta(key, sha, filepath, blob_sha)
                entry_count += 1

            commit_count += 1

        self.store.mark_indexed(key)
        print(f"Repo read: {path}")
        print(f"  commits:       {commit_count}")
        print(f"  delta entries: {entry_count}")

    def _parent_tree(self, repo, commit):
        """Finds the root tree SHA of a commit's first parent, or None.

        None also covers shallow clones, whose boundary commit has a parent SHA
        whose object was never fetched. Treating that as "no parent" makes the
        boundary a full snapshot, which is the honest answer -- we cannot know
        what changed there.
        """
        if not commit.parents:
            return None
        try:
            return repo[commit.parents[0]].tree
        except KeyError:
            return None  # shallow boundary: parent object is missing

    def _entries(self, repo, tree_sha):
        """Non-recursive `name -> (mode, sha)` for one tree; {} if tree_sha is None."""
        if tree_sha is None:
            return {}
        return {name: (mode, sha) for name, mode, sha in repo[tree_sha].iteritems()}

    def _deleted_under(self, repo, was, path: bytes):
        """Tombstones for whatever used to live at `path` (a file or a whole directory)."""
        mode, sha = was
        if mode == MODE_TREE:
            for p, _ in self.store.flatten(repo, sha, path + b"/"):
                yield (p, DELETED)
        elif mode != MODE_SUBMODULE:
            yield (path, DELETED)

    def _diff_trees(self, repo, old_sha, new_sha, prefix: bytes):
        """Recursively diffs two Git trees and yields changes as deltas.

        Yields `(filepath, blob_sha)` for added or modified files, and
        `(filepath, DELETED)` for files that are gone -- including every file
        under a removed directory, and anything replaced by something of a
        different kind (file <-> directory <-> submodule).

        Args:
            repo (Repo): An open Dulwich repository object.
            old_sha (bytes | None): SHA of the parent tree (None for root commits).
            new_sha (bytes | None): SHA of the current commit's tree.
            prefix (bytes): Path prefix accumulated during recursive descent.

        Yields:
            tuple[bytes, bytes | None]: (filepath, blob_sha | DELETED)
        """
        # the whole point: identical sha => identical contents, prune here
        if old_sha == new_sha:
            return

        old = self._entries(repo, old_sha)
        new = self._entries(repo, new_sha)

        for name, (mode, sha) in new.items():
            was = old.get(name)
            if was == (mode, sha):
                continue  # unchanged file, or unchanged subtree
            path = prefix + name

            if mode == MODE_TREE:
                # recurse only into the subtree that moved
                old_sub = was[1] if was and was[0] == MODE_TREE else None
                yield from self._diff_trees(repo, old_sub, sha, path + b"/")
                # a file was replaced by a directory of the same name
                if was and was[0] != MODE_TREE:
                    yield from self._deleted_under(repo, was, path)

            elif mode == MODE_SUBMODULE:
                # gitlink: that sha is a commit elsewhere, not a blob -- but
                # whatever used to live at this name is gone now
                if was:
                    yield from self._deleted_under(repo, was, path)

            else:
                yield (path, sha)
                # a directory was replaced by a file of the same name
                if was and was[0] == MODE_TREE:
                    yield from self._deleted_under(repo, was, path)

        # anything in the parent that is gone now needs a tombstone, otherwise
        # replay finds the old entry and thinks the file still exists
        for name, was in old.items():
            if name not in new:
                yield from self._deleted_under(repo, was, prefix + name)
