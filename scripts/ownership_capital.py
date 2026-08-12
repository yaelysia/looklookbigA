from ownership_capital_base import *
import ownership_capital_base as _base
import ownership_capital_top_holders as _top_holders
import ownership_capital_institutional as _institutional
import ownership_capital_shareholder_count as _shareholder_count


fetch_top_holders = _top_holders.fetch_top_holders
normalize_top_holders = _top_holders.normalize_top_holders
TOP_HOLDER_HISTORY_PERIODS = _top_holders.TOP_HOLDER_HISTORY_PERIODS
DATA_CENTER_ENDPOINT = _top_holders.DATA_CENTER_ENDPOINT

fetch_institutional_holdings = _institutional.fetch_institutional_holdings
normalize_institutional_holdings = _institutional.normalize_institutional_holdings
INSTITUTIONAL_HISTORY_PERIODS = _institutional.INSTITUTIONAL_HISTORY_PERIODS
INSTITUTIONAL_DETAIL_ENDPOINT = _institutional.INSTITUTIONAL_DETAIL_ENDPOINT

fetch_shareholder_count = _shareholder_count.fetch_shareholder_count
normalize_shareholder_count = _shareholder_count.normalize_shareholder_count
SHAREHOLDER_COUNT_HISTORY_LIMIT = _shareholder_count.HISTORY_LIMIT


def finalize_snapshot(snapshot_path, base, execution_mode):
    _base.finalize_snapshot(snapshot_path, base, execution_mode)
    _top_holders.extend_snapshot(snapshot_path, base, execution_mode)
    _institutional.extend_snapshot(snapshot_path, base, execution_mode)
    _shareholder_count.extend_snapshot(snapshot_path, base, execution_mode)
