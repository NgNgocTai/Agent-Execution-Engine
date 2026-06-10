from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-sonnet-4-20250514"
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_host: str = "https://cloud.langfuse.com"
    ssh_user: str = ""
    ssh_password: str = ""
    ssh_key_path: str = ""
    ssh_port: int = 22

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


settings = Settings()
