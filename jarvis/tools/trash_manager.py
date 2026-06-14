"""Management of deleted files within the Jarvis trash system."""

from __future__ import annotations

import json
import logging
import shutil
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

LOG = logging.getLogger(__name__)


class TrashEntry:
    def __init__(
        self,
        id: str,
        original_path: str,
        trashed_at: str,
        task_id: str | None = None,
        type: str = "file",
    ) -> None:
        self.id = id
        self.original_path = original_path
        self.trashed_at = trashed_at
        self.task_id = task_id
        self.type = type

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "original_path": self.original_path,
            "trashed_at": self.trashed_at,
            "task_id": self.task_id,
            "type": self.type,
        }

    @classmethod
    def from_dict(cls, data: dict) -> TrashEntry:
        return cls(**data)


class TrashManager:
    """Handles moving files to trash and restoring them."""

    def __init__(self, workspace_root: Path) -> None:
        self.workspace_root = workspace_root.resolve()
        self.trash_dir = self.workspace_root / ".jarvis" / "trash"
        self.entries_dir = self.trash_dir / "entries"
        self.metadata_dir = self.trash_dir / "metadata"

        # Ensure directories exist
        self.entries_dir.mkdir(parents=True, exist_ok=True)
        self.metadata_dir.mkdir(parents=True, exist_ok=True)

    def trash(self, path: Path, task_id: str | None = None) -> str:
        """Move a file or directory to trash."""
        if not path.exists():
            raise FileNotFoundError(f"Path not found: {path}")

        entry_id = str(uuid4())
        entry_type = "directory" if path.is_dir() else "file"
        
        # 1. Create Metadata
        entry = TrashEntry(
            id=entry_id,
            original_path=str(path.resolve()),
            trashed_at=datetime.now(UTC).isoformat(),
            task_id=task_id,
            type=entry_type,
        )
        
        metadata_path = self.metadata_dir / f"{entry_id}.json"
        metadata_path.write_text(json.dumps(entry.to_dict(), indent=2), encoding="utf-8")

        # 2. Move Entry
        target_path = self.entries_dir / entry_id
        shutil.move(str(path), str(target_path))
        
        LOG.info(f"Trashed {entry_type} {path} as {entry_id}")
        return entry_id

    def restore(self, entry_id: str) -> Path:
        """Restore a trashed entry to its original location."""
        metadata_path = self.metadata_dir / f"{entry_id}.json"
        if not metadata_path.exists():
            raise ValueError(f"Trash entry not found: {entry_id}")

        entry = TrashEntry.from_dict(json.loads(metadata_path.read_text(encoding="utf-8")))
        source_path = self.entries_dir / entry_id
        target_path = Path(entry.original_path)

        if not source_path.exists():
            raise FileNotFoundError(f"Trashed content not found for entry: {entry_id}")

        if target_path.exists():
            raise FileExistsError(f"Cannot restore; path already occupied: {target_path}")

        # Ensure parent directory exists
        target_path.parent.mkdir(parents=True, exist_ok=True)

        # Move back
        shutil.move(str(source_path), str(target_path))
        
        # Cleanup metadata
        metadata_path.unlink()
        
        LOG.info(f"Restored entry {entry_id} to {target_path}")
        return target_path

    def list_entries(self) -> list[TrashEntry]:
        """List all entries in the trash."""
        entries = []
        for meta_file in self.metadata_dir.glob("*.json"):
            try:
                data = json.loads(meta_file.read_text(encoding="utf-8"))
                entries.append(TrashEntry.from_dict(data))
            except Exception as e:
                LOG.error(f"Failed to read trash metadata {meta_file}: {e}")
        return sorted(entries, key=lambda x: x.trashed_at, reverse=True)
