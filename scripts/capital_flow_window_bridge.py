def install(capital_flow_context):
    if getattr(capital_flow_context, "_completed_window_semantics_installed", False):
        return

    def volume_structure(mins):
        # _structure_for_window owns exclusion of the newest potentially
        # accumulating minute. Give it raw windows with one predecessor bar so
        # last_30m / last_15m contain 30 / 15 classifiable completed minutes.
        values = list(mins or [])
        return {
            "full_session": capital_flow_context._structure_for_window(values),
            "last_30m": capital_flow_context._structure_for_window(values[-32:]),
            "last_15m": capital_flow_context._structure_for_window(values[-17:]),
        }

    capital_flow_context._volume_structure = volume_structure

    # This bridge is already installed by the production runner and unit tests.
    # Reuse that stable hook to activate the review hardening without changing
    # the hot-path orchestration order.
    import capital_flow_changes
    import capital_flow_review_hardening
    import history_store
    capital_flow_review_hardening.install(
        capital_flow_context,
        capital_flow_changes,
        history_store,
    )

    capital_flow_context._completed_window_semantics_installed = True
