"""Shared markers for expensive, evaluation-only, platform- or dependency-bound tests.

scripts/check_release.py decides whether a skip is acceptable purely from its
reason string, so keep these reasons stable and use the markers instead of
ad-hoc skip messages.
"""
import importlib.util
import os
import sys
import unittest

SLOW_ENV = 'HANDOUTER_SLOW_TESTS'
EVAL_ENV = 'HANDOUTER_EVAL_TESTS'
SLOW_REASON = 'slow test; set HANDOUTER_SLOW_TESTS=1'
EVAL_REASON = 'evaluation tooling test; set HANDOUTER_EVAL_TESTS=1'
WINDOWS_REASON = 'Windows-only test'
MACOS_REASON = 'macOS-only test'
POSIX_REASON = 'POSIX-only test'
CLOUD_DEPS_REASON = 'optional cloud dependencies not installed'
HTML_REASON = 'HTML extra not installed'
JSONSCHEMA_REASON = 'jsonschema test dependency not installed'


def _enabled(name: str) -> bool:
    return os.environ.get(name) == '1'


def _installed(*modules: str) -> bool:
    return all(importlib.util.find_spec(name) is not None for name in modules)


def slow(obj):
    # Read the switch when the test module is imported, not when this helper is,
    # because check_release imports this module before setting the switches.
    return unittest.skipUnless(_enabled(SLOW_ENV), SLOW_REASON)(obj)


def eval_only(obj):
    return unittest.skipUnless(_enabled(EVAL_ENV), EVAL_REASON)(obj)


windows_only = unittest.skipUnless(os.name == 'nt', WINDOWS_REASON)
macos_only = unittest.skipUnless(sys.platform == 'darwin', MACOS_REASON)
posix_only = unittest.skipIf(os.name == 'nt', POSIX_REASON)
requires_cloud_deps = unittest.skipUnless(_installed('pydantic'), CLOUD_DEPS_REASON)
requires_html = unittest.skipUnless(_installed('markdown_it', 'mdit_py_plugins', 'latex2mathml'), HTML_REASON)
requires_jsonschema = unittest.skipUnless(_installed('jsonschema'), JSONSCHEMA_REASON)
