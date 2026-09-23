"""The classifier's questions, asked of a chat model instead of jev.

jev returns a full distribution over up to 255 labels with a calibrated confidence, for a fraction
of a cent, and the loop is built around that. Nothing else is shaped quite like it. This asks an
ordinary chat model the same Choice and Noul questions through a JSON schema, and reads a
distribution back out of its reply, so an endpoint that does not serve jev is still somewhere the
loop can run. It is slower and dearer, and its confidence is the model's own opinion rather than a
calibrated number: the floor in --min-confidence means less here.

It answers `system_one` in the TypeSafe SDK's own answer types, so `decide`, the call count and the
scenario harness cannot tell it from the real client.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import anthropic
import openai
from typesafe_sdk import Choice, ChoiceAnswer, Noul, NoulAnswer

from .writer import Writer, parse_json

MAX_SCORED = 8  # alternatives the model is asked to score per question
CHOICE_TOKENS = 2048
NOUL_TOKENS = 256

CHOICE_SYSTEM = (
    "You are the decision model of a computer-use agent. You are given the state of a user's screen "
    "and a set of questions. Each question lists labels with a description of what choosing that "
    "label would mean. Answer every question: pick the single label that best fits the state, give "
    "your confidence in it between 0 and 1, and score the labels you seriously considered so their "
    "probabilities sum to about 1. Use only labels from that question's own list, exactly as "
    "written. Be honest about confidence: a low number is how the agent knows to stop instead of "
    "acting on a guess."
)

NOUL_SYSTEM = (
    "You judge one yes/no claim about the state you are given. Reply with the probability between "
    "0 and 1 that the claim is true. Be calibrated: 0.5 means genuinely uncertain."
)


class ClassifierError(RuntimeError):
    """The chat model could not be reached, or answered a question with a label it was not offered."""


class ChatClassifier:
    """A chat model behind the one call the loop makes of its classifier, `system_one`.

    `client` is any writer client (`messages.create`, Anthropic-shaped); in practice an OpenAIWriter.
    Its requests are counted as the classifier's, not the writer's, because that is the job they do.
    """

    def __init__(self, client: Writer, model: str, label: str):
        self._client = client
        self.model = model
        self.label = label

    def __enter__(self) -> ChatClassifier:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def system_one(self, *, state: dict, questions: dict) -> SimpleNamespace:
        nouls = {name: q for name, q in questions.items() if isinstance(q, Noul)}
        choices = {name: q for name, q in questions.items() if isinstance(q, Choice)}
        if set(questions) - set(nouls) - set(choices):
            raise ClassifierError(f"no chat form for the questions {sorted(set(questions) - set(nouls) - set(choices))}")
        answers: dict[str, Any] = {name: self._noul(state, q.instructions) for name, q in nouls.items()}
        if choices:
            answers.update(self._choices(state, choices))
        return SimpleNamespace(answers=answers)

    def _choices(self, state: dict, questions: dict[str, Choice]) -> dict[str, ChoiceAnswer]:
        labels = {name: list(q.criteria) for name, q in questions.items()}
        packet = {
            "state": state,
            "questions": {name: {"question": q.instructions, "labels": dict(q.criteria)} for name, q in questions.items()},
        }
        properties = {name: _answer_schema(labels[name]) for name in questions}
        data = self._ask(CHOICE_SYSTEM, packet, properties, CHOICE_TOKENS)
        return {name: read_answer(data.get(name), labels[name], name) for name in questions}

    def _noul(self, state: dict, claim: str) -> NoulAnswer:
        properties = {"probability": {"type": "number"}, "reason": {"type": "string"}}
        data = self._ask(NOUL_SYSTEM, {"state": state, "claim": claim}, properties, NOUL_TOKENS)
        return NoulAnswer(noul=clamp(data.get("probability")))

    def _ask(self, system: str, packet: dict, properties: dict, max_tokens: int) -> dict:
        schema = {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}
        try:
            response = self._client.messages.create(
                model=self.model,
                max_tokens=max_tokens,
                system=f"{system}\n\nAnswer with a single JSON object and nothing else, matching this schema:\n{json.dumps(schema)}",
                messages=[{"role": "user", "content": [{"type": "text", "text": json.dumps(packet)}]}],
                output_config={"format": {"type": "json_schema", "schema": schema}},
            )
        except (anthropic.APIError, openai.APIError) as e:
            raise ClassifierError(f"{self.label} failed: {e}") from e
        return parse_json("".join(block.text for block in response.content if block.type == "text"))


def _answer_schema(labels: list[str]) -> dict[str, Any]:
    """One question's slot in the reply: the pick, how sure, and what else was in the running."""
    return {
        "type": "object",
        "properties": {
            "choice": {"type": "string", "enum": labels, "description": "the label you pick, copied exactly"},
            "confidence": {"type": "number", "description": "0 to 1, how sure you are of that label"},
            "alternatives": {
                "type": "array",
                "maxItems": MAX_SCORED,
                "description": "the labels you considered, your pick among them, with probabilities summing to about 1",
                "items": {
                    "type": "object",
                    "properties": {"label": {"type": "string", "enum": labels}, "probability": {"type": "number"}},
                    "required": ["label", "probability"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["choice", "confidence", "alternatives"],
        "additionalProperties": False,
    }


def read_answer(raw: object, labels: list[str], name: str) -> ChoiceAnswer:
    """One question's reply as a ChoiceAnswer, holding the model to the labels it was offered.

    A model that invents a label has not answered the question, so that is an error rather than a
    fallback: acting on a made-up label would click something nobody chose. The probabilities are
    normalised and the pick is guaranteed a share, so a reply that scores everything but its own
    pick still leaves the run a confidence to gate on.
    """
    if not isinstance(raw, dict) or raw.get("choice") not in labels:
        raise ClassifierError(
            f"the chat classifier answered {name!r} with {json.dumps(raw)[:160]}, which is not one of its labels"
        )
    choice = raw["choice"]
    scored: dict[str, float] = {}
    for entry in raw.get("alternatives") or []:
        if isinstance(entry, dict) and entry.get("label") in labels:
            scored[entry["label"]] = scored.get(entry["label"], 0.0) + clamp(entry.get("probability"), upper=None)
    confidence = clamp(raw.get("confidence"))
    scored.setdefault(choice, confidence)
    total = sum(scored.values())
    probabilities = {label: round(p / total, 4) for label, p in scored.items()} if total > 0 else {choice: 1.0}
    return ChoiceAnswer(choice=choice, confidence=confidence, probabilities=probabilities)


def clamp(value: object, upper: float | None = 1.0) -> float:
    """A number the model sent, as a probability: never below zero, never above `upper`, zero if not a number."""
    try:
        number = max(0.0, float(value))
    except (TypeError, ValueError):
        return 0.0
    return number if upper is None else min(upper, number)
