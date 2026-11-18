import sys
from pathlib import Path
import unittest
from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from repositories.migrations import configuration, upgrade, adopt_legacy


class MigrationTest(unittest.TestCase):
    def test_skill_report_upgrade_preserves_existing_artifacts(self):
        engine = create_engine("sqlite://")
        with engine.begin() as connection:
            config = configuration()
            config.attributes["connection"] = connection
            command.upgrade(config, "0008")
            connection.execute(text("INSERT INTO skill_versions(id,name,version,content_hash,manifest,archive,status,uploaded_by,created_at) VALUES ('old','custom_alarm','1.0.0','hash','{}','original-archive','quarantined','operator',CURRENT_TIMESTAMP)"))
            upgrade(connection)
            row = connection.execute(text("SELECT archive, validation_report FROM skill_versions WHERE id='old'")).one()
            self.assertEqual(tuple(row), ("original-archive", None))
        engine.dispose()

    def test_fresh_database_and_repeat_upgrade(self):
        engine = create_engine("sqlite://")
        with engine.begin() as connection:
            upgrade(connection)
            upgrade(connection)
            self.assertEqual(connection.scalar(text("SELECT version_num FROM alembic_version")), ScriptDirectory.from_config(configuration()).get_current_head())
            self.assertIn("owner_id", {col["name"] for col in inspect(connection).get_columns("conversations")})
        engine.dispose()

    def test_legacy_adoption_preserves_history(self):
        engine = create_engine("sqlite://")
        with engine.begin() as connection:
            config = configuration()
            config.attributes["connection"] = connection
            command.upgrade(config, "0001")
            connection.execute(text("DROP TABLE alembic_version"))
            connection.execute(text("INSERT INTO conversations(id, provider, title, created_at, updated_at) VALUES ('legacy', 'mock', 'Old', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"))
            adopt_legacy(connection)
            self.assertEqual(connection.scalar(text("SELECT owner_id FROM conversations WHERE id='legacy'")), "local")
        engine.dispose()

    def test_arbitrary_schema_is_never_stamped(self):
        engine = create_engine("sqlite://")
        with engine.begin() as connection:
            connection.execute(text("CREATE TABLE conversations(id TEXT)"))
            with self.assertRaises(ValueError):
                adopt_legacy(connection)
            self.assertNotIn("alembic_version", inspect(connection).get_table_names())
        engine.dispose()


class RequestedSkillMigrationTest(unittest.TestCase):
    def test_runs_gain_a_nullable_requested_skill_column(self):
        engine = create_engine("sqlite://")
        with engine.begin() as connection:
            upgrade(connection)
            columns = {c["name"]: c for c in inspect(connection).get_columns("runs")}
            self.assertIn("requested_skill", columns)
            self.assertTrue(columns["requested_skill"]["nullable"])
        engine.dispose()
