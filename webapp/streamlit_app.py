"""
Streamlit front end — "the dining room"
=======================================
This screen does NOT import the optimiser. It only talks to the FastAPI backend
over HTTP (the kitchen window). That is the whole point of the split: this file
could be deleted and rewritten in React tomorrow, and the backend would not
change one line.

What it does
------------
1. Asks the API which scenarios exist          (GET /scenarios)
2. Shows only the inputs the chosen one needs   (FIELDS map below)
3. Sends the form to the engine                 (POST /run)
4. Renders the result: design, KPIs, table+chart, flows

Run it (with the API already running on port 8000):
    pip install -r webapp/requirements.txt
    streamlit run webapp/streamlit_app.py
"""

from __future__ import annotations

import os

import pandas as pd
import requests
import streamlit as st

API_URL = os.environ.get("SCENARIO_API_URL", "http://localhost:8000")

# Which inputs each scenario actually uses. Add a row when you wire a new
# scenario; anything not listed falls back to DEFAULT_FIELDS.
FIELDS: dict[str, list[str]] = {
    "S00_FIXED_DESIGN_EVAL":        ["pv_mw", "bess_mw", "bess_mwh", "grid_ceiling_mw"],
    "S11_BTM_SSR_TARGET_BESS":      ["pv_mw", "target_ssr_pct"],
    "S12_BTM_GC_TARGET_BESS":       ["pv_mw", "target_gc_mw"],
    "S21_BTM_SSR_CURVE":            ["pv_mw"],
    "S22_BTM_GC_CURVE":             ["pv_mw"],
    "S31_SUB_GC_TARGET_BESS":       ["target_gc_mw"],
    "S32_SUB_GC_CURVE":             [],
    "S33_SUB_FIXED_BESS_EVAL":      ["bess_mw", "bess_mwh", "grid_ceiling_mw"],
    "S34_SUB_FIRMNESS_TARGET_BESS": ["target_firmness_pct", "grid_ceiling_mw"],
    "S41_OPERATIONAL_VERIFY":       ["pv_mw", "bess_mw", "bess_mwh", "grid_ceiling_mw"],
    "S42_DELIVERABLE_BESS_SIZE":    ["pv_mw", "deliverable_target", "target_ssr_pct", "target_gc_mw"],
    "S51_BTM_GCMIN_BESSOPT":        ["pv_mw"],
    "S52_BTM_GCMIN_FIXED_DESIGN":   ["pv_mw", "bess_mw", "bess_mwh"],
    "S62_BTM_PVBESS_SSR_SURFACE":   ["pv_sweep_mw", "ssr_targets_pct"],
    "S71_BTM_SSR_PLUS_GC_BESS":     ["pv_mw", "target_ssr_pct", "target_gc_mw"],
    "S91_OFFGRID_FIRMNESS_PVBESS":  ["target_firmness_pct", "pv_sweep_mw"],
    "S93_STANDALONE_EXPORTLIMIT_BESS": ["pv_mw", "ssr_targets_pct"],
}
DEFAULT_FIELDS = ["pv_mw", "target_ssr_pct", "target_gc_mw", "bess_mw",
                  "bess_mwh", "grid_ceiling_mw", "target_firmness_pct"]


# ── Tiny API helpers ──────────────────────────────────────────────────────────

@st.cache_data(ttl=60)
def api_get(path: str, **params):
    r = requests.get(f"{API_URL}{path}", params=params, timeout=30)
    r.raise_for_status()
    return r.json()


def api_run(payload: dict):
    r = requests.post(f"{API_URL}/run", json=payload, timeout=600)
    if r.status_code >= 400:
        try:
            detail = r.json().get("detail", r.text)
        except Exception:
            detail = r.text
        raise RuntimeError(detail)
    return r.json()


def api_upload(uploaded_file):
    """Send an uploaded Excel to the backend; return its validated summary."""
    files = {"file": (uploaded_file.name, uploaded_file.getvalue(),
                      "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")}
    r = requests.post(f"{API_URL}/upload-profile", files=files, timeout=120)
    if r.status_code >= 400:
        try:
            detail = r.json().get("detail", r.text)
        except Exception:
            detail = r.text
        raise RuntimeError(detail)
    return r.json()


def records_to_df(rec: dict | None) -> pd.DataFrame | None:
    if not rec:
        return None
    return pd.DataFrame(rec["rows"], columns=rec["columns"])


# ── Shared leaf renderer — used by BOTH the wizard and the direct picker ───────

