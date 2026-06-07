"""Batch-mode drawing digest tests (Files-API upload + Message Batches).

Hermetic: a fake client provides ``.beta.files.upload/delete``,
``.beta.messages.batches.create``, and ``.messages.batches.retrieve/results``,
so the whole submit → poll → collect path runs without PyMuPDF or the network.
One end-to-end pipeline test renders a synthetic PDF and is skipped when PyMuPDF
is absent.
"""
from __future__ import annotations

import threading
from pathlib import Path

import pytest

from src.drawings.batch_digest import collect_drawing_batch, submit_drawing_batch
from src.drawings.digest_cache import DigestCache
from src.drawings.file_upload import FILES_API_BETA, upload_sheet_images
from src.drawings.models import ImageTile, RenderedSheet, SheetRef
from tests.fixtures.fake_anthropic import (
    FakeBatchResult,
    FakeBatchResultEnvelope,
    FakeMessage,
    FakeTextBlock,
    FakeUsage,
    batch_errored_result,
)

OPUS = "claude-opus-4-8"
NOSLEEP = lambda _s: None  # noqa: E731 - tests never actually wait on the poll


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #


class _Obj:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class _FakeFiles:
    def __init__(self):
        self.uploaded_ids: list[str] = []
        self.deleted: list[str] = []
        self._n = 0

    def upload(self, *, file):
        name, data, ctype = file
        assert ctype == "image/png"
        fid = f"file_{self._n}"
        self._n += 1
        self.uploaded_ids.append(fid)
        return _Obj(id=fid)

    def delete(self, file_id):
        self.deleted.append(file_id)


class _FakeBatches:
    """Serves both the beta (create) and sync (retrieve/results) namespaces."""

    def __init__(self, client):
        self._c = client

    def create(self, *, requests, betas=None):
        self._c.create_calls.append({"requests": list(requests), "betas": betas})
        self._c.submitted = list(requests)
        return _Obj(id="batch_abc")

    def retrieve(self, batch_id):
        self._c.retrieve_calls.append(batch_id)
        n = len(self._c.submitted)
        return _Obj(
            processing_status=self._c.status,
            request_counts=_Obj(
                succeeded=n, errored=0, canceled=0, expired=0, processing=0
            ),
        )

    def results(self, batch_id):
        order = self._c.submitted
        if self._c.reverse_results:
            order = list(reversed(order))
        for req in order:
            yield self._c.responder(req)


class _FakeClient:
    def __init__(self, responder, *, status="ended", reverse_results=False):
        self.responder = responder
        self.status = status
        self.reverse_results = reverse_results
        self.create_calls: list[dict] = []
        self.retrieve_calls: list[str] = []
        self.submitted: list[dict] = []
        self.files = _FakeFiles()
        batches = _FakeBatches(self)
        self.beta = _Obj(files=self.files, messages=_Obj(batches=batches))
        self.messages = _Obj(batches=batches)


def _succeed(req, *, in_tok=100, out_tok=20):
    cid = req["custom_id"]
    msg = FakeMessage(
        content=[FakeTextBlock(text=f"digest body for {cid}")],
        usage=FakeUsage(input_tokens=in_tok, output_tokens=out_tok),
        stop_reason="end_turn",
    )
    return FakeBatchResult(
        custom_id=cid, result=FakeBatchResultEnvelope(type="succeeded", message=msg)
    )


def _make_sheet(index: int, *, rows: int = 2, cols: int = 2) -> RenderedSheet:
    ref = SheetRef(
        pdf_path=Path(f"M-10{index}.pdf"),
        page_index=0,
        source_name=f"M-10{index}.pdf",
        page_count=1,
    )
    overview = ImageTile(
        png_bytes=f"OVERVIEW-{index}".encode(),
        width_px=2000,
        height_px=1500,
        kind="overview",
    )
    tiles = [
        ImageTile(
            png_bytes=f"T{index}-{r}{c}".encode(),
            width_px=2000,
            height_px=1500,
            kind="tile",
            row=r,
            col=c,
            label=f"r{r}c{c}",
        )
        for r in range(rows)
        for c in range(cols)
    ]
    return RenderedSheet(
        ref=ref,
        overview=overview,
        tiles=tiles,
        page_width_pt=3168,
        page_height_pt=2448,
        rows=rows,
        cols=cols,
    )


def _run_batch(client, sheets, *, cache=None):
    batch = submit_drawing_batch(
        iter(sheets), client=client, model=OPUS, cache=cache, total=len(sheets)
    )
    return batch, collect_drawing_batch(
        batch, client=client, cache=cache, sleep=NOSLEEP
    )


# --------------------------------------------------------------------------- #
# Files-API upload helper
# --------------------------------------------------------------------------- #


