"""Public, offline lecture material runtime.

This package contains no website adapters, cloud credentials or authoring rules.
"""

LOCAL_PROTOCOL_VERSION = 1


def local_budget_env() -> str | None:
    """Resolve the local media policy in this standalone public wheel."""
    import os

    if "COURSE_COMPILER_LOCAL_MAX_BYTES" in os.environ:
        return os.environ["COURSE_COMPILER_LOCAL_MAX_BYTES"]
    return os.environ.get("HANDOUTER_LOCAL_MAX_BYTES")
