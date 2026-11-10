"""New-core child environment (CSL-785, candidate). See README.md."""

from recursion_core.child_env.launch import spawn
from recursion_core.child_env.policy import (
    AUTH_MODES,
    BASE_NAMES,
    ENGINE_PROFILES,
    ChildRoute,
    EngineProfile,
    EnvRefused,
    ValidatedEnv,
    build_child_env,
)

__all__ = ["AUTH_MODES", "BASE_NAMES", "ENGINE_PROFILES", "ChildRoute", "EngineProfile", "EnvRefused", "ValidatedEnv",
           "build_child_env", "spawn"]
