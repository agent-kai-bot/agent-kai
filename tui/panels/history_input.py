"""Input widget with bash-style up/down arrow history navigation.

Subclasses Textual's ``Input`` to add the kind of line-history that
every shell has had since the 70s: type a few commands, hit Up to
recall them, edit, hit Enter to send. History is per-instance and
optionally persisted to a file in the same format as ``.bash_history``
(one entry per line) so it survives across TUI restarts.

Why a subclass instead of an App-level on_key handler:

  Textual routes key events to the focused widget first. Catching Up
  and Down at the App level via BINDINGS only fires if the focused
  widget does not consume them — and Input's BINDINGS already include
  navigation keys. The most reliable way to bind Up/Down to history
  navigation specifically when the input is focused is to put the
  bindings on the input widget itself. As a bonus this keeps the
  history state encapsulated with the widget rather than on the app.
"""

from __future__ import annotations

from pathlib import Path

from textual.binding import Binding
from textual.widgets import Input


class HistoryInput(Input):
    """Single-line input with bash-style history (Up/Down arrows).

    History semantics match GNU readline / bash:

    - Up arrow walks backward through previously submitted lines
    - Down arrow walks forward; from the most recent past entry it
      lands on the in-progress draft (whatever the user was typing
      before they started browsing)
    - Consecutive duplicate lines are collapsed (only the most
      recent of a run is kept)
    - Empty / whitespace-only submissions are not added
    - Submitting any line resets the history cursor and clears the
      saved draft

    Persistence: pass ``history_path`` to load on init and
    auto-append on every ``remember()`` call. The file format is
    plain text, one entry per line. Capped at ``max_history`` —
    on overflow the oldest entries are dropped both from memory
    and (lazily) from disk on the next save.
    """

    # Up and Down are not used by single-line Input for cursor
    # movement (only Left and Right are), so we can claim them
    # without colliding with the parent's bindings. Leaving the
    # `show=False` flag set keeps these out of the footer hint
    # since they're discoverable through normal terminal habits.
    BINDINGS = [
        Binding("up", "history_prev", "Previous", show=False),
        Binding("down", "history_next", "Next", show=False),
    ]

    def __init__(
        self,
        *args,
        max_history: int = 200,
        history_path: Path | None = None,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self._history: list[str] = []
        self._history_index: int = -1  # -1 == draft mode (not browsing)
        self._draft: str = ""           # in-progress text saved when Up pressed
        self._max_history: int = max_history
        self._history_path: Path | None = history_path
        if history_path is not None:
            self._load_from_disk()

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------

    def remember(self, text: str) -> None:
        """Record a submitted line and reset the cursor.

        Call this after the user submits but before the input is
        cleared (the actual clear happens in the parent app's
        on_input_submitted handler). Deduplication: a run of
        identical entries is collapsed to one — same as
        ``HISTCONTROL=ignoredups`` in bash. Whitespace-only lines
        are dropped completely (not added to history).
        """
        text = text.strip()
        # Always reset the cursor and draft regardless of whether we
        # actually appended — the user submitted, so any in-progress
        # browse session is over.
        self._history_index = -1
        self._draft = ""
        if not text:
            return
        if self._history and self._history[-1] == text:
            return  # dedupe consecutive
        self._history.append(text)
        if len(self._history) > self._max_history:
            # Drop the oldest. Cheap because max_history is small (200).
            self._history = self._history[-self._max_history:]
        if self._history_path is not None:
            self._append_to_disk(text)

    # ------------------------------------------------------------------
    # actions (bound to Up / Down)
    # ------------------------------------------------------------------

    def action_history_prev(self) -> None:
        """Walk one entry backward (older). Up arrow."""
        if not self._history:
            return
        if self._history_index == -1:
            # Save whatever the user was typing so Down can restore it.
            self._draft = self.value
            self._history_index = len(self._history) - 1
        elif self._history_index > 0:
            self._history_index -= 1
        else:
            return  # already at oldest entry
        self._set_value_and_eol(self._history[self._history_index])

    def action_history_next(self) -> None:
        """Walk one entry forward (newer). Down arrow."""
        if self._history_index == -1:
            return  # already on the draft, nothing newer
        if self._history_index < len(self._history) - 1:
            self._history_index += 1
            self._set_value_and_eol(self._history[self._history_index])
        else:
            # We were on the most recent past entry — return to the draft.
            self._history_index = -1
            self._set_value_and_eol(self._draft)

    # ------------------------------------------------------------------
    # internal helpers
    # ------------------------------------------------------------------

    def _set_value_and_eol(self, text: str) -> None:
        """Update the visible input value and put the cursor at EOL.

        bash always parks the cursor at the end of the recalled line
        rather than at column 0, so the user can just keep typing to
        append or hit Backspace to edit. Mirror that.
        """
        self.value = text
        self.cursor_position = len(text)

    def _load_from_disk(self) -> None:
        """Load history entries from ``self._history_path`` if it exists.

        Silently no-ops on missing file or read errors — history is a
        convenience, not load-bearing, so a corrupt file should never
        block the TUI from starting.
        """
        path = self._history_path
        if path is None or not path.exists():
            return
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except Exception:
            return
        # Strip whitespace, drop empties, dedupe consecutive duplicates,
        # cap at max_history (most recent N).
        cleaned: list[str] = []
        for raw in lines:
            line = raw.strip()
            if not line:
                continue
            if cleaned and cleaned[-1] == line:
                continue
            cleaned.append(line)
        if len(cleaned) > self._max_history:
            cleaned = cleaned[-self._max_history:]
        self._history = cleaned

    def _append_to_disk(self, text: str) -> None:
        """Append a single entry to the history file.

        Append-only writes are O(1) and don't require holding the
        whole file in memory. The cap is enforced lazily — on next
        startup, ``_load_from_disk`` will trim to the most recent
        ``max_history`` entries. The on-disk file may exceed the
        cap between sessions; that's fine and matches how
        ``.bash_history`` behaves.
        """
        path = self._history_path
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as f:
                f.write(text + "\n")
        except Exception:
            # Persistence failures must not break the TUI.
            pass
