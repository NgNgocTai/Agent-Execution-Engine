import sys
from pathlib import Path
import unittest
from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from repositories.migrations import configuration, upgrade, adopt_legacy


class MigrationTest(unittest.TestCase):
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
