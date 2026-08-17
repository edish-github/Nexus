"""The remediation step schema — the contract every downstream component shares.

Guardian executes these, Chronicler mutates them, Bedrock is required to emit
them, and the seed generator authors them by hand. Defining the shape in one
place is what keeps those four in agreement.

A step is `{action, target, params, inverse}`:

    {"action": "scale_connection_pool",
     "target": "payments",
     "params": {"max_size": 200},
     "inverse": {"action": "scale_connection_pool", "params": {"max_size": 50}}}

`inverse` is what Guardian replays, in reverse order, when the verification
window shows degradation. A step with no inverse is irreversible, which forces
its playbook into the approval tier rather than the auto tier.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

# The action vocabulary the simulated fleet's control API understands. Guardian
# dispatches on these; anything else is rejected before it can be executed.
ACTIONS: tuple[str, ...] = (
    "scale_connection_pool",
    "recycle_connections",
    "restart_worker",
    "rolling_restart",
    "set_circuit_breaker",
    "flush_cache",
    "warm_cache",
    "set_cache_ttl",
    "rotate_certificate",
    "prune_disk",
    "extend_volume",
    "rollback_deploy",
    "pin_deploy_version",
    "scale_thread_pool",
    "shed_load",
    "set_dns_ttl",
    "failover_resolver",
    "set_retry_budget",
    "scale_replicas",
    "throttle_ingress",
)

# Actions that cannot be undone by replaying an inverse. A playbook containing
# any of these is not `reversible` and never runs on the auto tier.
IRREVERSIBLE_ACTIONS: frozenset[str] = frozenset({"rotate_certificate", "prune_disk"})


class StepInverse(BaseModel):
    """The compensating action for a step. No `target`: it inherits the step's."""

    model_config = ConfigDict(extra="forbid")

    action: str
    params: dict[str, Any] = Field(default_factory=dict)

    @field_validator("action")
    @classmethod
    def _known_action(cls, v: str) -> str:
        if v not in ACTIONS:
            raise ValueError(f"unknown action {v!r}; expected one of {', '.join(ACTIONS)}")
        return v


class RemediationStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: str
    target: str
    params: dict[str, Any] = Field(default_factory=dict)
    inverse: StepInverse | None = None

    @field_validator("action")
    @classmethod
    def _known_action(cls, v: str) -> str:
        if v not in ACTIONS:
            raise ValueError(f"unknown action {v!r}; expected one of {', '.join(ACTIONS)}")
        return v

    @property
    def reversible(self) -> bool:
        return self.inverse is not None and self.action not in IRREVERSIBLE_ACTIONS


class PlaybookDraft(BaseModel):
    """A playbook proposed by Bedrock (novel-incident birth, mutation, merge).

    Validated before it reaches the database: a malformed genome is stillborn,
    logged, and never executed.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=3, max_length=120)
    outcome_category: str = Field(min_length=3, max_length=64)
    rationale: str = ""
    remediation_steps: list[RemediationStep] = Field(min_length=1, max_length=12)

    @property
    def reversible(self) -> bool:
        return all(s.reversible for s in self.remediation_steps)

    def inverse_steps(self) -> list[dict[str, Any]]:
        """The rollback program: every inverse, in reverse execution order."""
        out: list[dict[str, Any]] = []
        for step in reversed(self.remediation_steps):
            if step.inverse is None:
                continue
            out.append(
                {"action": step.inverse.action, "target": step.target,
                 "params": step.inverse.params}
            )
        return out


# Stored playbooks carry this in place of a service name. A playbook is
# procedural memory — "how to relieve pool exhaustion" — not "how to fix
# payments", so the service it acts on comes from the prediction being handled,
# and is bound at execution time.
SERVICE_PLACEHOLDER = "{service}"


def parse_steps(raw: Any, *, service: str | None = None) -> list[RemediationStep]:
    """Validate a `remediation_steps` JSONB value read back from the database.

    With `service`, the placeholder target is bound to it, which is what turns a
    stored playbook into an executable one.
    """
    if not isinstance(raw, list):
        raise ValueError(f"remediation_steps must be a JSON array, got {type(raw).__name__}")
    steps = [RemediationStep.model_validate(item) for item in raw]
    return bind_target(steps, service) if service else steps


def bind_target(steps: list[RemediationStep], service: str) -> list[RemediationStep]:
    """Resolve `{service}` targets against the service actually being remediated."""
    return [
        step.model_copy(update={"target": service})
        if step.target == SERVICE_PLACEHOLDER else step
        for step in steps
    ]


def inverse_program(steps: list[RemediationStep]) -> list[dict[str, Any]]:
    """Build the rollback program for an already-validated step list."""
    return PlaybookDraft(
        name="rollback", outcome_category="rollback", remediation_steps=steps
    ).inverse_steps()
