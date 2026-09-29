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
        return len(self.indexed_repos)

    # --- indexing ----------------------------------------------------------

    def index_repo(self, path: str):
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
        """Root tree of the first parent, or None.

        None also covers a shallow clone, whose boundary commit has a parent
        sha whose object was never fetched. Treating that as "no parent" makes
        the boundary a full snapshot, which is the honest answer -- we cannot
        know what changed there.
        """
        if not commit.parents:
            return None
        try:
            return repo[commit.parents[0]].tree
        except KeyError:
            return None  # shallow boundary: parent object is missing

    def _entries(self, repo, tree_sha):
        """name -> (mode, sha) for one tree, non-recursive."""
        if tree_sha is None:
            return {}
        return {name: (mode, sha) for name, mode, sha in repo[tree_sha].iteritems()}

    def _diff_trees(self, repo, old_sha, new_sha, prefix: bytes):
        """Yield (path, blob_sha | DELETED) for everything that differs."""
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
        """Every (path, blob_sha) under a tree. Memoised on tree sha."""
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
        """Open a repo once and keep the handle; reads need it live."""
        key = os.path.realpath(path)
        repo = self.repos.get(key)
        if repo is None:
            repo = Repo(key)
            self.repos[key] = repo
        return repo

    def _repo_for(self, commit_sha):
        path = self.commit_repo.get(commit_sha)
        if path is None:
            raise KeyError(f"commit {commit_sha!r} is not in the index")
        return self.get_repo(path)

    def snapshot_at(self, commit_sha, repo_path=None):
        """Full {path: blob_sha} at a commit, read straight from its tree.

        The commit's root tree already IS the snapshot, so this is one
        traversal of that tree. Memoised per tree sha, so commits that share
        subtrees share the work.
        """
        if isinstance(commit_sha, str):
            commit_sha = commit_sha.encode()
        repo = self.get_repo(repo_path) if repo_path else self._repo_for(commit_sha)

        tree_sha = repo[commit_sha].tree
        return dict(self._flatten(repo, tree_sha, b""))

    def blob_at(self, commit_sha, filepath, repo_path=None):
        """blob_sha for one path at one commit, or DELETED if absent.

        Walks only the trees along the path -- O(depth), not O(repo).
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
        """[(commit_sha, blob_sha | DELETED)] for commits where this changed.

        In ingestion order, which get_walker() gives roughly newest-first --
        it is date order, not a strict chain. repo_path may be omitted only
        while a single repo is indexed.
        """
        if isinstance(filepath, str):
            filepath = filepath.encode()
        return self.path_changes.get((self._one_repo(repo_path), filepath), [])

    def paths(self, repo_path=None):
        """Every path this repo's index has ever seen."""
        key = self._one_repo(repo_path)
        return [p for (r, p) in self.path_changes if r == key]

    def _one_repo(self, repo_path):
        if repo_path is not None:
            return os.path.realpath(repo_path)
        if len(self.indexed_repos) == 1:
            return next(iter(self.indexed_repos))
        raise ValueError(
            f"repo_path is required with {self.repos_indexed} repos indexed"
        )

    def close(self):
        for repo in self.repos.values():
            repo.close()
        self.repos.clear()
