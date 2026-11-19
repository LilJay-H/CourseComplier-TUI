"""Shared error type for offline standard-package validation (no runtime RPC)."""

class LocalError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message
