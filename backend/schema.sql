-- ===========================================================================
-- DIP web-app API — Postgres schema
-- ===========================================================================
-- Run this against your database to create everything the persistence layer
-- needs. The schema name must match DIP_DB_SCHEMA in your .env (default below).
--
--   psql "<conn>" -f backend/schema.sql
--
-- Idempotent: safe to re-run (CREATE ... IF NOT EXISTS throughout). The
-- `scenarios` table is a read-model; the API re-seeds it from the code registry
-- on startup, so you do NOT need to populate it by hand.
-- ===========================================================================

CREATE SCHEMA IF NOT EXISTS gigapark_bess_designer;
SET search_path TO gigapark_bess_designer;

-- ---------------------------------------------------------------------------
-- projects — who/where a run belongs to
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS projects (
    project_id    BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_oid      UUID,
    project_name  VARCHAR(255)  NOT NULL,
    description   TEXT,
    latitude      DOUBLE PRECISION,
    longitude     DOUBLE PRECISION,
    area_ha       DOUBLE PRECISION,
    is_active     BOOLEAN       NOT NULL DEFAULT TRUE,
    created_at    TIMESTAMPTZ   NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ   NOT NULL DEFAULT now(),
    CONSTRAINT uq_projects_user_name UNIQUE (user_oid, project_name)
);

-- ---------------------------------------------------------------------------
-- profiles — a reusable time-series dataset, stored as ONE JSONB row.
-- `series` holds the per-unit arrays; the API rebuilds a DataFrame at run time
-- (timestamps derived from start_ts + i*dt_hours when the grid is regular).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS profiles (
    profile_id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name               VARCHAR(255)     NOT NULL,
    source             VARCHAR(512),
    dt_hours           DOUBLE PRECISION NOT NULL,
    n_steps            INTEGER          NOT NULL,
    start_ts           TIMESTAMPTZ,
    load_nameplate_mw  DOUBLE PRECISION,
    pv_nameplate_mw    DOUBLE PRECISION,
    series             JSONB            NOT NULL,   -- {"load_mw":[…],"pv_pu":[…],"wind_pu":[…]}
    meta               JSONB,
    created_at         TIMESTAMPTZ      NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- scenarios — read-model of the code registry (S00…S123). Seeded by the API.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS scenarios (
    scenario_id   VARCHAR(64) PRIMARY KEY,          -- e.g. 'S32_SUB_GC_CURVE'
    name          VARCHAR(255) NOT NULL,
    stage         VARCHAR(16),
    topology      VARCHAR(32),                       -- grid_connected_btm | bess_load_only | off_grid | standalone_gen
    family        VARCHAR(16),
    answer_mode   VARCHAR(32),                       -- point | curve | surface | frontier | forward_eval | bound
    status        VARCHAR(32),                       -- ready | needs_pv_variable | needs_minor | needs_consumer_layer
    needs         TEXT,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- runs — one execution. request + result are JSONB so the shape stays flexible
-- across answer modes (point/curve/surface/…) without schema changes.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS runs (
    run_id       BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    project_id   BIGINT       NOT NULL REFERENCES projects(project_id)  ON DELETE CASCADE,
    profile_id   BIGINT                REFERENCES profiles(profile_id)  ON DELETE SET NULL,
    scenario_id  VARCHAR(64)  NOT NULL REFERENCES scenarios(scenario_id),
    status       VARCHAR(16)  NOT NULL DEFAULT 'pending',   -- pending | running | completed | failed
    message      TEXT,
    dt_hours     DOUBLE PRECISION,
    request      JSONB        NOT NULL,                     -- full RunRequest, verbatim
    result       JSONB,                                     -- ScenarioResult JSON (design+kpis+table+economics)
    created_at   TIMESTAMPTZ  NOT NULL DEFAULT now(),
    started_at   TIMESTAMPTZ,
    finished_at  TIMESTAMPTZ,
    updated_at   TIMESTAMPTZ  NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_runs_project_id  ON runs (project_id);
CREATE INDEX IF NOT EXISTS ix_runs_scenario_id ON runs (scenario_id);
CREATE INDEX IF NOT EXISTS ix_runs_status      ON runs (status);

-- ---------------------------------------------------------------------------
-- run_flows — the per-timestep FlowsFrame, only for scenarios that emit one.
-- Kept in its own row so the hot runs.result summary stays small.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS run_flows (
    run_id     BIGINT PRIMARY KEY REFERENCES runs(run_id) ON DELETE CASCADE,
    n_steps    INTEGER,
    series     JSONB   NOT NULL,                            -- {"columns":[…],"rows":[[…]]}
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
