# Trust explanation implementation report

The existing **Why Trust It?** header action now opens the short topic. Its
**I'm not convinced — show me exactly what runs →** button opens a second modal
on top. Closing the dossier restores the short topic and its button's focus.
The reader inferred from the application is the design professional who must
decide whether to act on a specification finding.

The discovery ledger was written before the replacement copy. It covers the
desktop reviewer, exported HTML report/chat, offline trace viewer, and shipped
trace/recovery/separate-applier commands. Developer evaluation and build scripts
are outside that runtime scope.

## Delivered

- [Claims ledger](TRUST_CLAIMS.md): claim IDs, implementation files/symbols,
  action/automatic/network inventories, exact fact bindings and contradictions.
- [Shared short topic and dossier](../src/gui/trust_content.py): the lead,
  seven mechanism-backed points, all dossier sections, inline SVG and runtime cards.
- [Native stacked dialogs](../src/gui/trust_dialogs.py), connected through the
  [existing help entry](../src/gui/about_usage_dialogs.py) and GUI header.
- [Offline dossier](TRUST.md), generated from the same content. The dialog
  resolves local paths; the saved document names platform trace defaults.
- [Fact/contract tests](../tests/test_trust_content.py) and
  [native interaction tests](../tests/test_trust_dialogs.py).
- Maintenance rules in README and CLAUDE.md; corrections to current help,
  README, developer notes and Word/HTML source-quote labels. The old trust audit
  chapter now points explicitly to the current contract.

No runtime dependency was added. No review, networking, credential, billing,
retention, cancellation, export-path or model/tool behavior was changed.

## Inventory summary

| Inventory | Count and scope |
| --- | --- |
| User actions | 40 families; shared-mechanism controls and aliases are named together |
| Automatic behaviors | 13 families, including startup, counting, dependent stages, retries and recording |
| Runtime cards | 53, matching every action/automatic ledger ID; each has the same five rows |
| Network | 8 route categories; dynamic source/result/installer/redirect/proxy destinations are explicitly included, not represented as a finite host list |
| Models | 3 distinct default identities, 7 registered identities; explicit overrides can introduce others |
| Short claims / dossier sections | 7 / 12 |

The counts are checked by the inventory/model tests. More cards than the usual
desktop-only example are needed because this app also ships report chat and
companion commands.

## Claims narrowed or dropped

- **Every output has complete declared ancestry:** the report has origin/stage,
  confidence and evidence labels, not sentence-by-sentence ancestry.
- **Nothing runs until you start a review / traffic goes only to Anthropic:**
  startup checks, tokenizer download, pre-review counting, PDF count preflight,
  retries/follow-ups, browser links and configurable/dynamic endpoints are named.
- **Every automated judgment can be re-derived:** mechanical gates can be
  inspected and reproduced; another model call need not repeat its judgment.
- **Two independent AIs check every issue:** model routing, local skips, cache
  replay, shared models/vendor and configured overrides prevent that promise.
- **Locally classified means no AI:** eligible findings can first use model triage.
- **Verified/grounded proves source support:** accepted URL provenance and a
  required quote field do not prove the quote appears, is authoritative, or
  supports the conclusion. Report quote labels now identify their supplier.
- **Every anchor is word-for-word checked:** matching allows collapsed whitespace;
  unavailable/unattributable source text can bypass that check.
- **Source files can never change:** review does not apply proposals, but export
  can overwrite an input if you select its path. The separate applier is a writer.
- **Coordination checks the whole package:** default checks are within each
  module/chunk; cross-boundary relationships can be missed.
- **All phases receive all context / research is one search call:** payloads and
  tool budgets differ by stage; research tasks run in parallel where applicable.
- **Stop undoes work or guarantees no charge:** remote batches can continue;
  consumed work can be billed; chat display changes are not rolled back.
- **Retention is continuous / traces are complete and harmless:** pruning runs
  at recorder start; traces are selective, can fail, and can contain project text.
- **Dollar totals, timing, strict schemas, effort ceilings or unknown-model
  compatibility are unconditional guarantees:** estimates, per-stage bounds and
  explicit configuration exceptions replace those assertions.
- **Secure means isolated or fully private:** specific mechanisms are described;
  client code does not prove provider policy, model obedience or workstation security.

## Findings: behavior deliberately retained

These correspond to the numbered findings and implementation sources in the ledger.

1. Loading/changing inputs can upload a counting request before review submission.
   Clearing cancels scheduled work, not an active HTTP request.
2. Windows automatic update checks and missing-tokenizer downloads are exceptions
   to click-only networking; started work has automatic follow-ups/retries.
3. Anchor validation is whitespace-tolerant and skips unavailable/unattributed text.
4. Local classification can follow AI triage; neither model independence nor a
   second remote check is guaranteed for every finding.
