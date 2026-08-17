"""Live-mode generator: the demo's load-ramp button, as an HTTP service.

Run it next to the stack and it does two things: drives the simulated fleet
forward on a wall-clock ticker, and writes each service's trailing window into
`telemetry_embeddings` — the sensory tier Oracle reads. Nothing downstream knows
the telemetry is synthetic; it arrives through exactly the ingestion path real
telemetry would.

    make live                                   # serve on :8000, ingest every 3s
    curl -XPOST :8000/ramp -d '{"service":"payments",
                                "archetype":"connection_pool_exhaustion",
                                "speed":1}'

At `speed=1` a ramp reaches failure onset in 36 ticks, so with the default
3-second tick a precursor window plays out in under two minutes of demo. Raise
`speed` to compress it further, at the cost of window resolution; change
`GENERATOR_TICK_SECONDS` to change the wall-clock pace without touching the
shape of the window Oracle sees.

Ingestion is best-effort by design. If the database is unreachable the ticker
keeps simulating and logs the failure, because a dead connection should not
freeze the fleet mid-demo.
"""
from __future__ import annotations

import asyncio
import contextlib
import os
from datetime import UTC, datetime

from pydantic import BaseModel, Field

from nexus_common import db, embeddings, log

from .archetypes import ARCHETYPES, SERVICES
from .fleet import FleetSimulator

logger = log.get_logger("generator")

TICK_SECONDS = float(os.environ.get("GENERATOR_TICK_SECONDS", "3"))
INGEST = os.environ.get("GENERATOR_INGEST", "1") not in ("0", "false", "False")


def ingest_window(sim: FleetSimulator, service: str) -> str | None:
    """Embed a service's trailing window and write it to the sensory tier.

    Returns the new row id, or None when the window is too short to embed.
    """
    if not sim.ready(service):
        return None
    state = sim.services[service]
    vector = embeddings.embed(sim.window_text(service))
    rows = db.query(
        """
        INSERT INTO telemetry_embeddings
            (service_name, region, metric_type, embedding, raw_metrics, captured_at)
        VALUES (%s, %s, %s, %s::VECTOR, %s::JSONB, %s)
        RETURNING id
        """,
        (
            service,
            state.region,
            "trajectory_window",
            embeddings.to_vector_literal(vector),
            _json(sim.window_digest(service)),
            datetime.now(UTC),
        ),
    )
    return str(rows[0][0])


def _json(value: object) -> str:
    import json

    return json.dumps(value, default=str)


class RampRequest(BaseModel):
    service: str
    archetype: str
    speed: float = Field(default=1.0, gt=0, le=12)


class ActionRequest(BaseModel):
    service: str
    action: str
    params: dict = Field(default_factory=dict)
    # Set by Guardian when rolling back: the key of the step being undone.
    revert_of: str | None = None


def build_app():  # noqa: C901 — a flat router; splitting it would only scatter it
    from fastapi import FastAPI, HTTPException

    sim = FleetSimulator()
    app = FastAPI(title="NEXUS synthetic fleet", version="2.1")

    async def ticker() -> None:
        while True:
            await asyncio.sleep(TICK_SECONDS)
            try:
                sim.tick()
                if INGEST:
                    for service in sim.services:
                        ingest_window(sim, service)
            except Exception as e:  # a broken tick must not kill the fleet
                logger.warning("tick failed", error=str(e))

    @app.on_event("startup")
    async def _start() -> None:
        app.state.ticker = asyncio.create_task(ticker())
        logger.info("fleet started", tick_seconds=TICK_SECONDS, ingest=INGEST,
                    provider=embeddings.provider_name())

    @app.on_event("shutdown")
    async def _stop() -> None:
        app.state.ticker.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await app.state.ticker

    @app.get("/health")
    def health() -> dict:
        return {"ok": True, "services": list(SERVICES),
                "archetypes": [a.key for a in ARCHETYPES],
                "embedding_provider": embeddings.provider_name()}

    @app.get("/fleet")
    def fleet() -> dict:
        return {"services": sim.snapshot()}

    @app.get("/telemetry/{service}")
    def telemetry(service: str) -> dict:
        try:
            return {
                "service": service,
                "ready": sim.ready(service),
                "window": sim.window(service),
                "digest": sim.window_digest(service),
                "canonical_text": sim.window_text(service) if sim.ready(service) else None,
            }
        except (KeyError, ValueError) as e:
            raise HTTPException(status_code=404, detail=str(e)) from e

    @app.post("/ramp")
    @app.post("/induce_ramp")
    def ramp(req: RampRequest) -> dict:
        """start_ramp(service, archetype, speed) — the demo's load-ramp button."""
        try:
            state = sim.start_ramp(req.service, req.archetype, req.speed)
        except KeyError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        logger.info("ramp started", service=req.service, archetype=req.archetype,
                    speed=req.speed)
        return {"service": state.name, "archetype": state.archetype,
                "speed": state.speed, "status": state.status}

    @app.post("/stop/{service}")
    def stop(service: str) -> dict:
        try:
            state = sim.stop_ramp(service)
        except KeyError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        return {"service": state.name, "status": state.status}

    @app.post("/action")
    def action(req: ActionRequest) -> dict:
        """The seam Guardian executes remediation steps against."""
        try:
            return sim.apply_action(req.service, req.action, req.params, req.revert_of)
        except KeyError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e

    @app.post("/tick")
    def tick(service: str | None = None) -> dict:
        """Advance the simulation by hand — useful when scripting a rehearsal."""
        sim.tick(service)
        return {"services": sim.snapshot()}

    return app


def main() -> None:
    import uvicorn

    uvicorn.run(build_app(), host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))


if __name__ == "__main__":
    main()
