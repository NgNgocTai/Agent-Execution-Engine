from pathlib import Path

from pydantic_settings import BaseSettings

ROOT_DIR = Path(__file__).resolve().parent.parent.parent


class Settings(BaseSettings):
    openai_api_key: str = ""
    openai_model: str = "openai/gpt-oss-120b:free"
    openai_base_url: str = "https://openrouter.ai/api/v1"
    anthropic_api_key: str = ""
    anthropic_model: str = "anthropic/claude-3-haiku"
    anthropic_base_url: str = "https://openrouter.ai/api/v1"
    anthropic_max_tokens: int = 4096
    anthropic_reasoning_max_tokens: int = 1024
    deepseek_api_key: str = ""
    deepseek_model: str = "deepseek/deepseek-r1"
    deepseek_base_url: str = "https://openrouter.ai/api/v1"
    database_url: str = "sqlite+aiosqlite:///./conversations.db"
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_host: str = "https://cloud.langfuse.com"
    langfuse_trace_thinking_content: bool = False
    ssh_host: str = ""
    ssh_user: str = ""
    ssh_password: str = ""
    ssh_key_path: str = ""
    ssh_port: int = 22
    clickhouse_host: str = ""
    clickhouse_port: int = 8123
    clickhouse_user: str = ""
    clickhouse_password: str = ""
    clickhouse_database: str = "alarm_data"
    postgres_host: str = ""
    postgres_port: int = 5432
    postgres_user: str = ""
    postgres_password: str = ""
    postgres_db: str = ""
    cors_origins: str = "*"

    model_config = {
        "env_file": str(ROOT_DIR / ".env"),
        "env_file_encoding": "utf-8",
        # The root dotenv is also shared with the optional evaluation scripts.
        # Their settings must not prevent the backend from starting.
        "extra": "ignore",
    }


settings = Settings()
