"""Files-API transport for the per-sheet drawing digest.

A single sheet carries 1 overview + N tiles (37 images for the default 6x6
grid). Inlining those as base64 in one Messages request pushes the body past
Anthropic's **32 MB request-size limit** for a dense E-size sheet, which the API
rejects with HTTP 400 — the failure that made every sheet in a permit set fail
at once. Per Anthropic's vision guidance ("For many images, consider uploading
with the Files API and referencing by ``file_id`` to keep request payloads
small"), the fix is to upload each image once and reference it by ``file_id`` so
the request body stays tiny. The same file-id references ride into the Message
Batches API for the 50% batch discount, and the batch's 256 MB envelope is never
approached because each item body is just a handful of ids.

Uploaded files are best-effort deleted after the batch is collected
(:func:`delete_files`); they cost nothing to store, but cleanup keeps the org's
file storage from accumulating a fresh image set on every run.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .digest import build_user_content_blocks
from .models import ImageTile, RenderedSheet

# Files API beta header. Current/recognized per Anthropic's Files API docs; the
# SDK sets it automatically on ``client.beta.files.*`` calls. It must also be
# attached to the message/batch that *references* a file_id — for the batch path
# that is the ``betas=`` arg on ``client.beta.messages.batches.create``. An
# *unrecognized* anthropic-beta value is rejected with HTTP 400, so this is the
# one current value, attached only where a file_id is actually used.
FILES_API_BETA = "files-api-2025-04-14"


def _file_image_block(file_id: str) -> dict:
    """A content block referencing a previously-uploaded image by ``file_id``."""
    return {"type": "image", "source": {"type": "file", "file_id": file_id}}


def _uploaded_id(uploaded: Any) -> str:
    """Read the id off an SDK ``FileObject`` (attr) or a plain-dict variant."""
    if isinstance(uploaded, dict):
        return str(uploaded.get("id", ""))
    return str(getattr(uploaded, "id", ""))


@dataclass
class SheetUpload:
    """One sheet's images uploaded to the Files API.

    ``content`` is the user-turn content (file-id image blocks in the same order
    and with the same labels/framing as the inline-base64 path); ``file_ids`` is
    every uploaded id, for post-collection cleanup.
    """

    content: list[dict]
    file_ids: list[str] = field(default_factory=list)


def _safe_stem(sheet: RenderedSheet) -> str:
    raw = sheet.ref.display_label
    return "".join(c if c.isalnum() else "_" for c in raw)[:60] or "sheet"


def upload_sheet_images(client: Any, sheet: RenderedSheet) -> SheetUpload:
    """Upload a sheet's overview + tiles via the Files API; build file-id content.

    Returns the user-turn content blocks (image-by-file_id, identical framing to
    the base64 path via :func:`~drawing_analyzer.digest.build_user_content_blocks`)
    plus the uploaded ``file_id``s for cleanup. Raises on an upload failure; the
    caller treats the sheet as failed and deletes any ids already uploaded, so a
    partial upload never leaks files.
    """
    stem = _safe_stem(sheet)
    file_ids: list[str] = []
    mapping: dict[int, str] = {}

    def _upload(image: ImageTile, name: str) -> None:
        uploaded = client.beta.files.upload(file=(name, image.png_bytes, "image/png"))
        fid = _uploaded_id(uploaded)
        file_ids.append(fid)
        mapping[id(image)] = fid

    try:
        _upload(sheet.overview, f"{stem}-overview.png")
        for tile in sheet.tiles:
            _upload(tile, f"{stem}-r{tile.row + 1}c{tile.col + 1}.png")
    except Exception:
        delete_files(client, file_ids)  # don't leak a half-uploaded set
        raise

    content = build_user_content_blocks(sheet, lambda t: _file_image_block(mapping[id(t)]))
    return SheetUpload(content=content, file_ids=file_ids)


def delete_files(client: Any, file_ids: list[str]) -> None:
    """Best-effort delete uploaded files; never raises (cleanup is advisory)."""
    files = getattr(getattr(client, "beta", None), "files", None)
    deleter = getattr(files, "delete", None)
    if deleter is None:
        return
    for fid in file_ids:
        if not fid:
            continue
        try:
            deleter(fid)
        except Exception:  # noqa: BLE001 - cleanup must never sink a run
            pass
