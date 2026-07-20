# Pinned standards provenance

Provenance record for the standard editions each module pins in its
`CodeCycle`. Each row records the adopted (or referenced) edition, where it was
confirmed (or why it is still unconfirmed), the date checked, and a confidence
level. The machine-readable source of truth is the `StandardEdition.source`
field on each entry; this file carries the longer-form provenance that does not
fit in a one-line `source`.

Sections:

- **California 2025 cycle** (`CALIFORNIA_2025`, `california_k12_mep` module) —
  below.
- **Data-center IBC/IFC 2024 cycle** (`DATACENTER_IBC_2024`, `datacenter_fire`
  module) — later in this file.
- **Data-center architecture IBC 2024 fallback**
  (`DATACENTER_ARCHITECTURE_IBC_2024`, `datacenter_architecture` module) — after
  the fire-protection cycle.
- **Data-center electrical IBC 2024 fallback**
  (`DATACENTER_ELECTRICAL_IBC_2024`, `datacenter_electrical` module) — after
  the architecture cycle.

---

# California 2025 cycle

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

---

# Data-center IBC/IFC 2024 cycle

Provenance record for the standard editions pinned in
`src/modules/datacenter_fire.py::DATACENTER_IBC_2024.standards` — the code basis
for the `datacenter_fire` module (WS-0 / WS-1 of
`docs/hyperscale_datacenter_module_plan.md`).

**Jurisdiction decision** (`datacenter_fire_module_plan.md` §3.1): hyperscale
data centers are built across many states and provinces, each adopting the
I-codes on its own schedule with its own amendments. Rather than pin one
jurisdiction, the module pins the **model codes** — IBC and IFC, current
editions — as the code basis. State / provincial / local / AHJ facts are
per-project data supplied via Project Context (v1 posture) and, once the
location-aware engine work lands, by the research phase. A state-pinned variant
(e.g. a Virginia USBC cycle) would be a *separate* module with its own
registry-unique cycle label — never a multi-jurisdictional cycle.

> **Access limitation (2026-07).** The authoritative referenced-standards tables
> — ICC Digital Codes (`codes.iccsafe.org`) and UpCodes — returned **HTTP 403**
> to automated fetching during this research pass (the same limitation the
> California section documents). NFSA, MeyerFire, and the NPS crosswalk PDF also
> 403'd. The editions below therefore rest on web *search summaries* of those
> sources plus secondary/industry sources, **not** a direct read of the
> published code. That is enough to pin best-grounded values and flag risk, but
> **not** enough to clear an entry off `UNVERIFIED`. A maintainer with code
> access (a purchased 2024 IBC/IFC, or a logged-in ICC/UpCodes session) should
> make the final confirmation against the Chapter 35 / Chapter 80 tables.

Date of this research pass: **2026-07-14**.

## Base codes (matter of public record)

| Code | Year | Confidence | Source |
|---|---|---|---|
| IBC | 2024 | High | ICC published the 2024 I-codes; 2024 is the current edition (2027 is the next cycle). |
| IFC | 2024 | High | As above. |
| ASCE 7 | 7-22 | High | The 2024 IBC references ASCE 7-22, replacing ASCE 7-16 (ASCE, StructureMag, SEAO all corroborate). Previous edition 7-16. |

The base-code years are not `StandardEdition` entries and carry their provenance
in `BaseCode.source`; they are well-grounded and treated as reliable. The
`StandardEdition` entries below are the referenced-standard editions and are all
`UNVERIFIED` pending a primary-table read.

## Pinned standard editions — research findings

**Every entry is `UNVERIFIED`** (primary tables paywalled). Values are the
best-grounded estimates from secondary sources plus knowledge of the NFPA
revision cycles and the 2024 I-code reference freeze (~2022 for the install
family).

