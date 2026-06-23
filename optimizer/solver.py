import pyomo.environ as pyo


DEFAULT_TIME_LIMIT_SECONDS = 60


def create_highs_solver(time_limit_seconds=DEFAULT_TIME_LIMIT_SECONDS):
    solver = pyo.SolverFactory("appsi_highs")
    solver.options["time_limit"] = time_limit_seconds
    return solver
