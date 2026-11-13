"""The one error an upload raises.

Its message is shown to the visitor as it is, so it says what was wrong
with the file and what limit applies, and never contains file contents,
paths on the server or a traceback.
"""

from __future__ import annotations


class UploadError(ValueError):
    """The file was refused. `str(exc)` is safe to show in the browser."""


class ConvertedTooLarge(UploadError):
    """The converted data passed its size cap while it was being written.
    The caller decides what to tell the visitor: the cap may be the per-upload
    limit or the room left in the store."""
