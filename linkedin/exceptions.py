class AuthenticationError(Exception):
    """Custom exception for 401 Unauthorized errors."""
    pass


class TerminalStateError(Exception):
    """Profile is already done or dead — caller must skip it"""
    pass


class SkipProfile(Exception):
    """Profile must be skipped."""
    pass


class ReachedConnectionLimit(Exception):
    """ Weekly connection limit reached. """
    pass


class MessageSendAmbiguous(Exception):
    """Voyager POST failed in a way that may have delivered the message.

    Raised when the API call timed out, returned a 5xx after retries, or the
    response body could not be parsed. Callers MUST NOT retry or fall back —
    doing so risks double-delivery. Surface to the operator instead.
    """
    pass


class LeadNotFoundError(Exception):
    """send_message task received a public_id with no matching Lead."""
    pass


class LoginFailed(Exception):
    """playwright_login could not reach /feed — captcha unsolved, timeout,
    or claim was stolen mid-login. Callers should release the claim and
    retry on the next loop iteration rather than crashing the worker.
    """
    pass

