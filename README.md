# SearchGit

A **distributed historical code search engine**.

Most code search — GitHub's included — only searches `HEAD`, the current state of a
repository. Code that was deleted, an API that was deprecated, the version of a
function before the bug was fixed: all invisible. SearchGit indexes the *entire commit
history* of 1,000+ repositories so you can search across time, not just across files.

**This repo is at V0.** What works today is the ingestion core: the tree-diffing indexer
and a console harness to poke at it against one hardcoded repo. The trigram index,
SQLite, and the distributed pipeline are not built yet. See
[Not built yet](#not-built-yet).

---

## The problem

A repo with 10,000 files and 50,000 commits is 500 million file-versions. Index those
naively, as one document each, and you need petabytes and a compute cluster.

The target here is commodity hardware and a hard **1 TB** ceiling for 1,000+ repos.

## The idea: index blobs, not files

Git is a content-addressable filesystem. A file that goes unchanged across 10,000
commits is *one blob*, stored once, referenced 10,000 times. The 500-million number is
an illusion created by counting references instead of content.

So we index unique blobs, and separately record where each blob is **born** and where it
**dies**. A delta store, not a snapshot store.

## Architecture: two paths

The system splits into a write path optimized for *storage* and a read path optimized
for *compute*, and the split is the core design commitment.

### Write path — ingestion

1. Clone bare `.git` repos (no working directory).
2. Walk the commit graph.
3. For each commit, **diff its tree against its parent's**. If a subtree's sha matches
   the parent's, nothing beneath it changed — skip it without opening it. This is what
   makes ingestion cheap; most commits touch one file in one directory.
4. For each newly-seen blob, extract text into 3-character **trigrams** and store them
   in a compressed inverted index (**Roaring Bitmaps**).
5. Log the delta — `(commit_sha, filepath, blob_sha)` — in **SQLite**.

### Read path — search and reconstruction

1. Text query (`verify_jwt`) → trigram index → a set of blob IDs.
2. Blob IDs → SQLite → the commits and paths where those blobs were introduced.
3. **The trick: we never reconstruct repository state from our own database.** Want the
   file contents, or the full tree at that moment in history? We ask the `.git` packfile
   on disk. Git's own backend traverses its trees in microseconds, and we avoid storing
   hundreds of gigabytes of snapshot data to answer a question git can already answer.

That asymmetry is the whole design: **our index answers "where does this text live in
history"; git answers "what did the repo look like at commit X".** Neither side
duplicates the other.

### Stack

| Layer | Choice |
|---|---|
| Object storage | Bare `.git` packfiles |
| Metadata | SQLite |
| Text index | Roaring Bitmaps over trigrams |
| Ingestion | Event-driven workers streaming tree-diffs, low RAM |

---

## What's here in V0

```
repo_indexer.py   RepoIndexer -- the tree-diffing delta indexer
main.py           console harness, hardcoded to physics_engine.git
learn.py          scratch file from working out the dulwich API
```

```
./venv/bin/python main.py
```

| Command | Answered by |
|---|---|
| `files [commit]` | git — full tree at that commit |
| `blob <path> [commit]` | git — one path at that commit |
| `cat <path> [commit]` | git — file contents |
| `changes <path>` | the delta index |
| `paths` | the delta index |
| `log [n]`, `stats` | — |

### The data model

```python
file_index[(commit_sha, filepath)] = blob_sha   # or DELETED
path_changes[(repo_path, filepath)] = [(commit_sha, blob_sha), ...]
```

An entry exists **only where a path changed**. On `physics_engine.git`: 65 commits ×
13 files would be 544 rows as a snapshot store; the delta store holds **203**. That
ratio widens with repo size, because commit count grows while per-commit churn doesn't.

Deletions are recorded as explicit tombstones (`DELETED`). They are not optional —
without them a query walking history finds the last known blob and reports a file that
no longer exists.

### Verified

Against all 65 commits of `physics_engine.git`: 65 `snapshot_at` calls and 544
`blob_at` calls match a direct independent tree walk exactly.

---

## Not built yet

Everything below is understood and deliberately deferred. V0 is the ingestion core.

### Missing components

- **Trigram index / Roaring Bitmaps.** The actual text search. Nothing indexes blob
  *contents* yet — we only record where blobs live.
- **SQLite.** All three views (`file_index`, `path_changes`) are in-memory dicts and die
  with the process. One `(repo, commit_sha, path, blob_sha)` table with indexes on
  `path` and `commit_sha` replaces both and gives the reverse lookup for free.
- **Multi-repo ingestion at scale.** The class takes any path and scopes by repo, but
  nothing clones, queues, or parallelizes.
- **Binary filtering.** `physics_engine.git` has a compiled binary committed to it — a
  large blob, no searchable text, churning every build. Needs a size/binary check before
  content indexing.

### Known correctness gaps

Each of these is a real defect, understood, with a known fix. None blocks V0.

- **Merge commits over-report.** We diff against the first parent only, so a change
  merged in appears twice: once at its original commit, once at the merge. `changes
  spring.hpp` shows 5 rows where 4 are real edits. Fix: git's own history
  simplification — drop a path if its blob is treesame to *any* parent.
- **Only history reachable from HEAD is walked.** `get_walker()` defaults to HEAD, so
  commits on unmerged branches and tags are invisible. Fix: `include=` all refs. On this
  repo that is 4 more commits.
- **Mode-only changes count as changes.** `chmod +x` alters `(mode, sha)` while the blob
  sha is unchanged, so we record a change that isn't one. Fix: compare blob shas, not
  `(mode, sha)` pairs.
- **A directory replaced by a submodule leaks entries.** The gitlink hits `continue` in
  pass 1, and pass 2 skips the name because it still exists — so files under the old
  directory never get tombstones and the index still claims they exist.
- **`DELETED` is overloaded.** `blob_at` returns it for "absent", "is a directory" and
  "is a submodule" alike. Callers can't distinguish. Fix: separate sentinels.
- **Re-indexing a repo duplicates rows.** `file_index` dedupes by key; `path_changes`
  appends. Fix: forget a repo's entries before re-indexing it.
- **No rename detection.** A rename is a tombstone plus a birth sharing one blob sha.
  Linkable in a post-pass by matching shas within a commit; we don't.
- **`changes_to` ordering is approximate.** Rows come in `get_walker()` order, which is
  date order, not a linear chain. Branches mean adjacent rows can be siblings rather
  than successive versions.
- **`tree_cache` is unbounded** and keeps a full listing per distinct tree, including one
  per root tree touched by `snapshot_at`. Only matters under multi-gigabyte sweeps; an
  LRU bound is the fix, and it is not worth building yet.

### Fixed in V0

- `path_changes` is keyed `(repo_path, filepath)`, so two repos that both contain a
  `README.md` no longer merge their histories. Repo paths are `realpath`-normalized, so
  `x.git` and `./x.git` are one scope.
- Ingestion prints this repo's commit and entry counts, not a cumulative total.
- A shallow clone's boundary commit no longer raises `KeyError` on the missing parent
  object — it is treated as parentless, making it a full snapshot, which is the honest
  answer when we can't know what changed.
