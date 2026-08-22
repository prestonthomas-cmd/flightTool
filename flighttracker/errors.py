"""Exception types shared across the package."""


class FlightTrackerError(Exception):
    """Base class for every error this tool raises deliberately."""


class ConfigError(FlightTrackerError):
    """The watchlist file is missing, malformed, or internally inconsistent."""

    def __init__(self, problems):
        if isinstance(problems, str):
            problems = [problems]
        self.problems = list(problems)
        super().__init__("\n".join(f"- {p}" for p in self.problems))


class FetchError(FlightTrackerError):
    """A price lookup failed after every retry was used up.

    `permanent` marks the failures that no retry could have fixed — a missing
    dependency, an API that no longer exists — which are also the ones that
    will hit every other search in the run in exactly the same way.
    """

    def __init__(self, message: str, permanent: bool = False):
        super().__init__(message)
        self.permanent = permanent
