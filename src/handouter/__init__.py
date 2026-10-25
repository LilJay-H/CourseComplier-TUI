"""Course Compiler core package (legacy Python namespace: handouter).

The package prepares local lecture materials and an Agent handoff bundle. It does
not invoke a language model or upload course materials.
"""

__version__ = "0.2.0"


def branded_env(legacy_name: str, default: str | None = None) -> str | None:
    """Read a public Course Compiler setting with its Handouter fallback.

    Presence, including an empty value, wins over the legacy setting. Callers
    keep their existing validation so an explicitly empty new value cannot
    silently turn on a less restrictive legacy configuration.
    """
    import os

    if not legacy_name.startswith("HANDOUTER_"):
        raise ValueError("expected a legacy Handouter environment variable")
    current_name = "COURSE_COMPILER_" + legacy_name[len("HANDOUTER_"):]
    if current_name in os.environ:
        value = os.environ[current_name]
        if legacy_name == "HANDOUTER_REQUIRE_TMPFS" and value not in {"0", "1"}:
            raise ValueError(f"{current_name} must be '0' or '1'")
        return value
    if legacy_name in os.environ:
        return os.environ[legacy_name]
    return default


def required_branded_env(legacy_name: str) -> str:
    value = branded_env(legacy_name)
    if value is None or not value.strip():
        raise KeyError("COURSE_COMPILER_" + legacy_name[len("HANDOUTER_"):])
    return value
