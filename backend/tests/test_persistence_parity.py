"""
Parity: the DB-backed path must produce the same engine result as the stateless
POST /run path. We prove it without a live database by round-tripping the profile
through the JSONB envelope (build_envelope → df_from_profile) and running the same
shared engine core (api.main.execute_run) on both frames.
"""
import json
import sys
import types
from pathlib import Path

_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import pytest

from api.main import RunRequest, execute_run, _DEFAULT_PROFILE, _load_profile
from services.profile_store import build_envelope, df_from_profile


def _fake_profile_from_df(df, dt_hours, pv_np, load_np):
    """Build the JSONB envelope and wrap it in a Profile-shaped object (no DB)."""
    env = build_envelope(df, dt_hours=dt_hours, name="test",
                         load_nameplate_mw=load_np, pv_nameplate_mw=pv_np)
    return types.SimpleNamespace(**env)


@pytest.mark.parametrize("scenario_id", ["S32_SUB_GC_CURVE", "S00_FIXED_DESIGN_EVAL"])
def test_db_path_matches_stateless(scenario_id):
    df, load_np, pv_np, dt_hours = _load_profile(str(_DEFAULT_PROFILE))
    req = RunRequest(scenario_id=scenario_id, load_peak_mw=24.0, bess_mw=10.0, bess_mwh=40.0)

    # Stateless path (what POST /run returns).
    out_direct = execute_run(req, df.copy(), pv_np, dt_hours)

    # DB path: profile → JSONB envelope → reconstructed frame → same engine core.
    prof = _fake_profile_from_df(df, dt_hours, pv_np, load_np)
    df2, dt2 = df_from_profile(prof)
    out_db = execute_run(req, df2, prof.pv_nameplate_mw or 0.0, dt2)

    # Design + KPIs + curve table must be identical (timestamps/flows aside).
    assert out_direct["design"] == out_db["design"]
    assert out_direct["kpis"] == out_db["kpis"]
    assert out_direct["table"] == out_db["table"]
    assert out_direct["feasible"] == out_db["feasible"]

    # The whole result must be JSON-serialisable (guards numpy/NaN leaks into JSONB).
    json.dumps(out_db)


def test_profile_envelope_roundtrip_preserves_series():
    df, load_np, pv_np, dt_hours = _load_profile(str(_DEFAULT_PROFILE))
    prof = _fake_profile_from_df(df, dt_hours, pv_np, load_np)
    df2, dt2 = df_from_profile(prof)

    assert dt2 == dt_hours
    assert len(df2) == len(df)
    # load_mw and pv_pu must round-trip within float tolerance.
    assert (df2["load_mw"] - df["load_mw"]).abs().max() < 1e-9
    assert (df2["pv_pu"] - df["pv_pu"]).abs().max() < 1e-9
