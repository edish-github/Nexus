"""Thirty seed playbooks with a family tree, and the evolution log that explains it.

The seeded population is not decoration — the demo's staged beats are properties
of this data, so they are declared here rather than arranged by hand later:

* **A challenger that can win.** `pool-gen4-adaptive` has zero trials, so its
  Beta(1,1) posterior is flat and Thompson sampling gives it a real chance
  against a 0.90 incumbent. That is exploration made visible.
* **A bad-fix lane.** `deploy-gen2-scaleout` treats a latency regression by adding
  replicas instead of rolling back. Its posterior sits just above the retirement
  line, so it is still selectable — and still wrong. It is the rollback beat.
* **Two merge-ready pairs.** `heap-gen2-drain`/`heap-gen2-drain-b` and
  `cache-gen3-jitter`/`cache-gen3-jitter-b` sit inside 0.15 cosine distance of
  one another with both posterior means above 0.5, which is exactly Chronicler's
  merge predicate.
* **A promotion on the cusp.** `pool-gen3-adaptive` is at 17/1 — posterior mean
  0.900, trials 18. One more success crosses `> 0.9 and trials >= 10` and it
  promotes to the GLOBAL institutional table on camera.
* **Ancestors that died.** Three generation-1 playbooks are retired, so the
  genealogy tree renders with grey nodes and the family has visible depth.

Fitness is never stored. Every posterior mean quoted above is
`(success_count + 1) / (success_count + failure_count + 2)`, computed from the
counters at read time.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# --------------------------------------------------------------------------- #
# Step programs — one canonical remediation per archetype, plus the parameter
# tweaks that distinguish each descendant from its parent.
# --------------------------------------------------------------------------- #

def _step(action: str, target: str, params: dict, inverse: dict | None) -> dict:
    step: dict = {"action": action, "target": target, "params": params}
    if inverse is not None:
        step["inverse"] = inverse
    return step


def pool_program(*, max_size: int, idle_timeout_s: int, breaker: float | None = None) -> list[dict]:
    steps = [
        _step("scale_connection_pool", "{service}", {"max_size": max_size},
              {"action": "scale_connection_pool", "params": {"max_size": 50}}),
        _step("recycle_connections", "{service}", {"idle_timeout_s": idle_timeout_s},
              {"action": "recycle_connections", "params": {"idle_timeout_s": 300}}),
    ]
    if breaker is not None:
        steps.append(
            _step("set_circuit_breaker", "{service}",
                  {"error_threshold": breaker, "half_open_after_s": 30},
                  {"action": "set_circuit_breaker",
                   "params": {"error_threshold": 0.5, "half_open_after_s": 120}})
        )
    return steps


def heap_program(*, replicas: int, drain_seconds: int) -> list[dict]:
    return [
        _step("scale_replicas", "{service}", {"count": replicas},
              {"action": "scale_replicas", "params": {"count": 4}}),
        # A rolling restart is its own inverse: the compensating action is to
        # return the batch to a clean process, which is the same operation.
        _step("rolling_restart", "{service}", {"batch": 1, "drain_seconds": drain_seconds},
              {"action": "rolling_restart", "params": {"batch": 1, "drain_seconds": 30}}),
    ]


def cache_program(*, ttl_seconds: int, jitter_seconds: int, throttle_rps: int | None = None
                  ) -> list[dict]:
    steps = [
        _step("set_cache_ttl", "{service}",
              {"ttl_seconds": ttl_seconds, "jitter_seconds": jitter_seconds},
              {"action": "set_cache_ttl", "params": {"ttl_seconds": 60, "jitter_seconds": 0}}),
        _step("warm_cache", "{service}", {"key_set": "top_1000"},
              {"action": "flush_cache", "params": {"scope": "warmed"}}),
    ]
    if throttle_rps is not None:
        steps.append(
            _step("throttle_ingress", "{service}", {"rps": throttle_rps},
                  {"action": "throttle_ingress", "params": {"rps": 5000}})
        )
    return steps


def cert_program(*, shed_pct: int | None = None, retry_ratio: float | None = None) -> list[dict]:
    steps: list[dict] = []
    if shed_pct is not None:
        steps.append(
            _step("shed_load", "{service}", {"drop_pct": shed_pct},
                  {"action": "shed_load", "params": {"drop_pct": 0}})
        )
    # Certificate rotation cannot be undone by replaying an inverse, which is
    # what pins this whole family to the approval tier however confident Oracle is.
    steps.append(_step("rotate_certificate", "{service}", {"issuer": "internal-ca"}, None))
    if retry_ratio is not None:
        steps.append(
            _step("set_retry_budget", "{service}", {"ratio": retry_ratio},
                  {"action": "set_retry_budget", "params": {"ratio": 0.3}})
        )
    return steps


def disk_program(*, target_gib: int | None, prune_days: int | None) -> list[dict]:
    steps: list[dict] = []
    if target_gib is not None:
        steps.append(
            _step("extend_volume", "{service}", {"target_gib": target_gib},
                  {"action": "extend_volume", "params": {"target_gib": 500}})
        )
    if prune_days is not None:
        # Deleting data is irreversible: no inverse, so the playbook needs a human.
        steps.append(
            _step("prune_disk", "{service}",
                  {"paths": ["/var/log", "/tmp"], "older_than_days": prune_days}, None)
        )
    return steps


def deploy_program(*, pin: bool) -> list[dict]:
    steps = [
        _step("rollback_deploy", "{service}", {"to": "previous"},
              {"action": "pin_deploy_version", "params": {"version": "candidate"}}),
    ]
    if pin:
        steps.append(
            _step("pin_deploy_version", "{service}", {"version": "last_known_good"},
                  {"action": "pin_deploy_version", "params": {"version": "auto"}})
        )
    return steps


def deploy_scaleout_program() -> list[dict]:
    """The bad fix: throw replicas at a code regression. Treats the symptom, adds
    load to a slower binary, and reliably makes p99 worse. Kept alive on purpose."""
    return [
        _step("scale_replicas", "{service}", {"count": 12},
              {"action": "scale_replicas", "params": {"count": 4}}),
        _step("throttle_ingress", "{service}", {"rps": 3000},
              {"action": "throttle_ingress", "params": {"rps": 5000}}),
    ]


def threadpool_program(*, size: int, drop_pct: int, retry_ratio: float | None = None
                       ) -> list[dict]:
    steps = [
        _step("scale_thread_pool", "{service}", {"size": size},
              {"action": "scale_thread_pool", "params": {"size": 48}}),
        _step("shed_load", "{service}", {"drop_pct": drop_pct},
              {"action": "shed_load", "params": {"drop_pct": 0}}),
    ]
    if retry_ratio is not None:
        steps.append(
            _step("set_retry_budget", "{service}", {"ratio": retry_ratio},
                  {"action": "set_retry_budget", "params": {"ratio": 0.3}})
        )
    return steps


def dns_program(*, ttl_seconds: int, failover: bool, retry_ratio: float) -> list[dict]:
    steps = [
        _step("set_dns_ttl", "{service}", {"ttl_seconds": ttl_seconds},
              {"action": "set_dns_ttl", "params": {"ttl_seconds": 300}}),
        _step("set_retry_budget", "{service}", {"ratio": retry_ratio},
              {"action": "set_retry_budget", "params": {"ratio": 0.25}}),
    ]
    if failover:
        steps.insert(
            1,
            _step("failover_resolver", "{service}", {"to": "secondary"},
                  {"action": "failover_resolver", "params": {"to": "primary"}}),
        )
    return steps


# --------------------------------------------------------------------------- #
# The population
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class PlaybookSpec:
    slug: str
    name: str
    archetype: str
    generation: int
    parent: str | None
    steps: list[dict]
    successes: int
    failures: int
    status: str = "active"              # active | retired | merged
    memory_tier: str = "operational"    # experimental | operational | institutional | retired
    # Intended cosine distance from the archetype's precursor centroid. Siblings
    # sit far enough apart that they are distinct candidates; a near-duplicate is
    # placed relative to its twin instead.
    spread: float = 0.24
    near_duplicate_of: str | None = None
    merged_into: str | None = None
    born_days_ago: float = 70.0
    rollbacks: int = 0
    note: str = ""

    @property
    def posterior_mean(self) -> float:
        return (self.successes + 1) / (self.successes + self.failures + 2)

    @property
    def trials(self) -> int:
        return self.successes + self.failures

    def steps_template(self) -> list[dict]:
        """The steps as stored: targets left as the `{service}` placeholder."""
        return [dict(step) for step in self.steps]

    def steps_for(self, service: str) -> list[dict]:
        """Bind the `{service}` placeholder in every step target."""
        out = []
        for step in self.steps:
            bound = dict(step)
            bound["target"] = step["target"].format(service=service)
            out.append(bound)
        return out


SPECS: tuple[PlaybookSpec, ...] = (
    # -- connection pool exhaustion: the deepest family, four generations ------
    PlaybookSpec(
        "pool-gen1-static", "Static pool bump", "connection_pool_exhaustion", 1, None,
        pool_program(max_size=80, idle_timeout_s=120), successes=6, failures=11,
        status="retired", memory_tier="retired", spread=0.31, born_days_ago=86,
        note="First attempt: a fixed bump that under-provisioned under real load.",
    ),
    PlaybookSpec(
        "pool-gen2-drain", "Pool bump with connection drain", "connection_pool_exhaustion", 2,
        "pool-gen1-static", pool_program(max_size=150, idle_timeout_s=60),
        successes=14, failures=6, spread=0.26, born_days_ago=71,
        note="Mutation after repeated failures: recycle idle connections as well as widen.",
    ),
    PlaybookSpec(
        "pool-gen3-adaptive", "Adaptive pool with breaker", "connection_pool_exhaustion", 3,
        "pool-gen2-drain", pool_program(max_size=200, idle_timeout_s=30, breaker=0.10),
        successes=17, failures=1, spread=0.18, born_days_ago=44,
        note="Promotion candidate: posterior mean 0.900 at 18 trials, one success from GLOBAL.",
    ),
    PlaybookSpec(
        "pool-gen3-breaker-first", "Breaker-first pool relief", "connection_pool_exhaustion", 3,
        "pool-gen2-drain", pool_program(max_size=160, idle_timeout_s=45, breaker=0.06),
        successes=6, failures=4, spread=0.29, born_days_ago=38,
        note="Sibling branch: trips the breaker earlier, sheds more traffic than needed.",
    ),
    PlaybookSpec(
        "pool-gen4-adaptive", "Predictive pool pre-scale", "connection_pool_exhaustion", 4,
        "pool-gen3-adaptive", pool_program(max_size=240, idle_timeout_s=25, breaker=0.12),
        successes=0, failures=0, memory_tier="experimental", spread=0.21, born_days_ago=3,
        note="The challenger: zero trials, flat Beta(1,1) prior, everything to prove.",
    ),

    # -- memory leak: includes merge-ready pair A ------------------------------
    PlaybookSpec(
        "heap-gen1-restart", "Blind rolling restart", "memory_leak_oom", 1, None,
        heap_program(replicas=4, drain_seconds=0), successes=4, failures=9,
        status="retired", memory_tier="retired", spread=0.33, born_days_ago=88,
        note="Restarted without draining; dropped in-flight requests every time.",
    ),
    PlaybookSpec(
        "heap-gen2-drain", "Drain-then-restart", "memory_leak_oom", 2, "heap-gen1-restart",
        heap_program(replicas=6, drain_seconds=30), successes=11, failures=4,
        spread=0.22, born_days_ago=64,
        note="Merge pair A: converged on the same fix as heap-gen2-drain-b.",
    ),
    PlaybookSpec(
        "heap-gen2-drain-b", "Graceful recycle with headroom", "memory_leak_oom", 2,
        "heap-gen1-restart", heap_program(replicas=6, drain_seconds=45),
        successes=9, failures=3, near_duplicate_of="heap-gen2-drain", born_days_ago=59,
        note="Merge pair A: independently evolved, 0.06 cosine from its twin.",
    ),
    PlaybookSpec(
        "heap-gen3-headroom", "Pre-emptive headroom scale", "memory_leak_oom", 3,
        "heap-gen2-drain", heap_program(replicas=8, drain_seconds=30),
        successes=24, failures=1, memory_tier="institutional", spread=0.17,
        born_days_ago=41,
        note="Already promoted: 25 trials, posterior mean 0.926, lives in the GLOBAL table.",
    ),

    # -- cache stampede: includes merge-ready pair B ---------------------------
    PlaybookSpec(
        "cache-gen1-flush", "Flush and hope", "cache_stampede", 1, None,
        cache_program(ttl_seconds=60, jitter_seconds=0), successes=3, failures=10,
        status="retired", memory_tier="retired", spread=0.34, born_days_ago=84,
        note="Flushing during a stampede is pouring petrol on it. Retired for cause.",
    ),
    PlaybookSpec(
        "cache-gen2-ttl", "TTL extension with warm", "cache_stampede", 2, "cache-gen1-flush",
        cache_program(ttl_seconds=240, jitter_seconds=30), successes=12, failures=5,
        spread=0.27, born_days_ago=66,
    ),
    PlaybookSpec(
        "cache-gen3-jitter", "Jittered TTL with ingress throttle", "cache_stampede", 3,
        "cache-gen2-ttl", cache_program(ttl_seconds=300, jitter_seconds=60, throttle_rps=1200),
        successes=15, failures=3, spread=0.19, born_days_ago=37,
        note="Merge pair B.",
    ),
    PlaybookSpec(
        "cache-gen3-jitter-b", "Staggered expiry with shed", "cache_stampede", 3,
        "cache-gen2-ttl", cache_program(ttl_seconds=320, jitter_seconds=75, throttle_rps=1400),
        successes=10, failures=4, near_duplicate_of="cache-gen3-jitter", born_days_ago=33,
        note="Merge pair B: 0.07 cosine from its twin, both well above 0.5.",
    ),

    # -- certificate expiry: the approval-tier family -------------------------
    PlaybookSpec(
        "cert-gen1-rotate", "Rotate certificate", "cert_expiry", 1, None,
        cert_program(), successes=8, failures=3, spread=0.28, born_days_ago=79,
        note="Irreversible: rotation has no inverse, so this never runs on the auto tier.",
    ),
    PlaybookSpec(
        "cert-gen2-shed-rotate", "Shed then rotate", "cert_expiry", 2, "cert-gen1-rotate",
        cert_program(shed_pct=15), successes=11, failures=2, spread=0.22, born_days_ago=52,
    ),
    PlaybookSpec(
        "cert-gen3-guarded", "Guarded rotation with retry budget", "cert_expiry", 3,
        "cert-gen2-shed-rotate", cert_program(shed_pct=10, retry_ratio=0.08),
        successes=7, failures=1, spread=0.19, born_days_ago=26,
    ),

    # -- disk exhaustion ------------------------------------------------------
    PlaybookSpec(
        "disk-gen1-prune", "Prune old logs", "disk_full", 1, None,
        disk_program(target_gib=None, prune_days=3), successes=9, failures=6,
        spread=0.30, born_days_ago=81,
        note="Irreversible: deleted data does not come back, so approval tier.",
    ),
    PlaybookSpec(
        "disk-gen2-extend", "Extend volume", "disk_full", 2, "disk-gen1-prune",
        disk_program(target_gib=800, prune_days=None), successes=13, failures=3,
        spread=0.24, born_days_ago=57,
        note="Fully reversible, so this is the branch that can act unattended.",
    ),
    PlaybookSpec(
        "disk-gen3-extend-prune", "Extend then prune", "disk_full", 3, "disk-gen2-extend",
        disk_program(target_gib=1000, prune_days=7), successes=8, failures=2,
        spread=0.20, born_days_ago=29,
    ),

    # -- bad deploy: the rollback lane ---------------------------------------
    PlaybookSpec(
        "deploy-gen1-rollback", "Roll back deploy", "bad_deploy_latency_regression", 1, None,
        deploy_program(pin=False), successes=15, failures=4, spread=0.26, born_days_ago=83,
    ),
    PlaybookSpec(
        "deploy-gen2-scaleout", "Scale out under regression",
        "bad_deploy_latency_regression", 2, "deploy-gen1-rollback",
        deploy_scaleout_program(), successes=2, failures=5, spread=0.20,
        born_days_ago=48, rollbacks=4,
        note="The bad fix. Posterior mean 0.333 — comfortably above the 0.2 retirement "
             "line, so Thompson sampling still hands it the occasional turn. It fails, "
             "Guardian rolls it back, and Chronicler mutates it. That is demo Moment 3. "
             "Its spread is deliberately tight: a wrong playbook is only dangerous if "
             "it looks applicable, and one parked outside Sentinel's retrieval radius "
             "would never be retrieved and so could never lose on camera. "
             "These exact counters are load-bearing and were measured, not guessed. Two "
             "things pull against each other: retirement headroom wants more failures, "
             "and being *selectable at all* wants a wide posterior — and trials narrow a "
             "Beta. At 1/6 the mean was 0.222 with one failure of headroom, so three "
             "rehearsals retired it and the demo silently lost its best beat. Raising it "
             "to 3/10 bought headroom and tightened the posterior until it won one draw "
             "in a hundred thousand — worse, not better. 2/5 is the corner: six failures "
             "of headroom, and it takes a competition roughly one time in 1,600.",
    ),
    PlaybookSpec(
        "deploy-gen2-pin", "Roll back and pin", "bad_deploy_latency_regression", 2,
        "deploy-gen1-rollback", deploy_program(pin=True), successes=16, failures=2,
        spread=0.21, born_days_ago=45,
    ),
    PlaybookSpec(
        "deploy-gen3-canary", "Canary-guarded rollback", "bad_deploy_latency_regression", 3,
        "deploy-gen2-pin", deploy_program(pin=True), successes=5, failures=1,
        memory_tier="experimental", spread=0.18, born_days_ago=12,
    ),

    # -- thread pool starvation: the family with a completed historical merge --
    PlaybookSpec(
        "thread-gen1-widen", "Widen thread pool", "thread_pool_starvation", 1, None,
        threadpool_program(size=96, drop_pct=0), successes=7, failures=6,
        spread=0.29, born_days_ago=85,
    ),
    PlaybookSpec(
        "thread-gen2-shed", "Widen and shed", "thread_pool_starvation", 2, "thread-gen1-widen",
        threadpool_program(size=128, drop_pct=10), successes=10, failures=4,
        status="merged", merged_into="thread-gen3-canonical", spread=0.23, born_days_ago=62,
        note="Merged, not deleted: the un-merge answer is that both parents are still here.",
    ),
    PlaybookSpec(
        "thread-gen2-budget", "Widen with retry budget", "thread_pool_starvation", 2,
        "thread-gen1-widen", threadpool_program(size=120, drop_pct=5, retry_ratio=0.1),
        successes=9, failures=5, status="merged", merged_into="thread-gen3-canonical",
        spread=0.25, born_days_ago=60,
    ),
    PlaybookSpec(
        "thread-gen3-canonical", "Canonical starvation relief", "thread_pool_starvation", 3,
        "thread-gen2-shed", threadpool_program(size=128, drop_pct=8, retry_ratio=0.1),
        successes=13, failures=2, spread=0.16, born_days_ago=34,
        note="Synthesized from both merged parents; carries the union of their lineage.",
    ),

    # -- DNS cascade ----------------------------------------------------------
    PlaybookSpec(
        "dns-gen1-ttl", "Shorten DNS TTL", "dns_timeout_cascade", 1, None,
        dns_program(ttl_seconds=120, failover=False, retry_ratio=0.15),
        successes=6, failures=5, spread=0.30, born_days_ago=77,
    ),
    PlaybookSpec(
        "dns-gen2-failover", "Fail over resolver", "dns_timeout_cascade", 2, "dns-gen1-ttl",
        dns_program(ttl_seconds=60, failover=True, retry_ratio=0.08),
        successes=12, failures=3, spread=0.23, born_days_ago=54,
    ),
    PlaybookSpec(
        "dns-gen3-budget", "Resolver failover with tight retry budget", "dns_timeout_cascade",
        3, "dns-gen2-failover", dns_program(ttl_seconds=45, failover=True, retry_ratio=0.05),
        successes=9, failures=2, spread=0.18, born_days_ago=21,
    ),
)

BY_SLUG: dict[str, PlaybookSpec] = {s.slug: s for s in SPECS}


@dataclass(frozen=True)
class LifecycleEvent:
    """One backfilled `evolution_log` row."""

    event_type: str
    playbook_slug: str
    parent_slug: str | None
    fitness_before: float | None
    fitness_after: float | None
    days_ago: float
    details: dict = field(default_factory=dict)


def _mean(successes: int, failures: int) -> float:
    return (successes + 1) / (successes + failures + 2)


def lifecycle_events(max_growth_per_playbook: int = 5) -> list[LifecycleEvent]:
    """Reconstruct the history that produced the seeded population.

    Every playbook gets a birth (or a mutation, if it has a parent). Its trials
    are replayed as a bounded number of growth events with the posterior mean
    before and after each, so the fitness chart on the playbook detail page has a
    real curve. Retirement, promotion, merge and rollback events are emitted for
    the playbooks whose state implies them.
    """
    events: list[LifecycleEvent] = []
    for spec in SPECS:
        birth_type = "mutation" if spec.parent else "birth"
        events.append(
            LifecycleEvent(
                event_type=birth_type,
                playbook_slug=spec.slug,
                parent_slug=spec.parent,
                fitness_before=_mean(BY_SLUG[spec.parent].successes, BY_SLUG[spec.parent].failures)
                if spec.parent
                else None,
                fitness_after=0.5,  # every newborn starts at the Beta(1,1) mean
                days_ago=spec.born_days_ago,
                details={"generation": spec.generation, "note": spec.note} if spec.note
                else {"generation": spec.generation},
            )
        )

        # Replay the trials as growth events, bucketed so a 25-trial playbook
        # does not produce 25 rows.
        trials = spec.trials
        if trials:
            buckets = min(max_growth_per_playbook, trials)
            s_done = f_done = 0
            for b in range(buckets):
                before = _mean(s_done, f_done)
                s_target = round(spec.successes * (b + 1) / buckets)
                f_target = round(spec.failures * (b + 1) / buckets)
                gained_s, gained_f = s_target - s_done, f_target - f_done
                s_done, f_done = s_target, f_target
                elapsed = spec.born_days_ago * (1 - (b + 1) / (buckets + 1))
                events.append(
                    LifecycleEvent(
                        event_type="growth",
                        playbook_slug=spec.slug,
                        parent_slug=None,
                        fitness_before=before,
                        fitness_after=_mean(s_done, f_done),
                        days_ago=elapsed,
                        details={"successes": gained_s, "failures": gained_f,
                                 "cumulative_trials": s_done + f_done},
                    )
                )

        for i in range(spec.rollbacks):
            events.append(
                LifecycleEvent(
                    event_type="rollback",
                    playbook_slug=spec.slug,
                    parent_slug=None,
                    fitness_before=spec.posterior_mean,
                    fitness_after=spec.posterior_mean,
                    days_ago=spec.born_days_ago * (0.6 - 0.1 * i),
                    details={"reason": "verification window showed degradation",
                             "inverse_steps_executed": True},
                )
            )

        if spec.status == "retired":
            events.append(
                LifecycleEvent(
                    "retirement", spec.slug, None, spec.posterior_mean, spec.posterior_mean,
                    days_ago=spec.born_days_ago * 0.25,
                    details={"reason": "posterior mean below 0.2 after 5+ trials",
                             "trials": spec.trials},
                )
            )
        if spec.memory_tier == "institutional":
            events.append(
                LifecycleEvent(
                    "promotion", spec.slug, None, spec.posterior_mean, spec.posterior_mean,
                    days_ago=spec.born_days_ago * 0.2,
                    details={"reason": "posterior mean > 0.9 with 10+ trials",
                             "trials": spec.trials, "target": "institutional_playbooks"},
                )
            )
        if spec.merged_into:
            # Recorded against the canonical child, with the absorbed parent in
            # parent_id — the same shape as a mutation, so the genealogy tree
            # draws a merge edge from each parent into the child.
            child = BY_SLUG[spec.merged_into]
            events.append(
                LifecycleEvent(
                    "merge", child.slug, spec.slug, spec.posterior_mean, child.posterior_mean,
                    days_ago=spec.born_days_ago * 0.5,
                    details={"reason": "cosine distance < 0.15 with both posteriors > 0.5",
                             "absorbed_parent": spec.slug, "parent_status": "merged"},
                )
            )

    events.sort(key=lambda e: -e.days_ago)
    return events
