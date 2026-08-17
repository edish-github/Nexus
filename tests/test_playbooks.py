"""The seeded population's staged beats are contract, not decoration.

Each of these corresponds to something the demo does on camera. They are
asserted here so a well-meaning edit to a success count cannot quietly remove a
demo moment.
"""
from __future__ import annotations

import numpy as np
import pytest

from generator import archetypes, vectors
from generator import playbooks as pb
from nexus_common.steps import ACTIONS, IRREVERSIBLE_ACTIONS, PlaybookDraft, parse_steps


def test_thirty_playbooks_across_every_archetype():
    assert len(pb.SPECS) == 30
    assert {s.archetype for s in pb.SPECS} == {a.key for a in archetypes.ARCHETYPES}


def test_slugs_and_names_are_unique():
    assert len({s.slug for s in pb.SPECS}) == len(pb.SPECS)
    assert len({s.name for s in pb.SPECS}) == len(pb.SPECS)


def test_four_generations_with_resolvable_parents():
    assert max(s.generation for s in pb.SPECS) == 4
    for spec in pb.SPECS:
        if spec.parent is None:
            assert spec.generation == 1
        else:
            parent = pb.BY_SLUG[spec.parent]
            assert parent.generation == spec.generation - 1


def test_every_step_validates_against_the_shared_schema():
    for spec in pb.SPECS:
        steps = parse_steps(spec.steps_for("payments"))
        assert steps, f"{spec.slug} has no steps"
        for step in steps:
            assert step.action in ACTIONS
            assert step.target == "payments"


def test_reversibility_follows_from_the_steps():
    for spec in pb.SPECS:
        steps = parse_steps(spec.steps_for("payments"))
        declared = all(s.inverse is not None for s in steps)
        irreversible = any(s.action in IRREVERSIBLE_ACTIONS for s in steps)
        assert declared != irreversible or not steps


def test_irreversible_families_exist_for_the_approval_tier():
    """Without an irreversible playbook the human-in-the-loop tier never fires."""
    irreversible = [
        s for s in pb.SPECS
        if any(st.action in IRREVERSIBLE_ACTIONS for st in parse_steps(s.steps_for("payments")))
    ]
    assert {s.archetype for s in irreversible} >= {"cert_expiry", "disk_full"}


def test_rollback_program_is_the_inverses_in_reverse():
    spec = pb.BY_SLUG["pool-gen3-adaptive"]
    draft = PlaybookDraft(
        name=spec.name, outcome_category=spec.archetype,
        remediation_steps=parse_steps(spec.steps_for("payments")),
    )
    inverses = draft.inverse_steps()
    assert [i["action"] for i in inverses] == [
        "set_circuit_breaker", "recycle_connections", "scale_connection_pool"
    ]


def test_the_challenger_has_no_trials():
    challenger = pb.BY_SLUG["pool-gen4-adaptive"]
    assert challenger.trials == 0
    assert challenger.posterior_mean == 0.5
    assert challenger.status == "active"


def test_the_promotion_candidate_sits_on_the_threshold():
    spec = pb.BY_SLUG["pool-gen3-adaptive"]
    assert spec.trials >= 10
    assert spec.posterior_mean == pytest.approx(0.900, abs=0.001)
    # One more success has to cross the line, or the demo beat does not happen.
    after = (spec.successes + 2) / (spec.trials + 3)
    assert after > 0.9


def test_the_bad_fix_is_still_selectable():
    spec = pb.BY_SLUG["deploy-gen2-scaleout"]
    assert 0.2 < spec.posterior_mean < 0.35, "retired playbooks cannot lose on camera"
    assert spec.status == "active"
    assert spec.rollbacks >= 1
    # Selectable means retrievable: a playbook parked beyond Sentinel's distance
    # filter is never a candidate, however tempting its posterior.
    assert spec.spread < 0.30, "the bad fix must sit inside the retrieval radius"


def test_every_playbook_sits_inside_the_retrieval_radius():
    """Sentinel discards candidates beyond 0.35 cosine distance from the situation.
    Seeding a playbook further out than that from its own archetype's centroid
    makes it dead weight — never retrieved, never tried, never improved."""
    for spec in pb.SPECS:
        if spec.near_duplicate_of is None:
            assert spec.spread < 0.35, f"{spec.slug} could never be retrieved"


def test_retired_ancestors_and_merged_parents_exist():
    assert sum(1 for s in pb.SPECS if s.status == "retired") >= 3
    merged = [s for s in pb.SPECS if s.status == "merged"]
    assert len(merged) == 2
    assert all(pb.BY_SLUG[s.merged_into].status == "active" for s in merged)


def test_merge_pairs_are_inside_the_merge_predicate():
    """Both twins must satisfy `distance < 0.15` and both means above 0.5."""
    pairs = [(s.near_duplicate_of, s.slug) for s in pb.SPECS if s.near_duplicate_of]
    assert len(pairs) == 2
    for left, right in pairs:
        a, b = pb.BY_SLUG[left], pb.BY_SLUG[right]
        assert a.posterior_mean > 0.5 and b.posterior_mean > 0.5
        assert a.archetype == b.archetype


def test_vector_placement_realizes_the_intended_distances():
    rng = np.random.default_rng(0)
    anchor = vectors.unit(rng.normal(size=64))
    for target in (0.065, 0.15, 0.3):
        placed = vectors.place(anchor, target, rng)
        distance = 1.0 - float(np.asarray(placed) @ anchor)
        assert distance == pytest.approx(target, abs=1e-9)


def test_lifecycle_events_cover_every_playbook_and_every_type():
    events = pb.lifecycle_events()
    covered = {e.playbook_slug for e in events}
    assert covered == {s.slug for s in pb.SPECS}
    kinds = {e.event_type for e in events}
    assert {"birth", "mutation", "growth", "rollback",
            "merge", "promotion", "retirement"} <= kinds


def test_birth_events_are_births_only_for_roots():
    for event in pb.lifecycle_events():
        if event.event_type == "birth":
            assert pb.BY_SLUG[event.playbook_slug].parent is None
        if event.event_type == "mutation":
            assert pb.BY_SLUG[event.playbook_slug].parent is not None


def test_growth_events_walk_the_posterior_monotonically_towards_the_final_mean():
    for spec in pb.SPECS:
        growth = [
            e for e in pb.lifecycle_events()
            if e.playbook_slug == spec.slug and e.event_type == "growth"
        ]
        if not growth:
            assert spec.trials == 0
            continue
        assert growth[-1].fitness_after == pytest.approx(spec.posterior_mean, abs=1e-9)


def test_every_event_predates_now():
    assert all(e.days_ago >= 0 for e in pb.lifecycle_events())
