# Pinned standards provenance — California 2025 cycle

Provenance record for the standard editions pinned in
`src/core/code_cycles.py::CALIFORNIA_2025.standards`. Each row records the
adopted edition, where it was confirmed (or why it is still unconfirmed), the
date checked, and a confidence level. The machine-readable source of truth is the
`StandardEdition.source` field on each entry; this file carries the longer-form
provenance that does not fit in a one-line `source`.

**Why this file exists.** A pinned edition string is a quiet correctness
dependency: the reviewer and verifier reason *against* these editions, so a wrong
edition produces a confidently-wrong finding that no model call can catch. The
`source` field already flags unverified entries with an `UNVERIFIED` prefix
(surfaced by `cycle.unverified_standards()`); this file is the human-readable
companion a maintainer uses when confirming them.

> **Access limitation (2026-06).** The authoritative referenced-standards tables
> — ICC Digital Codes (`codes.iccsafe.org`), UpCodes, and the California Energy
> Commission PDFs (`energy.ca.gov`) — all returned **HTTP 403** to automated
> fetching during this research pass. The findings below therefore rest on web
> *search summaries* of those sources plus secondary/industry sources, **not** a
> direct read of the published code. That is enough to *correct a likely error*
> and to *flag risk*, but **not** enough to flip an entry off `UNVERIFIED`. A
> maintainer with code access (a purchased CBC 2025 / Title 24 Part 6, or a
> logged-in ICC/UpCodes session) should make the final confirmation.

Date of this research pass: **2026-06-04**.

---

## Verified entries (for reference — not researched in this pass)

These were already confirmed against the California Fire Code 2025, Chapter 80
adoption table and are **not** `UNVERIFIED`:

| Standard | Edition | Source |
|---|---|---|
| NFPA 13 | 2025, as amended by California | CFC 2025, Ch. 80 |
| NFPA 14 | 2024 | CFC 2025, Ch. 80 |
| NFPA 20 | 2025, as amended by California | CFC 2025, Ch. 80 |
| NFPA 24 | 2025 | CFC 2025, Ch. 80 |
| NFPA 25 | 2013 California Edition | CFC 2025, Ch. 80 (NFPA 25-2011 as amended) |
| NFPA 72 | 2025, as amended by California | CFC 2025, Ch. 80 / OSFM IB 26-002 |
| ASHRAE 15 | 2022 | California Mechanical Code 2025 (A2L provisions) |

---

## Unverified entries — research findings

| Standard | Pinned edition | Confidence it's correct | Change this pass |
|---|---|---|---|
| ASHRAE 62.1 | 2019 | Medium–high | note sharpened (kept 2019) |
| ASHRAE 90.1 | 2019 | Medium–high | **corrected 2022 → 2019** |
| IAPMO Uniform Plumbing TSC | 2024 | Medium (UPC-2024 base solid) | note sharpened |
| UL 300 | 2005 (revised) | Medium | note sharpened |
| UL 555 | 2006 (revised) | Medium | note sharpened |
| UL 555S | 2014 (revised) | Medium | note sharpened |
| UL 268 | 2016 (revised) | Medium | note sharpened |
| UL 1479 | 2015 (revised) | **Low — likely stale** | flagged highest-risk |

### ASHRAE 62.1 — 2019  *(confidence: medium–high)*

The 2025 California Energy Code (Title 24, Part 6) references the **2019** edition
of ASHRAE 62.1 (and 62.2). The 2022 editions exist and are newer, and ASHRAE
formally commented on the 2025 update asking California to move the reference to
the 2022 editions — but the adopted code stayed at 2019. The pinned value (2019)
is therefore consistent with the evidence.

- Source (search summary): "the draft Building Energy Efficiency Standards
  reference the 2019 editions of Standards 62.1 and 62.2 … the latest editions of
  both were issued in 2022."
- To confirm: Title 24 Part 6 referenced-standards table / 2025 Reference
  Appendices (CEC-400-2025-010-AP) and the Nonresidential Compliance Manual
  (CEC-400-2025-008).

### ASHRAE 90.1 — 2019  *(confidence: medium–high; CORRECTED from 2022)*

