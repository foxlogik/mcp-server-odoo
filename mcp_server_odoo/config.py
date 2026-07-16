"""Configuration management for Odoo MCP Server.

This module handles loading and validation of environment variables
for connecting to Odoo via XML-RPC.
"""

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Literal, Optional

from dotenv import load_dotenv

logger = logging.getLogger(__name__)


@dataclass
class OdooConfig:
    """Configuration for Odoo connection and MCP server settings."""

    # Required fields
    url: str

    # Authentication (one method required)
    api_key: Optional[str] = None
    username: Optional[str] = None
    password: Optional[str] = None

    # Optional fields with defaults
    database: Optional[str] = None
    log_level: str = "INFO"
    default_limit: int = 10
    max_limit: int = 100
    max_smart_fields: int = 15
    locale: Optional[str] = None

    # MCP transport configuration
    transport: Literal["stdio", "streamable-http"] = "stdio"
    host: str = "localhost"
    port: int = 8000

    # YOLO mode configuration
    yolo_mode: str = "off"  # "off", "read", or "true"

    # User pinning — when set, EVERY tool call is forced to run as this Odoo user
    # via res.users.mcp_execute_as_user, and any user_id supplied by the model is
    # ignored. Set per-request by claude-service (ODOO_ACT_AS_UID) for end-user
    # chat sessions so record rules + field security are enforced for the sender,
    # regardless of the admin service account the connection authenticates as.
    # None means "not pinned" (the trusted-automation / admin default).
    act_as_uid: Optional[int] = None

    def __post_init__(self):
        """Validate configuration after initialization."""
        # Validate URL
        if not self.url:
            raise ValueError("ODOO_URL is required")

        # Ensure URL format
        if not self.url.startswith(("http://", "https://")):
            raise ValueError("ODOO_URL must start with http:// or https://")

        # Validate YOLO mode
        valid_yolo_modes = {"off", "read", "true"}
        if self.yolo_mode not in valid_yolo_modes:
            raise ValueError(
                f"Invalid YOLO mode: {self.yolo_mode}. "
                f"Must be one of: {', '.join(valid_yolo_modes)}"
            )

        # Validate authentication (relaxed for YOLO mode)
        has_api_key = bool(self.api_key)
        has_credentials = bool(self.username and self.password)

        # In YOLO mode, we might need username even with API key for standard auth
        if self.is_yolo_enabled:
            if not has_credentials and not (has_api_key and self.username):
                raise ValueError("YOLO mode requires either username/password or username/API key")
        else:
            if not has_api_key and not has_credentials:
                raise ValueError(
                    "Authentication required: provide either ODOO_API_KEY or "
                    "both ODOO_USER and ODOO_PASSWORD"
                )

        # Validate numeric fields
        if self.default_limit <= 0:
            raise ValueError("ODOO_MCP_DEFAULT_LIMIT must be positive")

        if self.max_limit <= 0:
            raise ValueError("ODOO_MCP_MAX_LIMIT must be positive")

        if self.default_limit > self.max_limit:
            raise ValueError("ODOO_MCP_DEFAULT_LIMIT cannot exceed ODOO_MCP_MAX_LIMIT")

        # Validate log level
        valid_log_levels = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        if self.log_level.upper() not in valid_log_levels:
            raise ValueError(
                f"Invalid log level: {self.log_level}. "
                f"Must be one of: {', '.join(valid_log_levels)}"
            )

        # Validate transport
        valid_transports = {"stdio", "streamable-http"}
        if self.transport not in valid_transports:
            raise ValueError(
                f"Invalid transport: {self.transport}. "
                f"Must be one of: {', '.join(valid_transports)}"
            )

        # Validate port
        if self.port <= 0 or self.port > 65535:
            raise ValueError("Port must be between 1 and 65535")

    @property
    def uses_api_key(self) -> bool:
        """Check if configuration uses API key authentication."""
        return bool(self.api_key)

    @property
    def uses_credentials(self) -> bool:
        """Check if configuration uses username/password authentication."""
        return bool(self.username and self.password)

    @property
    def is_yolo_enabled(self) -> bool:
        """Check if any YOLO mode is active."""
        return self.yolo_mode != "off"

    @property
    def is_user_pinned(self) -> bool:
        """Whether every call is forced to run as a specific end-user."""
        return self.act_as_uid is not None

    @property
    def is_write_allowed(self) -> bool:
        """Check if write operations are allowed in current mode."""
        return self.yolo_mode == "true"

    def get_endpoint_paths(self) -> Dict[str, str]:
        """Get appropriate endpoint paths based on mode.

        The DB endpoint always uses the server-wide ``/xmlrpc/db`` path
        so that database listing works even when multiple databases exist
        (MCP addon routes require a DB context that isn't available yet).

        Returns:
            Dict[str, str]: Mapping of endpoint names to paths
        """
        if self.is_yolo_enabled:
            # Use standard Odoo endpoints in YOLO mode
            return {"db": "/xmlrpc/db", "common": "/xmlrpc/2/common", "object": "/xmlrpc/2/object"}
        else:
            # DB endpoint is always server-wide; common/object use MCP routes
            return {
                "db": "/xmlrpc/db",
                "common": "/mcp/xmlrpc/common",
                "object": "/mcp/xmlrpc/object",
            }

    @classmethod
    def from_env(cls, env_file: Optional[Path] = None) -> "OdooConfig":
        """Create configuration from environment variables.

        Args:
            env_file: Optional path to .env file

        Returns:
            OdooConfig: Validated configuration object
        """
        return load_config(env_file)


