"""
Seed the ``scenarios`` read-model from the code registry.

The registry in ``core.scenarios`` is the single source of truth. This mirrors it
into the DB so the API can serve ``/scenarios`` from Postgres and FK-validate a
run's ``scenario_id``. Idempotent upsert — safe to run on every startup.
"""
from __future__ import annotations

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from core.scenarios import REGISTRY
from db.models import Scenario


async def seed_scenarios(session: AsyncSession) -> int:
    """Upsert every registry row into ``scenarios``. Returns the row count."""
    rows = [
        {
            "scenario_id": s.id,
            "name": s.name,
            "stage": s.stage,
            "topology": s.topology,
            "family": s.family,
            "answer_mode": s.answer_mode,
            "status": s.status,
            "needs": s.needs or None,
        }
        for s in REGISTRY
    ]
    if not rows:
        return 0

    stmt = pg_insert(Scenario).values(rows)
    stmt = stmt.on_conflict_do_update(
        index_elements=[Scenario.scenario_id],
        set_={
            "name": stmt.excluded.name,
            "stage": stmt.excluded.stage,
            "topology": stmt.excluded.topology,
            "family": stmt.excluded.family,
            "answer_mode": stmt.excluded.answer_mode,
            "status": stmt.excluded.status,
            "needs": stmt.excluded.needs,
        },
    )
    await session.execute(stmt)
    await session.commit()
    return len(rows)
