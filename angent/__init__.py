"""Angent — a self-improving, goal-driven deal-sourcing agent for solo angel investors.

This package contains the Python core (the critical path): the control loop,
the six cooperating agents, the ClickHouse blackboard persistence layer, the
pluggable scorer and sender interfaces, the governance gate, and the
observability helpers. Sponsor integrations are layered behind stable
interfaces / feature flags so no single integration can block the demo.
"""

__version__ = "0.1.0"
