"""Shared LLM error types.

Kept in a dedicated module so the HTTP client and the response parser can
raise and catch the same family without circular imports.
"""


class LLMError(Exception):
  """Base class for every language-model failure."""


class LLMConnectionError(LLMError):
  """The server could not be reached (not running, refused, DNS)."""


class LLMTimeoutError(LLMError):
  """The request exceeded the configured read timeout."""


class LLMHTTPError(LLMError):
  """The server answered with a non-200 status."""

  def __init__(self, status_code, body=""):
    self.status_code = status_code
    self.body = body.strip()
    detail = self.body[:300] if self.body else "no response body"
    super().__init__(f"HTTP {status_code}: {detail}")


class LLMResponseError(LLMError):
  """The response was not valid JSON or had an unexpected shape."""
