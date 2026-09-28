"""OSWorld's GPT agent as it ships, counting the tokens each task spends.

`scripts/osworld setup` copies this file into OSWorld's `mm_agents/`, beside the agent it extends,
and `run_multienv_luna.py` builds it. Nothing about how the agent acts changes: each reply the
Responses API sends goes on to the agent untouched, after its usage is added to a `Meter`
(`typesafe_computer_use.osworld.usage`, where it is tested).
"""

import time

from mm_agents.gpt_response_api import GPTResponseAPIAgent

from typesafe_computer_use.osworld.usage import Meter


class MeteredGPTResponseAPIAgent(GPTResponseAPIAgent):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.meter = self._new_meter()

    def _new_meter(self):
        return Meter(self.model, settings={"reasoning_effort": str(self.reasoning_effort)})

    def reset(self, *args, **kwargs):
        super().reset(*args, **kwargs)
        self.meter = self._new_meter()

    def _create_response(self, request_input, instructions):
        started = time.perf_counter()
        response = super()._create_response(request_input, instructions)
        self.meter.add(response, time.perf_counter() - started)
        return response

    def write_usage(self, folder):
        self.meter.write(folder)


__all__ = ["MeteredGPTResponseAPIAgent"]
