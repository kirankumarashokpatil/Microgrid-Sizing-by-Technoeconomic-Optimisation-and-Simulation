"""
Run orchestrator — the async lifecycle around one engine call.

Launched as a FastAPI BackgroundTask with a ``run_id``. It marks the run running,
reconstructs the request + profiles DataFrame from the DB, executes the SHARED
engine core (``api.main.execute_run`` — the exact same function the stateless
``POST /run`` uses, so results are identical), then persists the result and (when
present) the per-timestep flows, and flips the run to completed / failed.

The Pyomo/HiGHS solve is synchronous and CPU-bound, so it runs in a worker thread
(``asyncio.to_thread``) to keep the event loop free. Its stdout — which contains
unicode progress glyphs — is captured so it can't crash on a non-UTF-8 Windows
console.
"""
from __future__ import annotations

import asyncio
import contextlib
import io
from datetime import datetime, timezone
from pathlib import Path

from db.models import (
    RUN_COMPLETED, RUN_FAILED, RUN_RUNNING, Profile, Run, RunFlows,
)
from db.session import SessionLocal
from services.profile_store import df_from_profile


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def run_optimization(run_id: int) -> None:
    """Full run lifecycle. Never raises — failures are recorded on the run row."""
    async with SessionLocal() as session:
        run = await session.get(Run, run_id)
        if run is None:
            return
        try:
            run.status = RUN_RUNNING
            run.started_at = _now()
            run.message = None
            await session.commit()

            # Lazy import avoids a circular dependency (api.main imports this module).
            from api.main import RunRequest, execute_run, _DEFAULT_PROFILE, _load_profile

            req = RunRequest(**run.request)

            # Reconstruct the profiles frame: from the stored profile row when one is
            # attached, else the bundled default dataset.
            wind_np = 0.0
            if run.profile_id is not None:
                profile = await session.get(Profile, run.profile_id)
                if profile is None:
                    raise ValueError(f"Profile {run.profile_id} not found for run {run_id}")
                df, dt_hours = df_from_profile(profile)
                pv_np = profile.pv_nameplate_mw or 0.0
                # Area-fit wind nameplate lives in meta (no dedicated column).
                wind_np = float((profile.meta or {}).get("wind_nameplate_mw") or 0.0)
            else:
                path = req.profile_path or str(_DEFAULT_PROFILE)
                if not Path(path).exists():
                    raise ValueError(f"Profile file not found: {path}")
                df, _load_np, pv_np, dt_hours = _load_profile(path)

            # Execute the shared engine core off the event loop. stdout captured so
            # unicode progress prints can't crash a cp1252 console.
            def _run() -> dict:
                with contextlib.redirect_stdout(io.StringIO()):
                    return execute_run(req, df, pv_np, dt_hours, wind_np=wind_np)

            out = await asyncio.to_thread(_run)

            # Split the big per-timestep flows table off into its own row so the hot
            # runs.result summary stays small. Leave a light marker behind.
            flows = out.pop("flows", None)
            if flows and flows.get("rows"):
                session.add(RunFlows(
                    run_id=run_id,
                    n_steps=flows.get("n_total") or len(flows["rows"]),
                    series=flows,
                ))
                out["flows_stored"] = True
            else:
                out["flows_stored"] = False

            run.result = out
            run.dt_hours = out.get("dt_hours", dt_hours)
            run.status = RUN_COMPLETED
            run.finished_at = _now()
            await session.commit()

        except Exception as exc:  # noqa: BLE001 — record any failure on the run row
            await session.rollback()
            run = await session.get(Run, run_id)
            if run is not None:
                run.status = RUN_FAILED
                run.message = str(exc)[:2000]
                run.finished_at = _now()
                await session.commit()
