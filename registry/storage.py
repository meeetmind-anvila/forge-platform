"""
Content-addressable blob store.
Blobs stored as: blobs/{sha256[:2]}/{sha256}
Writes are atomic: temp → rename.
"""
import os
import tempfile
from pathlib import Path


class BlobStore:
    def __init__(self, root: str):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "tmp").mkdir(exist_ok=True)

    def tmp_path(self) -> str:
        """Return a path for a new temp file inside the store."""
        fd, path = tempfile.mkstemp(dir=str(self.root / "tmp"))
        os.close(fd)
        return path

    def path(self, sha256: str) -> Path:
        """Return the canonical path for a blob (may not exist yet)."""
        prefix = sha256[:2]
        return self.root / prefix / sha256

    def exists(self, sha256: str) -> bool:
        return self.path(sha256).exists()

    def commit(self, tmp_path: str, sha256: str) -> Path:
        """
        Atomically move tmp_path to the canonical blob location.
        If the blob already exists (race / dedup), tmp is removed.
        Returns the final blob path.
        """
        dest = self.path(sha256)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            Path(tmp_path).unlink(missing_ok=True)
            return dest
        # os.rename is atomic on POSIX when src/dst are on the same filesystem.
        # Both tmp and blobs/ live under self.root so this holds.
        os.rename(tmp_path, str(dest))
        return dest