| Standard | Pinned | Confidence | Notes |
|---|---|---|---|
| NFPA 13 (sprinklers) | 2022 | Medium–high | The 2024 Life Safety Code and multiple secondary sources cite NFPA 13-2022; well corroborated. |
| NFPA 14 (standpipe) | 2019 | **Low** | NFPA 14-2024 exists but postdates the 2024 IBC reference freeze, so 2019 is the likely referenced edition — but this may have moved. Verify first. |
| NFPA 20 (fire pumps) | 2022 | Medium | Good secondary corroboration for NFPA 20-2022. |
| NFPA 22 (water tanks) | 2018 | **Low** | NFPA 22-2023 exists and postdates the freeze; 2018 is the likely referenced edition. Verify. |
| NFPA 24 (private mains) | 2022 | Medium | Good secondary corroboration for NFPA 24-2022. |
| NFPA 25 (ITM) | 2020 | **Low–medium** | IFC-referenced; NFPA 25-2023 exists and postdates the freeze. The fire/operations code's ITM edition often differs from the building code's install editions. Verify against the 2024 IFC table. |
| NFPA 72 (alarm/detection) | 2022 | Medium–high | The 2024 Life Safety Code cites NFPA 72-2022; well corroborated. |
| NFPA 2001 (clean agent) | 2022 | Low–medium | 2022 edition is the one in the 2024 I-code reference window. Verify. |
| NFPA 855 (energy storage / BESS) | 2023 | Medium | Referenced by the 2024 IFC (§1207); current edition 2023 (a 2026 edition is in development). Confirm the code-referenced edition. |
| NFPA 75 (IT equipment) | 2024 | Medium | Current edition 2024 (well corroborated via NFPA/ANSI store listings); owner-invoked benchmark rather than strictly code-mandated. Pinned at the current edition per the module plan. |
| NFPA 76 (telecom) | 2024 | Medium | Current edition 2024 (issued Dec 2023; well corroborated); owner-invoked benchmark. Pinned at the current edition. |

**Notes on the split.** The install family (13/14/20/22/24/72) and the
special-hazard/BESS standards (2001/855) are the code-referenced editions the
2024 IBC/IFC point to. NFPA 75/76 are pinned at their *current* editions because
owner data-center standards routinely invoke them even where the code does not;
`StandardEdition.source` records that distinction for maintainers. FM Global
data sheets are deliberately **not** `StandardEdition` entries — they are
revision-dated guidance documents, not adopted-code editions
(`datacenter_fire_module_plan.md` §3.3); FM is represented as a jurisdictional
keyword, a top verifier source tier, and a review category. UL listings are not
pinned as `StandardEdition` entries either — the WS-1 review categories do not
cite UL standards by number.

## How to confirm and clear an `UNVERIFIED` flag

Same procedure as the California section: open the published referenced-standards
table (2024 IBC Chapter 35 / 2024 IFC Chapter 80), read the exact edition and
revision date, correct `edition` in `datacenter_fire.py` if wrong, replace the
`UNVERIFIED …` `source` string with a confirmed provenance string so
`is_verified` flips true, and update the row here. Because the verification
cache key folds in the cycle's standards fingerprint, correcting an edition
automatically re-grounds affected verdicts — no manual cache clear needed.

## Sources consulted (2026-07-14)

- ICC Digital Codes — 2024 IBC Chapter 35 referenced standards; UpCodes —
  2024 IBC Chapter 35 (Phoenix / Wichita-Sedgwick adoptions). *(Both 403 to
  automated fetch; values via search summaries only.)*
- NFSA — "Changes to the 2024 IBC and IFC." *(403 to automated fetch; via
  search.)*
- ASCE / StructureMag / SEAO — 2024 IBC references ASCE 7-22 (structural-change
  summaries).
- Electrical Contractor Magazine — 2024 Life Safety Code references NFPA 13-2022
  and NFPA 72-2022.
- MeyerFire — "Notable 2024 IBC Changes for Fire Protection." *(403 to
  automated fetch; via search.)*
- NPS NFPA/IFC crosswalk (nps.gov PDF). *(403 to automated fetch; via search.)*
- NFPA / ANSI / ICC store listings — current editions of NFPA 75 (2024), NFPA 76
  (2024), NFPA 855 (2023).

---

# Data-center architecture IBC 2024 fallback

Provenance record for the model-code fallback in
`src/modules/datacenter_architecture.py::DATACENTER_ARCHITECTURE_IBC_2024`. It
supports architectural specification reviews for hyperscale data-center
projects in the United States and Canada. It is deliberately a fallback, not a
claim that every project has adopted the 2024 I-codes.

