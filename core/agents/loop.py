"""
The agent loop: Gemini function calling with a step limit, a deadline and a trace.

    model -> (function calls?) -> run tools in parallel -> results back to model -> ...
          -> text answer

Automatic function calling is off, so every step is visible in the trace and the
loop can enforce limits. On the last allowed step the tools are withdrawn and the
model must answer from what it has.
"""

import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout
from dataclasses import dataclass, field

from google.genai import types

from core import llm
from core.agents import tools as agent_tools
from core.agents.evidence import EvidenceBoard
from core.log import run_in_context

logger = logging.getLogger(__name__)

MAX_STEPS = 3
MIN_SECONDS_FOR_A_CALL = 4
TOOL_TIMEOUT_ERROR = "timed out"
FINAL_NUDGE = "Your tool budget is used up. Write your final output now from the evidence you have."


@dataclass
class LoopResult:
    text: str | None
    steps: list[dict] = field(default_factory=list)
    llm_calls: int = 0
    error: str | None = None


def _accept(response) -> bool:
    return bool(response.function_calls) or llm.has_text(response)


def run_tools(calls: list, board: EvidenceBoard, agent: str, deadline: float) -> list[dict]:
    """Runs tool calls in parallel; a call still running at the deadline becomes {"error": "timed out"}."""
    pool = ThreadPoolExecutor(max_workers=min(4, len(calls)))
    try:
        futures = [pool.submit(run_in_context(agent_tools.run_tool), c.name, dict(c.args or {}), board, agent, deadline)
                   for c in calls]
        outputs = []
        for call, future in zip(calls, futures):
            try:
                outputs.append(future.result(timeout=max(0.0, deadline - time.monotonic())))
            except FuturesTimeout:
                logger.warning("Tool %s timed out at the deadline", call.name)
                outputs.append({"error": TOOL_TIMEOUT_ERROR})
        return outputs
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


def run_tool_loop(agent: str, system: str, prompt: str, tool_names: list[str], board: EvidenceBoard,
                  deadline: float, max_steps: int = MAX_STEPS) -> LoopResult:
    contents = [types.Content(role="user", parts=[types.Part.from_text(text=prompt)])]
    result = LoopResult(text=None)
    tool = agent_tools.declarations(tool_names) if tool_names else None

    for step in range(max_steps + 1):
        remaining = deadline - time.monotonic()
        if remaining < MIN_SECONDS_FOR_A_CALL:
            result.error = "time budget used up"
            break
        use_tools = tool is not None and step < max_steps
        if not use_tools and step > 0:
            contents.append(types.Content(role="user", parts=[types.Part.from_text(text=FINAL_NUDGE)]))
        config = types.GenerateContentConfig(
            system_instruction=system, temperature=0.2, max_output_tokens=1200,
            tools=[tool] if use_tools else None,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )
        started = time.monotonic()
        try:
            response = llm.call(contents, config, budget_seconds=min(remaining, llm.GEMINI_TOTAL_BUDGET_SECONDS),
                                accept=_accept)
        except llm.LLMUnavailableError as e:
            result.error = f"model unavailable ({e})"
            break
        finally:
            result.llm_calls += 1

        calls = list(response.function_calls or []) if use_tools else []
        if not calls:
            result.text = response.text.strip() if llm.has_text(response) else None
            result.steps.append({"agent": agent, "step": step + 1, "action": "answer",
                                 "seconds": round(time.monotonic() - started, 2)})
            return result

        # Run this step's tool calls in parallel, then hand the results back. Each result is
        # awaited only until the deadline, and stragglers aren't joined (a `with` block would wait
        # for them), so a hanging data provider can't push the agent past its budget (review A, S3).
        contents.append(response.candidates[0].content)
        outputs = run_tools(calls, board, agent, deadline)
        parts = []
        for call, output in zip(calls, outputs):
            parts.append(types.Part.from_function_response(name=call.name, response=output))
            result.steps.append({
                "agent": agent, "step": step + 1, "action": "tool", "tool": call.name,
                "args": json.loads(json.dumps(dict(call.args or {}), default=str)),
                "evidence": [e["id"] for e in output.get("evidence", []) if "id" in e],
                "error": output.get("error"), "seconds": round(time.monotonic() - started, 2),
            })
        contents.append(types.Content(role="user", parts=parts))

    return result
