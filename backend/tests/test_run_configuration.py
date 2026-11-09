import asyncio
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.config import settings
from core.schemas import (
    NormalizedResponse,
)
from services.agent_service import AgentEngine
from services.skills.registry import SkillRegistry
from services.run_configuration import (
    capture_configuration,
    bind_configuration,
    execution_settings,
    _configuration,
)
from services.workflow.state import CheckpointIncompatible


class ConfigurationTest(unittest.TestCase):
    def test_run_config_pins_models_budget_protocol_prompt_but_not_credentials(self):
        with (
            patch.object(settings, "openai_model", "original-model"),
            patch.object(settings, "openai_api_key", "must-not-persist"),
        ):
            raw = capture_configuration("openai")
        self.assertNotIn("must-not-persist", raw)
        original = json.loads(raw)
        with (
            patch.object(settings, "openai_model", "changed-model"),
            patch.object(settings, "llm_run_token_limit", 1024),
        ):
            token = bind_configuration(raw, "openai")
            try:
                self.assertEqual(execution_settings().openai_model, "original-model")
                self.assertEqual(
                    execution_settings().llm_run_token_limit,
                    original["options"]["llm_run_token_limit"],
                )
                from services.prompts import get_system_prompt

                self.assertEqual(get_system_prompt(), original["prompt"])
            finally:
                _configuration.reset(token)
            self.assertEqual(execution_settings().openai_model, "changed-model")
        for malformed in (None, "{}", raw.replace(original["prompt_hash"], "invalid")):
            with self.assertRaises(CheckpointIncompatible):
                bind_configuration(malformed, "openai")
        with self.assertRaises(CheckpointIncompatible):
            bind_configuration(raw, "anthropic")

    def test_fallback_provider_models_and_endpoints_remain_pinned(self):
        from controllers.chat import _get_provider

        with (
            patch.object(settings, "openai_model", "primary-old"),
            patch.object(settings, "anthropic_model", "fallback-old"),
            patch.object(settings, "llm_fallback_providers", "anthropic"),
            patch.object(settings, "anthropic_protocol", "openai_compatible"),
        ):
            raw = capture_configuration("openai")
        with (
            patch.object(settings, "openai_model", "primary-new"),
            patch.object(settings, "anthropic_model", "fallback-new"),
            patch.object(settings, "llm_fallback_providers", ""),
            patch.object(settings, "openai_api_key", "rotated-key"),
            patch("services.providers.openai_provider.AsyncOpenAI") as primary,
            patch("openai.AsyncOpenAI"),
        ):
            token = bind_configuration(raw, "openai")
            try:
                provider = _get_provider("openai")
                self.assertEqual(provider.model_name, "primary-old")
                self.assertEqual(
                    [p.model_name for p in provider.fallbacks], ["fallback-old"]
                )
                self.assertEqual(primary.call_args.kwargs["api_key"], "rotated-key")
            finally:
                _configuration.reset(token)
            self.assertEqual(provider.config.openai_model, "primary-old")
            self.assertEqual(
                provider.fallbacks[0].config.anthropic_model, "fallback-old"
            )
        invalid = json.loads(raw)
        invalid["options"]["llm_max_attempts"] = "2"
        with self.assertRaises(CheckpointIncompatible):
            bind_configuration(json.dumps(invalid), "openai")


class RunConfigurationPersistenceTest(unittest.IsolatedAsyncioTestCase):
    async def test_queued_run_uses_persisted_config_after_settings_change_and_retry(
        self,
    ):
        from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
        from repositories.models import Base
        from repositories.run_models import Run
        from services.run_service import create_run
        from services.run_worker import RunWorker
        from services.job_service import claim
        from services.tool_executor import ToolExecutor

        with tempfile.TemporaryDirectory() as directory:
            db = create_async_engine(
                "sqlite+aiosqlite:///" + Path(directory, "runs.db").as_posix()
            )
            try:
                async with db.begin() as connection:
                    await connection.run_sync(Base.metadata.create_all)
                sessions = async_sessionmaker(db, expire_on_commit=False)
                with patch.object(settings, "openai_model", "pinned-model"):
                    async with sessions() as session:
                        run = await create_run(
                            session,
                            message="hello",
                            provider="openai",
                            conversation_id=None,
                            idempotency_key="same",
                        )
                seen = []

                class Provider:
                    model_name = "pinned-model"

                    async def chat_completion(self, messages, tools):
                        seen.append(
                            (execution_settings().openai_model, messages[0].content)
                        )
                        yield NormalizedResponse(
                            text_content="done", stop_reason="end_turn"
                        )

                with (
                    patch.object(settings, "openai_model", "new-model"),
                    patch.object(settings, "llm_run_token_limit", 1024),
                ):
                    async with sessions() as session:
                        retry = await create_run(
                            session,
                            message="hello",
                            provider="openai",
                            conversation_id=None,
                            idempotency_key="same",
                        )
                        self.assertEqual(run.execution_config, retry.execution_config)
                        _, lease = await claim(session, "worker")
                    registry = SkillRegistry()
                    worker = RunWorker(
                        AgentEngine(ToolExecutor(registry), registry),
                        sessions,
                        lambda name: Provider(),
                    )
                    await worker.process(run.id, lease)
                async with sessions() as session:
                    completed = await session.get(Run, run.id)
                    self.assertEqual(completed.status, "succeeded")
                self.assertEqual(
                    seen, [("pinned-model", json.loads(run.execution_config)["prompt"])]
                )
                self.assertIsNone(_configuration.get())
            finally:
                await db.dispose()


