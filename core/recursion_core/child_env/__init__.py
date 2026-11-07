"""New-core child environment (CSL-785, candidate). See README.md."""

from recursion_core.child_env.launch import spawn
from recursion_core.child_env.policy import BASE_NAMES, ENGINE_PROFILES, EngineProfile, EnvRefused, ValidatedEnv, build_child_env

__all__ = ["BASE_NAMES", "ENGINE_PROFILES", "EngineProfile", "EnvRefused", "ValidatedEnv", "build_child_env", "spawn"]
