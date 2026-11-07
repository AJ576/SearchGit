import os

from dulwich.repo import Repo

# git object modes (octal in git, ints here)
MODE_TREE = 0o040000       # 16384 -> directory / subtree
MODE_SUBMODULE = 0o160000  # 57344 -> gitlink, points at another repo's commit

DELETED = None  # tombstone: path existed in the parent, not in this commit


class RepoIndexer:
    """Indexes repos as a *delta* store, and reads through to git.

    Write path: we only record a (commit, path) entry when the blob at that
    path actually changed relative to the commit's first parent. If a subtree's
    sha is identical to the parent's, nothing underneath it can have changed,
    so we don't even open it. That gives the search engine what it needs --
    where each blob is born and where it dies.

    Read path: we do NOT replay those deltas. Git's tree objects already are
    complete snapshots, so snapshot_at()/blob_at() just ask git. The delta
    store is an index, not a source of truth.
    """

    def __init__(self):
        """Initializes the indexer with empty delta stores and repo caches."""
        # (commit_sha, filepath) -> blob_sha, or DELETED
        # only present when the path changed at that commit
        self.file_index = {}
        # (repo_path, filepath) -> [(commit_sha, blob_sha | DELETED), ...]
        # inverted view of file_index, built during ingestion so the hot
        # "where did this path change?" query is a lookup, not a scan.
        # keyed by repo too: two repos both having a README.md must not end up
        # sharing one history
        self.path_changes = {}
        # commit_sha -> repo path, so a read knows which repo to ask
        self.commit_repo = {}
        # repo path -> open Repo. the read path needs live handles
        self.repos = {}
        # tree_sha -> [(relative_path, blob_sha), ...], used for full listings
        # (root commits, and tombstoning a deleted directory)
        self.tree_cache = {}
        self.indexed_repos = set()

    @property
    def repos_indexed(self):
        """Returns the total number of distinct repositories indexed."""
        return len(self.indexed_repos)

    # --- indexing ----------------------------------------------------------

    def index_repo(self, path: str):
        """Indexes all commits of a repository into delta and path-change maps.

        Walks the repository's commit graph in topological/chronological order.
        For each commit:
          1. Identifies the root tree of its first parent (if any).
          2. Diffs the parent tree against the commit's tree using `_diff_trees`.
          3. Stores delta records `(sha, filepath) -> blob_sha` in `self.file_index`.
          4. Appends `(sha, blob_sha)` to `self.path_changes` for fast file-history lookups.

        Args:
            path (str): File system path to the bare or non-bare Git repository.
        """
        # realpath so the scope key is canonical -- "x.git" and "./x.git" are
        # the same repo and must not become two scopes
        key = os.path.realpath(path)
        repo = self.get_repo(key)
        commit_count = 0
        entry_count = 0

        for entry in repo.get_walker():
            commit = entry.commit
            sha = commit.id

            self.commit_repo[sha] = key
            parent_tree = self._parent_tree(repo, commit)

            for filepath, blob_sha in self._diff_trees(
                repo, parent_tree, commit.tree, b""
            ):
                self.file_index[(sha, filepath)] = blob_sha
                self.path_changes.setdefault((key, filepath), []).append(
                    (sha, blob_sha)
                )
                entry_count += 1

            commit_count += 1

        self.indexed_repos.add(key)
        print(f"Repo read: {path}")
        print(f"  commits:       {commit_count}")
        print(f"  delta entries: {entry_count}")

    def _parent_tree(self, repo, commit):
        """Finds the root tree SHA of a commit's first parent, or None.

        None also covers root commits (no parents) and shallow clones, whose boundary
        commit has a parent SHA whose object was never fetched. Treating that as
        "no parent" makes the boundary a full snapshot, which is the honest answer --
        we cannot know what changed there.

        Args:
            repo (Repo): An open Dulwich repository object.
            commit (Commit): The commit object being examined.

        Returns:
            bytes | None: The 40-byte SHA of the parent's tree, or None if no parent exists.
        """
        if not commit.parents:
            return None
        try:
            return repo[commit.parents[0]].tree
        except KeyError:
            return None  # shallow boundary: parent object is missing

    def _entries(self, repo, tree_sha):
        """Reads non-recursive entries for a single tree object.

        Args:
            repo (Repo): An open Dulwich repository object.
            tree_sha (bytes | None): SHA of the tree to inspect.

        Returns:
            dict[bytes, tuple[int, bytes]]: Mapping of `entry_name -> (mode, sha)`.
                Returns an empty dictionary if `tree_sha` is None.
        """
        if tree_sha is None:
            return {}
        return {name: (mode, sha) for name, mode, sha in repo[tree_sha].iteritems()}

    def _diff_trees(self, repo, old_sha, new_sha, prefix: bytes):
        """Recursively diffs two Git trees and yields changes as deltas.

        Yields `(filepath, blob_sha)` for added or modified files, and
        `(filepath, DELETED)` for files or directory subtrees that were removed.
        If a subtree's SHA is identical between parent and child, it prunes recursion
        immediately because subtrees with matching SHAs have identical contents.

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

            if mode == MODE_TREE:
                # recurse only into the subtree that moved
                old_sub = was[1] if was and was[0] == MODE_TREE else None
                yield from self._diff_trees(repo, old_sub, sha, prefix + name + b"/")
                # a file was replaced by a directory of the same name
                if was and was[0] not in (MODE_TREE, MODE_SUBMODULE):
                    yield (prefix + name, DELETED)

            elif mode == MODE_SUBMODULE:
                continue  # gitlink: that sha is a commit elsewhere, not a blob

            else:
                yield (prefix + name, sha)
                # a directory was replaced by a file of the same name
                if was and was[0] == MODE_TREE:
                    for p, _ in self._flatten(repo, was[1], prefix + name + b"/"):
                        yield (p, DELETED)

        # anything in the parent that is gone now needs a tombstone, otherwise
        # replay finds the old entry and thinks the file still exists
        for name, (mode, sha) in old.items():
            if name in new:
                continue
            if mode == MODE_TREE:
                for p, _ in self._flatten(repo, sha, prefix + name + b"/"):
                    yield (p, DELETED)
            elif mode == MODE_SUBMODULE:
                continue
            else:
                yield (prefix + name, DELETED)

    def _flatten(self, repo, tree_sha, prefix: bytes):
        """Flattens a tree into a list of every (relative_path, blob_sha) under it.

        Memoized by `tree_sha` in `self.tree_cache` so that unchanged subtrees across
        commits and directory deletion tombstones avoid redundant disk traversals.

        Args:
            repo (Repo): An open Dulwich repository object.
            tree_sha (bytes): SHA of the Git tree to flatten.
            prefix (bytes): Prefix path to prepend to all flattened relative paths.

        Returns:
            list[tuple[bytes, bytes]]: List of `(path, blob_sha)` for all blobs in tree.
        """
        cached = self.tree_cache.get(tree_sha)
        if cached is None:
            cached = []
            for name, mode, obj_sha in repo[tree_sha].iteritems():
                if mode == MODE_TREE:
                    cached.extend(self._flatten(repo, obj_sha, name + b"/"))
                elif mode == MODE_SUBMODULE:
                    continue
                else:
                    cached.append((name, obj_sha))
            self.tree_cache[tree_sha] = cached
        if not prefix:
            return cached
        return [(prefix + p, sha) for p, sha in cached]

    # --- reads: answered by git, not by replaying deltas -------------------

    def get_repo(self, path):
        """Opens a Git repo once and keeps the handle; reads need it live.

        Normalizes the path with `os.path.realpath` to ensure unique handles per repo.

        Args:
            path (str): File system path to the Git repository.

        Returns:
            Repo: Open Dulwich repository instance.
        """
        key = os.path.realpath(path)
        repo = self.repos.get(key)
        if repo is None:
            repo = Repo(key)
            self.repos[key] = repo
        return repo

    def _repo_for(self, commit_sha):
        """Looks up the open Repo handle associated with a given commit SHA.

        Args:
            commit_sha (bytes): 40-character commit SHA.

        Returns:
            Repo: Open Dulwich repository containing this commit.

        Raises:
            KeyError: If the commit SHA was never indexed.
        """
        path = self.commit_repo.get(commit_sha)
        if path is None:
            raise KeyError(f"commit {commit_sha!r} is not in the index")
        return self.get_repo(path)

    def snapshot_at(self, commit_sha, repo_path=None):
        """Full {path: blob_sha} at a commit, read straight from its tree.

        The commit's root tree already IS the snapshot, so this is one
        traversal of that tree. Memoised per tree sha, so commits that share
        subtrees share the work.

        Args:
            commit_sha (bytes | str): The commit SHA to snapshot.
            repo_path (str, optional): Repository path (inferred from commit if omitted).

        Returns:
            dict[bytes, bytes]: Map of all active file paths to blob SHAs at that commit.
        """
        if isinstance(commit_sha, str):
            commit_sha = commit_sha.encode()
        repo = self.get_repo(repo_path) if repo_path else self._repo_for(commit_sha)

        tree_sha = repo[commit_sha].tree
        return dict(self._flatten(repo, tree_sha, b""))

    def blob_at(self, commit_sha, filepath, repo_path=None):
        """Retrieves the blob SHA for one path at one commit, or DELETED if absent.

        Walks only the trees along the path -- O(depth), not O(repo).

        Args:
            commit_sha (bytes | str): The commit SHA to inspect.
            filepath (bytes | str): Relative path to the file.
            repo_path (str, optional): Repository path (inferred from commit if omitted).

        Returns:
            bytes | None: Blob SHA if file exists, or DELETED (None) if absent / directory.
        """
        if isinstance(commit_sha, str):
            commit_sha = commit_sha.encode()
        if isinstance(filepath, str):
            filepath = filepath.encode()
        repo = self.get_repo(repo_path) if repo_path else self._repo_for(commit_sha)

        sha = repo[commit_sha].tree
        parts = [p for p in filepath.split(b"/") if p]
        if not parts:
            return DELETED

        for i, part in enumerate(parts):
            last = i == len(parts) - 1
            try:
                mode, sha = repo[sha][part]
            except KeyError:
                return DELETED  # path does not exist at this commit

            if last:
                # a directory or submodule is not a blob
                if mode in (MODE_TREE, MODE_SUBMODULE):
                    return DELETED
                return sha
            if mode != MODE_TREE:
                return DELETED  # tried to descend through a file

        return DELETED

    # --- reads answered by the delta index ---------------------------------

    def changes_to(self, filepath, repo_path=None):
        """[(commit_sha, blob_sha | DELETED)] for commits where this path changed.

        In ingestion order, which get_walker() gives roughly newest-first --
        it is date order, not a strict chain. repo_path may be omitted only
        while a single repo is indexed.

        Args:
            filepath (bytes | str): Relative file path to query.
            repo_path (str, optional): Repository path (required if multiple repos indexed).

        Returns:
            list[tuple[bytes, bytes | None]]: List of (commit_sha, blob_sha | DELETED) pairs.
        """
        if isinstance(filepath, str):
            filepath = filepath.encode()
        return self.path_changes.get((self._one_repo(repo_path), filepath), [])

    def paths(self, repo_path=None):
        """Every path this repo's index has ever seen.

        Args:
            repo_path (str, optional): Repository path (required if multiple repos indexed).

        Returns:
            list[bytes]: List of unique relative file paths ever indexed for the repository.
        """
        key = self._one_repo(repo_path)
        return [p for (r, p) in self.path_changes if r == key]

    def _one_repo(self, repo_path):
        """Resolves the canonical repository path when optional in single-repo setups.

        Args:
            repo_path (str | None): Explicit repo path, or None to auto-detect.

        Returns:
            str: Canonical real path of the repository.

        Raises:
            ValueError: If repo_path is omitted and multiple repos have been indexed.
        """
        if repo_path is not None:
            return os.path.realpath(repo_path)
        if len(self.indexed_repos) == 1:
            return next(iter(self.indexed_repos))
        raise ValueError(
            f"repo_path is required with {self.repos_indexed} repos indexed"
        )

    def close(self):
        """Closes all open repository handles and clears internal repo caches."""
        for repo in self.repos.values():
            repo.close()
        self.repos.clear()