def render_scenario(selected_id: str, profile_path: str | None):
    """Show one scenario's contract, its conditional form, run it, render results.
    Both entry points (guided wizard, direct pick) funnel through here, so the
    form + result logic lives in exactly one place."""
    detail = api_get(f"/scenarios/{selected_id}")
    is_ready = detail["status"] == "ready"

    with st.expander("📖 What this scenario does", expanded=True):
        st.markdown(f"**{selected_id} — {detail['name']}**  ·  topology "
                    f"`{detail['topology']}`  ·  answer mode `{detail['answer_mode']}`")
        st.markdown(f"**Inputs:** {detail['inputs']}")
        st.markdown(f"**Outputs:** {detail['outputs']}")
        st.markdown(f"**How it works:** {detail['how']}")
        if not is_ready:
            st.warning(f"⏳ Not runnable yet — {detail['needs']}")
    if not is_ready:
        return

    summary = api_get("/profile-summary",
                      **({"profile_path": profile_path} if profile_path else {}))
    default_pv = float(summary.get("pv_nameplate_mw") or 150.0)
    peak_load = float(summary.get("peak_load_mw") or 100.0)
    st.info(f"Dataset: peak load **{peak_load:.0f} MW**, PV nameplate "
            f"**{default_pv:.0f} MW**, {summary.get('n_timesteps', 0):,} timesteps.")

    fields = FIELDS.get(selected_id, DEFAULT_FIELDS)

    with st.form(f"run_form_{selected_id}"):
        st.subheader("Inputs")
        payload: dict = {"scenario_id": selected_id}
        if profile_path:
            payload["profile_path"] = profile_path
        c1, c2 = st.columns(2)
        with c1:
            if "pv_mw" in fields:
                payload["pv_mw"] = st.number_input("PV nameplate (MW)", value=default_pv, step=5.0, min_value=0.0)
            if "target_ssr_pct" in fields:
                payload["target_ssr_pct"] = st.number_input("Target SSR (%)", value=60.0, step=5.0, min_value=0.0, max_value=100.0)
            if "target_gc_mw" in fields:
                payload["target_gc_mw"] = st.number_input("Target grid connection (MW)", value=round(peak_load * 0.8), step=5.0, min_value=0.0)
            if "target_firmness_pct" in fields:
                payload["target_firmness_pct"] = st.number_input("Firmness target (%)", value=99.0, step=0.5, min_value=0.0, max_value=100.0)
            if "deliverable_target" in fields:
                payload["deliverable_target"] = st.selectbox("Deliverable target", ["ssr", "gc"])
        with c2:
            if "bess_mw" in fields:
                payload["bess_mw"] = st.number_input("BESS power (MW)", value=20.0, step=5.0, min_value=0.0)
            if "bess_mwh" in fields:
                payload["bess_mwh"] = st.number_input("BESS energy (MWh)", value=80.0, step=10.0, min_value=0.0)
            if "grid_ceiling_mw" in fields:
                gc = st.number_input("Grid ceiling (MW) — 0 = use site max", value=0.0, step=5.0, min_value=0.0)
                if gc > 0:
                    payload["grid_ceiling_mw"] = gc
            if "pv_sweep_mw" in fields:
                txt = st.text_input("PV sweep (MW, comma-separated)", "50,100,150,200,250")
                payload["pv_sweep_mw"] = [float(x) for x in txt.split(",") if x.strip()]
            if "ssr_targets_pct" in fields:
                txt = st.text_input("SSR/curtailment targets (comma-separated)", "10,20,30,40,50,60,70,80")
                payload["ssr_targets_pct"] = [float(x) for x in txt.split(",") if x.strip()]

        with st.expander("⚙️ Battery & site assumptions (advanced)", expanded=False):
            a1, a2, a3 = st.columns(3)
            payload["eff_charge"] = a1.slider("Charge eff.", 0.80, 0.99, 0.95)
            payload["eff_discharge"] = a1.slider("Discharge eff.", 0.80, 0.99, 0.95)
            payload["min_soc_pct"] = a2.slider("Min SOC (%)", 0.0, 50.0, 10.0)
            payload["max_soc_pct"] = a2.slider("Max SOC (%)", 50.0, 100.0, 90.0)
            payload["eol_capacity_retention_pct"] = a3.slider("EoL retention (%)", 50.0, 100.0, 80.0)
            payload["site_max_grid_mw"] = a3.number_input("Site max grid (MW)", value=200.0, step=10.0)
            payload["solver_time_limit"] = st.number_input("Solver time limit (s)", value=120, step=30, min_value=10)

        submitted = st.form_submit_button("🚀 Run scenario", type="primary")

    if submitted:
        with st.spinner("Solving on the engine…"):
            try:
                st.session_state["result"] = api_run(payload)
            except Exception as exc:
                st.error(f"Run failed: {exc}")
                return

    if st.session_state.get("result", {}).get("id") == selected_id:
        _render_result(st.session_state["result"])


