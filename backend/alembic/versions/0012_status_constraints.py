"""CHECK constraints on status columns, after auditing legacy values.

The audit runs first and aborts without changing anything if any row holds a
value outside the allowed set, so a typo or a retired status is resolved
explicitly (reconcile or fix the rows) rather than silently rewritten.
"""

from alembic import op
import sqlalchemy as sa
from repositories.status_values import CONSTRAINED, check_sql, constraint_name

revision = "0012"
down_revision = "0011"


def upgrade():
    connection = op.get_bind()
    problems = []
    for table, values in CONSTRAINED.items():
        placeholders = ", ".join(f":v{i}" for i in range(len(values)))
        rows = connection.execute(
            sa.text(
                f"SELECT status, count(*) FROM {table} "
                f"WHERE status NOT IN ({placeholders}) GROUP BY status"
            ),
            {f"v{i}": value for i, value in enumerate(values)},
        ).all()
        problems += [f"{table}.status={status!r} ({count} row(s))" for status, count in rows]
    if problems:
        raise RuntimeError(
            "Cannot add status CHECK constraints; unexpected legacy values: "
            + "; ".join(problems)
            + ". Reconcile or correct these rows, then rerun the migration."
        )
    for table, values in CONSTRAINED.items():
        with op.batch_alter_table(table) as batch:
            batch.create_check_constraint(constraint_name(table), check_sql(values))


def downgrade():
    raise RuntimeError("Status constraint downgrade requires backup/restore")
