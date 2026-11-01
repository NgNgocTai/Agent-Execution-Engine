"""Explicit release migrations and offline SQLite backup/adoption.

Run with NOC_AGENT_ENV_FILE pointing at the intended environment. No operation
prints a DSN or message contents. Legacy adoption requires a verified backup.
"""
import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
from contextlib import closing
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sqlalchemy import create_engine, select, func, text, DateTime
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine
from core.config import settings
from repositories.migrations import upgrade, adopt_legacy

LEGACY_TABLES = ("conversations", "messages", "pending_approvals")


def fingerprint(connection):
    result = {}
    for table in LEGACY_TABLES:
        count, digest = 0, hashlib.sha256()
        for row in connection.execute(f"SELECT * FROM {table} ORDER BY id"):
            digest.update(json.dumps(list(row), ensure_ascii=False, default=str).encode())
            count += 1
        result[table] = {"count": count, "sha256": digest.hexdigest()}
    return result


def backup_sqlite(source, destination):
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if source == destination or not source.is_file():
        raise ValueError("Backup requires a different destination and an existing SQLite file")
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Reserve a fresh destination; never replace a previous backup.
    with destination.open("xb"):
        pass
    with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as original:
        with closing(sqlite3.connect(destination)) as backup:
            original.backup(backup)
            if backup.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("Backup integrity check failed")
            if backup.execute("PRAGMA foreign_key_check").fetchone():
                raise ValueError("Backup contains orphaned references")
            content = fingerprint(backup)
    return {"tables": content, "file_sha256": hashlib.sha256(destination.read_bytes()).hexdigest()}


def adopt_sqlite(source, backup):
    source, backup = Path(source).resolve(), Path(backup).resolve()
    if source == backup or not source.is_file() or not backup.is_file():
        raise ValueError("Adoption requires the original database and its separate backup")
    with closing(sqlite3.connect(backup.as_uri() + "?mode=ro", uri=True)) as saved:
        expected = fingerprint(saved)
    engine = create_engine("sqlite:///" + source.as_posix(), connect_args={"timeout": 1})
    try:
        with engine.connect() as connection:
            connection.exec_driver_sql("BEGIN EXCLUSIVE")
            raw = connection.connection.driver_connection
            if fingerprint(raw) != expected:
                raise ValueError("Source differs from backup; stop writers and create a fresh backup")
            adopt_legacy(connection)
            connection.commit()
    finally:
        engine.dispose()
    return {"status": "adopted", "legacy_tables": expected}


async def migrate():
    engine = create_async_engine(settings.database_url)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(upgrade)
    finally:
        await engine.dispose()


