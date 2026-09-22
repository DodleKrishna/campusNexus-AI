"""Deterministic services layer (Context Service and friends).

Nothing under app/services may call an LLM, plan, or route between agents --
see CLAUDE.md's description of the Context Service as a service, not an
agent.
"""
