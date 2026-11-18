"""Automated dead-code cleanup PRs for the Safe Sunset pipeline (GitHub REST API, sandbox)."""
from .change import CleanupChange, CleanupError, build_cleanup_change, remove_feature_block  # noqa: F401
from .github import CleanupPR, get_cleanup_provider, set_cleanup_provider  # noqa: F401