The Project Requirements Profile remains controlling: each run researches the
actual state, provincial, and local adoption; amendments; accessibility law;
AHJs; and client requirements for the entered city, state/province, country,
and client. Canadian projects use their researched NBC/NFC/NECB or provincial
lineage and do not treat the US model-code fallback as governing.

Date checked against primary publisher sources: **2026-07-20**.

## Model-code fallback

| Code or standard | Fallback edition | Status and scope |
|---|---|---|
| IBC | 2024, ICC Digital Codes Version 2.0 | Verified published model code; actual adoption is researched per project. |
| IFC | 2024, Version 1.0 | Verified published model code; actual adoption is researched per project. |
| IECC | 2024 | Verified published model code; jurisdictional compliance path is researched per project. |
| IEBC | 2024, Version 1.0 | Verified published model code; relevant only when the project scope invokes existing-building provisions. |
| ASCE 7 | 7-22 with Supplement No. 1 | Exact base 2024 IBC Version 2.0 reference; the detector retains `7-22` as its machine comparison token. Project-location research can identify additional supplements incorporated by an adopting jurisdiction. |
| ICC A117.1 | 2017 with Supplement 1 | Exact accessibility-standard form published with the 2024 IBC provisions; federal, provincial, state, and local accessibility requirements still govern applicability. |
| ASHRAE 90.1 | 2022 | Verified 2024 IECC/ASHRAE alternate compliance publication; adoption and selected compliance path are researched per project. |
| NFPA 80 | 2022 | Verified 2024 IBC Chapter 35 reference. |
| NFPA 101 | 2024 | Referenced by the 2024 IBC/IFC only for the narrow Section 1030.6.2 condition; otherwise owner/AHJ-invoked where applicable. |
| NFPA 285 | 2023 | Verified 2024 IBC Chapter 35 reference and NFPA publication edition. |
| ASTM E119 | 20 | Verified 2024 IBC reference; corrected from the initial E119-2022 candidate. |
| ASTM E84 | 21a | Verified 2024 IBC reference; corrected from the initial E84-2023 candidate. |

Every architecture `StandardEdition.source` records a primary publisher page,
so `cycle.unverified_standards()` is empty. That means the edition was verified
for the model-code fallback only. It does not bypass location research or make
a voluntary standard legally applicable to a particular project.

## Primary sources

