"""Provider registry: the single home for per-provider display/action facts.

Re-exports the registry's public surface so callers use ``from scrying_at_home
import providers`` and read ``providers.get(...)`` etc., while the descriptor
table itself lives in ``registry`` (kept a stdlib-only leaf).
"""
from scrying_at_home.providers.registry import (
    Provider,
    get,
    all_providers,
    ingest_dir_providers,
    is_local_cli,
    resume_cli_args,
    resume_shell,
    provider_url,
)

__all__ = [
    "Provider",
    "get",
    "all_providers",
    "ingest_dir_providers",
    "is_local_cli",
    "resume_cli_args",
    "resume_shell",
    "provider_url",
]
