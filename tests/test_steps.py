"""The step schema is the contract between Guardian, Chronicler and Bedrock.

Rejection matters more than acceptance here: an LLM-authored playbook that
reaches the database malformed is a genome that executes against production.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from nexus_common.steps import (
    ACTIONS,
    IRREVERSIBLE_ACTIONS,
    SERVICE_PLACEHOLDER,
    PlaybookDraft,
    RemediationStep,
    inverse_program,
    parse_steps,
)

VALID = {
    "action": "scale_connection_pool",
    "target": "payments",
    "params": {"max_size": 200},
    "inverse": {"action": "scale_connection_pool", "params": {"max_size": 50}},
}


def test_a_well_formed_step_parses():
    step = RemediationStep.model_validate(VALID)
    assert step.action == "scale_connection_pool"
    assert step.reversible


def test_a_step_without_an_inverse_is_irreversible():
    step = RemediationStep.model_validate({"action": "rotate_certificate", "target": "auth"})
    assert not step.reversible


def test_an_inverse_does_not_rescue_an_irreversible_action():
    """Declaring an inverse for `prune_disk` does not bring deleted data back."""
    step = RemediationStep.model_validate({
        "action": "prune_disk", "target": "auth", "params": {"older_than_days": 3},
        "inverse": {"action": "extend_volume", "params": {"target_gib": 500}},
    })
    assert step.action in IRREVERSIBLE_ACTIONS
    assert not step.reversible


def test_unknown_actions_are_rejected():
    with pytest.raises(ValidationError, match="unknown action"):
        RemediationStep.model_validate({"action": "rm_minus_rf", "target": "payments"})


def test_unknown_inverse_actions_are_rejected():
    with pytest.raises(ValidationError, match="unknown action"):
        RemediationStep.model_validate(
            {**VALID, "inverse": {"action": "drop_database", "params": {}}}
        )


def test_extra_fields_are_rejected():
    """Bedrock inventing a field is a signal the model misunderstood the schema."""
    with pytest.raises(ValidationError):
        RemediationStep.model_validate({**VALID, "urgency": "high"})


def test_a_missing_target_is_rejected():
    with pytest.raises(ValidationError):
        RemediationStep.model_validate({"action": "flush_cache", "params": {}})


def test_parse_steps_rejects_a_non_array():
    with pytest.raises(ValueError, match="JSON array"):
        parse_steps({"action": "flush_cache", "target": "payments"})


def test_inverse_program_reverses_execution_order():
    steps = parse_steps([
        {"action": "scale_connection_pool", "target": "p", "params": {"max_size": 200},
         "inverse": {"action": "scale_connection_pool", "params": {"max_size": 50}}},
        {"action": "set_circuit_breaker", "target": "p", "params": {"error_threshold": 0.1},
         "inverse": {"action": "set_circuit_breaker", "params": {"error_threshold": 0.5}}},
    ])
    program = inverse_program(steps)
    assert [s["action"] for s in program] == ["set_circuit_breaker", "scale_connection_pool"]
    assert all(s["target"] == "p" for s in program)


def test_inverse_program_skips_steps_that_have_none():
    steps = parse_steps([
        {"action": "shed_load", "target": "p", "params": {"drop_pct": 10},
         "inverse": {"action": "shed_load", "params": {"drop_pct": 0}}},
        {"action": "rotate_certificate", "target": "p", "params": {}},
    ])
    assert [s["action"] for s in inverse_program(steps)] == ["shed_load"]


def test_a_draft_needs_at_least_one_step():
    with pytest.raises(ValidationError):
        PlaybookDraft(name="empty", outcome_category="disk_full", remediation_steps=[])


def test_a_draft_is_reversible_only_when_every_step_is():
    draft = PlaybookDraft(
        name="mixed", outcome_category="cert_expiry",
        remediation_steps=parse_steps([
            {"action": "shed_load", "target": "p", "params": {},
             "inverse": {"action": "shed_load", "params": {}}},
            {"action": "rotate_certificate", "target": "p", "params": {}},
        ]),
    )
    assert not draft.reversible


def test_the_action_vocabulary_has_no_duplicates():
    assert len(set(ACTIONS)) == len(ACTIONS)
    assert IRREVERSIBLE_ACTIONS <= set(ACTIONS)


# -- target binding -------------------------------------------------------- #

def test_a_stored_playbook_keeps_the_service_placeholder():
    """Procedural memory is generic: the service comes from the prediction."""
    from generator import playbooks as pb

    for spec in pb.SPECS:
        for step in spec.steps_template():
            assert step["target"] == SERVICE_PLACEHOLDER, spec.slug


def test_parse_steps_binds_the_placeholder_to_the_named_service():
    steps = parse_steps(
        [{"action": "scale_connection_pool", "target": SERVICE_PLACEHOLDER,
          "params": {"max_size": 200}}],
        service="checkout",
    )
    assert steps[0].target == "checkout"


def test_binding_leaves_an_explicit_target_alone():
    steps = parse_steps(
        [{"action": "flush_cache", "target": "auth", "params": {}}], service="checkout")
    assert steps[0].target == "auth"


def test_parsing_without_a_service_leaves_the_placeholder_intact():
    steps = parse_steps(
        [{"action": "flush_cache", "target": SERVICE_PLACEHOLDER, "params": {}}])
    assert steps[0].target == SERVICE_PLACEHOLDER
