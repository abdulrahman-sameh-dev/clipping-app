"""Package metadata for the standalone AI provider system."""

__version__ = "0.1.0"

from clipping_ai.errors import AIError, ErrorCategory  # noqa: F401

__all__ = ["AIError", "ErrorCategory", "__version__"]