- ICC Digital Codes: [2024 IBC Version 2.0](https://codes.iccsafe.org/content/IBC2024V2.0),
  [2024 IFC Version 1.0](https://codes.iccsafe.org/content/IFC2024V1.0),
  [2024 IECC](https://codes.iccsafe.org/content/IECC2024P1), and
  [2024 IEBC Version 1.0](https://codes.iccsafe.org/content/IEBC2024V1.0/preface).
- ICC [2024 IBC Version 2.0 content updates](https://codes.iccsafe.org/content/IBC2024V2.0/content-updates-version-2-0)
  for ASCE 7-22 with Supplement No. 1, and
  [Chapter 35](https://codes.iccsafe.org/content/IBC2024V2.0/chapter-35-referenced-standards)
  for ICC A117.1, NFPA 80, NFPA 285, and ASTM references.
- ICC [2024 IFC Chapter 80](https://codes.iccsafe.org/content/IFC2024P1/chapter-80-referenced-standards)
  for the narrow NFPA 101-2024 reference at Section 1030.6.2.
- ICC [A117.1-2017 with Supplement 1](https://codes.iccsafe.org/content/IBCACCPB2024P1/icc-a117-1-2017-with-supplement-1-american-national-standard-standard-for-accessible-and-usable-buildings-and-facilities).
- ICC/ASHRAE [2024 IECC with ASHRAE 90.1-2022](https://codes.iccsafe.org/content/IECCASHRAE2024P1).
- ASCE [ASCE/SEI 7-22](https://www.asce.org/publications-and-news/codes-and-standards/asce-sei-7-22).
- NFPA [NFPA 80-2022](https://link.nfpa.org/all-publications/80/2022),
  [NFPA 101-2024](https://link.nfpa.org/all-publications/101/2024), and
  [NFPA 285-2023](https://link.nfpa.org/all-publications/285/2023).
- ASTM [E119-20](https://store.astm.org/e0119-20.html) and
  [E84-21a](https://store.astm.org/e0084-21a.html).

---

# Data-center electrical IBC 2024 fallback

Provenance record for the model-code fallback in
`src/modules/datacenter_electrical.py::DATACENTER_ELECTRICAL_IBC_2024`. It
supports electrical specification reviews for hyperscale data-center projects
in the United States and Canada. It is a review baseline, not a claim that the
2024 I-codes or one national electrical-code edition governs every project.

The Project Requirements Profile remains controlling. Every run researches the
actual state, provincial/territorial, and local adoption and amendments; utility
service/interconnection rules; AHJ requirements; and client reliability and
commissioning criteria for the entered city, state/province, country, and
client. Canadian projects dynamically resolve the applicable provincial or
territorial electrical and building-code basis. CSA C22.1 and related Canadian
standards are therefore not hard-coded as universal fallback editions.

Date checked against primary publisher sources: **2026-07-20**.

## Model-code and electrical fallback

| Code or standard | Fallback edition | Status and scope |
|---|---|---|
| IBC / IFC / IECC / IEBC | 2024 | Published US model-code fallback; actual adoption and amendments are researched per project. |
| ASCE 7 | 7-22 with Supplement No. 1 | Base 2024 IBC Version 2.0 reference; jurisdiction-specific incorporation remains project data. |
| NFPA 70 (NEC) | 2023 | 2024 IBC/IFC referenced edition for the US fallback; actual state/local adoption governs. |
| NFPA 110 | 2022 | Applies where the emergency power supply system or controlling owner criterion invokes it. |
| NFPA 111 | 2022 | Applies where a stored-energy emergency/standby system invokes it. |
| ASHRAE 90.1 | 2022 | 2024 IECC reference; adoption and chosen energy-code path remain project-specific. |
| ASHRAE 90.4 | 2022, with published errata through Dec. 11, 2023 | Data-center energy-performance reference; applicability follows the adopted energy-code path. |
| IEEE 1584 | 2018, with Aug. 30, 2019 errata | Arc-flash calculation reference where the study scope invokes it. |
| UL 2200 | 2020 | Stationary generator-set listing reference where applicable. |

Every electrical `StandardEdition.source` records primary publisher evidence,
so `cycle.unverified_standards()` is empty. Conditional entries remain
conditional: pinning an edition gives the reviewer a precise comparison basis
when that standard applies, but does not independently make it legally or
contractually applicable.

NFPA 70E, NFPA 70B, NFPA 780, broad product families, and Canadian standards
are intentionally not universal pins. They may be controlling workplace,
maintenance, lightning-protection, product, owner, or Canadian requirements,
but the project profile and submitted documents must establish their
applicability and edition.

## Primary sources

- ICC [2024 IBC Section 2702.1.3](https://codes.iccsafe.org/s/IBC2024V2.0/chapter-27-electrical/IBC2024V2.0-Ch27-Sec2702.1.3)
  for code-required emergency/standby systems and NFPA 70, 110, and 111.
- ICC [2024 IBC Chapter 35](https://codes.iccsafe.org/content/IBC2024V2.0/chapter-35-referenced-standards)
  and [2024 IFC Chapter 80](https://codes.iccsafe.org/content/IFC2024P1/chapter-80-referenced-standards)
  for referenced electrical standards and editions.
- ICC [2024 IECC Chapter 6](https://codes.iccsafe.org/content/IECC2024P1/chapter-6-ce-referenced-standards)
  for ASHRAE 90.1-2022 and 90.4-2022.
- ASHRAE [Standard 90.4-2022 errata](https://www.ashrae.org/file%20library/technical%20resources/standards%20and%20guidelines/standards%20errata/standards/90.4-2022errata-12-11-2023-.pdf).
- IEEE [1584-2018 publication record](https://standards.ieee.org/ieee/1584/5802/)
  and [official errata](https://standards.ieee.org/wp-content/uploads/import/documents/erratas/1584-2018_errata.pdf).
- UL [UL 2200 Edition 3](https://www.shopulstandards.com/ProductDetail.aspx?productId=UL2200).
- National Research Council Canada, [Canada's construction system and model-code context](https://nrc.canada.ca/en/certifications-evaluations-standards/codes-canada/canadas-construction-system-context-model-codes),
  for the provincial/territorial adoption model that makes Canadian code basis
  project-specific.