class LegacyConfigurationTest(unittest.IsolatedAsyncioTestCase):
    async def test_missing_snapshot_blocks_both_approval_routes_before_side_effects(
        self,
    ):
        from datetime import datetime, timedelta
        from fastapi import HTTPException
        from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
        from sqlalchemy import select
        from repositories.models import Base, PendingApproval
        from repositories.run_models import RunCheckpoint
        from repositories.approval_repo import ApprovalRepository
        from controllers import chat, runs
        from services.run_service import create_run

        db = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with db.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            sessions = async_sessionmaker(db, expire_on_commit=False)
            async with sessions() as session:
                run = await create_run(
                    session,
                    message="hello",
                    provider="mock",
                    conversation_id=None,
                    idempotency_key="legacy",
                )
                run.execution_config = None
                run.status = "waiting_approval"
                session.add(
                    RunCheckpoint(
                        run_id=run.id,
                        version=1,
                        state=json.dumps({"pending_calls": [{"call_id": "one"}]}),
                    )
                )
                await ApprovalRepository(session).create_pending(
                    run.conversation_id,
                    "one",
                    "node_health_remediate",
                    {},
                    {"host": "node-a", "command": "docker restart api"},
                    datetime.now() + timedelta(minutes=10),
                )
            with (
                patch.object(chat, "engine", object()),
                patch.object(chat, "async_session", sessions),
                patch.object(runs, "async_session", sessions),
                patch.object(
                    chat.NodeHealthRemediateSkill, "execute_approved_command"
                ) as dispatch,
            ):
                for call in (
                    lambda: chat.approve_action(
                        chat.ApprovalDecisionRequest(
                            conversation_id=run.conversation_id,
                            call_id="one",
                            approved=True,
                        )
                    ),
                    lambda: runs.approve(
                        run.id, runs.RunApprovalRequest(call_id="one", approved=True)
                    ),
                ):
                    with self.assertRaises(HTTPException) as raised:
                        await call()
                    self.assertEqual(raised.exception.status_code, 409)
                    self.assertIn("configuration", raised.exception.detail)
                dispatch.assert_not_called()
            async with sessions() as session:
                pending = await session.scalar(select(PendingApproval))
                self.assertEqual(pending.status, "pending")
        finally:
            await db.dispose()

    async def test_run_context_is_isolated_across_concurrent_tasks(self):
        with patch.object(settings, "openai_model", "one"):
            first = capture_configuration("openai")
        with patch.object(settings, "openai_model", "two"):
            second = capture_configuration("openai")

        async def observe(raw):
            token = bind_configuration(raw, "openai")
            try:
                await asyncio.sleep(0)
                return execution_settings().openai_model
            finally:
                _configuration.reset(token)

        self.assertEqual(
            await asyncio.gather(observe(first), observe(second)), ["one", "two"]
        )
        self.assertIsNone(_configuration.get())


class RunConfigurationMigrationTest(unittest.TestCase):
    def test_upgrade_preserves_legacy_run_without_fabricating_snapshot(self):
        from alembic import command
        from sqlalchemy import create_engine, text
        from repositories.migrations import configuration, upgrade

        db = create_engine("sqlite://")
        try:
            with db.begin() as connection:
                config = configuration()
                config.attributes["connection"] = connection
                command.upgrade(config, "0009")
                connection.execute(
                    text(
                        "INSERT INTO conversations(id,provider,title,owner_id,created_at,updated_at) VALUES ('conv','mock','legacy','local',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"
                    )
                )
                connection.execute(
                    text(
                        "INSERT INTO runs(id,conversation_id,actor,provider,user_message,status,active_key,idempotency_key,request_hash,sequence,selected_skill_versions,created_at,updated_at) VALUES ('run','conv','local','mock','original','waiting_approval','conv','key','hash',2,'{}',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"
                    )
                )
                upgrade(connection)
                row = connection.execute(
                    text(
                        "SELECT user_message,status,sequence,execution_config FROM runs WHERE id='run'"
                    )
                ).one()
                self.assertEqual(tuple(row), ("original", "waiting_approval", 2, None))
        finally:
            db.dispose()
