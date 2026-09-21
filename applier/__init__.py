"""Spec Critic Edit Applier — a separate program that consumes the sidecar.

Spec Critic emits edit instructions and does not apply them. That separation
is deliberate and load-bearing: v3.0.0 deleted the surgical write-back stack
because silently rewriting a legal document is the loudest way to hide
uncertainty (``handbook/17_evolution_and_lessons.md``). This package is the
"separate, future applier program" the sidecar was always written for
(``src/output/edit_sidecar.py``), and it keeps that promise rather than
undoing it, in four ways:

1. **It never edits in place.** Every run writes ``<stem>.applied.docx``
   beside the source and leaves the original byte-identical.
2. **It defaults to Word tracked changes**, not silent replacement. The
   applier proposes inside Word's own review UI; Accept/Reject stays the
   human gate. ``--mode direct`` is opt-in.
3. **It is deterministic by default and costs nothing.** Locating an edit
   target is an index lookup against the element ids Spec Critic already
   minted (``src/input/extractor.ParagraphMapping.element_id``). No model
   call is made unless ``--assist`` is passed.
4. **It refuses rather than guesses.** An ambiguous, drifted, or missing
   target is reported in the receipt, never approximated.

The optional assist tier (:mod:`applier.assist`) is the one genuinely
agent-shaped part of the problem: when a spec has drifted since review, a
bounded tool loop searches the document for the intended target. It may
choose a *location* and nothing else — the replacement text always comes
from the sidecar, and a proposed element id that is not in the document's
real id set is dropped, mirroring how ``drawing_impact`` drops hallucinated
finding ids.

Import direction is a hard constraint: this package may read ``src`` (it must
resolve element ids with the same code that minted them, or the two would
drift and edits would land on the wrong paragraph), but nothing under ``src``
may ever import ``applier``. Pinned by ``tests/test_applier_isolation.py``.
"""
from __future__ import annotations

__version__ = "0.1.0"

__all__ = ["__version__"]
