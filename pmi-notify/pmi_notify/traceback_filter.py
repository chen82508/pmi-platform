"""Project-native traceback filtering.

An unfiltered `traceback.format_exception()` dump is mostly noise in a Slack
alert: dozens of frames from `httpx`, `asyncio`, `sqlalchemy`, or the
interpreter bootstrap bury the one or two frames that actually show where
*this service's* code broke. `filter_traceback` strips everything else so
the alert stays scannable.

Exclusion is marker-based (substring match against each frame's filename) so
it works out of the box for any project's `.venv` / `site-packages` layout
with zero configuration — this module has no notion of any specific caller's
directory structure. `include_root` is an optional, stricter opt-in for
callers that DO know their own package root and want every kept frame to be
inside it (belt-and-suspenders on top of the exclusion markers, not a
replacement for them — a frame could be outside a `.venv` yet still not be
part of the caller's own project, e.g. a sibling package in a monorepo).
"""

from __future__ import annotations

import traceback
from collections.abc import Sequence

DEFAULT_EXCLUDE_MARKERS: tuple[str, ...] = (
    ".venv/",
    "/site-packages/",
    "/dist-packages/",
    "/poetry/",
    "/pypoetry/",
    "/pipenv/",
    "<frozen ",
)

_NO_NATIVE_FRAMES_NOTE = "(no project-native frames in traceback — raised entirely inside a dependency)"


def filter_traceback(
    exc: BaseException,
    *,
    exclude_markers: Sequence[str] = DEFAULT_EXCLUDE_MARKERS,
    include_root: str | None = None,
    max_frames: int = 20,
) -> str:
    """Render `exc`'s traceback keeping only project-native frames.

    A frame is dropped if its filename contains any `exclude_markers`
    substring, or (when `include_root` is given) does NOT contain
    `include_root`. The exception's own `type: message` line is always
    appended, even when every frame was filtered out, so the alert never
    loses the one fact that matters most.

    `max_frames` keeps the LAST N surviving frames (closest to where the
    error actually occurred) plus a truncation note, so a deep native call
    stack can't blow out the Slack message size limit.
    """
    frames = traceback.extract_tb(exc.__traceback__)

    kept = [
        frame
        for frame in frames
        if not any(marker in frame.filename for marker in exclude_markers)
        and (include_root is None or include_root in frame.filename)
    ]

    header = ""
    if not kept:
        header = _NO_NATIVE_FRAMES_NOTE
    elif len(kept) > max_frames:
        header = f"... ({len(kept) - max_frames} earlier native frame(s) omitted) ..."

    frame_text = "".join(traceback.format_list(kept[-max_frames:])).rstrip("\n")
    tail = f"{exc.__class__.__name__}: {exc}"

    parts = [part for part in (header, frame_text, tail) if part]
    return "\n".join(parts)
