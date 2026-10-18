#!/usr/bin/env python3
"""Deterministic helper for Storyboard Duration / Capacity Engine.

It estimates a basic Chinese-dialogue duration range and adds user/model-provided
non-dialogue timing components. It is deliberately narrow: it does not decide
cinematic pacing or alter storyboard structure.
"""

from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Dict

FAST_CHARS_PER_SEC = 4.2
SLOW_CHARS_PER_SEC = 3.5
LIKELY_CHARS_PER_SEC = 3.8

COMPONENT_KEYS = (
    "serial_action_seconds",
    "pause_seconds",
    "movement_seconds",
    "readability_seconds",
    "reaction_seconds",
    "camera_read_seconds",
)


@dataclass
class CapacityEstimate:
    dialogue_chars: int
    dialogue_min_seconds: float
    dialogue_likely_seconds: float
    dialogue_max_seconds: float
    non_dialogue_seconds: float
    longest_non_dialogue_component_seconds: float
    confirmed_overlap_seconds: float
    action_action_overlap_seconds: float
    dialogue_action_overlap_requested_seconds: float
    dialogue_action_overlap_min_seconds: float
    dialogue_action_overlap_likely_seconds: float
    dialogue_action_overlap_max_seconds: float
    natural_min_seconds: float
    natural_likely_seconds: float
    natural_max_seconds: float
    declared_seconds: float | None
    status: str
    generation_limit_seconds: float | None
    generation_excess_seconds: float | None
    generation_status: str
    generation_natural_risk: str | None


def _round(value: float) -> float:
    return round(max(0.0, value), 2)


def _non_negative_number(payload: Dict[str, Any], key: str, default: float = 0.0) -> float:
    value = float(payload.get(key, default) or 0.0)
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"{key} must be a finite number >= 0")
    return value


def estimate(payload: Dict[str, Any]) -> CapacityEstimate:
    chars_raw = _non_negative_number(payload, "dialogue_chars")
    if not chars_raw.is_integer():
        raise ValueError("dialogue_chars must be an integer >= 0")
    chars = int(chars_raw)

    component_values = [_non_negative_number(payload, key) for key in COMPONENT_KEYS]
    components = sum(component_values)
    longest_component = max(component_values, default=0.0)

    legacy_present = "confirmed_overlap_seconds" in payload
    action_overlap_present = "action_action_overlap_seconds" in payload
    if legacy_present and action_overlap_present:
        raise ValueError(
            "use either confirmed_overlap_seconds (legacy) or action_action_overlap_seconds, not both"
        )
    action_overlap_requested = _non_negative_number(
        payload,
        "action_action_overlap_seconds" if action_overlap_present else "confirmed_overlap_seconds",
    )
    # Concurrent actions cannot take less time than the longest participating component.
    max_action_overlap = max(0.0, components - longest_component)
    action_overlap = min(action_overlap_requested, max_action_overlap)
    effective_action = components - action_overlap

    dialogue_overlap_requested = _non_negative_number(payload, "dialogue_action_overlap_seconds")

    dmin = chars / FAST_CHARS_PER_SEC if chars else 0.0
    dlikely = chars / LIKELY_CHARS_PER_SEC if chars else 0.0
    dmax = chars / SLOW_CHARS_PER_SEC if chars else 0.0

    dialogue_overlap_min = min(dialogue_overlap_requested, dmin, effective_action)
    dialogue_overlap_likely = min(dialogue_overlap_requested, dlikely, effective_action)
    dialogue_overlap_max = min(dialogue_overlap_requested, dmax, effective_action)

    natural_min = dmin + effective_action - dialogue_overlap_min
    natural_likely = dlikely + effective_action - dialogue_overlap_likely
    natural_max = dmax + effective_action - dialogue_overlap_max

    declared_raw = payload.get("declared_seconds", None)
    declared = None if declared_raw is None else _non_negative_number(payload, "declared_seconds")

    if declared is None:
        status = "不确定"
    elif declared + 1e-9 < natural_min:
        status = "过载"
    elif declared + 1e-9 < natural_max:
        status = "可行但偏紧"
    else:
        status = "充裕"

    generation_raw = payload.get("generation_limit_seconds", None)
    generation_limit = None if generation_raw is None else _non_negative_number(
        payload, "generation_limit_seconds"
    )
    if generation_limit is None:
        generation_status = "未指定生成段上限"
        generation_excess = None
        generation_natural_risk = None
    elif natural_min > generation_limit + 1e-9:
        generation_natural_risk = "确定冲突"
    elif natural_max > generation_limit + 1e-9:
        generation_natural_risk = "可能冲突"
    else:
        generation_natural_risk = "暂无冲突"

    if generation_limit is None:
        pass
    elif declared is None:
        generation_status = "不确定"
        generation_excess = None
    elif status == "过载":
        generation_status = "待修正银幕时长"
        generation_excess = None
    elif declared <= generation_limit + 1e-9:
        generation_status = "未超限"
        generation_excess = 0.0
    else:
        generation_status = "冲突"
        generation_excess = declared - generation_limit

    return CapacityEstimate(
        dialogue_chars=chars,
        dialogue_min_seconds=_round(dmin),
        dialogue_likely_seconds=_round(dlikely),
        dialogue_max_seconds=_round(dmax),
        non_dialogue_seconds=_round(components),
        longest_non_dialogue_component_seconds=_round(longest_component),
        confirmed_overlap_seconds=_round(action_overlap),
        action_action_overlap_seconds=_round(action_overlap),
        dialogue_action_overlap_requested_seconds=_round(dialogue_overlap_requested),
        dialogue_action_overlap_min_seconds=_round(dialogue_overlap_min),
        dialogue_action_overlap_likely_seconds=_round(dialogue_overlap_likely),
        dialogue_action_overlap_max_seconds=_round(dialogue_overlap_max),
        natural_min_seconds=_round(natural_min),
        natural_likely_seconds=_round(natural_likely),
        natural_max_seconds=_round(natural_max),
        declared_seconds=None if declared is None else _round(declared),
        status=status,
        generation_limit_seconds=None if generation_limit is None else _round(generation_limit),
        generation_excess_seconds=None if generation_excess is None else _round(generation_excess),
        generation_status=generation_status,
        generation_natural_risk=generation_natural_risk,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Estimate basic storyboard duration capacity from JSON input.")
    parser.add_argument("json_file", type=Path, help="Input JSON file")
    args = parser.parse_args()
    payload = json.loads(args.json_file.read_text(encoding="utf-8"))
    result = estimate(payload)
    print(json.dumps(asdict(result), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
