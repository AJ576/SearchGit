import pytest
from pyroaring import BitMap

from trigram import MAX_BLOB, TrigramIndex, extract, is_indexable
from store import DELETED, Store, canonical
from indexer import Indexer
from reader import Hit, Occurrence, Reader


class TestTrigram:
    def test_extract_short(self):
        assert extract(b"") == set()
        assert extract(b"a") == set()
        assert extract(b"ab") == set()

    def test_extract_valid(self):
        assert extract(b"abc") == {b"abc"}
        assert extract(b"AbC") == {b"abc"}
        assert extract(b"abcdef") == {b"abc", b"bcd", b"cde", b"def"}

    def test_extract_duplicates(self):
        assert extract(b"aaaa") == {b"aaa"}

    def test_is_indexable_size(self):
        assert is_indexable(b"x" * 100) is True
        assert is_indexable(b"x" * MAX_BLOB) is True
        assert is_indexable(b"x" * (MAX_BLOB + 1)) is False

    def test_is_indexable_nul_byte(self):
        assert is_indexable(b"\x00hello") is False
        assert is_indexable(b"hello\x00world") is False
        # NUL byte in first 8KB
        data_within_8k = b"x" * 8191 + b"\x00"
        assert is_indexable(data_within_8k) is False
        # NUL byte after first 8KB is allowed
        data_after_8k = b"x" * 8192 + b"\x00"
        assert is_indexable(data_after_8k) is True

    def test_trigram_index_add_and_candidates(self):
        idx = TrigramIndex()
        # Add indexable blob
        assert idx.add(0, b"Hello World") is True
        assert 0 in idx.indexed
        assert 0 not in idx.skipped
        assert b"hel" in idx.postings
        assert 0 in idx.postings[b"hel"]

        # Add unindexable blob
        assert idx.add(1, b"\x00binary") is False
        assert 1 in idx.skipped
        assert 1 not in idx.indexed

        # Candidates query < 3 bytes
        with pytest.raises(ValueError):
            idx.candidates(b"he")

        # Candidates query matching
        cands = idx.candidates(b"world")
        assert 0 in cands

        # Candidates query not matching
        cands_missing = idx.candidates(b"xyz")
        assert len(cands_missing) == 0

    def test_rarest_first_order(self):
        idx = TrigramIndex()
        # Create postings:
        # "abc" appears in 5 blobs
        # "bcd" appears in 2 blobs
        for i in range(5):
            idx.postings.setdefault(b"abc", BitMap()).add(i)
        for i in range(2):
            idx.postings.setdefault(b"bcd", BitMap()).add(i)

        # candidates for "abcd" needs "abc" and "bcd"
        cands = idx.candidates(b"abcd")
        assert list(cands) == [0, 1]

    def test_trigram_index_search(self):
        idx = TrigramIndex()
        blobs = {
            0: b"The quick brown fox",
            1: b"QUICK FOX",
            2: b"quick and fox but far apart",
            3: b"no match here",
        }
        for b_id, content in blobs.items():
            idx.add(b_id, content)

        def mock_read(b_id):
            return blobs.get(b_id)

        # Search case-sensitive
        matches_case = idx.search(b"quick", mock_read, case_sensitive=True)
        assert 0 in matches_case
        assert 2 in matches_case
        assert 1 not in matches_case  # QUICK is uppercase

        # Search case-insensitive
        matches_nocase = idx.search(b"quick", mock_read, case_sensitive=False)
        assert 0 in matches_nocase
        assert 1 in matches_nocase
        assert 2 in matches_nocase
        assert 3 not in matches_nocase

        # False positive filtering
        idx2 = TrigramIndex()
        idx2.add(10, b"abc def xyz")
        blobs2 = {10: b"abc def xyz"}
        assert idx2.search(b"abcxyz", lambda x: blobs2.get(x)) == []

        # Read returns None
        assert idx2.search(b"abc def", lambda x: None) == []


class TestStore:
    def test_store_blob_id_and_occurrences(self):
        store = Store()
        sha1 = b"sha_111111111111111111111111111111111111"
        sha2 = b"sha_222222222222222222222222222222222222"

        store.add_commit("repo1", b"c1", 1000)
        assert store.commit_times[("repo1", b"c1")] == 1000

        # First time sha1 is added
        store.add_delta("repo1", b"c1", b"file1.txt", sha1)
        assert store.blob_ids[sha1] == 0
        assert store.blob_shas[0] == sha1
        assert store.blob_repo[0] == "repo1"
        assert store.pending == [0]
        assert len(store.blob_occurrences[sha1]) == 1
        assert store.blob_occurrences[sha1][0] == ("repo1", b"c1", b"file1.txt")

        # Second time sha1 is added (different commit, same repo)
        store.add_commit("repo1", b"c2", 2000)
        store.add_delta("repo1", b"c2", b"file2.txt", sha1)
        # blob_ids should not change
        assert store.blob_ids[sha1] == 0
        # pending should NOT contain a new entry for sha1!
        assert store.pending == [0]
        # but occurrences should have both!
        assert len(store.blob_occurrences[sha1]) == 2
        assert store.blob_occurrences[sha1][1] == ("repo1", b"c2", b"file2.txt")

        # Third time sha1 is added (different repo)
        store.add_commit("repo2", b"c3", 1500)
        store.add_delta("repo2", b"c3", b"file1.txt", sha1)
        assert store.pending == [0]
        assert len(store.blob_occurrences[sha1]) == 3

        # DELETED should not assign blob_id or record occurrence
        store.add_delta("repo1", b"c2", b"file1.txt", DELETED)
        assert DELETED not in store.blob_occurrences
        assert len(store.blob_shas) == 1

        # take_pending clears pending
        pending = store.take_pending()
        assert pending == [0]
        assert store.pending == []

        # Add sha2
        store.add_delta("repo1", b"c1", b"fileB.txt", sha2)
        assert store.blob_ids[sha2] == 1
        assert store.pending == [1]

    def test_read_blob_invalid(self):
        store = Store()
        assert store.read_blob(999) is None
        assert store.read_blob(-1) is None


