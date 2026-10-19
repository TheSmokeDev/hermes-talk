import tomllib
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from talk_endpointing_benchmark import (
    EndpointingTrace,
    LatencyDistribution,
    evaluate_endpointing_traces,
)


def test_benchmark_module_is_in_the_distribution_module_allowlist() -> None:
    project = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))

    assert "talk_endpointing_benchmark" in project["tool"]["setuptools"]["py-modules"]


def test_synthetic_traces_produce_deterministic_nearest_rank_summary() -> None:
    traces = tuple(
        EndpointingTrace(
            trace_id=f"trace-{index}",
            annotated_utterance_end_ms=1_000,
            provider_speech_stop_ms=1_000,
            provider_turn_end_ms=1_000 + endpoint,
            first_audio_ms=2_000,
            local_playback_ms=2_000 + playback,
        )
        for index, (endpoint, playback) in enumerate(
            zip(range(1, 21), range(101, 121), strict=True)
        )
    )

    forward = evaluate_endpointing_traces(traces)
    reverse = evaluate_endpointing_traces(reversed(traces))

    assert forward == reverse
    assert forward.endpoint_latency == LatencyDistribution(20, 10, 19, 20)
    assert forward.playback_latency == LatencyDistribution(20, 110, 119, 120)
    assert forward.premature_cutoff_count == 0
    assert forward.false_split_count == 0
    assert forward.timeout_count == 0
    assert forward.false_activation_count == 0


def test_counts_classified_outcomes_and_excludes_negative_endpoint_latency() -> None:
    summary = evaluate_endpointing_traces(
        (
            EndpointingTrace(
                "premature",
                annotated_utterance_end_ms=100,
                provider_speech_stop_ms=90,
                provider_turn_end_ms=95,
            ),
            EndpointingTrace("timeout", annotated_utterance_end_ms=100, timed_out_at_ms=500),
            EndpointingTrace("false-activation", provider_speech_stop_ms=10),
            EndpointingTrace("true-negative"),
            EndpointingTrace(
                "zero-boundary",
                annotated_utterance_end_ms=100,
                provider_speech_stop_ms=100,
                provider_turn_end_ms=100,
                first_audio_ms=100,
                local_playback_ms=100,
            ),
        )
    )

    assert summary.trace_count == 5
    assert summary.endpoint_latency == LatencyDistribution(1, 0, 0, 0)
    assert summary.playback_latency == LatencyDistribution(1, 0, 0, 0)
    assert summary.premature_cutoff_count == 1
    assert summary.false_split_count == 1
    assert summary.timeout_count == 1
    assert summary.false_activation_count == 1


def test_silence_only_timeout_is_terminal_but_not_a_false_activation() -> None:
    summary = evaluate_endpointing_traces(
        (EndpointingTrace("silence-timeout", timed_out_at_ms=500),)
    )

    assert summary.timeout_count == 1
    assert summary.false_activation_count == 0


def test_empty_input_reports_absent_percentiles_honestly() -> None:
    summary = evaluate_endpointing_traces(())

    empty = LatencyDistribution(0, None, None, None)
    assert summary.trace_count == 0
    assert summary.endpoint_latency == empty
    assert summary.playback_latency == empty
    assert summary.premature_cutoff_count == 0
    assert summary.false_split_count == 0
    assert summary.timeout_count == 0
    assert summary.false_activation_count == 0


def test_trace_records_are_immutable() -> None:
    trace = EndpointingTrace("immutable", annotated_utterance_end_ms=1)

    with pytest.raises(FrozenInstanceError):
        trace.annotated_utterance_end_ms = 2  # type: ignore[misc]


@pytest.mark.parametrize(
    ("kwargs", "message"),
    (
        ({"trace_id": ""}, "trace_id"),
        ({"trace_id": "x", "annotated_utterance_end_ms": -1}, "non-negative"),
        (
            {
                "trace_id": "x",
                "provider_speech_stop_ms": 2,
                "provider_turn_end_ms": 1,
            },
            "speech-stop",
        ),
        ({"trace_id": "x", "first_audio_ms": 1}, "requires"),
        ({"trace_id": "x", "local_playback_ms": 1}, "requires"),
        (
            {
                "trace_id": "x",
                "provider_turn_end_ms": 2,
                "first_audio_ms": 1,
            },
            "turn-end",
        ),
        (
            {
                "trace_id": "x",
                "provider_turn_end_ms": 1,
                "first_audio_ms": 3,
                "local_playback_ms": 2,
            },
            "first-audio",
        ),
        (
            {
                "trace_id": "x",
                "provider_turn_end_ms": 1,
                "timed_out_at_ms": 2,
            },
            "mutually exclusive",
        ),
        (
            {
                "trace_id": "x",
                "annotated_utterance_end_ms": 2,
                "timed_out_at_ms": 1,
            },
            "cannot precede",
        ),
        (
            {
                "trace_id": "x",
                "provider_speech_stop_ms": 3,
                "timed_out_at_ms": 2,
            },
            "speech-stop cannot follow timeout",
        ),
    ),
)
def test_malformed_timelines_are_rejected(kwargs: dict[str, object], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        EndpointingTrace(**kwargs)  # type: ignore[arg-type]


def test_duplicate_trace_ids_and_untyped_inputs_are_rejected() -> None:
    with pytest.raises(ValueError, match="unique"):
        evaluate_endpointing_traces((EndpointingTrace("same"), EndpointingTrace("same")))

    with pytest.raises(TypeError, match="EndpointingTrace"):
        evaluate_endpointing_traces((object(),))  # type: ignore[arg-type]
