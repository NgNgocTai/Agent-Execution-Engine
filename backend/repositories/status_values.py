"""Allowed status values: one source for model CHECK constraints and migrations.

Each set is the union of every value the code writes or reads for that column
(see the state machines in upgrade.md). Adding a status means adding it here,
shipping a migration that replaces the constraint, and updating the code together.
"""

from sqlalchemy import CheckConstraint

RUN = (
    "queued",
    "running",
    "waiting_approval",
    "succeeded",
    "failed",
    "cancelled",
    "timed_out",
    "unknown",
)
# A job mirrors its run's status and may also be marked interrupted on lease loss.
JOB = RUN + ("interrupted",)
EXECUTION = ("running", "awaiting_approval", "succeeded", "failed", "unknown", "reconciled", "cancelled")
STEP = ("running", "waiting_approval", "succeeded", "failed", "cancelled", "unknown")
APPROVAL = (
    "pending",
    "approved",
    "rejected",
    "expired",
    "cancelled",
    "executing",
    "rejecting",
    "unknown",
    "reconciled",
)
SKILL_VERSION = ("quarantined", "approved", "rejected", "revoked")

CONSTRAINED = {
    "runs": RUN,
    "run_jobs": JOB,
    "tool_executions": EXECUTION,
    "run_steps": STEP,
    "pending_approvals": APPROVAL,
    "skill_versions": SKILL_VERSION,
}


def check_sql(values):
    return "status IN (" + ", ".join("'" + value + "'" for value in values) + ")"


def constraint_name(table):
    return f"ck_{table}_status"


def status_check(table):
    return CheckConstraint(check_sql(CONSTRAINED[table]), name=constraint_name(table))
