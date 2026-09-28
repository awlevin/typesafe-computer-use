"""The tokens an OSWorld agent spent on one task, counted from OpenAI Responses API replies.

OSWorld's GPT agent records no usage, so the Luna runner's agent (`osworld_overlay/mm_agents/
luna_agent.py`) adds each reply's `usage` to a `Meter` and writes it beside the task's score as
`usage.json`, in the shape of jev's `run.json`: tokens per model, the seconds the model took, and
the settings that change what a task costs, such as the reasoning effort. `results.py` reads either.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

USAGE_FILE = "usage.json"


def _field(value: Any, name: str) -> Any:
    """A field of an SDK object or of the dict it dumps to."""
    return value.get(name) if isinstance(value, dict) else getattr(value, name, None)


def _count(value: Any, *path: str) -> int:
    for name in path:
        if value is None:
            return 0
        value = _field(value, name)
    return int(value or 0)


@dataclass
class Meter:
    """One task's requests to one model. `input_tokens` leaves out the cached ones, as jev's run.json
    does; the Responses API counts them in. `reasoning_tokens` are part of `output_tokens`."""

    model: str
    requests: int = 0
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    seconds: float = 0.0
    settings: dict[str, str] = field(default_factory=dict)  # what the model ran with, such as its reasoning effort

    def add(self, response: Any, seconds: float) -> None:
        """Count one reply. A reply without usage still counts as a request."""
        usage = _field(response, "usage")
        cached = _count(usage, "input_tokens_details", "cached_tokens")
        self.requests += 1
        self.input_tokens += _count(usage, "input_tokens") - cached
        self.cached_input_tokens += cached
        self.output_tokens += _count(usage, "output_tokens")
        self.reasoning_tokens += _count(usage, "output_tokens_details", "reasoning_tokens")
        self.seconds += seconds

    def summary(self) -> dict:
        counts = {
            "requests": self.requests,
            "input_tokens": self.input_tokens,
            "cached_input_tokens": self.cached_input_tokens,
            "output_tokens": self.output_tokens,
            "reasoning_tokens": self.reasoning_tokens,
        }
        return {"usage": {self.model: counts}, "seconds": round(self.seconds, 3), "settings": self.settings}

    def write(self, folder: str | Path) -> Path:
        path = Path(folder) / USAGE_FILE
        path.write_text(json.dumps(self.summary(), indent=1) + "\n", encoding="utf-8")
        return path
