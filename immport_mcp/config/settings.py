from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Allow unknown env vars in .env (so local overrides like IMMPORT_METRIC_BASE_URL
    # won't cause pydantic validation errors). Keep env_file configured for local dev.
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ImmPort API — obtain from https://www.immport.org/auth/api/keys
    # Tokens are valid for 30 days; paste the api_key value from the downloaded JSON
    immport_api_key: str = ""
    immport_base_url: str = "https://immport.org/data/query"

    # MCP server
    mcp_server_name: str = "ImmPort MCP Server"
    mcp_transport: str = "stdio"  # stdio | streamable-http | sse
    mcp_host: str = "127.0.0.1"   # set to 0.0.0.0 for EC2/container
    mcp_port: int = 8000

    # Response cache (TTL in seconds, 0 = disabled)
    # WARNING: In multi-user HTTP mode set to 0 — the cache is not user-scoped.
    response_cache_ttl: int = 300
    response_cache_maxsize: int = 512

    # Lookup cache — controlled vocabulary tables are pre-fetched at startup
    # and refreshed on this interval. Data changes only on ImmPort data releases
    # (a few times per year) so 24h is safe. Set to 0 to disable background refresh.
    lookup_refresh_hours: float = 24.0

    # Usage metrics: each tool call sends one record (tool name, its parameters, your
    # ImmPort username) to ImmPort's metric service. Set IMMPORT_METRICS_ENABLED=false
    # to send nothing.
    immport_metrics_enabled: bool = True

    # Metric service configuration (can be set in .env)
    immport_metric_base_url: str = "https://www.immport.org"
    immport_metric_metric_path: str = "/metrics/write"
    mcp_metric_timeout_seconds: float = 2.0
    mcp_metric_max_concurrency: int = 16

    # Logging level for immport_mcp.* loggers (DEBUG | INFO | WARNING | ERROR).
    # INFO  → show metric payloads, POST results, and errors.
    # WARNING → show only failures (recommended for production).
    # DEBUG → show scheduling details too.
    mcp_log_level: str = "WARNING"


settings = Settings()
