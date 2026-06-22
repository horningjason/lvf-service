"""
Node-role identity: Forest Guide, Root AMS, or standalone (tree-node / child
/ plain LVF).

This module reads src.runtime_state's _forest_guide_mode / _root_ams flags
(the source of truth, set from LVF_FOREST_GUIDE_MODE / LVF_ROOT_AMS at import
time) and exposes a single mutually-exclusive role enum. It deliberately
imports nothing beyond enum/logging/runtime_state — no lifecycle, find_service,
or federation imports — so it can be imported from anywhere without creating
a cycle.

_routing_only (did GIS load at startup?) is a runtime OUTCOME, not a role,
and does not appear here — it stays in src.app.lifecycle.

Scope note: this module governs node-startup role identity only. The RFC 6739
protocol-level _root_ams / _forest_guide_mode checks inside
src/federation/sync.py and src/federation/coverage.py encode wire-protocol
predicates (e.g. whether to push to a parent vs. a Forest Guide) and are out
of scope for this module.
"""

from __future__ import annotations

import enum
import logging

from src import runtime_state

log = logging.getLogger(__name__)


class NodeRole(enum.Enum):
    FOREST_GUIDE = "forest_guide"
    ROOT_AMS = "root_ams"
    STANDALONE = "standalone"  # tree-node / child / plain LVF


def validate_role_config() -> None:
    """Enforce that node-type flags are mutually exclusive. Call once at startup."""
    if runtime_state._forest_guide_mode and runtime_state._root_ams:
        raise RuntimeError(
            "Invalid configuration: a node cannot be both a Forest Guide "
            "(LVF_FOREST_GUIDE_MODE=true) and a Root AMS (LVF_ROOT_AMS=true). "
            "These are mutually exclusive node types — set exactly one, or neither."
        )


def current_role() -> NodeRole:
    if runtime_state._forest_guide_mode:
        return NodeRole.FOREST_GUIDE
    if runtime_state._root_ams:
        return NodeRole.ROOT_AMS
    return NodeRole.STANDALONE
