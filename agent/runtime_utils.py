"""Runtime helpers for agent execution paths."""


EMPTY_RESPONSE_ERROR = "Error: agent returned an empty response."


def ensure_non_empty_response(response: str) -> str:
    """Normalize final agent output.

    Args:
        response: Candidate final response text from an agent run.

    Returns:
        The original response when non-empty, otherwise a stable error string.
    """
    if response and response.strip():
        return response
    return EMPTY_RESPONSE_ERROR
