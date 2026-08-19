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
    """A price lookup failed after every retry was used up."""
