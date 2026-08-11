"""Configuration subsystem — two-tier YAML, typed model, secret resolution."""

from jarn.config.loader import ConfigError, load_config
from jarn.config.migrations import (
    ConfigFileDiagnostic,
    ConfigMigrationPlan,
    ConfigMigrationResult,
    apply_config_migration,
    diagnose_config_file,
    migrate_config_file,
    plan_config_migration,
    restore_config_backup,
)
from jarn.config.schema import (
    Config,
    PermissionMode,
    PolicyConfig,
    ProviderConfig,
    ProviderType,
)

__all__ = [
    "Config",
    "ConfigError",
    "ConfigFileDiagnostic",
    "ConfigMigrationPlan",
    "ConfigMigrationResult",
    "PermissionMode",
    "PolicyConfig",
    "ProviderConfig",
    "ProviderType",
    "apply_config_migration",
    "diagnose_config_file",
    "load_config",
    "migrate_config_file",
    "plan_config_migration",
    "restore_config_backup",
]
