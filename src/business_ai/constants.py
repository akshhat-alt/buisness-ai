"""Shared path/window constants (Phase 9 extraction from app.py), kept in
one place at the package root — never duplicated into routers/ modules —
because PROJECT_ROOT is computed relative to `__file__`'s own location;
computing it again from a deeper module would silently point at the
wrong directory.
"""

from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
# Anchored to this project's own directory, never to the launching
# process's cwd. A cwd-relative "data" path is a real cross-project
# isolation hazard: this app could otherwise be started from a sibling
# project's directory and silently read/write that project's own
# data/ (found in dev when a mismatched schema surfaced the collision
# before any actual write happened).
DATA_ROOT = PROJECT_ROOT / "data"
STATIC_DIR = PROJECT_ROOT / "static"

# Automation windows (implementation constants, not tenant/platform-tunable
# knobs — see winback_after_days/deposit_amount_inr on TenantConfig for the
# dials that genuinely vary per business). Each of these is read by an
# admin/*/run endpoint meant to be invoked once a day by an external cron;
# there's no in-process scheduler in this app.
REENGAGEMENT_MIN_AGE_HOURS = 48  # give a lead a fair chance to book on their own first
REENGAGEMENT_MAX_AGE_HOURS = 24 * 14  # older than this is stale — don't blast old history on first run
REMINDER_WINDOW_START_HOURS = 20  # a ~24h-before reminder, with slack for cron timing drift
REMINDER_WINDOW_END_HOURS = 28
# A feedback theme reported at least this many times in one window is a
# pattern worth surfacing to management (digest, action brief, scorecard),
# not a one-off complaint.
RECURRING_FEEDBACK_THRESHOLD = 3
# A task overdue by more than this is a proactive-alert-worthy problem,
# not just a line in tomorrow's digest — see /api/v1/admin/task-escalation/run.
TASK_ESCALATION_HOURS = 48
# Phase 10: once a dependency risk (a bus-factor-1 process, a workload/
# knowledge concentration, sole-contact customers) has been flagged to
# the owner, don't re-flag the SAME risk again until this many hours have
# passed — an unresolved risk staying true every single day shouldn't
# mean a daily repeat of the same WhatsApp message forever.
DEPENDENCY_RISK_RENOTIFY_HOURS = 24 * 7

# Phase 11 — Self-Evolution Infrastructure (evolution.py). All thresholds
# are deliberately conservative: this pipeline is allowed to touch a
# tenant's live customer-assistant tone, so false positives (proposing
# too eagerly, or failing to roll back a real regression) are the
# expensive direction to get wrong, not "one fewer improvement."
EVOLUTION_LOOKBACK_HOURS = 24 * 14  # two weeks of conversation history per detection/monitoring window
EVOLUTION_MIN_SAMPLE_FOR_DETECTION = 10  # don't draw conclusions from a handful of conversations
EVOLUTION_FAILURE_DISSATISFACTION_RATE_THRESHOLD = 0.20  # 20% of recent questions showing dissatisfaction
EVOLUTION_SANDBOX_SAMPLE_SIZE = 5  # historical questions replayed (never to a real customer) per sandbox evaluation
EVOLUTION_MONITORING_MIN_HOURS_ACTIVE = 24  # give a promoted version a full day of real traffic before judging it
EVOLUTION_MONITORING_MIN_SAMPLE = 5  # per window (pre- and post-activation) before a rate comparison is trusted
EVOLUTION_MONITORING_REGRESSION_DELTA = 0.15  # a 15-point rise in dissatisfaction rate triggers automatic rollback

# Phase 17 — Restaurant Foundation (inventory.py). A low-stock ingredient
# is a same-day operational problem, not a slow-moving one — shorter than
# DEPENDENCY_RISK_RENOTIFY_HOURS's 7 days on purpose, so a still-low
# ingredient gets re-flagged daily rather than going quiet for a week
# while the kitchen keeps running short.
INVENTORY_ALERT_RENOTIFY_HOURS = 24

# Phase 22 — Restaurant Profitability Intelligence (menu_engineering.py).
# A trailing window, not all-time, so a dish's classification reflects
# recent demand, not a stale average from months ago.
MENU_ENGINEERING_WINDOW_DAYS = 30
# An ingredient must have genuinely triggered a low_stock alert at least
# this many times before a reorder suggestion fires — one alert could be
# a one-off order spike, not evidence the par level itself is too low.
REORDER_SUGGESTION_MIN_TRIGGER_COUNT = 2
# A conservative, bounded bump — never a wild jump — matching the same
# "small, reviewable adjustment" philosophy as Self-Evolution's own
# proposals; the owner still reviews and applies it, never auto-adopted.
REORDER_SUGGESTION_INCREASE_PCT = 0.25

# Phase 24 — Restaurant Autopilot (menu_engineering.py's
# build_menu_recommendations). A widely-cited restaurant-industry rule
# of thumb, not something this app invented — dishes above it are
# flagged for review, never auto-repriced.
HIGH_FOOD_COST_PCT_THRESHOLD = 35.0
# The same conservative, bounded, review-before-apply philosophy as
# REORDER_SUGGESTION_INCREASE_PCT — a Plowhorse (popular, thin margin)
# gets a small suggested price nudge, never a large jump that could
# scare off the exact demand that makes it popular.
PLOWHORSE_PRICE_INCREASE_PCT = 0.05

# Phase 3 — a rating snapshot drop of at least this many stars between
# two consecutive Google Places syncs is treated as a real signal worth
# an immediate owner alert (same "catch it before tomorrow's digest"
# philosophy as the chat dissatisfaction alert), not noise from normal
# rating fluctuation.
RATING_DROP_ALERT_THRESHOLD = 0.2