def test_upload_sheet_images_uses_file_ids_not_base64():
    client = _FakeClient(_succeed)
    sheet = _make_sheet(1, rows=2, cols=2)  # overview + 4 tiles = 5 images

    up = upload_sheet_images(client, sheet)

    images = [b for b in up.content if b["type"] == "image"]
    assert len(images) == 5
    # Every image is a file_id reference — no inline base64 in the request body.
    for blk in images:
        assert blk["source"]["type"] == "file"
        assert "data" not in blk["source"]
        assert blk["source"]["file_id"].startswith("file_")
    assert len(up.file_ids) == 5
    # The framing + per-tile labels + task instruction are preserved.
    texts = " ".join(b["text"] for b in up.content if b["type"] == "text")
    assert "OVERVIEW" in texts and "Tile r1c1" in texts and "digest" in texts.lower()


def test_upload_failure_cleans_up_partial_upload():
    class _Boom(_FakeFiles):
        def upload(self, *, file):
            if len(self.uploaded_ids) >= 2:
                raise RuntimeError("upload exploded")
            return super().upload(file=file)

    client = _FakeClient(_succeed)
    client.files = _Boom()
    client.beta.files = client.files

    with pytest.raises(RuntimeError, match="upload exploded"):
        upload_sheet_images(client, _make_sheet(1))
    # The two images uploaded before the failure are deleted — no leak.
    assert client.files.deleted == ["file_0", "file_1"]


# --------------------------------------------------------------------------- #
# submit + collect
# --------------------------------------------------------------------------- #


def test_batch_happy_path_parses_all_sheets():
    client = _FakeClient(_succeed)
    sheets = [_make_sheet(i) for i in (1, 2, 3)]

    batch, digests = _run_batch(client, sheets)

    assert len(digests) == 3
    assert all(d.ok for d in digests)
    assert [d.ref.source_name for d in digests] == ["M-101.pdf", "M-102.pdf", "M-103.pdf"]
    assert digests[0].text == "digest body for sheet__0"
    assert digests[0].input_tokens == 100 and digests[0].output_tokens == 20
    assert digests[0].image_token_estimate > 0
    # One batch, created on the beta namespace with the Files-API beta header.
    assert len(client.create_calls) == 1
    assert client.create_calls[0]["betas"] == [FILES_API_BETA]
    # 3 sheets × 5 images uploaded, all deleted after a successful collect.
    assert len(client.files.uploaded_ids) == 15
    assert sorted(client.files.deleted) == sorted(client.files.uploaded_ids)


def test_batch_request_shape_matches_digest():
    client = _FakeClient(_succeed)
    _run_batch(client, [_make_sheet(1)])

    params = client.create_calls[0]["requests"][0]["params"]
    assert params["model"] == OPUS
    assert params["thinking"] == {"type": "adaptive"}
    assert params["output_config"] == {"effort": "high"}
    assert params["messages"][0]["role"] == "user"
    # Body carries file-id images, never base64.
    images = [b for b in params["messages"][0]["content"] if b["type"] == "image"]
    assert images and all(b["source"]["type"] == "file" for b in images)


def test_batch_page_order_independent_of_result_order():
    client = _FakeClient(_succeed, reverse_results=True)  # results stream reversed
    sheets = [_make_sheet(i) for i in (1, 2, 3)]

    _, digests = _run_batch(client, sheets)

    # Still assembled in page order despite out-of-order results.
    assert [d.ref.source_name for d in digests] == ["M-101.pdf", "M-102.pdf", "M-103.pdf"]


def test_batch_errored_item_fails_only_that_sheet():
    def responder(req):
        if req["custom_id"] == "sheet__1":
            return batch_errored_result(
                custom_id="sheet__1", error_message="overloaded"
            )
        return _succeed(req)

    client = _FakeClient(responder)
    _, digests = _run_batch(client, [_make_sheet(i) for i in (1, 2, 3)])

    assert digests[0].ok and digests[2].ok
    assert not digests[1].ok
    assert "overloaded" in digests[1].error
    # Files are still cleaned up even with a mixed-result batch.
    assert sorted(client.files.deleted) == sorted(client.files.uploaded_ids)


def test_batch_missing_result_marks_sheet_failed():
    # Responder drops one custom_id entirely (no envelope returned for it).
    def responder(req):
        return _succeed(req) if req["custom_id"] != "sheet__1" else None

    client = _FakeClient(responder)

    class _Filtering(_FakeBatches):
        def results(self, batch_id):
            for req in self._c.submitted:
                r = self._c.responder(req)
                if r is not None:
                    yield r

    batches = _Filtering(client)
    client.beta.messages.batches = batches
    client.messages.batches = batches

    batch = submit_drawing_batch(
        iter([_make_sheet(i) for i in (1, 2)]), client=client, model=OPUS, total=2
    )
    digests = collect_drawing_batch(batch, client=client, sleep=NOSLEEP)

    assert digests[0].ok
    assert not digests[1].ok and "no result" in digests[1].error


