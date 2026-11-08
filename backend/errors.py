"""Hard-rule errors. The orchestrator stops on any HardRuleError."""


class HardRuleError(Exception):
    """Base for every refusal that must stop the agent loop."""

    def __init__(self, reason, detail=None):
        super().__init__(reason)
        self.reason = reason
        self.detail = detail or {}


class AuthorityError(HardRuleError):
    """authorize(actor, action) refused the action. Always audited before it is raised."""


class SeedGuardError(HardRuleError):
    """An agent-facing call asked for a seed it may not see (>= 80, or outside 60-79 for held-out)."""


class NotFound(Exception):
    pass
