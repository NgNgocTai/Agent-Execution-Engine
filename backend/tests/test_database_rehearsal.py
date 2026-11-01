from pathlib import Path
from contextlib import closing
import os
import sqlite3
import sys
import tempfile
import unittest
from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from repositories.migrations import configuration, upgrade
from scripts.control_database import backup_sqlite, adopt_sqlite, cutover_sqlite


def legacy_database(path):
    engine = create_engine("sqlite:///" + path.as_posix())
    with engine.begin() as connection:
        config = configuration(); config.attributes["connection"] = connection
        command.upgrade(config, "0001")
        connection.execute(text("DROP TABLE alembic_version"))
        connection.execute(text("INSERT INTO conversations(id, provider, title, created_at, updated_at) VALUES ('legacy', 'mock', 'Old', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"))
        connection.execute(text("INSERT INTO messages(id, conversation_id, role, content, is_error, created_at) VALUES (7, 'legacy', 'user', :content, 0, CURRENT_TIMESTAMP)"), {"content": "Kiểm tra mạng Việt Nam"})
    engine.dispose()


class BackupRehearsalTest(unittest.TestCase):
    def test_backup_restore_and_adoption_preserve_unicode_history(self):
        with tempfile.TemporaryDirectory() as directory:
            source, backup, restored = [Path(directory, name) for name in ("source.db", "backup.db", "restored.db")]
            legacy_database(source)
            report = backup_sqlite(source, backup)
            self.assertEqual(report["tables"]["messages"]["count"], 1)
            with restored.open("xb") as file: file.write(backup.read_bytes())
            adopt_sqlite(source, backup)
            adopt_sqlite(restored, backup)
            for path in (source, restored):
                with closing(sqlite3.connect(path)) as connection:
                    self.assertEqual(connection.execute("SELECT content FROM messages WHERE id=7").fetchone()[0], "Kiểm tra mạng Việt Nam")
                    self.assertEqual(connection.execute("SELECT version_num FROM alembic_version").fetchone()[0], ScriptDirectory.from_config(configuration()).get_current_head())
    def test_changed_source_is_not_adopted_against_a_stale_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            source, backup = Path(directory, "source.db"), Path(directory, "backup.db")
            legacy_database(source); backup_sqlite(source, backup)
            with closing(sqlite3.connect(source)) as connection:
                connection.execute("UPDATE messages SET content='changed'")
                connection.commit()
            with self.assertRaises(ValueError): adopt_sqlite(source, backup)


@unittest.skipUnless(os.environ.get("APPROVAL_TEST_POSTGRES_URL"), "Disposable PostgreSQL URL is not configured")
class PostgresMigrationRehearsalTest(unittest.IsolatedAsyncioTestCase):
    async def test_fresh_postgres_migration_and_repeat_are_idempotent(self):
        from postgres_fixture import postgres_database
        async with postgres_database() as engine:
            async with engine.begin() as connection:
                await connection.run_sync(upgrade)
                await connection.run_sync(upgrade)
                self.assertEqual(await connection.scalar(text("SELECT version_num FROM alembic_version")), ScriptDirectory.from_config(configuration()).get_current_head())
    async def test_legacy_cutover_into_empty_postgres_keeps_ids_and_sequence(self):
        from postgres_fixture import postgres_database
        async with postgres_database() as engine:
            with tempfile.TemporaryDirectory() as directory:
                source = Path(directory, "source.db"); legacy_database(source)
                report = await cutover_sqlite(source, "migrated-owner", os.environ["APPROVAL_TEST_POSTGRES_URL"], engine)
                self.assertEqual(report["tables"]["messages"], 1)
                async with engine.begin() as connection:
                    self.assertEqual(await connection.scalar(text("SELECT owner_id FROM conversations WHERE id='legacy'")), "migrated-owner")
                    next_id = await connection.scalar(text("INSERT INTO messages(conversation_id, role, content, is_error, created_at) VALUES ('legacy', 'assistant', 'new', false, CURRENT_TIMESTAMP) RETURNING id"))
                    self.assertGreater(next_id, 7)