class TestReaderAndHit:
    def test_hit_dataclass(self):
        occ1 = Occurrence(repo="repo1", commit=b"c1", path=b"p1", commit_time=100)
        occ2 = Occurrence(repo="repo1", commit=b"c2", path=b"p2", commit_time=200)
        hit = Hit(blob_sha=b"sha1", occurrences=[occ1, occ2])
        assert hit.first_seen == occ1

    def test_reader_search_ordering(self):
        store = Store()
        trigrams = TrigramIndex()
        reader = Reader(store, trigrams)

        sha_a = b"sha_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        sha_b = b"sha_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"

        store.blob_shas.extend([sha_a, sha_b])
        store.blob_ids[sha_a] = 0
        store.blob_ids[sha_b] = 1

        # Commit times: commit 1 (100), commit 2 (200), commit 3 (50)
        store.commit_times[("repo1", b"c1")] = 100
        store.commit_times[("repo1", b"c2")] = 200
        store.commit_times[("repo1", b"c3")] = 50

        # sha_a was seen in c2 (200) and c1 (100) -> first_seen is c1 (100)
        store.blob_occurrences[sha_a] = [
            ("repo1", b"c2", b"path_a2.txt"),
            ("repo1", b"c1", b"path_a1.txt"),
        ]

        # sha_b was seen in c3 (50) -> first_seen is c3 (50)
        store.blob_occurrences[sha_b] = [
            ("repo1", b"c3", b"path_b.txt"),
        ]

        # Populate trigrams so both match "common_query"
        content = b"common_query"
        trigrams.add(0, content)
        trigrams.add(1, content)

        # Mock read_blob to return content
        store.read_blob = lambda blob_id: content

        hits = reader.search("common_query")
        assert len(hits) == 2

        # hits should be sorted by first_seen time:
        # sha_b first_seen is at 50, sha_a first_seen is at 100
        assert hits[0].blob_sha == sha_b
        assert hits[0].first_seen.commit_time == 50
        assert hits[1].blob_sha == sha_a
        assert hits[1].first_seen.commit_time == 100

        # occurrences within sha_a should be sorted oldest first (100 then 200)
        assert [occ.commit_time for occ in hits[1].occurrences] == [100, 200]


class TestIntegrationWithPhysicsEngine:
    def test_index_and_search(self):
        store = Store()
        trigrams = TrigramIndex()
        indexer = Indexer(store, trigrams)
        reader = Reader(store, trigrams)

        repo_path = "physics_engine.git"
        indexer.index_repo(repo_path)

        key = canonical(repo_path)
        assert store.is_indexed(key)
        assert len(store.repo_commits[key]) > 0
        assert len(store.blob_ids) > 0

        # Pending should be cleared after indexing
        assert store.pending == []

        # Trigrams should have indexed blobs
        assert len(trigrams.indexed) > 0

        # Search for something in physics_engine.git
        hits = reader.search("Vector")
        assert len(hits) > 0

        for hit in hits:
            assert isinstance(hit.blob_sha, bytes)
            assert isinstance(hit.occurrences, list)
            assert len(hit.occurrences) > 0
            assert hit.first_seen == hit.occurrences[0]

            # Verify occurrences are sorted oldest first
            ctimes = [occ.commit_time for occ in hit.occurrences if occ.commit_time is not None]
            assert ctimes == sorted(ctimes)

            # Test reading full text via reader.read
            text = reader.read(hit.blob_sha)
            assert text is not None
            assert b"Vector" in text or b"vector" in text

        # Verify hits are sorted by first_seen time
        first_times = [
            hit.first_seen.commit_time
            for hit in hits
            if hit.first_seen.commit_time is not None
        ]
        assert first_times == sorted(first_times)

    def test_case_sensitivity(self):
        store = Store()
        trigrams = TrigramIndex()
        indexer = Indexer(store, trigrams)
        reader = Reader(store, trigrams)

        indexer.index_repo("physics_engine.git")

        hits_sensitive = reader.search("Vector", case_sensitive=True)
        hits_insensitive = reader.search("vector", case_sensitive=False)

        assert len(hits_insensitive) >= len(hits_sensitive)
        for hit in hits_sensitive:
            data = reader.read(hit.blob_sha)
            assert b"Vector" in data

    def test_read_non_blob_sha(self):
        store = Store()
        trigrams = TrigramIndex()
        indexer = Indexer(store, trigrams)
        reader = Reader(store, trigrams)

        repo_path = "physics_engine.git"
        indexer.index_repo(repo_path)

        repo = store.get_repo(repo_path)
        head_commit_sha = repo.head()
        root_tree_sha = repo[head_commit_sha].tree

        # Commit SHA and Tree SHA are valid git objects in repo, but not indexed blobs
        assert reader.read(head_commit_sha) is None
        assert reader.read(root_tree_sha) is None
        assert reader.read(b"nonexistent_sha_000000000000000000000000") is None
