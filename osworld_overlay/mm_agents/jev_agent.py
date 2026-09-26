"""jev as an OSWorld agent, where OSWorld's runners look for agents.

`scripts/osworld setup` copies this file into OSWorld's `mm_agents/`. The agent itself lives in this
repo, in `typesafe_computer_use.osworld.agent`, where it is tested.
"""

from typesafe_computer_use.osworld.agent import JevAgent

__all__ = ["JevAgent"]