**Corrected this pass: 2022 → 2019.** Multiple independent web sources
consistently state the 2025 California Energy Code references **90.1-2019**, and
ASHRAE's own comment letter petitioned California to update the reference *to*
90.1-2022 — which only makes sense if the adopted edition is 2019. The previous
maintainer note already suspected this ("multiple sources indicate … 90.1-2019,
not 90.1-2022"); this pass corroborates it across the CEC Nonresidential
Compliance Manual summary, the ASHRAE comment-letter context, and energy-code
change summaries.

- Source (search summary): "The 2025 California Energy Code references ASHRAE
  90.1-2019, though ASHRAE suggests that the proposed 2025 code update be amended
  to refer to the 2022 edition of 90.1, rather than the 2019 edition."
- Kept `UNVERIFIED` only because the primary table could not be read directly
  (403). The *value* is now the better-grounded one.
- To confirm: same CEC sources as 62.1.

### IAPMO Uniform Plumbing TSC — 2024  *(confidence: medium)*

The 2025 California Plumbing Code (Title 24, Part 5) is based on the **2024**
Uniform Plumbing Code, confirmed by the publisher (IAPMO) and the ICC store
listings; effective Jan 1, 2026. The UPC-2024 base is well corroborated. What is
*not* independently confirmed is the specific IAPMO installation
trade-standards-companion ("TSC") edition tied to that cycle — hence still
`UNVERIFIED`, though the 2024 cycle anchor is solid.

- Sources: iapmo.org (CPC 2025 page), ICC store (Title 24 Part 5), IAPMO epubs.

### UL listings — UL 300 / 555 / 555S / 268 / 1479

CBC 2025 is based on the **2024** IBC; the pinned UL editions match the **2021**
IBC Chapter 35 table (the CBC 2022 lineage). UL standards revise infrequently, and
web-snippet evidence indicates the 2024 IBC *still* cites the same editions for
several of these — but the authoritative 2024-IBC / CBC-2025 Chapter 35 table
could not be read directly (paywalled), so all stay `UNVERIFIED`.

| Standard | Pinned | Notes from this pass |
|---|---|---|
| UL 300 | 2005 (R2010) | 5th ed.; long-standing; matches 2021 IBC. |
| UL 555 | 2006 (rev. through Oct 2016) | 2024-IBC search snippets still cite UL 555-2006 with revisions through Oct 2016. |
| UL 555S | 2014 | Matches 2021 IBC; no newer code-referenced edition surfaced. |
| UL 268 | 2016 (rev. through Jul 2016) | 2024-IBC search snippets still cite UL 268-2016. |
| UL 1479 | 2015 | **Highest risk.** Newer UL 1479 **2022** and **2023** editions exist; a 2024-IBC-based code plausibly references a newer edition than 2015. Verify this one first. |

- To confirm: 2024 IBC Chapter 35 (`codes.iccsafe.org/content/IBC2024P1`) and the
  CBC 2025 Chapter 35 referenced-standards table.

---

## How to confirm and clear an `UNVERIFIED` flag

1. Open the published referenced-standards table for the relevant code (CFC/CBC
   Chapter 35, CMC, CPC, or Title 24 Part 6 Reference Appendices).
2. Read the exact edition (and revision date) for the standard.
3. If the pinned `edition` is wrong, correct it in `code_cycles.py`. Because the
   verification cache key now folds in a standards fingerprint
   (`verification_cache._standards_fingerprint`), correcting an edition
   automatically invalidates verdicts grounded against the old edition — no
   manual cache clear needed.
4. Replace the `UNVERIFIED …` `source` string with a confirmed provenance string
   (e.g. `"CBC 2025, Ch. 35, Table 35-…"`) so `is_verified` flips true.
5. Update the corresponding row here and re-run
   `tests/test_pinned_standards_editions.py`.

## Sources consulted (2026-06-04)

- California Energy Commission — 2025 Building Energy Efficiency Standards,
  Nonresidential Compliance Manual (CEC-400-2025-008), 2025 Reference Appendices
  (CEC-400-2025-010-AP). *(energy.ca.gov — 403 to automated fetch; via search.)*
- ASHRAE comment letter on the 2025 California Energy Code update
  (efiling.energy.ca.gov). *(403 to automated fetch; via search.)*
- IAPMO — 2025 California Plumbing Code page; ICC store — Title 24 Part 5.
- ICC Digital Codes — 2024 IBC Chapter 35; UpCodes — CBC 2025 Chapter 35.
  *(Both 403 to automated fetch; values via search summaries only.)*
- UL Solutions damper / firestop application guides (edition cross-checks).