5. Evidence gates prove accepted URL provenance/quote presence, not semantic support,
   adoption or accuracy. Optional evidence observation is not the default guarantee.
6. The updater permits arbitrary HTTPS hosts/redirects, can follow a downgrade
   before final-scheme rejection, has no total installer byte ceiling or independent
   publisher-signature check, and continues downloading after the offer closes.
7. Trace retention runs at recorder start. Other artifacts lack general age deletion;
   ordinary logs lack universal secret redaction; trace queues warn without a hard cap.
8. Research attaches web_fetch even for an unsupported override, unlike the gated
   verification/chat paths. Such research can fail.
9. Closing/discarding a batch does not cancel it remotely. Live review has no resume,
    the GUI has no review Stop button, no whole-run spend cap exists, exports can
    partly succeed, and default coordination is not comprehensive across boundaries.
10. Current chat keys are memory-only; older key-storage/model/audit descriptions
    differed. Current changelog wording was clarified; older analysis is identified.
11. Earlier timing copy mixed provider turnaround and the local polling bound.
    Current copy distinguishes them; it does not promise turnaround.
12. Explicit review-effort overrides bypass the default Opus ceiling. Local-skip
    routing is always enabled; there is no disable switch to document.
13. Earlier help overstated coordination, research-call count and universal context
    propagation. Those descriptions and unsupported speed figures were narrowed.
14. “Verbatim from search result” overstated the quote gate. Word/HTML labels now
    say “supplied by verifier”; verification itself was not changed.
15. Earlier strict-schema/effort/unknown-model compatibility statements omitted
    exceptions. Current documentation names switches and possible provider rejection.
16. Report export can overwrite an input selected as its destination; same-stem
    sidecars overwrite without their own confirmation. A temporary DOCX reproduction
    and regression test confirm this. No path guard or rollback was added.

Remaining disagreements are listed with file/line locations in the ledger's
**Remaining documentation disagreements** table: handbook turnaround, output-file
count, trace locations, directly actionable/deterministic statuses, injection-proof
wording, the report-only chat heading, and historical model/cap examples. Current
help/README/report labels were corrected; those older engineering narratives were
not rewritten as if their evaluations had been rerun.

## Presentation adaptations and section names

This is a native Tk application. Native titles, transient window ownership, modal
grabs, keyboard focus containment/return and focus rings implement the dialog
behavior. Tk does not have DOM ARIA dialog attributes. The inline SVG retains
`role="img"` and its `aria-label` in the shared/offline source; the native renderer
draws its primitives locally and displays the same text alternative.

The dossier has one document scroll and a fixed contents rail at wide widths.
Every section has a text anchor and remains in the document. Tables become labeled
cells at narrow widths. Small embedded blocks avoid a document-sized native canvas.
The window is bounded by the requested viewport-height ceiling.

No dossier section was dropped. Titles were adapted as follows:

| Requested title | App title |
| --- | --- |
| Where the output comes from | Where your findings come from |
| What runs when you click | What runs for each action |
| What the AI may touch, and what it cannot | What the AI may touch; the constraints and categorical limit remain |
| What the key terms mean, exactly | What the labels mean, exactly |
| Security and privacy, mechanism by mechanism | Security and privacy in specific terms |
| Money | What you pay for, and what the meter knows |

The remaining titles retain the requested wording. The native adaptation replaces
web dialog attributes; it does not claim browser accessibility semantics for Tk.

## Validation

The final checks use the installed development requirements and a virtual native
display, with no live API key:

```bash
DISPLAY=:99 python -m pytest -q
python -m build --outdir /tmp/spec-critic-trust-build
git diff --check
```

Trust tests check the complete inventory, all five runtime rows, explicit None,
source symbols, constant-derived facts, reviewed snapshot drift, models/settings,
host/asset constraints, real export overwrite behavior, quote labels, stacked opens,
single Escape, focus containment/return/reactivation, contents navigation, narrow
tables and theme switching. Native screenshots were also inspected.

The final full suite passed: **7,471 passed, 21 skipped**. All **15** trust tests,
including native interaction checks, ran successfully. The source archive and
wheel built successfully, and `git diff --check` passed.

The existing skips are **18** live-provider checks without an Anthropic key,
**one** unavailable offline-tokenizer check, **one** PyInstaller check and **one**
Playwright viewer check. A Windows installer build, live-provider behavior and
Windows/macOS screen-reader behavior were not verified in this Linux environment.

## Open questions

No clarification blocks this implementation. The app/reader/help placement were
inferred from the shipped UI. Provider retention/training terms and your organization's
approval for processing sensitive material remain matters to verify against the
current provider agreement; this document does not invent those guarantees.