# --------------------------------------------------------------------------- #
# Caching
# --------------------------------------------------------------------------- #


def test_cache_hit_skips_upload_and_batch_item():
    cache = DigestCache(None, persist=False)
    sheets = [_make_sheet(i) for i in (1, 2)]

    # First run digests both and writes the cache.
    client1 = _FakeClient(_succeed)
    _run_batch(client1, sheets, cache=cache)
    assert len(client1.create_calls) == 1
    assert len(client1.submitted) == 2

    # Second, identical run: both served from cache — no upload, no batch.
    client2 = _FakeClient(_succeed)
    _, digests = _run_batch(client2, sheets, cache=cache)
    assert [d.cached for d in digests] == [True, True]
    assert client2.create_calls == []  # nothing submitted
    assert client2.files.uploaded_ids == []  # nothing uploaded
    assert all(d.ok for d in digests)


def test_partial_cache_only_submits_the_miss():
    cache = DigestCache(None, persist=False)
    sheets = [_make_sheet(i) for i in (1, 2)]

    # Warm the cache for sheet 1 only by running it alone first.
    _run_batch(_FakeClient(_succeed), [sheets[0]], cache=cache)

    client = _FakeClient(_succeed)
    _, digests = _run_batch(client, sheets, cache=cache)

    assert digests[0].cached is True
    assert digests[1].cached is False and digests[1].ok
    # Only sheet 2 was uploaded + submitted.
    assert len(client.submitted) == 1
    assert client.submitted[0]["custom_id"] == "sheet__1"
    assert len(client.files.uploaded_ids) == 5  # one sheet's worth of images


# --------------------------------------------------------------------------- #
# Upload-failure handling at submit time
# --------------------------------------------------------------------------- #


def test_submit_upload_failure_captures_sheet_and_continues():
    state = {"n": 0}
    lock = threading.Lock()

    class _OneBadUpload(_FakeFiles):
        def upload(self, *, file):
            with lock:
                state["n"] += 1
                # Fail the first image of the SECOND sheet (6th upload overall).
                fail = state["n"] == 6
            if fail:
                raise RuntimeError("boom upload")
            return super().upload(file=file)

    client = _FakeClient(_succeed)
    client.files = _OneBadUpload()
    client.beta.files = client.files

    _, digests = _run_batch(client, [_make_sheet(1), _make_sheet(2)])

    assert digests[0].ok  # first sheet fine
    assert not digests[1].ok and "upload failed" in digests[1].error
    # Only the good sheet became a batch item.
    assert len(client.submitted) == 1


# --------------------------------------------------------------------------- #
# Detach / not-collected handling
# --------------------------------------------------------------------------- #


def test_batch_detach_marks_sheets_and_leaves_files():
    client = _FakeClient(_succeed)
    batch = submit_drawing_batch(
        iter([_make_sheet(1)]), client=client, model=OPUS, total=1
    )
    # Force the poll to immediately exceed the elapsed bound → "detached".
    digests = collect_drawing_batch(
        batch, client=client, sleep=NOSLEEP, max_elapsed_seconds=-1
    )

    assert not digests[0].ok
    assert "not collected" in digests[0].error
    assert "batch_abc" in digests[0].error
    # Files left in place for the still-running remote batch — not deleted.
    assert client.files.deleted == []


# --------------------------------------------------------------------------- #
# End-to-end via the pipeline (renders a synthetic PDF; needs PyMuPDF)
# --------------------------------------------------------------------------- #


def test_pipeline_use_batch_combines_digests(tmp_path):
    pymupdf = pytest.importorskip("pymupdf")
    from src.drawings.pipeline import extract_drawing_context

    doc = pymupdf.open()
    for i in range(2):
        page = doc.new_page(width=792, height=612)
        page.insert_text((72, 72), f"SHEET M-10{i + 1} TEST")
    path = tmp_path / "set.pdf"
    doc.save(str(path))
    doc.close()

    client = _FakeClient(_succeed)
    progress: list[tuple] = []
    ctx = extract_drawing_context(
        [path],
        client=client,
        rows=2,
        cols=2,
        use_batch=True,
        progress=lambda d, t, label: progress.append((d, t, label)),
    )

    assert ctx.sheet_count == 2
    assert ctx.ok_sheet_count == 2
    assert "## Sheet 1/2" in ctx.combined_text
    assert "digest body for sheet__0" in ctx.combined_text
    assert ctx.total_input_tokens == 200  # 2 sheets × 100
    assert len(client.create_calls) == 1  # one batch for the set
    assert progress[-1] == (2, 2, "Done")
