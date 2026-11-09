"""Pre-trade checklist extracted from the course workflow.

This module does not score indicators. Every item must be reviewed, while the
location, timeframe, volume and price-structure gates must also pass.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class ChecklistAssessment:
    signal_reviewed: bool
    location_reviewed: bool
    location_valid: bool
    timeframe_reviewed: bool
    timeframe_aligned: bool
    volume_reviewed: bool
    volume_confirmed: bool
    repeated_signal_risk_reviewed: bool
    structure_reviewed: bool
    structure_confirmed: bool
    oi_reviewed: bool
    vetoes: tuple[str, ...] = ()


@dataclass(frozen=True)
class ChecklistResult:
    allowed: bool
    reasons: tuple[str, ...]


def evaluate_checklist(assessment: ChecklistAssessment) -> ChecklistResult:
    reviews = {
        "divergence/trap classification not reviewed": assessment.signal_reviewed,
        "volume-zone location not reviewed": assessment.location_reviewed,
        "signal timeframe and holding plan not reviewed": assessment.timeframe_reviewed,
        "volume not reviewed": assessment.volume_reviewed,
        "repeated-divergence risk not reviewed": assessment.repeated_signal_risk_reviewed,
        "price structure not reviewed": assessment.structure_reviewed,
        "open interest not reviewed": assessment.oi_reviewed,
    }
    gates = {
        "setup is outside an approved volume zone": assessment.location_valid,
        "signal timeframe conflicts with holding plan": assessment.timeframe_aligned,
        "volume confirmation is absent": assessment.volume_confirmed,
        "price structure is not confirmed": assessment.structure_confirmed,
    }
    reasons = [message for message, passed in reviews.items() if not passed]
    reasons.extend(message for message, passed in gates.items() if not passed)
    reasons.extend(f"veto: {message}" for message in assessment.vetoes)
    return ChecklistResult(not reasons, tuple(reasons) or ("all seven checks completed",))
