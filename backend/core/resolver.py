"""
Scenario Resolver
-----------------
Detects WHICH scenario to run from the user's INPUTS, instead of asking them to
hand-pick it from a question tree. The inputs already declare intent — this module
encodes the same decision the registry describes in its `fixed`/`target`/`mode`
columns, and returns the matching scenario id plus a plain-English reason.

The discriminators (exactly the signals a planner would reason about):
  • topology   — grid-connected BTM · backup (no generation) · off-grid · standalone
  • pv_mode    — a fixed PV nameplate, a unit profile to SIZE, or no generation
  • bess_mode  — a fixed battery (evaluate it) or a battery to SIZE
  • target     — an SSR target · a grid-connection cap · firmness · curtailment
  • shape      — one design point · a full curve · a PV–BESS surface

Same logic is reused by the API (`/resolve`) and any client, so everyone agrees
on the mapping. The result is advisory: the UI shows it and lets the user override.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from core.scenarios import get_scenario, Status


@dataclass
class Resolution:
    """The detected scenario plus why, and whether it is runnable today."""
    scenario_id: str
    reason: str
    ready: bool
    signals: dict = field(default_factory=dict)
    fallback_id: Optional[str] = None   # a READY alternative when the match isn't ready

    def as_dict(self) -> dict:
        return {
            "scenario_id": self.scenario_id,
            "reason": self.reason,
            "ready": self.ready,
            "signals": self.signals,
            "fallback_id": self.fallback_id,
        }


# ──────────────────────────────────────────────────────────────────────────────
# Signal extraction — turn raw inputs into the five discriminators
# ──────────────────────────────────────────────────────────────────────────────

def _num(v) -> Optional[float]:
    """Coerce to a positive-or-zero float, or None when absent/blank."""
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _signals(inp: dict) -> dict:
    pv_mw          = _num(inp.get("pv_mw"))
    pv_unit_profile = bool(inp.get("pv_unit_profile"))     # PV given as a profile to SIZE
    bess_mw        = _num(inp.get("bess_mw"))
    bess_mwh       = _num(inp.get("bess_mwh"))
    grid_available = inp.get("grid_available", True) and not inp.get("off_grid", False)
    export_limit   = _num(inp.get("export_limit_mw"))
    standalone     = bool(inp.get("standalone"))

    has_gen = pv_unit_profile or (pv_mw is not None and pv_mw > 0)
    if pv_unit_profile:
        pv_mode = "profile"        # size the PV
    elif pv_mw is not None and pv_mw > 0:
        pv_mode = "fixed"          # PV nameplate is given
    else:
        pv_mode = "none"           # no on-site generation

    bess_fixed = (bess_mw is not None and bess_mw > 0 and
                  bess_mwh is not None and bess_mwh > 0)

    # topology
    if not grid_available:
        topology = "off_grid"
    elif standalone and has_gen:
        topology = "standalone"            # generation that exports, not a BTM load
    elif not has_gen:
        topology = "backup"                # load + battery, no on-site generation
    else:
        topology = "btm"                   # generation + grid + load (the common case)

    # which targets were actually supplied (None ⇒ not chosen, ignore defaults)
    targets = []
    if _num(inp.get("target_ssr_pct")) is not None:        targets.append("ssr")
    if _num(inp.get("target_gc_mw")) is not None:          targets.append("gc")
    if _num(inp.get("target_firmness_pct")) is not None:   targets.append("firmness")
    if _num(inp.get("target_curtailment_pct")) is not None: targets.append("curtailment")
    if inp.get("min_grid"):                                 targets.append("min_gc")

    # answer shape
    has_sweep = bool(inp.get("pv_sweep_mw")) and bool(inp.get("ssr_targets_pct") or inp.get("gc_targets_mw"))
    if has_sweep:
        shape = "surface"
    elif inp.get("want_curve") or inp.get("ssr_targets_pct") or inp.get("gc_targets_mw"):
        shape = "curve"
    else:
        shape = "point"

    return {
        "topology": topology, "pv_mode": pv_mode, "bess_fixed": bess_fixed,
        "targets": targets, "shape": shape, "verify": bool(inp.get("verify")),
    }


# ──────────────────────────────────────────────────────────────────────────────
# Decision tree — signals → scenario id (mirrors the registry)
# ──────────────────────────────────────────────────────────────────────────────

def _decide(s: dict) -> tuple[str, str]:
    """Return (scenario_id, reason) for the extracted signals."""
    topo, pv, fixed, targets, shape = (
        s["topology"], s["pv_mode"], s["bess_fixed"], s["targets"], s["shape"])
    has = lambda t: t in targets

    # ── Off-grid ──────────────────────────────────────────────────────────
    if topo == "off_grid":
        return "S91_OFFGRID_FIRMNESS_PVBESS", (
            "No grid connection → off-grid system; sizing PV+BESS for a firmness target.")

    # ── Standalone generation (export-limited) ────────────────────────────
    if topo == "standalone":
        return "S93_STANDALONE_EXPORTLIMIT_BESS", (
            "Export-limited standalone generation → sizing BESS for a curtailment target.")

    # ── Backup / no generation ────────────────────────────────────────────
    if topo == "backup":
        if fixed:
            return "S33_SUB_FIXED_BESS_EVAL", (
                "No generation and a fixed battery → evaluate the backup design.")
        if has("firmness"):
            return "S34_SUB_FIRMNESS_TARGET_BESS", (
                "No generation, firmness target → size the backup battery for firmness.")
        if shape == "curve":
            return "S32_SUB_GC_CURVE", (
                "No generation, grid-connection sweep → battery-vs-grid backup curve.")
        return "S31_SUB_GC_TARGET_BESS", (
            "No generation, grid-connection target → min backup battery for that grid cap.")

    # ── Grid-connected BTM (generation + grid) ────────────────────────────
    if fixed:
        if s["verify"]:
            return "S41_OPERATIONAL_VERIFY", (
                "Fixed PV+BESS design → verify its KPIs under causal operation.")
        if has("gc") or has("min_gc"):
            return "S52_BTM_GCMIN_FIXED_DESIGN", (
                "Fixed design, asking for grid connection → GCmin of the fixed design.")
        return "S00_FIXED_DESIGN_EVAL", (
            "Fixed PV+BESS design, no sizing target → evaluate KPIs + flows.")

    if pv == "fixed":   # PV nameplate given, size the battery
        if has("ssr") and (has("gc") or has("min_gc")):
            return "S71_BTM_SSR_PLUS_GC_BESS", (
                "Fixed PV, both SSR and grid-cap targets → size BESS for SSR within the grid limit.")
        if has("ssr"):
            if shape == "curve":
                return "S21_BTM_SSR_CURVE", (
                    "Fixed PV, SSR target, full curve → BESS curve across SSR targets.")
            return "S11_BTM_SSR_TARGET_BESS", (
                "Fixed PV, single SSR target → minimum BESS for that SSR.")
        if has("min_gc"):
            return "S51_BTM_GCMIN_BESSOPT", (
                "Fixed PV, lowest achievable grid connection → GCmin with optimised BESS.")
        if has("gc"):
            if shape == "curve":
                return "S22_BTM_GC_CURVE", (
                    "Fixed PV, grid-connection target, full curve → BESS curve across GC targets.")
            return "S12_BTM_GC_TARGET_BESS", (
                "Fixed PV, single grid-connection target → minimum BESS for that GC.")
        # no explicit target → default to the SSR curve (the headline analysis)
        return "S21_BTM_SSR_CURVE", (
            "Fixed PV, no explicit target → default to the SSR sizing curve.")

    if pv == "profile":  # size PV and BESS together
        if has("ssr"):
            return "S62_BTM_PVBESS_SSR_SURFACE", (
                "PV given as a profile to size + SSR target → PV–BESS surface vs SSR.")
        if has("gc"):
            return "S64_BTM_PVBESS_GC_SURFACE", (
                "PV given as a profile to size + grid target → PV–BESS surface vs grid connection.")
        return "S62_BTM_PVBESS_SSR_SURFACE", (
            "PV to be sized, no explicit target → PV–BESS surface vs SSR.")

    # Fallback — should be unreachable; default to the headline SSR curve.
    return "S21_BTM_SSR_CURVE", "Defaulting to the SSR sizing curve."


# ──────────────────────────────────────────────────────────────────────────────
# Public entry point
# ──────────────────────────────────────────────────────────────────────────────

# When the natural match isn't runnable yet, fall back to the nearest READY one.
_READY_FALLBACK = {
    "S61_BTM_PVBESS_SSR_TARGET": "S62_BTM_PVBESS_SSR_SURFACE",
    "S63_BTM_PVBESS_GC_TARGET":  "S64_BTM_PVBESS_GC_SURFACE",
    "S64_BTM_PVBESS_GC_SURFACE": "S62_BTM_PVBESS_SSR_SURFACE",
}


def resolve_scenario(inputs: dict) -> Resolution:
    """Detect the scenario for a set of inputs. Never raises on unknown input —
    it always returns a best-match Resolution with a reason."""
    sig = _signals(inputs or {})
    sid, reason = _decide(sig)

    ready = True
    fallback = None
    try:
        spec = get_scenario(sid)
        ready = spec.status == Status.READY
    except KeyError:
        ready = False
    if not ready:
        fb = _READY_FALLBACK.get(sid)
        if fb:
            try:
                if get_scenario(fb).status == Status.READY:
                    fallback = fb
            except KeyError:
                fallback = None

    return Resolution(scenario_id=sid, reason=reason, ready=ready,
                      signals=sig, fallback_id=fallback)
