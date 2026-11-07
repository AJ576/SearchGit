"""Inverted trigram index over blob contents using Roaring Bitmaps.

Operates purely on integer blob IDs and raw bytes. Has no Git knowledge.
"""

from pyroaring import BitMap

MAX_BLOB = 1_000_000


def extract(data: bytes) -> set[bytes]:
    """Unique LOWERCASE 3-byte windows of data. Empty set if len < 3."""
    if isinstance(data, str):
        data = data.encode()
    if len(data) < 3:
        return set()
    data = data.lower()
    return {data[i : i + 3] for i in range(len(data) - 2)}


def is_indexable(data: bytes) -> bool:
    """False if len > MAX_BLOB or NUL byte in first 8KB (binary)."""
    if isinstance(data, str):
        data = data.encode()
    if len(data) > MAX_BLOB:
        return False
    if b"\x00" in data[:8192]:
        return False
    return True


class TrigramIndex:
    """Inverted trigram index mapping 3-byte windows to matching blob IDs."""

    postings: dict[bytes, BitMap]  # trigram -> blob ids
    indexed: BitMap                # ids trigrammed
    skipped: BitMap                # ids seen but not indexable

    def __init__(self):
        """Initializes empty postings and tracking bitmaps."""
        self.postings = {}
        self.indexed = BitMap()
        self.skipped = BitMap()

    def add(self, blob_id: int, data: bytes) -> bool:
        """Index one blob. Return False if skipped (not indexable)."""
        if not is_indexable(data):
            self.skipped.add(blob_id)
            return False
        self.indexed.add(blob_id)
        for tri in extract(data):
            bm = self.postings.get(tri)
            if bm is None:
                bm = BitMap()
                self.postings[tri] = bm
            bm.add(blob_id)
        return True

    def candidates(self, query: bytes) -> BitMap:
        """Lowercase query -> trigrams -> intersect bitmaps, rarest first.
        Missing trigram => empty BitMap. Raise ValueError if len(query) < 3.
        May contain false positives."""
        if isinstance(query, str):
            query = query.encode()
        if len(query) < 3:
            raise ValueError(f"query must be at least 3 bytes, got {len(query)}")
        trigrams = extract(query)
        if not trigrams:
            return BitMap()
        for tri in trigrams:
            if tri not in self.postings:
                return BitMap()
        sorted_trigrams = sorted(trigrams, key=lambda t: len(self.postings[t]))
        result = self.postings[sorted_trigrams[0]].copy()
        for tri in sorted_trigrams[1:]:
            result &= self.postings[tri]
            if not result:
                break
        return result

    def search(self, query: bytes, read_blob, case_sensitive: bool = True) -> list[int]:
        """candidates() then VERIFY against real bytes from read_blob(id).
        case_sensitive: `query in data`; else compare lowercased."""
        if isinstance(query, str):
            query = query.encode()
        cands = self.candidates(query)
        matching = []
        q_lower = query.lower() if not case_sensitive else None
        for blob_id in cands:
            data = read_blob(blob_id)
            if data is None:
                continue
            if case_sensitive:
                if query in data:
                    matching.append(blob_id)
            else:
                if q_lower in data.lower():
                    matching.append(blob_id)
        return matching
