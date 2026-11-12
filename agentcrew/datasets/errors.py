"""The one error an upload raises.

Its message is shown to the visitor as it is, so it says what was wrong
with the file and what limit applies, and never contains file contents,
paths on the server or a traceback.
"""

from __future__ import annotations


class UploadError(ValueError):
    """The file was refused. `str(exc)` is safe to show in the browser."""
