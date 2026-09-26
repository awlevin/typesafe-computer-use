"""Count the requests and tokens each model takes, at the two clients every request goes through."""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from types import SimpleNamespace

CLASSIFIER = "classifier"
WRITER = "writer"
MODELS = (CLASSIFIER, WRITER)


@dataclass
class Usage:
    """The tokens one model took, over the requests that came back with a reply.

    `input_tokens` are the uncached ones, including those written to a cache; `cached_input_tokens`
    were read from one. A reply that reports no usage still counts as a request, with zero tokens.
    """

    requests: int = 0
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0

    def record(self, input_tokens: int = 0, cached_input_tokens: int = 0, output_tokens: int = 0) -> None:
        self.requests += 1
        self.input_tokens += input_tokens
        self.cached_input_tokens += cached_input_tokens
        self.output_tokens += output_tokens


@dataclass
class Calls:
    """How many requests went to each model, and the seconds they took.

    The point of the design is that the classifier takes nearly all of them. This is the number
    that says whether it does: a run the writer has to steer at every turn shows up here.
    """

    count: dict[str, int] = field(default_factory=lambda: dict.fromkeys(MODELS, 0))
    seconds: dict[str, float] = field(default_factory=lambda: dict.fromkeys(MODELS, 0.0))
    usage: dict[str, Usage] = field(default_factory=dict)  # keyed by the model id each reply names

    @contextmanager
    def record(self, model: str) -> Iterator[None]:
        """Count one request to `model`. A request that fails was still made, so it still counts."""
        started = time.perf_counter()
        try:
            yield
        finally:
            self.count[model] += 1
            self.seconds[model] += time.perf_counter() - started

    def used(self, model: str, input_tokens: int = 0, cached_input_tokens: int = 0, output_tokens: int = 0) -> None:
        """Add one reply's tokens to `model`, the model id it came from."""
        self.usage.setdefault(model, Usage()).record(input_tokens, cached_input_tokens, output_tokens)

    def share(self, model: str) -> float | None:
        """The fraction of all requests that went to `model`, or None before any request."""
        total = sum(self.count.values())
        return self.count[model] / total if total else None

    def summary(self) -> dict:
        return {
            model: {
                "calls": self.count[model],
                "share": None if self.share(model) is None else round(self.share(model), 3),
                "seconds": round(self.seconds[model], 3),
            }
            for model in MODELS
        }

    def tokens(self) -> dict:
        """Tokens per model id, for run.json."""
        return {model: asdict(usage) for model, usage in self.usage.items()}

    def line(self) -> str:
        """One log line: `calls: classifier 12 (86%, 3.1s)  writer 2 (14%, 8.0s)`."""
        parts = [f"{model} {self.count[model]} ({(self.share(model) or 0):.0%}, {self.seconds[model]:.1f}s)" for model in MODELS]
        return "calls: " + "  ".join(parts)


class MeteredClassifier:
    """The TypeSafe client, counting each request. `system_one` is the only call the package makes.

    TypeSafe's reply names its `model` and reports `usage` as `input_tokens` and `output_tokens`,
    with no cached count.
    """

    def __init__(self, client, calls: Calls):
        self._client = client
        self._calls = calls

    def system_one(self, **request):
        with self._calls.record(CLASSIFIER):
            reply = self._client.system_one(**request)
        usage = getattr(reply, "usage", None)
        self._calls.used(
            _model(getattr(reply, "model", None), request.get("model"), CLASSIFIER),
            input_tokens=_tokens(usage, "input_tokens"),
            output_tokens=_tokens(usage, "output_tokens"),
        )
        return reply


class MeteredWriter:
    """The Anthropic client, counting each request. `messages.create` is the only call the package makes."""

    def __init__(self, client, calls: Calls):
        self._client = client
        self._calls = calls
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **request):
        with self._calls.record(WRITER):
            reply = self._client.messages.create(**request)
        usage = getattr(reply, "usage", None)
        self._calls.used(
            _model(request.get("model"), WRITER),
            input_tokens=_tokens(usage, "input_tokens") + _tokens(usage, "cache_creation_input_tokens"),
            cached_input_tokens=_tokens(usage, "cache_read_input_tokens"),
            output_tokens=_tokens(usage, "output_tokens"),
        )
        return reply


def _model(*names) -> str:
    """The first name that is a model id, as a reply or a request may leave it out."""
    return next(name for name in names if isinstance(name, str) and name)


def _tokens(usage, name: str) -> int:
    """One count from a usage object, or 0 when the reply leaves it out or reports it as None."""
    count = getattr(usage, name, None)
    return count if isinstance(count, int) else 0
