from dataclasses import replace

from trade_checklist import ChecklistAssessment, evaluate_checklist


def complete_assessment():
    return ChecklistAssessment(
        signal_reviewed=True,
        location_reviewed=True,
        location_valid=True,
        timeframe_reviewed=True,
        timeframe_aligned=True,
        volume_reviewed=True,
        volume_confirmed=True,
        repeated_signal_risk_reviewed=True,
        structure_reviewed=True,
        structure_confirmed=True,
        oi_reviewed=True,
    )


def test_all_seven_reviews_and_required_gates_allow_entry():
    result = evaluate_checklist(complete_assessment())
    assert result.allowed
    assert result.reasons == ("all seven checks completed",)


def test_missing_review_and_failed_gate_block_without_scoring():
    assessment = replace(
        complete_assessment(),
        oi_reviewed=False,
        volume_confirmed=False,
    )
    result = evaluate_checklist(assessment)
    assert not result.allowed
    assert "open interest not reviewed" in result.reasons
    assert "volume confirmation is absent" in result.reasons


def test_market_veto_always_blocks_entry():
    result = evaluate_checklist(
        replace(complete_assessment(), vetoes=("top-holder concentration risk",))
    )
    assert not result.allowed
    assert result.reasons == ("veto: top-holder concentration risk",)
