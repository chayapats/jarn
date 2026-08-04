"""Small, dependency-free helpers shared across J.A.R.N. subsystems.

Modules here import from the standard library only — never from other ``jarn``
packages — so any layer (config, memory, permissions, the agent runtime) can use
them without dragging a subsystem onto its import path.
"""
