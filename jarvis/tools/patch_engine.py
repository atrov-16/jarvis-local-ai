"""Engine for applying Search & Replace patches to text files."""

from __future__ import annotations

import re
from typing import NamedTuple


class PatchHunk(NamedTuple):
    search: str
    replace: str


class PatchResult(NamedTuple):
    success: bool
    content: str | None = None
    error: str | None = None


class PatchEngine:
    """Handles the identification and replacement of text blocks within a file."""

    def apply_hunks(self, content: str, hunks: list[PatchHunk]) -> PatchResult:
        """Apply a series of hunks to the content sequentially."""
        current_content = content
        
        for i, hunk in enumerate(hunks):
            # 1. Normalize line endings for the search block
            search_block = hunk.search.replace("\r\n", "\n")
            replace_block = hunk.replace.replace("\r\n", "\n")
            
            # 2. Check for exact match
            matches = list(re.finditer(re.escape(search_block), current_content))
            
            if not matches:
                # Try normalization (strip trailing whitespace per line)
                norm_content = self._normalize_content(current_content)
                norm_search = self._normalize_content(search_block)
                matches = list(re.finditer(re.escape(norm_search), norm_content))
                
                if not matches:
                    return PatchResult(
                        success=False, 
                        error=f"Hunk {i+1} not found. Search block must match exactly (including whitespace)."
                    )
                
                # If we found it in normalized content, we need to apply it carefully.
                # For Phase 1, we prefer strict matching to avoid corruption.
                return PatchResult(
                    success=False,
                    error=f"Hunk {i+1} found but with whitespace differences. Please provide an exact match."
                )

            if len(matches) > 1:
                return PatchResult(
                    success=False, 
                    error=f"Hunk {i+1} is ambiguous; found {len(matches)} occurrences."
                )

            # 3. Apply replacement
            match = matches[0]
            current_content = (
                current_content[:match.start()] + 
                replace_block + 
                current_content[match.end():]
            )

        return PatchResult(success=True, content=current_content)

    def _normalize_content(self, content: str) -> str:
        """Remove trailing whitespace from each line and normalize newlines."""
        lines = [line.rstrip() for line in content.splitlines()]
        return "\n".join(lines)
