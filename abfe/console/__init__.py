"""Public console operations; implementations are grouped by purpose."""

from abfe.console.assets import console_script, console_styles, display_time
from abfe.console.datasets import overview_html
from abfe.console.palette import DATASET_COLORS, REMARK_COLORS, STAGES, TILE_STATUSES
from abfe.console.publish import publish_reports
from abfe.console.runs import report_html
from abfe.console.state import presentation
from abfe.console.summary import summarize_runs
from abfe.console.terminal import number
from abfe.console.tiles import (
    accepted_dataset_status,
    compound_status,
    dataset_status_state,
)
