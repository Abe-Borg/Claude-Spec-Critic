"""Constructed coordination cases for the EX-06 candidate stage (plan EX-06).

Each case is a small set of specifications — each document individually
plausible — with a declared cross-check plan per module and labels:

- **conflicts**: two elements, in two specifications, that state one
  requirement for one item in one scope two incompatible ways. The candidate
  stage should select the pair. A miss is a *missed conflict*.
- **controls**: two elements that look alike but are not a conflict —
  different equipment, a different phase or building, an existing item and a
  new one, compatible values, attributes that are not the same attribute, a
  pair cross-check already compared, or a pair outside the pass's scope. The
  candidate stage must not select the pair. A selection is a *false join*.

The passages are constructed: written for this set in the register of a
construction specification, quoting no published document. Tags, divisions,
and values are illustrative. Two splits:

- ``tuning`` — written with the rules and used while writing them;
- ``held_out`` — written after the rules were frozen (``facts.POLICY_VERSION``
  ``cx1``) and scored once. ``evals.coordination.RECORDED_HELD_OUT_RESULT``
  pins that score; a rule change that moves it is a new policy version that
  needs new held-out cases.

What this set measures is the *deterministic* stage — which pairs the rules
would send — not whether the model then judges them correctly: that needs
the live protocol in ``evals.coordination``.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Iterable

SPLIT_TUNING = "tuning"
SPLIT_HELD_OUT = "held_out"
SPLITS = (SPLIT_TUNING, SPLIT_HELD_OUT)

FIRE = "datacenter_fire"
ELEC = "datacenter_electrical"
ESS = "datacenter_electronic_safety_security"
ARCH = "datacenter_architecture"
CA = "california_k12_mep"

CATEGORY_RESPONSIBILITY = "responsibility"
CATEGORY_ELECTRICAL = "electrical"
CATEGORY_RATING = "rating"
CATEGORIES = (CATEGORY_RESPONSIBILITY, CATEGORY_ELECTRICAL, CATEGORY_RATING)

SCOPE_MODULE = "module"
SCOPE_PROGRAM = "program"


@dataclass(frozen=True)
class Doc:
    """One specification: its file name, modules, and elements.

    ``elements`` are ``(element_id, heading, text)``; an id starting with
    ``t`` is a table row.
    """

    file_name: str
    modules: tuple[str, ...]
    elements: tuple[tuple[str, str, str], ...]


@dataclass(frozen=True)
class Pair:
    """A labeled pair of elements: ``(file, element)`` on each side."""

    pair_id: str
    a: tuple[str, str]
    b: tuple[str, str]
    category: str
    note: str


@dataclass(frozen=True)
class Case:
    case_id: str
    split: str
    description: str
    docs: tuple[Doc, ...]
    #: Per module, the file groups its cross-check planned in one request.
    plans: tuple[tuple[str, tuple[tuple[str, ...], ...]], ...]
    scope: str = SCOPE_MODULE
    conflicts: tuple[Pair, ...] = ()
    controls: tuple[Pair, ...] = ()
    tags: tuple[str, ...] = field(default_factory=tuple)


def _doc(name: str, modules, *elements) -> Doc:
    if isinstance(modules, str):
        modules = (modules,)
    return Doc(name, tuple(modules), tuple(elements))


def _apart(module: str, *files: str) -> tuple[str, tuple[tuple[str, ...], ...]]:
    """A module whose cross-check put every listed file in its own request."""
    return (module, tuple((f,) for f in files))


def _together(module: str, *files: str) -> tuple[str, tuple[tuple[str, ...], ...]]:
    """A module whose cross-check sent every listed file in one request."""
    return (module, (tuple(files),))


# Frequently used file names.
F_PUMPS = "21 30 00 Fire Pumps.docx"
F_WATER = "21 11 00 Facility Fire-Suppression Water-Service Piping.docx"
F_WET = "21 13 13 Wet-Pipe Sprinkler Systems.docx"
F_DRY = "21 13 16 Dry-Pipe Sprinkler Systems.docx"
F_AGENT = "21 22 00 Clean-Agent Fire-Extinguishing Systems.docx"
F_CONTROLLERS = "26 29 13 Enclosed Controllers.docx"
F_TRANSFER = "26 36 00 Transfer Switches.docx"
F_GEN = "26 32 13 Engine Generators.docx"
F_POWER = "26 05 00 Common Work Results for Electrical.docx"
F_ALARM = "28 46 21 Fire Detection and Alarm.docx"
F_DUCTACC = "23 33 00 Air Duct Accessories.docx"
F_FANS = "23 34 16 Centrifugal HVAC Fans.docx"
F_BAS = "25 00 00 Integrated Automation.docx"


TUNING_CASES: tuple[Case, ...] = (
    Case(
        "cx-t01", SPLIT_TUNING,
        "Fire pump motor voltage in the fire spec against its feeder in the electrical spec.",
        (
            _doc(F_PUMPS, FIRE, ("p3", "2.01 FIRE PUMP", "Fire pump FP-1 motor: 480 V, 3-phase, 60 Hz.")),
            _doc(F_CONTROLLERS, ELEC, ("p5", "2.03 FIRE PUMP CONTROLLER FEEDERS",
                                       "Fire pump controller feeder for FP-1: 208 V, 3-phase, 4-wire.")),
        ),
        (_apart(FIRE, F_PUMPS), _apart(ELEC, F_CONTROLLERS)),
        SCOPE_PROGRAM,
        conflicts=(Pair("t01a", (F_PUMPS, "p3"), (F_CONTROLLERS, "p5"), CATEGORY_ELECTRICAL,
                        "480 V motor against a 208 V feeder for the same tagged pump"),),
        tags=("tag", "cross_module"),
    ),
    Case(
        "cx-t02", SPLIT_TUNING,
        "Motor nameplate voltage against the nominal system voltage it serves.",
        (
            _doc(F_PUMPS, FIRE, ("p3", "2.01 FIRE PUMP", "Fire pump FP-2 motor: 460 V, 3-phase, 60 Hz.")),
            _doc(F_CONTROLLERS, ELEC, ("p5", "2.03 FEEDERS", "Feeder to FP-2: 480 V, 3-phase.")),
        ),
        (_apart(FIRE, F_PUMPS), _apart(ELEC, F_CONTROLLERS)),
        SCOPE_PROGRAM,
        controls=(Pair("t02c", (F_PUMPS, "p3"), (F_CONTROLLERS, "p5"), CATEGORY_ELECTRICAL,
                       "460 V nameplate on a 480 V nominal system is compatible"),),
        tags=("compatible_values",),
    ),
    Case(
        "cx-t03", SPLIT_TUNING,
        "Duct smoke detector furnish/install responsibility split across two divisions of one module.",
        (
            _doc(F_DUCTACC, CA, ("p2", "2.05 DUCT SMOKE DETECTORS",
                                 "Duct smoke detectors shall be furnished by Division 28 and installed under this Section.")),
            _doc(F_BAS, CA, ("p4", "1.04 COORDINATION",
                             "Duct smoke detectors shall be furnished and installed by Division 25.")),
        ),
        (_apart(CA, F_DUCTACC, F_BAS),),
        SCOPE_MODULE,
        conflicts=(Pair("t03a", (F_DUCTACC, "p2"), (F_BAS, "p4"), CATEGORY_RESPONSIBILITY,
                        "furnish by 28 vs 25; install by 23 vs 25"),),
        tags=("responsibility", "cross_chunk", "this_section"),
    ),
    Case(
        "cx-t04", SPLIT_TUNING,
        "A division and a named contractor are not comparable without a mapping.",
        (
            _doc(F_FANS, CA, ("p6", "3.02 CONNECTIONS", "Power wiring to EF-1 shall be provided by Division 26.")),
            _doc(F_BAS, CA, ("p9", "3.01 INSTALLATION", "Power wiring to EF-1 by the electrical contractor.")),
        ),
        (_apart(CA, F_FANS, F_BAS),),
        SCOPE_MODULE,
        controls=(Pair("t04c", (F_FANS, "p6"), (F_BAS, "p9"), CATEGORY_RESPONSIBILITY,
                       "Division 26 and 'the electrical contractor' may be the same party"),),
        tags=("not_comparable",),
    ),
    Case(
        "cx-t05", SPLIT_TUNING,
        "The same air compressor voltage conflict, but cross-check sent both specs together.",
        (
            _doc(F_WET, FIRE, ("p2", "2.08 AIR SUPPLY", "AC-1 air compressor: 120 V, 1-phase.")),
            _doc(F_DRY, FIRE, ("p4", "2.04 AIR COMPRESSOR", "Air compressor AC-1: 208 V, 1-phase.")),
        ),
        (_together(FIRE, F_WET, F_DRY),),
        SCOPE_MODULE,
        controls=(Pair("t05c", (F_WET, "p2"), (F_DRY, "p4"), CATEGORY_ELECTRICAL,
                       "co-analyzed: cross-check already compared these two specs"),),
        tags=("co_analyzed",),
    ),
    Case(
        "cx-t06", SPLIT_TUNING,
        "The same conflict when a split Division 21 put the two specs in different parts.",
        (
            _doc(F_WET, FIRE, ("p2", "2.08 AIR SUPPLY", "AC-1 air compressor: 120 V, 1-phase.")),
            _doc(F_DRY, FIRE, ("p4", "2.04 AIR COMPRESSOR", "Air compressor AC-1: 208 V, 1-phase.")),
        ),
        (_apart(FIRE, F_WET, F_DRY),),
        SCOPE_MODULE,
        conflicts=(Pair("t06a", (F_WET, "p2"), (F_DRY, "p4"), CATEGORY_ELECTRICAL,
                        "120 V against 208 V for one tagged compressor"),),
        tags=("tag", "cross_chunk"),
    ),
    Case(
        "cx-t07", SPLIT_TUNING,
        "Two fire pumps in two construction phases.",
        (
            _doc(F_PUMPS, FIRE, ("p3", "2.01 FIRE PUMP", "Phase 1 fire pump: rated 1,000 gpm at 125 psi.")),
            _doc(F_WATER, FIRE, ("p6", "2.02 SERVICE SIZE",
                                 "Size the fire service for the Phase 2 fire pump: 1,500 gpm.")),
        ),
        (_apart(FIRE, F_PUMPS, F_WATER),),
        SCOPE_MODULE,
        controls=(Pair("t07c", (F_PUMPS, "p3"), (F_WATER, "p6"), CATEGORY_RATING,
                       "Phase 1 and Phase 2 pumps are different pumps"),),
        tags=("scope_phase",),
    ),
    Case(
        "cx-t08", SPLIT_TUNING,
        "A rated capacity under an article heading against a named pump in another spec.",
        (
            _doc(F_PUMPS, FIRE,
                 ("p4", "2.01 FIRE PUMP", "2.01 FIRE PUMP"),
                 ("p5", "2.01 FIRE PUMP", "A. Rated capacity: 1,000 gpm at 125 psi.")),
            _doc(F_WATER, FIRE, ("p7", "2.03 FIRE PUMP SUCTION", "Fire pump rated capacity shall be 1,500 gpm.")),
        ),
        (_apart(FIRE, F_PUMPS, F_WATER),),
        SCOPE_MODULE,
        conflicts=(Pair("t08a", (F_PUMPS, "p5"), (F_WATER, "p7"), CATEGORY_RATING,
                        "1,000 gpm against 1,500 gpm for the project's one fire pump"),),
        tags=("heading_subject", "term"),
    ),
    Case(
        "cx-t09", SPLIT_TUNING,
        "A rated pressure and a churn pressure are different attributes.",
        (
            _doc(F_PUMPS, FIRE, ("p5", "2.01 FIRE PUMP", "FP-3 rated pressure: 125 psi.")),
            _doc(F_WATER, FIRE, ("p8", "3.04 TESTING", "Record FP-3 churn pressure of 145 psi.")),
        ),
        (_apart(FIRE, F_PUMPS, F_WATER),),
        SCOPE_MODULE,
        controls=(Pair("t09c", (F_PUMPS, "p5"), (F_WATER, "p8"), CATEGORY_RATING,
                       "churn pressure is not the rated pressure"),),
        tags=("role",),
    ),
    Case(
        "cx-t10", SPLIT_TUNING,
        "Different tagged pumps.",
        (
            _doc(F_PUMPS, FIRE, ("p5", "2.01 FIRE PUMPS", "FP-1 rated capacity: 1,000 gpm.")),
            _doc(F_WATER, FIRE, ("p6", "2.02 SERVICE SIZE", "FP-2 rated capacity: 1,500 gpm.")),
        ),
        (_apart(FIRE, F_PUMPS, F_WATER),),
        SCOPE_MODULE,
        controls=(Pair("t10c", (F_PUMPS, "p5"), (F_WATER, "p6"), CATEGORY_RATING,
                       "FP-1 and FP-2 are different pumps"),),
        tags=("different_tags",),
    ),
    Case(
        "cx-t11", SPLIT_TUNING,
        "An existing fire pump and a new one.",
        (
            _doc(F_PUMPS, FIRE, ("p5", "2.01 FIRE PUMP", "Fire pump: 480 V, 3-phase.")),
            _doc(F_CONTROLLERS, ELEC, ("p7", "3.05 DEMOLITION",
                                       "Disconnect the existing fire pump: 208 V, 3-phase.")),
        ),
        (_apart(FIRE, F_PUMPS), _apart(ELEC, F_CONTROLLERS)),
        SCOPE_PROGRAM,
        controls=(Pair("t11c", (F_PUMPS, "p5"), (F_CONTROLLERS, "p7"), CATEGORY_ELECTRICAL,
                       "an existing pump being removed and the new pump"),),
        tags=("scope_status",),
    ),
    Case(
        "cx-t12", SPLIT_TUNING,
        "A panel's notification-circuit voltage and its primary supply.",
        (
            _doc(F_ALARM, ESS, ("p6", "2.04 FIRE ALARM CONTROL UNIT",
                                "FACP notification appliance circuits: 24 VDC.")),
            _doc(F_POWER, ELEC, ("p9", "3.06 FIRE ALARM POWER", "FACP primary power: 120 V.")),
        ),
        (_apart(ESS, F_ALARM), _apart(ELEC, F_POWER)),
        SCOPE_PROGRAM,
        controls=(Pair("t12c", (F_ALARM, "p6"), (F_POWER, "p9"), CATEGORY_ELECTRICAL,
                       "a 24 VDC circuit and a 120 V supply are different attributes"),),
        tags=("voltage_class",),
    ),
    Case(
        "cx-t13", SPLIT_TUNING,
        "The panel's supply voltage under two names for one panel.",
        (
            _doc(F_ALARM, ESS, ("p5", "2.04 FIRE ALARM CONTROL UNIT",
                                "Fire alarm control unit primary power: 277 V.")),
            _doc(F_POWER, ELEC, ("p9", "3.06 FIRE ALARM POWER",
                                 "Provide a dedicated 120 V branch circuit to the FACP.")),
        ),
        (_apart(ESS, F_ALARM), _apart(ELEC, F_POWER)),
        SCOPE_PROGRAM,
        conflicts=(Pair("t13a", (F_ALARM, "p5"), (F_POWER, "p9"), CATEGORY_ELECTRICAL,
                        "277 V against a 120 V circuit for the one control unit"),),
        tags=("synonym", "cross_module"),
    ),
    Case(
        "cx-t14", SPLIT_TUNING,
        "Releasing-panel programming assigned to two divisions.",
        (
            _doc(F_AGENT, FIRE, ("p3", "1.05 RESPONSIBILITIES",
                                 "Programming of the releasing control panel shall be provided under this Section.")),
            _doc(F_ALARM, ESS, ("p8", "1.06 RELEASING SERVICE",
                                "Division 28 shall program the releasing panel and all agent release sequences.")),
        ),
        (_apart(FIRE, F_AGENT), _apart(ESS, F_ALARM)),
        SCOPE_PROGRAM,
        conflicts=(Pair("t14a", (F_AGENT, "p3"), (F_ALARM, "p8"), CATEGORY_RESPONSIBILITY,
                        "programming by Division 21 against Division 28"),),
        tags=("responsibility", "cross_module", "this_section"),
    ),
    Case(
        "cx-t15", SPLIT_TUNING,
        "A transfer switch ampere rating stated twice.",
        (
            _doc(F_PUMPS, FIRE, ("p9", "2.06 POWER TRANSFER", "ATS-FP: 600 A, 480 V.")),
            _doc(F_TRANSFER, ELEC, ("p4", "2.02 FIRE PUMP TRANSFER SWITCH", "ATS-FP: 400 A, 3-pole.")),
        ),
        (_apart(FIRE, F_PUMPS), _apart(ELEC, F_TRANSFER)),
        SCOPE_PROGRAM,
        conflicts=(Pair("t15a", (F_PUMPS, "p9"), (F_TRANSFER, "p4"), CATEGORY_RATING,
                        "600 A against 400 A for one tagged switch"),),
        tags=("tag", "current"),
    ),
    Case(
        "cx-t16", SPLIT_TUNING,
        "Motor horsepower against the controller's rating.",
        (
            _doc(F_PUMPS, FIRE, ("p4", "2.01 FIRE PUMP", "FP-1 driver: 100 hp electric motor.")),
            _doc(F_CONTROLLERS, ELEC, ("p6", "2.03 FIRE PUMP CONTROLLER",
                                       "FP-1 controller shall be rated for a 75 hp motor.")),
        ),
        (_apart(FIRE, F_PUMPS), _apart(ELEC, F_CONTROLLERS)),
        SCOPE_PROGRAM,
        conflicts=(Pair("t16a", (F_PUMPS, "p4"), (F_CONTROLLERS, "p6"), CATEGORY_RATING,
                        "100 hp motor on a controller rated for 75 hp"),),
        tags=("tag", "hp"),
    ),
    Case(
        "cx-t17", SPLIT_TUNING,
        "Horsepower and kilowatts are not compared.",
        (
            _doc(F_PUMPS, FIRE, ("p4", "2.01 FIRE PUMP", "FP-4 motor: 100 hp.")),
            _doc(F_CONTROLLERS, ELEC, ("p6", "2.03 CONTROLLERS", "FP-4 motor: 75 kW.")),
        ),
        (_apart(FIRE, F_PUMPS), _apart(ELEC, F_CONTROLLERS)),
        SCOPE_PROGRAM,
        controls=(Pair("t17c", (F_PUMPS, "p4"), (F_CONTROLLERS, "p6"), CATEGORY_RATING,
                       "100 hp is about 75 kW; the attributes are not compared"),),
        tags=("different_units",),
    ),
    Case(
        "cx-t18", SPLIT_TUNING,
        "A minimum and a value that meets it.",
        (
            _doc(F_PUMPS, FIRE, ("p5", "2.01 FIRE PUMP", "Fire pump minimum rated capacity: 1,000 gpm.")),
            _doc(F_WATER, FIRE, ("p7", "2.03 SUCTION", "Fire pump rated capacity: 1,250 gpm.")),
        ),
        (_apart(FIRE, F_PUMPS, F_WATER),),
        SCOPE_MODULE,
        controls=(Pair("t18c", (F_PUMPS, "p5"), (F_WATER, "p7"), CATEGORY_RATING,
                       "1,250 gpm meets a 1,000 gpm minimum"),),
        tags=("bound",),
    ),
    Case(
        "cx-t19", SPLIT_TUNING,
        "Metric and US flow for one pump, disagreeing and agreeing.",
        (
            _doc(F_PUMPS, FIRE,
                 ("p5", "2.01 FIRE PUMPS", "FP-5 rated flow: 63 L/s."),
                 ("p6", "2.01 FIRE PUMPS", "FP-6 rated flow: 63 L/s.")),
            _doc(F_WATER, FIRE,
                 ("p7", "2.03 SUCTION", "FP-5 rated capacity: 1,500 gpm."),
                 ("p8", "2.03 SUCTION", "FP-6 rated capacity: 1,000 gpm.")),
        ),
        (_apart(FIRE, F_PUMPS, F_WATER),),
        SCOPE_MODULE,
        conflicts=(Pair("t19a", (F_PUMPS, "p5"), (F_WATER, "p7"), CATEGORY_RATING,
                        "63 L/s is about 1,000 gpm, not 1,500"),),
        controls=(Pair("t19c", (F_PUMPS, "p6"), (F_WATER, "p8"), CATEGORY_RATING,
                       "63 L/s is 1,000 gpm within conversion rounding"),),
        tags=("conversion",),
    ),
    Case(
        "cx-t20", SPLIT_TUNING,
        "Supervisory-switch wiring under two names and two divisions.",
        (
            _doc(F_WET, FIRE, ("p5", "3.06 CONNECTIONS", "Wiring of tamper switches under this Section.")),
            _doc(F_ALARM, ESS, ("p9", "3.03 WIRING", "Valve supervisory switches shall be wired by Division 28.")),
        ),
        (_apart(FIRE, F_WET), _apart(ESS, F_ALARM)),
        SCOPE_PROGRAM,
        conflicts=(Pair("t20a", (F_WET, "p5"), (F_ALARM, "p9"), CATEGORY_RESPONSIBILITY,
                        "wiring by Division 21 against Division 28"),),
        tags=("synonym", "responsibility", "bare_form"),
    ),
    Case(
        "cx-t21", SPLIT_TUNING,
        "A cross-module conflict under the module scope is out of scope.",
        (
            _doc(F_PUMPS, FIRE, ("p3", "2.01 FIRE PUMP", "Fire pump FP-1 motor: 480 V, 3-phase, 60 Hz.")),
            _doc(F_CONTROLLERS, ELEC, ("p5", "2.03 FEEDERS", "Feeder for FP-1: 208 V, 3-phase.")),
        ),
        (_apart(FIRE, F_PUMPS), _apart(ELEC, F_CONTROLLERS)),
        SCOPE_MODULE,
        controls=(Pair("t21c", (F_PUMPS, "p3"), (F_CONTROLLERS, "p5"), CATEGORY_ELECTRICAL,
                       "the module scope does not compare modules"),),
        tags=("scope_module",),
    ),
    Case(
        "cx-t22", SPLIT_TUNING,
        "One pump named by tag in one spec and by name in the other.",
        (
            _doc(F_PUMPS, FIRE, ("p3", "2.01 FIRE PUMP", "The fire pump, FP-1, shall have a 480 V motor.")),
            _doc(F_CONTROLLERS, ELEC, ("p5", "2.03 FEEDERS", "Fire pump feeder: 208 V, 3-phase.")),
        ),
        (_apart(FIRE, F_PUMPS), _apart(ELEC, F_CONTROLLERS)),
        SCOPE_PROGRAM,
        conflicts=(Pair("t22a", (F_PUMPS, "p3"), (F_CONTROLLERS, "p5"), CATEGORY_ELECTRICAL,
                        "one fire pump: 480 V against a 208 V feeder"),),
        tags=("tag_vs_term",),
    ),
    Case(
        "cx-t23", SPLIT_TUNING,
        "Two buildings.",
        (
            _doc(F_PUMPS, FIRE, ("p5", "2.01 FIRE PUMPS", "Building A fire pump: 1,000 gpm.")),
            _doc(F_WATER, FIRE, ("p6", "2.02 SERVICE", "Building B fire pump: 1,500 gpm.")),
        ),
        (_apart(FIRE, F_PUMPS, F_WATER),),
        SCOPE_MODULE,
        controls=(Pair("t23c", (F_PUMPS, "p5"), (F_WATER, "p6"), CATEGORY_RATING,
                       "different buildings' pumps"),),
        tags=("scope_building",),
    ),
    Case(
        "cx-t24", SPLIT_TUNING,
        "Single-phase against three-phase for one compressor.",
        (
            _doc(F_DRY, FIRE, ("p4", "2.04 AIR COMPRESSOR", "AC-2 air compressor: 208 V, 1-phase.")),
            _doc(F_POWER, ELEC, ("p8", "3.05 EQUIPMENT CONNECTIONS", "AC-2: 208 V, 3-phase circuit.")),
        ),
        (_apart(FIRE, F_DRY), _apart(ELEC, F_POWER)),
        SCOPE_PROGRAM,
        conflicts=(Pair("t24a", (F_DRY, "p4"), (F_POWER, "p8"), CATEGORY_ELECTRICAL,
                        "1-phase against 3-phase"),),
        tags=("tag", "phase_count"),
    ),
    Case(
        "cx-t25", SPLIT_TUNING,
        "Two statements that agree.",
        (
            _doc(F_PUMPS, FIRE, ("p3", "2.01 FIRE PUMP", "FP-7: 480 V, 3-phase, 60 Hz.")),
            _doc(F_CONTROLLERS, ELEC, ("p5", "2.03 FEEDERS", "FP-7 feeder: 480Y/277 V, 3-phase.")),
        ),
        (_apart(FIRE, F_PUMPS), _apart(ELEC, F_CONTROLLERS)),
        SCOPE_PROGRAM,
        controls=(Pair("t25c", (F_PUMPS, "p3"), (F_CONTROLLERS, "p5"), CATEGORY_ELECTRICAL,
                       "480 V and 480Y/277 V agree"),),
        tags=("agreement",),
    ),
    Case(
        "cx-t26", SPLIT_TUNING,
        "An equipment schedule row against a paragraph.",
        (
            _doc(F_PUMPS, FIRE, ("t0r2", "2.09 SCHEDULE",
                                 "FP-8 | Electric fire pump | 1,500 gpm | 125 psi | 100 hp | 480 V")),
            _doc(F_CONTROLLERS, ELEC, ("p6", "2.03 CONTROLLERS", "FP-8 motor: 150 hp.")),
        ),
        (_apart(FIRE, F_PUMPS), _apart(ELEC, F_CONTROLLERS)),
        SCOPE_PROGRAM,
        conflicts=(Pair("t26a", (F_PUMPS, "t0r2"), (F_CONTROLLERS, "p6"), CATEGORY_RATING,
                        "100 hp in the schedule against 150 hp"),),
        tags=("table_row",),
    ),
    Case(
        "cx-t27", SPLIT_TUNING,
        "A generator's standby rating stated twice in one module, planned apart.",
        (
            _doc(F_GEN, ELEC, ("p4", "2.02 ENGINE GENERATOR", "GEN-1 standby rating: 2,000 kW.")),
            _doc("01 91 13 General Commissioning Requirements.docx", ELEC,
                 ("p12", "3.07 LOAD BANK TEST", "Load bank test GEN-1 at its 2,500 kW standby rating.")),
        ),
        (_apart(ELEC, F_GEN, "01 91 13 General Commissioning Requirements.docx"),),
        SCOPE_MODULE,
        conflicts=(Pair("t27a", (F_GEN, "p4"), ("01 91 13 General Commissioning Requirements.docx", "p12"),
                        CATEGORY_RATING, "2,000 kW against 2,500 kW standby"),),
        tags=("tag", "kw", "cross_chunk"),
    ),
    Case(
        "cx-t28", SPLIT_TUNING,
        "Two different compressors that share a generic name and state no tag or scope.",
        (
            _doc(F_DRY, FIRE, ("p4", "2.04 AIR COMPRESSOR",
                               "Provide a tank-mounted air compressor for the dry-pipe system. "
                               "The air compressor shall be 120 V, 1-phase.")),
            _doc("21 13 19 Preaction Sprinkler Systems.docx", FIRE,
                 ("p5", "2.05 AIR SUPPLY", "The air compressor shall be 208 V, 1-phase, riser-mounted.")),
        ),
        (_apart(FIRE, F_DRY, "21 13 19 Preaction Sprinkler Systems.docx"),),
        SCOPE_MODULE,
        controls=(Pair("t28c", (F_DRY, "p4"), ("21 13 19 Preaction Sprinkler Systems.docx", "p5"),
                       CATEGORY_ELECTRICAL,
                       "the dry-pipe and the preaction compressors are different units"),),
        tags=("generic_name",),
    ),
    Case(
        "cx-t29", SPLIT_TUNING,
        "Provide and install by one division agree.",
        (
            _doc(F_DUCTACC, CA, ("p3", "2.06 FIRE/SMOKE DAMPERS",
                                 "Fire/smoke dampers shall be provided by Division 23.")),
            _doc(F_BAS, CA, ("p5", "1.04 COORDINATION",
                             "Combination fire/smoke dampers shall be installed by Division 23 and "
                             "wired by Division 25.")),
        ),
        (_apart(CA, F_DUCTACC, F_BAS),),
        SCOPE_MODULE,
        controls=(Pair("t29c", (F_DUCTACC, "p3"), (F_BAS, "p5"), CATEGORY_RESPONSIBILITY,
                       "both assign the dampers' installation to Division 23"),),
        tags=("agreement", "responsibility"),
    ),
)


# Held-out cases: written after the cx1 rules are frozen, scored once.
HELD_OUT_CASES: tuple[Case, ...] = ()


ALL_CASES: tuple[Case, ...] = TUNING_CASES + HELD_OUT_CASES


def cases(split: str | None = None) -> tuple[Case, ...]:
    if split is None:
        return ALL_CASES
    return tuple(c for c in ALL_CASES if c.split == split)


def validate(cases_: Iterable[Case] = ALL_CASES) -> list[str]:
    """Problems with the dataset; ``[]`` when it is sound."""
    problems: list[str] = []
    seen_cases: set[str] = set()
    seen_pairs: set[str] = set()
    for case in cases_:
        if case.case_id in seen_cases:
            problems.append(f"{case.case_id}: duplicate case id")
        seen_cases.add(case.case_id)
        if case.split not in SPLITS:
            problems.append(f"{case.case_id}: unknown split {case.split!r}")
        if case.scope not in (SCOPE_MODULE, SCOPE_PROGRAM):
            problems.append(f"{case.case_id}: unknown scope {case.scope!r}")
        if not (case.conflicts or case.controls):
            problems.append(f"{case.case_id}: no label")
        elements: dict[str, set[str]] = {}
        names = [doc.file_name for doc in case.docs]
        if len(set(names)) != len(names):
            problems.append(f"{case.case_id}: a file name is used twice")
        for doc in case.docs:
            ids = [e[0] for e in doc.elements]
            if len(set(ids)) != len(ids):
                problems.append(f"{case.case_id}: {doc.file_name} repeats an element id")
            elements[doc.file_name] = set(ids)
            for module in doc.modules:
                if module not in {FIRE, ELEC, ESS, ARCH, CA}:
                    problems.append(f"{case.case_id}: unknown module {module!r}")
        planned_modules = {module for module, _groups in case.plans}
        for doc in case.docs:
            for module in doc.modules:
                if module not in planned_modules:
                    problems.append(f"{case.case_id}: no plan for {module} ({doc.file_name})")
        for module, groups in case.plans:
            for group in groups:
                for name in group:
                    if name not in elements:
                        problems.append(f"{case.case_id}: plan names unknown file {name!r}")
        for pair in case.conflicts + case.controls:
            if pair.pair_id in seen_pairs:
                problems.append(f"{case.case_id}: duplicate pair id {pair.pair_id}")
            seen_pairs.add(pair.pair_id)
            if pair.category not in CATEGORIES:
                problems.append(f"{case.case_id}: {pair.pair_id} unknown category")
            if pair.a[0] == pair.b[0]:
                problems.append(f"{case.case_id}: {pair.pair_id} pairs a file with itself")
            for side in (pair.a, pair.b):
                if side[1] not in elements.get(side[0], set()):
                    problems.append(f"{case.case_id}: {pair.pair_id} names missing element {side}")
    return problems


def _case_json(case: Case) -> dict:
    return {
        "case_id": case.case_id,
        "split": case.split,
        "description": case.description,
        "docs": [
            {"file_name": d.file_name, "modules": list(d.modules),
             "elements": [list(e) for e in d.elements]}
            for d in case.docs
        ],
        "plans": [[m, [list(g) for g in groups]] for m, groups in case.plans],
        "scope": case.scope,
        "conflicts": [[p.pair_id, list(p.a), list(p.b), p.category] for p in case.conflicts],
        "controls": [[p.pair_id, list(p.a), list(p.b), p.category] for p in case.controls],
    }


def case_digest(case: Case) -> str:
    return hashlib.sha256(
        json.dumps(_case_json(case), sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def dataset_digest(split: str | None = None) -> str:
    digests = [case_digest(c) for c in cases(split)]
    return hashlib.sha256("\n".join(digests).encode("utf-8")).hexdigest()


__all__ = [
    "ALL_CASES",
    "CATEGORIES",
    "Case",
    "Doc",
    "HELD_OUT_CASES",
    "Pair",
    "SPLITS",
    "SPLIT_HELD_OUT",
    "SPLIT_TUNING",
    "TUNING_CASES",
    "case_digest",
    "cases",
    "dataset_digest",
    "validate",
]