async def cutover_sqlite(source, owner, destination_url=None, destination_engine=None, backup_source=None):
    """Offline, all-or-nothing import into an empty migrated PostgreSQL DB."""
    from repositories.models import Base, Conversation, Message, PendingApproval, RemediationGuard
    from core.clock import utcnow
    url = destination_url or settings.database_url
    if make_url(url).drivername != "postgresql+asyncpg" or not owner.strip():
        raise ValueError("Cutover requires a PostgreSQL asyncpg destination and an explicit owner")
    source = Path(source).resolve()
    if not source.is_file():
        raise ValueError("SQLite source does not exist")
    # Exclusive lock deliberately prevents dual writes during the offline copy.
    original = sqlite3.connect(source, timeout=1)
    original.row_factory = sqlite3.Row
    destination = destination_engine or create_async_engine(url)
    try:
        original.execute("BEGIN EXCLUSIVE")
        if backup_source:
            with closing(sqlite3.connect(Path(backup_source).resolve().as_uri() + "?mode=ro", uri=True)) as saved:
                if fingerprint(original) != fingerprint(saved):
                    raise ValueError("Source differs from the verified backup")
        tables = {row[0] for row in original.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
        if tables != set(LEGACY_TABLES):
            raise ValueError("Only the legacy three-table schema is supported; do not discard existing run history")
        if original.execute("PRAGMA integrity_check").fetchone()[0] != "ok" or original.execute("PRAGMA foreign_key_check").fetchone():
            raise ValueError("Source database failed integrity validation")
        report = {}
        async with destination.begin() as connection:
            await connection.run_sync(upgrade)
            for table in Base.metadata.sorted_tables:
                if await connection.scalar(select(func.count()).select_from(table)):
                    raise ValueError("Destination contains data; cutover refuses to merge writers")
            for table in (Conversation.__table__, Message.__table__, PendingApproval.__table__):
                count = 0
                reader = original.execute(f"SELECT * FROM {table.name} ORDER BY id")
                while batch := reader.fetchmany(500):
                    records = []
                    for source_row in batch:
                        record = dict(source_row)
                        for column in table.columns:
                            if isinstance(column.type, DateTime) and record.get(column.name):
                                value = datetime.fromisoformat(record[column.name])
                                record[column.name] = value.astimezone(timezone.utc).replace(tzinfo=None) if value.tzinfo else value
                        if table.name == "conversations": record.update(owner_id=owner, deleted_at=None)
                        if table.name == "messages": record["is_error"] = bool(record["is_error"])
                        for name in ("tool_calls", "arguments", "approval_metadata", "result"):
                            if record.get(name) is not None: json.loads(record[name])
                        if table.name == "pending_approvals":
                            if record["status"] == "pending": record.update(status="expired", resolved_at=utcnow(), error="Expired during control database cutover")
                            elif record["status"] in {"executing", "rejecting"}: record["status"] = "unknown"
                        records.append(record)
                    await connection.execute(table.insert(), records)
                    count += len(records)
                actual = await connection.scalar(select(func.count()).select_from(table))
                if actual != count:
                    raise ValueError("Destination row count differs")
                report[table.name] = count
                if table.name != "conversations":
                    await connection.execute(text(f"SELECT setval(pg_get_serial_sequence('{table.name}', 'id'), COALESCE((SELECT max(id) FROM {table.name}), 1), EXISTS(SELECT 1 FROM {table.name}))"))
            unknown = (await connection.execute(select(PendingApproval.id, PendingApproval.approval_metadata).where(PendingApproval.status == "unknown"))).all()
            guarded = set()
            for approval_id, metadata in unknown:
                host = json.loads(metadata).get("host", "").lower()
                if host and host not in guarded:
                    await connection.execute(RemediationGuard.__table__.insert().values(host=host, approval_id=approval_id, available_after=None))
                    guarded.add(host)
            # Verify first/last message contents, including Unicode round trips.
            for order in ("ASC", "DESC"):
                sample = original.execute(f"SELECT id, content FROM messages ORDER BY id {order} LIMIT 1").fetchone()
                if sample:
                    copied = (await connection.execute(select(Message.content).where(Message.id == sample[0]))).scalar_one()
                    if copied != sample[1]: raise ValueError("Message sample content differs after copy")
        return {"status": "copied", "tables": report, "unknown_host_guards": len(guarded)}
    finally:
        original.rollback()
        original.close()
        if destination_engine is None:
            await destination.dispose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=["migrate", "backup-sqlite", "adopt-sqlite", "cutover-postgres"])
    parser.add_argument("--source")
    parser.add_argument("--backup")
    parser.add_argument("--maintenance", action="store_true", help="Acknowledge that application writers are stopped")
    parser.add_argument("--owner", default="")
    args = parser.parse_args()
    if args.operation == "migrate":
        asyncio.run(migrate())
        print(json.dumps({"status": "migrated"}))
    elif args.operation == "cutover-postgres":
        if not args.source or not args.owner or not args.maintenance or not args.backup:
            parser.error("Cutover requires --source, --backup, --owner and --maintenance")
        print(json.dumps(asyncio.run(cutover_sqlite(args.source, args.owner, backup_source=args.backup))))
    elif args.source and args.backup:
        if args.operation == "adopt-sqlite" and not args.maintenance:
            parser.error("Legacy adoption requires --maintenance and stopped writers")
        print(json.dumps(backup_sqlite(args.source, args.backup) if args.operation == "backup-sqlite" else adopt_sqlite(args.source, args.backup)))
    else:
        parser.error("--source and --backup are required")


if __name__ == "__main__":
    main()
