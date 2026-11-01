from pathlib import Path
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect


def configuration():
    return Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))


def upgrade(connection):
    config = configuration()
    config.attributes["connection"] = connection
    command.upgrade(config, "head")


def adopt_legacy(connection):
    """Stamp only an exact legacy schema. Never silently stamp arbitrary tables."""
    inspector = inspect(connection)
    expected = {
        "conversations": {"id", "provider", "title", "created_at", "updated_at"},
        "messages": {"id", "conversation_id", "role", "content", "thinking_content", "tool_calls", "tool_call_id", "name", "is_error", "created_at"},
        "pending_approvals": {"id", "conversation_id", "call_id", "tool_name", "arguments", "approval_metadata", "status", "created_at", "expires_at", "resolved_at", "result", "error"},
    }
    if set(inspector.get_table_names()) != set(expected):
        raise ValueError("Legacy table set differs; inspect the backup before migration")
    for table, columns in expected.items():
        discovered = inspector.get_columns(table)
        if {column["name"] for column in discovered} != columns:
            raise ValueError(f"Legacy columns differ: {table}")
        from sqlalchemy import Integer, DateTime, Boolean, String
        for column in discovered:
            name = column["name"]
            kind = Integer if name == "id" and table != "conversations" else DateTime if name.endswith("_at") else Boolean if name == "is_error" else String
            if not isinstance(column["type"], kind):
                raise ValueError(f"Legacy column type differs: {table}.{name}")
        keys = inspector.get_pk_constraint(table)["constrained_columns"]
        if keys != ["id"]:
            raise ValueError(f"Invalid primary key: {table}")
    for table in ("messages", "pending_approvals"):
        if not any(fk["constrained_columns"] == ["conversation_id"] and fk["referred_table"] == "conversations" for fk in inspector.get_foreign_keys(table)):
            raise ValueError(f"Invalid conversation foreign key: {table}")
    unique = inspector.get_unique_constraints("pending_approvals")
    if not any(set(constraint["column_names"]) == {"conversation_id", "call_id"} for constraint in unique):
        raise ValueError("Legacy approval uniqueness constraint is missing")
    config = configuration()
    config.attributes["connection"] = connection
    command.stamp(config, "0001")
    upgrade(connection)
