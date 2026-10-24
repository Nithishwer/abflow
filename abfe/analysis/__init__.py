"""Public analysis operations; implementations are grouped by purpose."""

from abfe.analysis.combine import binding, binding_state, check_compatibility
from abfe.analysis.convergence import decide, half_difference
from abfe.analysis.estimate import run_analysis
from abfe.analysis.fingerprints import fingerprint, stamp
from abfe.analysis.history import historical_csv, import_analysis
from abfe.analysis.incidents import incident
from abfe.analysis.inputs import read_window, validate_inputs
from abfe.analysis.policy import protocol, settings
from abfe.analysis.provenance import verify
from abfe.analysis.records import latest, persist, selected, tables
from abfe.analysis.run import analyse, current_decision
from abfe.analysis.values import close_values, digest, encode, finite, timestamp
from abfe.analysis.view import attach