def _render_result(result: dict):
    st.success(f"✅ {result['id']} — {result['name']}"
               + ("" if result["feasible"] else "  (INFEASIBLE)"))
    d1, d2 = st.columns(2)
    if result["design"]:
        with d1:
            st.subheader("Design")
            st.table(pd.DataFrame(
                [(k, v) for k, v in result["design"].items() if v is not None],
                columns=["Field", "Value"]))
    if result["kpis"]:
        with d2:
            st.subheader("KPIs")
            st.table(pd.DataFrame(
                [(k, v) for k, v in result["kpis"].items() if v is not None],
                columns=["Metric", "Value"]))

    table = records_to_df(result.get("table"))
    if table is not None:
        st.subheader("Curve / surface")
        st.dataframe(table, use_container_width=True)
        num = table.select_dtypes("number")
        if num.shape[1] >= 2:
            st.line_chart(num)

    flows = records_to_df(result.get("flows"))
    if flows is not None:
        st.subheader("Flows (time series)")
        if result["flows"].get("truncated"):
            st.caption(f"Showing first {len(flows):,} of {result['flows']['n_total']:,} timesteps.")
        plot_cols = [c for c in ["load_mw", "pv_used_mw", "grid_import_mw",
                                 "bess_discharge_mw", "bess_charge_mw"] if c in flows.columns]
        if plot_cols:
            st.line_chart(flows[plot_cols])
        st.dataframe(flows.head(200), use_container_width=True)

    if result.get("notes"):
        st.info(result["notes"])


# ── Page ──────────────────────────────────────────────────────────────────────

st.set_page_config(page_title="Scenario Explorer", layout="wide")
st.title("⚡ Scenario Explorer")
st.caption("Front end (this screen) talks only to the API. The optimiser lives behind it.")

# Is the backend up?
try:
    health = api_get("/health")
except Exception as exc:
    st.error(
        f"Cannot reach the backend at **{API_URL}**.\n\n"
        f"Start it first in another terminal:\n\n"
        f"```\nuvicorn webapp.api:app --reload --port 8000\n```\n\nDetails: {exc}"
    )
    st.stop()

scenarios = api_get("/scenarios")
ready = [s for s in scenarios if s["ready"]]

# Sidebar: data source + coverage.
with st.sidebar:
    st.header("Data source")
    up = st.file_uploader("Upload profiles (.xlsx)", type=["xlsx", "xls"],
                          help="8760 PV+Load sheet, or BESS_Input sheet. "
                               "Leave empty to use the bundled dataset.")
    if up is not None:
        if st.session_state.get("uploaded_name") != up.name:
            try:
                info = api_upload(up)
                st.session_state["profile_path"] = info["profile_path"]
                st.session_state["uploaded_name"] = up.name
                st.success(f"Loaded {info['filename']} — "
                           f"{info['n_timesteps']:,} steps, peak load "
                           f"{info['peak_load_mw']:.0f} MW.")
            except Exception as exc:
                st.error(f"Upload rejected: {exc}")
    else:
        st.session_state.pop("profile_path", None)
        st.session_state.pop("uploaded_name", None)
        st.caption("Using bundled `8760_PV&Load Profiles.xlsx`.")

    st.divider()
    cov = api_get("/coverage")
    st.subheader("Registry coverage")
    st.metric("Total", cov.get("total", 0))
    st.metric("Ready", cov.get("ready", 0))
    st.caption(f"Needs PV variable: {cov.get('needs_pv_variable', 0)} · "
               f"consumer layer: {cov.get('needs_consumer_layer', 0)} · "
               f"minor: {cov.get('needs_minor', 0)}")

_profile_path = st.session_state.get("profile_path")

# Two ways in: a guided wizard (business questions → one scenario) or a direct
# pick from the full list. Both end in the same render_scenario() leaf.
mode = st.radio("How do you want to choose?",
                ["🧭 Guided wizard", "📋 Pick a scenario directly"],
                horizontal=True)

if mode.startswith("🧭"):
    wiz = api_get("/wizard")
    questions = wiz["questions"]

    top = st.columns([4, 1])
    top[0].caption("Answer each question; the path narrows to one scenario.")
    if top[1].button("↺ Start over"):
        for k in [k for k in st.session_state if k.startswith("wiz_")]:
            del st.session_state[k]
        st.session_state.pop("result", None)
        st.rerun()

    # Walk the tree by following the answers remembered in session_state. Only
    # the questions on the current path are rendered, so the form self-prunes.
    cur, leaf, guard = wiz["start"], None, 0
    while cur is not None and guard < 25:
        guard += 1
        q = questions[cur]
        opt_labels = [o["label"] for o in q["options"]]
        choice = st.radio(q["text"], opt_labels, key=f"wiz_{cur}", index=None)
        if choice is None:
            break
        opt = q["options"][opt_labels.index(choice)]
        if "leaf" in opt:
            leaf = opt["leaf"]
            cur = None
        else:
            cur = opt.get("next")

    if leaf is not None:
        st.divider()
        if not leaf["runnable"]:
            st.warning(f"➡️ This leads to **{leaf['scenario_id']} — {leaf['name']}**, "
                       f"which isn't runnable yet — {leaf['needs']}")
        else:
            st.markdown(f"➡️ This maps to **{leaf['scenario_id']} — {leaf['name']}**")
            render_scenario(leaf["scenario_id"], _profile_path)

else:
    only_ready = st.toggle("Only runnable scenarios", value=True)
    shown = ready if only_ready else scenarios
    labels = {s["id"]: f"{s['id']} — {s['name']}" + ("" if s["ready"] else "  ⏳") for s in shown}
    selected_id = st.selectbox("Scenario", [s["id"] for s in shown],
                               format_func=lambda x: labels[x])
    render_scenario(selected_id, _profile_path)
