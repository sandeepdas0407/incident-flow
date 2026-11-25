from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # ServiceNow
    sn_instance_url: str = "https://example.service-now.com"
    sn_username: str = ""
    sn_password: str = ""
    sn_intake_group: str = "AI Ops"
    sn_human_group: str = "Service Desk"
    sn_agent_user: str = "ai.ops.agent"
    sn_incident_close_code: str = "Resolved by change"

    # GitHub
    github_token: str = ""
    github_repo: str = ""
    github_base_branch: str = "main"

    # Claude
    anthropic_api_key: str = ""
    claude_model: str = "claude-opus-5-5"
    claude_effort: str = "medium"
    chat_effort: str = "low"  # the chatbot: keep replies snappy

    # Behaviour
    sn_poll_seconds: int = 10
    gh_poll_seconds: int = 15
    triage_min_confidence: float = 0.7
    max_fix_attempts: int = 2  # follow-up PRs after a failed post-deploy validation, then a human takes over

    # Azure (read-only, for post-deploy validation; uses the local `az` login)
    azure_resource_group: str = "incidentflow-rg"
    azure_vm_name: str = "incidentflow-vm"
    azure_nsg_name: str = "incidentflow-nsg"
    app_url: str = ""
    database_url: str = "sqlite:///./incident_flow.db"


@lru_cache
def get_settings() -> Settings:
    return Settings()