def load_config(env_file: Optional[Path] = None) -> OdooConfig:
    """Load configuration from environment variables and .env file.

    Args:
        env_file: Optional path to .env file. If not provided,
                 looks for .env in current directory.

    Returns:
        OdooConfig: Validated configuration object

    Raises:
        ValueError: If required configuration is missing or invalid
    """
    # Check if we have a .env file or environment variables
    if env_file:
        if not env_file.exists():
            raise ValueError(
                f"Configuration file not found: {env_file}\n"
                "Please create a .env file based on .env.example"
            )
        load_dotenv(env_file)
    else:
        # Try to load .env from current directory
        default_env = Path(".env")
        env_loaded = False

        if default_env.exists():
            load_dotenv(default_env)
            env_loaded = True

        # If no .env file found and no ODOO_URL in environment, raise error
        if not env_loaded and not os.getenv("ODOO_URL"):
            raise ValueError(
                "No .env file found and ODOO_URL not set in environment.\n"
                "Please create a .env file based on .env.example or set environment variables."
            )

    # Helper function to get int with default
    def get_int_env(key: str, default: int) -> int:
        value = os.getenv(key)
        if value is None:
            return default
        try:
            return int(value)
        except ValueError:
            raise ValueError(f"{key} must be a valid integer") from None

    # Parse the pinned end-user id. Exactly two values mean "not pinned":
    #   - unset / empty string — the documented signal claude-service sends for
    #     trusted-automation runs (it always sets the var, empty when unpinned);
    #   - an un-substituted "${...}" literal — the spawning environment never
    #     defined the var at all (interactive session, no pinning intended).
    # Anything else that is not a positive integer is a malformed pinning
    # attempt and must FAIL CLOSED: raising here aborts server startup, so a
    # session that was meant to be pinned can never silently run as admin.
    def get_act_as_uid() -> Optional[int]:
        raw = (os.getenv("ODOO_ACT_AS_UID") or "").strip()
        if not raw:
            return None
        if raw.startswith("${") and raw.endswith("}"):
            logger.warning(
                "ODOO_ACT_AS_UID was not substituted (%r) — running unpinned. "
                "If this session was meant to be pinned to an end user, the "
                "spawning environment failed to set the variable.",
                raw,
            )
            return None
        if not raw.isdigit() or int(raw) <= 0:
            raise ValueError(
                f"ODOO_ACT_AS_UID must be a positive integer user id, got {raw!r}. "
                "Refusing to start unpinned: a malformed pin must never fall back "
                "to the admin service account."
            )
        return int(raw)

    # Helper function to parse YOLO mode
    def get_yolo_mode() -> str:
        yolo_env = os.getenv("ODOO_YOLO", "off").strip().lower()
        # Map various inputs to valid modes
        if yolo_env in ["", "false", "0", "off", "no"]:
            return "off"
        elif yolo_env in ["read", "readonly", "read-only"]:
            return "read"
        elif yolo_env in ["true", "1", "yes", "full"]:
            return "true"
        else:
            # Invalid value - will be caught by validation
            return yolo_env

    # Create configuration
    config = OdooConfig(
        url=os.getenv("ODOO_URL", "").strip(),
        api_key=os.getenv("ODOO_API_KEY", "").strip() or None,
        username=os.getenv("ODOO_USER", "").strip() or None,
        password=os.getenv("ODOO_PASSWORD", "").strip() or None,
        database=os.getenv("ODOO_DB", "").strip() or None,
        log_level=os.getenv("ODOO_MCP_LOG_LEVEL", "INFO").strip(),
        default_limit=get_int_env("ODOO_MCP_DEFAULT_LIMIT", 10),
        max_limit=get_int_env("ODOO_MCP_MAX_LIMIT", 100),
        max_smart_fields=get_int_env("ODOO_MCP_MAX_SMART_FIELDS", 15),
        transport=os.getenv("ODOO_MCP_TRANSPORT", "stdio").strip(),
        host=os.getenv("ODOO_MCP_HOST", "localhost").strip(),
        port=get_int_env("ODOO_MCP_PORT", 8000),
        locale=os.getenv("ODOO_LOCALE", "").strip() or None,
        yolo_mode=get_yolo_mode(),
        act_as_uid=get_act_as_uid(),
    )

    return config


# Singleton configuration instance
_config: Optional[OdooConfig] = None


def get_config() -> OdooConfig:
    """Get the singleton configuration instance.

    Returns:
        OdooConfig: The configuration object

    Raises:
        ValueError: If configuration is not yet loaded
    """
    global _config
    if _config is None:
        _config = load_config()
    return _config


def set_config(config: OdooConfig) -> None:
    """Set the singleton configuration instance.

    This is primarily useful for testing.

    Args:
        config: The configuration object to set
    """
    global _config
    _config = config


def reset_config() -> None:
    """Reset the singleton configuration instance.

    This is primarily useful for testing.
    """
    global _config
    _config = None
