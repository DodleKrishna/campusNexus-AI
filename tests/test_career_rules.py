"""Unit tests for deterministic opportunity eligibility (app/rules/opportunity_eligibility.py)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.rules.opportunity_eligibility import compute_eligibility
from app.schemas.career import (
    CareerProfile,
    OpportunityEligibilityStatus,
    OpportunitySummary,
    RequiredSkillSummary,
    StudentSkillSummary,
)

NOW = datetime(2026, 9, 23, tzinfo=timezone.utc)

PROFILE = CareerProfile(
    student_code="STU-DEMO-001",
    full_name="Aditi Rao",
    department_code="CSE",
    year=3,
    cgpa=7.8,
    skills=[
        StudentSkillSummary(name="Python", proficiency=4),
        StudentSkillSummary(name="Machine Learning", proficiency=3),
    ],
)


def _opportunity(**overrides) -> OpportunitySummary:
    defaults = dict(
        opportunity_id=1,
        title="AI Software Engineering Intern",
        company="NimbusCloud Technologies",
        opportunity_type="internship",
        minimum_cgpa=7.0,
        deadline=NOW + timedelta(days=45),
        status="open",
        allowed_departments=["CSE"],
        eligible_years=[3, 4],
        required_skills=[
            RequiredSkillSummary(name="Python", minimum_proficiency=2),
            RequiredSkillSummary(name="Machine Learning", minimum_proficiency=2),
        ],
    )
    defaults.update(overrides)
    return OpportunitySummary(**defaults)


def test_eligible_when_all_mandatory_criteria_met() -> None:
    result = compute_eligibility(PROFILE, _opportunity(), now=NOW)
    assert result.status == OpportunityEligibilityStatus.ELIGIBLE
    assert result.blocking_reasons == []


def test_cgpa_below_minimum_is_blocking() -> None:
    result = compute_eligibility(PROFILE, _opportunity(minimum_cgpa=8.5), now=NOW)
    assert result.status == OpportunityEligibilityStatus.NOT_ELIGIBLE
    assert any("CGPA" in r for r in result.blocking_reasons)


def test_department_not_allowed_is_blocking() -> None:
    result = compute_eligibility(PROFILE, _opportunity(allowed_departments=["ECE", "MECH"]), now=NOW)
    assert result.status == OpportunityEligibilityStatus.NOT_ELIGIBLE
    assert any("department" in r for r in result.blocking_reasons)


def test_year_not_eligible_is_blocking() -> None:
    result = compute_eligibility(PROFILE, _opportunity(eligible_years=[4]), now=NOW)
    assert result.status == OpportunityEligibilityStatus.NOT_ELIGIBLE
    assert any("year" in r for r in result.blocking_reasons)


def test_expired_deadline_is_blocking() -> None:
    result = compute_eligibility(PROFILE, _opportunity(deadline=NOW - timedelta(days=1)), now=NOW)
    assert result.status == OpportunityEligibilityStatus.NOT_ELIGIBLE
    assert any("deadline" in r for r in result.blocking_reasons)


def test_closed_status_is_blocking() -> None:
    result = compute_eligibility(PROFILE, _opportunity(status="expired"), now=NOW)
    assert result.status == OpportunityEligibilityStatus.NOT_ELIGIBLE
    assert any("status" in r for r in result.blocking_reasons)


def test_missing_mandatory_skill_is_blocking() -> None:
    opp = _opportunity(
        required_skills=[
            RequiredSkillSummary(name="Docker", minimum_proficiency=3),
            RequiredSkillSummary(name="Kubernetes", minimum_proficiency=3),
        ]
    )
    result = compute_eligibility(PROFILE, opp, now=NOW)
    assert result.status == OpportunityEligibilityStatus.NOT_ELIGIBLE
    assert set(result.missing_mandatory_skills) == {"Docker", "Kubernetes"}


def test_proficiency_below_minimum_counts_as_missing() -> None:
    opp = _opportunity(required_skills=[RequiredSkillSummary(name="Python", minimum_proficiency=5)])
    result = compute_eligibility(PROFILE, opp, now=NOW)
    assert result.status == OpportunityEligibilityStatus.NOT_ELIGIBLE
    assert result.missing_mandatory_skills == ["Python"]


def test_missing_recommended_skill_does_not_block_eligibility() -> None:
    """The core Phase 6 requirement: a recommended (minimum_proficiency=None)
    skill the student lacks must never make them ineligible -- proven with a
    synthetic opportunity, not by touching the approved seed corpus (same
    precedent as Phase 4's synthetic-Evidence ambiguous-threshold test)."""
    opp = _opportunity(
        required_skills=[
            RequiredSkillSummary(name="Python", minimum_proficiency=2),  # mandatory, met
            RequiredSkillSummary(name="TensorFlow", minimum_proficiency=None),  # recommended, missing
        ]
    )
    result = compute_eligibility(PROFILE, opp, now=NOW)
    assert result.status == OpportunityEligibilityStatus.ELIGIBLE
    assert result.blocking_reasons == []
    assert result.missing_recommended_skills == ["TensorFlow"]
    assert result.missing_mandatory_skills == []


def test_recommended_skill_present_is_not_reported_as_a_gap() -> None:
    opp = _opportunity(required_skills=[RequiredSkillSummary(name="Python", minimum_proficiency=None)])
    result = compute_eligibility(PROFILE, opp, now=NOW)
    assert result.missing_recommended_skills == []


def test_already_applied_is_recorded_but_does_not_affect_eligibility() -> None:
    result = compute_eligibility(PROFILE, _opportunity(), now=NOW, already_applied=True, application_status="under_review")
    assert result.status == OpportunityEligibilityStatus.ELIGIBLE
    assert result.already_applied is True
    assert result.application_status == "under_review"


def test_multiple_blocking_reasons_are_all_reported() -> None:
    opp = _opportunity(minimum_cgpa=9.0, allowed_departments=["ECE"])
    result = compute_eligibility(PROFILE, opp, now=NOW)
    assert len(result.blocking_reasons) >= 2
