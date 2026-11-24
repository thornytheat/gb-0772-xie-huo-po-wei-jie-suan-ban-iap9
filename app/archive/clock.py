"""Compatibility entry point. Canonical implementation: app.curb.calibrate."""
from app.curb.calibrate import *  # noqa: F401,F403
from app.curb.calibrate import assess
from app.archive.clocks import (ClockStore, confirm, receive, recompute_for_docket,
                                stateless_review, extract_corrections)

for _name in ['run', 'reconcile', 'settle', 'settle_first', 'release_now', 'clear_orders', 'check_clocks', 'verify', 'quote_both', 'compare_dues', 'handover_review', 'build_handover']:
    globals().setdefault(_name, assess)
run = assess
clock = assess
