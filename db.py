"""SQLite storage layer for SearchGit.

Provides persistent, indexed SQLite database for:
- Repositories metadata (path, indexed_at)
- Commits (commit_sha, repo_path, commit_time)
- Blobs (blob_id, blob_sha, repo_path)
- File deltas (repo_path, commit_sha, filepath, blob_sha)
- Trigram inverted index postings (trigram, roaring bitmap blob)
- Trigram metadata (indexed & skipped roaring bitmaps)
"""

import sqlite3
from typing import Iterator
from pyroaring import BitMap


def _to_str(val: str | bytes | None) -> str | None:
    """Safely converts a string or bytes value to a str for SQLite storage."""
    if val is None:
        return None
    if isinstance(val, bytes):
        return val.decode("utf-8", errors="surrogateescape")
    return str(val)


def _to_bytes(val: str | bytes | None) -> bytes | None:
    """Safely converts a database string back to raw bytes for Git operations."""
    if val is None:
        return None
    if isinstance(val, str):
        return val.encode("utf-8", errors="surrogateescape")
    return val


class Database:
    """SQLite database manager for SearchGit index data.

    Manages persistent storage for repos, commits, deltas, and trigram bitmaps.
    """

    def __init__(self, path: str = ":memory:"):
        """Connects to SQLite at `path` and creates required tables and indexes."""
        self.path = path
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self._configure_pragmas()
        self._init_schema()

    def _configure_pragmas(self):
        if self.path != ":memory:":
            self.conn.execute("PRAGMA journal_mode = WAL;")
        self.conn.execute("PRAGMA synchronous = NORMAL;")
        self.conn.execute("PRAGMA foreign_keys = ON;")

    def _init_schema(self):
        with self.conn:
            self.conn.executescript("""
                CREATE TABLE IF NOT EXISTS repos (
                    path TEXT PRIMARY KEY,
                    indexed_at TIMESTAMP DEFAULT NULL
                );

                CREATE TABLE IF NOT EXISTS commits (
                    commit_sha TEXT NOT NULL,
                    repo_path TEXT NOT NULL,
                    commit_time INTEGER,
                    PRIMARY KEY (repo_path, commit_sha),
                    FOREIGN KEY (repo_path) REFERENCES repos(path) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_commits_repo ON commits(repo_path);
                CREATE INDEX IF NOT EXISTS idx_commits_sha ON commits(commit_sha);

                CREATE TABLE IF NOT EXISTS blobs (
                    blob_id INTEGER PRIMARY KEY,
                    blob_sha TEXT UNIQUE NOT NULL,
                    repo_path TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_blobs_sha ON blobs(blob_sha);

                CREATE TABLE IF NOT EXISTS file_deltas (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    repo_path TEXT NOT NULL,
                    commit_sha TEXT NOT NULL,
                    filepath TEXT NOT NULL,
                    blob_sha TEXT,  -- NULL indicates DELETED tombstone
                    FOREIGN KEY (repo_path) REFERENCES repos(path) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_deltas_path ON file_deltas(repo_path, filepath, id);
                CREATE INDEX IF NOT EXISTS idx_deltas_commit ON file_deltas(repo_path, commit_sha);
                CREATE INDEX IF NOT EXISTS idx_deltas_blob ON file_deltas(blob_sha);

                CREATE TABLE IF NOT EXISTS trigram_postings (
                    trigram BLOB PRIMARY KEY,
                    bitmap BLOB NOT NULL
                );

                CREATE TABLE IF NOT EXISTS trigram_meta (
                    key TEXT PRIMARY KEY,
                    bitmap BLOB NOT NULL
                );
            """)

    # --- repos -------------------------------------------------------------

    def ensure_repo(self, repo_path: str):
        """Ensures a repo entry exists so foreign keys are satisfied."""
        repo_str = _to_str(repo_path)
        with self.conn:
            self.conn.execute(
                "INSERT OR IGNORE INTO repos (path) VALUES (?)",
                (repo_str,),
            )

    def record_repo(self, repo_path: str):
        """Records a canonical repo path as fully indexed."""
        repo_str = _to_str(repo_path)
        with self.conn:
            self.conn.execute(
                "INSERT INTO repos (path, indexed_at) VALUES (?, CURRENT_TIMESTAMP) "
                "ON CONFLICT(path) DO UPDATE SET indexed_at = CURRENT_TIMESTAMP",
                (repo_str,),
            )

    def is_repo_indexed(self, repo_path: str) -> bool:
        """Returns True if this canonical repo path is already recorded."""
        repo_str = _to_str(repo_path)
        cur = self.conn.execute(
            "SELECT 1 FROM repos WHERE path = ? AND indexed_at IS NOT NULL", (repo_str,)
        )
        return cur.fetchone() is not None

    def get_indexed_repos(self) -> set[str]:
        """Returns the set of all canonical repo paths indexed."""
        cur = self.conn.execute("SELECT path FROM repos WHERE indexed_at IS NOT NULL")
        return {row[0] for row in cur.fetchall()}

    def clear_repo(self, repo_path: str):
        """Clears all records for a repository (deltas, commits, and repo entry)."""
        repo_str = _to_str(repo_path)
        with self.conn:
            self.conn.execute("DELETE FROM file_deltas WHERE repo_path = ?", (repo_str,))
            self.conn.execute("DELETE FROM commits WHERE repo_path = ?", (repo_str,))
            self.conn.execute("DELETE FROM repos WHERE path = ?", (repo_str,))

    # --- commits -----------------------------------------------------------

    def record_commit(self, repo_path: str, commit_sha: bytes | str, commit_time: int | None = None):
        """Records a commit belonging to a repo, with optional commit timestamp."""
        repo_str = _to_str(repo_path)
        sha_str = _to_str(commit_sha)
        self.ensure_repo(repo_path)
        self.conn.execute(
            "INSERT OR REPLACE INTO commits (commit_sha, repo_path, commit_time) VALUES (?, ?, ?)",
            (sha_str, repo_str, commit_time),
        )

    def record_commits_batch(self, commits: list[tuple[str, str | bytes, int | None]]):
        """Batch records commits: [(repo_path, commit_sha, commit_time), ...]."""
        rows = [(_to_str(sha), _to_str(repo), time) for repo, sha, time in commits]
        self.conn.executemany(
            "INSERT OR REPLACE INTO commits (commit_sha, repo_path, commit_time) VALUES (?, ?, ?)",
            rows,
        )

    def get_all_commits_in(self, repo_path: str) -> set[bytes]:
        """Returns all commit SHAs (as bytes) recorded for a repository."""
        repo_str = _to_str(repo_path)
        cur = self.conn.execute(
            "SELECT commit_sha FROM commits WHERE repo_path = ?", (repo_str,)
        )
        return {_to_bytes(row[0]) for row in cur.fetchall()}

    def get_all_commit_times(self) -> dict[tuple[str, bytes], int]:
        """Returns {(repo_path, commit_sha): commit_time} for all known commits."""
        cur = self.conn.execute(
            "SELECT repo_path, commit_sha, commit_time FROM commits WHERE commit_time IS NOT NULL"
        )
        return {(row[0], _to_bytes(row[1])): row[2] for row in cur.fetchall()}

    def get_repo_for_commit(self, commit_sha: bytes | str) -> str | None:
        """Finds the repo_path for a commit SHA."""
        sha_str = _to_str(commit_sha)
        cur = self.conn.execute(
            "SELECT repo_path FROM commits WHERE commit_sha = ? LIMIT 1", (sha_str,)
        )
        row = cur.fetchone()
        return row[0] if row else None

    # --- blobs -------------------------------------------------------------

    def record_blob(self, blob_id: int, blob_sha: bytes | str, repo_path: str):
        """Records a new global blob id, sha, and source repo."""
        sha_str = _to_str(blob_sha)
        repo_str = _to_str(repo_path)
        self.conn.execute(
            "INSERT OR IGNORE INTO blobs (blob_id, blob_sha, repo_path) VALUES (?, ?, ?)",
            (blob_id, sha_str, repo_str),
        )

    def record_blobs_batch(self, blobs: list[tuple[int, str | bytes, str]]):
        """Batch records blobs: [(blob_id, blob_sha, repo_path), ...]."""
        rows = [(b_id, _to_str(sha), _to_str(repo)) for b_id, sha, repo in blobs]
        self.conn.executemany(
            "INSERT OR IGNORE INTO blobs (blob_id, blob_sha, repo_path) VALUES (?, ?, ?)",
            rows,
        )

    def get_all_blobs(self) -> tuple[dict[bytes, int], list[bytes], dict[int, str]]:
        """Loads (blob_ids, blob_shas, blob_repo) from persistent storage."""
        cur = self.conn.execute(
            "SELECT blob_id, blob_sha, repo_path FROM blobs ORDER BY blob_id ASC"
        )
        blob_ids = {}
        blob_shas = []
        blob_repo = {}
        for b_id, sha_str, repo in cur.fetchall():
            sha_bytes = _to_bytes(sha_str)
            blob_ids[sha_bytes] = b_id
            while len(blob_shas) <= b_id:
                blob_shas.append(None)
            blob_shas[b_id] = sha_bytes
            blob_repo[b_id] = repo
        return blob_ids, blob_shas, blob_repo

    # --- deltas ------------------------------------------------------------

    def insert_delta(
        self,
        repo_path: str,
        commit_sha: bytes | str,
        filepath: bytes | str,
        blob_sha: bytes | str | None,
    ):
        """Records that `filepath` changed to `blob_sha` (or NULL/DELETED) at `commit_sha`."""
        self.ensure_repo(repo_path)
        self.conn.execute(
            "INSERT INTO file_deltas (repo_path, commit_sha, filepath, blob_sha) VALUES (?, ?, ?, ?)",
            (_to_str(repo_path), _to_str(commit_sha), _to_str(filepath), _to_str(blob_sha)),
        )

    def insert_deltas_batch(
        self,
        deltas: list[tuple[str, str | bytes, str | bytes, str | bytes | None]],
    ):
        """Batch inserts deltas: [(repo_path, commit_sha, filepath, blob_sha), ...]."""
        for r, _, _, _ in deltas:
            self.ensure_repo(r)
        rows = [
            (_to_str(r), _to_str(c), _to_str(p), _to_str(b))
            for r, c, p, b in deltas
        ]
        self.conn.executemany(
            "INSERT INTO file_deltas (repo_path, commit_sha, filepath, blob_sha) VALUES (?, ?, ?, ?)",
            rows,
        )

    def get_history(self, repo_path: str, filepath: bytes | str) -> list[tuple[bytes, bytes | None]]:
        """Returns [(commit_sha, blob_sha)] for all recorded changes to a file, oldest first."""
        cur = self.conn.execute(
            "SELECT commit_sha, blob_sha FROM file_deltas WHERE repo_path = ? AND filepath = ? ORDER BY id ASC",
            (_to_str(repo_path), _to_str(filepath)),
        )
        return [(_to_bytes(c), _to_bytes(b)) for c, b in cur.fetchall()]

    def get_paths(self, repo_path: str) -> list[bytes]:
        """Returns all distinct filepaths recorded for a repository."""
        cur = self.conn.execute(
            "SELECT DISTINCT filepath FROM file_deltas WHERE repo_path = ?",
            (_to_str(repo_path),),
        )
        return [_to_bytes(row[0]) for row in cur.fetchall()]

    def get_blob_occurrences(self, blob_sha: bytes | str) -> list[tuple[str, bytes, bytes]]:
        """Returns [(repo_path, commit_sha, filepath)] for where blob_sha changed."""
        cur = self.conn.execute(
            "SELECT repo_path, commit_sha, filepath FROM file_deltas WHERE blob_sha = ? ORDER BY id ASC",
            (_to_str(blob_sha),),
        )
        return [(r, _to_bytes(c), _to_bytes(p)) for r, c, p in cur.fetchall()]

    def get_all_path_changes(self) -> dict[tuple[str, bytes], list[tuple[bytes, bytes | None]]]:
        """Loads all path changes into memory: {(repo_path, filepath): [(commit, blob), ...]}."""
        cur = self.conn.execute(
            "SELECT repo_path, filepath, commit_sha, blob_sha FROM file_deltas ORDER BY id ASC"
        )
        changes = {}
        for r_str, p_str, c_str, b_str in cur.fetchall():
            key = (r_str, _to_bytes(p_str))
            changes.setdefault(key, []).append((_to_bytes(c_str), _to_bytes(b_str)))
        return changes

    def get_all_blob_occurrences(self) -> dict[bytes, list[tuple[str, bytes, bytes]]]:
        """Loads occurrences for all blobs: {blob_sha: [(repo, commit, path), ...]}."""
        cur = self.conn.execute(
            "SELECT blob_sha, repo_path, commit_sha, filepath FROM file_deltas WHERE blob_sha IS NOT NULL ORDER BY id ASC"
        )
        occs = {}
        for b_str, r_str, c_str, p_str in cur.fetchall():
            b_bytes = _to_bytes(b_str)
            occs.setdefault(b_bytes, []).append((r_str, _to_bytes(c_str), _to_bytes(p_str)))
        return occs

    def get_delta_count(self, repo_path: str | None = None) -> int:
        """Returns count of delta entries, optionally filtered by repo."""
        if repo_path is not None:
            cur = self.conn.execute(
                "SELECT count(*) FROM file_deltas WHERE repo_path = ?",
                (_to_str(repo_path),),
            )
        else:
            cur = self.conn.execute("SELECT count(*) FROM file_deltas")
        return cur.fetchone()[0]

    def get_commit_count(self, repo_path: str | None = None) -> int:
        """Returns count of recorded commits, optionally filtered by repo."""
        if repo_path is not None:
            cur = self.conn.execute(
                "SELECT count(*) FROM commits WHERE repo_path = ?",
                (_to_str(repo_path),),
            )
        else:
            cur = self.conn.execute("SELECT count(*) FROM commits")
        return cur.fetchone()[0]

    # --- trigram bitmaps ---------------------------------------------------

    def save_trigrams(
        self,
        postings: dict[bytes, BitMap],
        indexed: BitMap,
        skipped: BitMap,
    ):
        """Persists trigram postings and tracking bitmaps to SQLite."""
        with self.conn:
            rows = [
                (_to_bytes(tri), bm.serialize())
                for tri, bm in postings.items()
            ]
            self.conn.executemany(
                "INSERT OR REPLACE INTO trigram_postings (trigram, bitmap) VALUES (?, ?)",
                rows,
            )
            self.conn.execute(
                "INSERT OR REPLACE INTO trigram_meta (key, bitmap) VALUES (?, ?)",
                ("indexed", indexed.serialize()),
            )
            self.conn.execute(
                "INSERT OR REPLACE INTO trigram_meta (key, bitmap) VALUES (?, ?)",
                ("skipped", skipped.serialize()),
            )

    def load_trigrams(self) -> tuple[dict[bytes, BitMap], BitMap, BitMap]:
        """Loads all persisted trigram postings and tracking bitmaps from SQLite."""
        postings = {}
        cur = self.conn.execute("SELECT trigram, bitmap FROM trigram_postings")
        for tri, bm_bytes in cur.fetchall():
            postings[_to_bytes(tri)] = BitMap.deserialize(bm_bytes)

        indexed = BitMap()
        skipped = BitMap()
        cur_meta = self.conn.execute("SELECT key, bitmap FROM trigram_meta")
        for key, bm_bytes in cur_meta.fetchall():
            if key == "indexed":
                indexed = BitMap.deserialize(bm_bytes)
            elif key == "skipped":
                skipped = BitMap.deserialize(bm_bytes)

        return postings, indexed, skipped

    # --- lifecycle / transactions ------------------------------------------

    def commit(self):
        """Flushes the current transaction to disk."""
        self.conn.commit()

    def close(self):
        """Commits and closes the connection."""
        self.conn.commit()
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type is None:
            self.commit()
        self.close()
