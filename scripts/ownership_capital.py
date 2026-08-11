from ownership_capital_base import *
import ownership_capital_base as _base
import ownership_capital_top_holders as _top_holders


fetch_top_holders = _top_holders.fetch_top_holders
normalize_top_holders = _top_holders.normalize_top_holders
TOP_HOLDER_HISTORY_PERIODS = _top_holders.TOP_HOLDER_HISTORY_PERIODS
DATA_CENTER_ENDPOINT = _top_holders.DATA_CENTER_ENDPOINT


def finalize_snapshot(snapshot_path, base, execution_mode):
    _base.finalize_snapshot(snapshot_path, base, execution_mode)
    _top_holders.extend_snapshot(snapshot_path, base, execution_mode)
