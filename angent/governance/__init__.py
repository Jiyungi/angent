"""Non-bypassable Governance Gate: human approval + budget/rate-limit enforcement."""

from .gate import ApprovalResult, GovernanceGate

__all__ = ["ApprovalResult", "GovernanceGate"]
