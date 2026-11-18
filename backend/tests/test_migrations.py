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


class StatusConstraintTest(unittest.TestCase):
    RUN = ("INSERT INTO runs(id,conversation_id,actor,provider,user_message,status,active_key,idempotency_key,request_hash,sequence,"
           "selected_skill_versions,created_at,updated_at) VALUES ('{id}','conv','a','mock','m','{status}',NULL,'{id}','h',0,'{{}}',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)")

    def migrate(self, connection, target):
        config = configuration()
        config.attributes["connection"] = connection
        command.upgrade(config, target)

    def test_every_constrained_table_rejects_unknown_status_after_upgrade(self):
        from sqlalchemy.exc import IntegrityError
        from repositories.status_values import CONSTRAINED, constraint_name

        engine = create_engine("sqlite://")
        with engine.begin() as connection:
            upgrade(connection)
            for table in CONSTRAINED:
                names = {c["name"] for c in inspect(connection).get_check_constraints(table)}
                self.assertIn(constraint_name(table), names, table)
            connection.execute(text(self.RUN.format(id="ok", status="waiting_approval")))
            with self.assertRaises(IntegrityError):
                with connection.begin_nested():
                    connection.execute(text(self.RUN.format(id="bad", status="done")))
        engine.dispose()

    def test_upgrade_aborts_without_changes_when_legacy_rows_hold_unknown_values(self):
        engine = create_engine("sqlite://")
        with engine.begin() as connection:
            self.migrate(connection, "0011")
            connection.execute(text(self.RUN.format(id="legacy", status="completed")))
            connection.execute(text(self.RUN.format(id="fine", status="queued")))
            with self.assertRaises(RuntimeError) as raised:
                self.migrate(connection, "head")
            message = str(raised.exception)
            self.assertIn("runs.status='completed' (1 row(s))", message)
            self.assertNotIn("fine", message)
            self.assertEqual(connection.scalar(text("SELECT version_num FROM alembic_version")), "0011")
            self.assertEqual(connection.scalar(text("SELECT count(*) FROM runs")), 2)
            self.assertEqual({c["name"] for c in inspect(connection).get_check_constraints("runs")}, set())
            connection.execute(text("UPDATE runs SET status='failed' WHERE id='legacy'"))
            self.migrate(connection, "head")
            self.assertEqual(connection.scalar(text("SELECT status FROM runs WHERE id='legacy'")), "failed")
        engine.dispose()

    def test_constraints_keep_existing_keys_indexes_and_data(self):
        engine = create_engine("sqlite://")
        with engine.begin() as connection:
            self.migrate(connection, "0011")
            connection.execute(text(self.RUN.format(id="keep", status="running")))
            self.migrate(connection, "head")
            inspector = inspect(connection)
            self.assertEqual(connection.scalar(text("SELECT status FROM runs WHERE id='keep'")), "running")
            self.assertIn("uq_run_actor_key", {u["name"] for u in inspector.get_unique_constraints("runs")})
            self.assertIn("uq_execution_run_call", {u["name"] for u in inspector.get_unique_constraints("tool_executions")})
            self.assertTrue(any(i["column_names"] == ["status"] for i in inspector.get_indexes("run_jobs")))
        engine.dispose()

    def test_models_declare_the_same_constraints_as_the_migration(self):
        from repositories.models import Base
        from repositories.status_values import CONSTRAINED, constraint_name

        for table in CONSTRAINED:
            declared = {c.name for c in Base.metadata.tables[table].constraints}
            self.assertIn(constraint_name(table), declared, table)

    def test_every_status_written_by_the_code_is_allowed(self):
        """Literal statuses in services/controllers must be in some allowed set."""
        import re
        from repositories import status_values as values

        allowed = set().union(*map(set, values.CONSTRAINED.values()), values.INTERVENTION)
        root = Path(__file__).resolve().parents[1]
        pattern = re.compile(r"""(?:status\s*=\s*|\.values\(status=|Status\.in_\(\[)\s*["']([a-z_]+)["']""")
        # Literals that look like statuses but are not values of a constrained column:
        # alarm state, grounding verdicts, skill-pipeline stage results, API payloads.
        not_database = {"active", "grounded", "uncited", "invalid_citation", "unsupported_numbers", "passed", "success", "not_started", "ok"}
        found = set()
        for folder in ("services", "controllers", "repositories"):
            for path in (root / folder).rglob("*.py"):
                found |= set(pattern.findall(path.read_text(encoding="utf-8")))
        self.assertEqual(sorted(found - allowed - not_database), [])
