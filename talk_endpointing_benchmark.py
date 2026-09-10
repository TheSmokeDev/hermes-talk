"""Deterministic, content-free metrics for endpointing trace fixtures.

Times are monotonic integer milliseconds chosen by the caller.  This module has
no clock, provider, audio, or transcript dependency and deliberately stores no
media or text.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from math import ceil


@dataclass(frozen=True, slots=True)
class EndpointingTrace:
    """Receipts for one annotated opportunity to activate.

    ``annotated_utterance_end_ms=None`` describes a period in which no utterance
    was annotated; any provider or playback receipt then counts as one false
    activation.  A timeout is terminal but is not itself an activation.  A
    speech-stop before the annotation is a premature cutoff, and a turn-end
    before it is a false split.  ``timed_out_at_ms`` is an explicit terminal
    receipt, not a timeout inferred from missing data.
    """

    trace_id: str
    annotated_utterance_end_ms: int | None = None
    provider_speech_stop_ms: int | None = None
    provider_turn_end_ms: int | None = None
    first_audio_ms: int | None = None
    local_playback_ms: int | None = None
    timed_out_at_ms: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.trace_id, str) or not self.trace_id.strip():
            raise ValueError("trace_id must be a non-empty string")

        names = (
            "annotated_utterance_end_ms",
            "provider_speech_stop_ms",
            "provider_turn_end_ms",
            "first_audio_ms",
            "local_playback_ms",
            "timed_out_at_ms",
        )
        for name in names:
            value = getattr(self, name)
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value < 0
            ):
                raise ValueError(f"{name} must be a non-negative integer or None")

        stop = self.provider_speech_stop_ms
        turn = self.provider_turn_end_ms
        audio = self.first_audio_ms
        playback = self.local_playback_ms
        timeout = self.timed_out_at_ms
        annotated = self.annotated_utterance_end_ms

        if stop is not None and turn is not None and stop > turn:
            raise ValueError("provider speech-stop cannot follow provider turn-end")
        if audio is not None and turn is None:
            raise ValueError("first-audio requires a provider turn-end receipt")
        if turn is not None and audio is not None and turn > audio:
            raise ValueError("provider turn-end cannot follow first-audio")
        if playback is not None and audio is None:
            raise ValueError("local-playback requires a first-audio receipt")
        if audio is not None and playback is not None and audio > playback:
            raise ValueError("first-audio cannot follow local-playback")
        if stop is not None and timeout is not None and stop > timeout:
            raise ValueError("provider speech-stop cannot follow timeout")
        if timeout is not None and any(value is not None for value in (turn, audio, playback)):
            raise ValueError("timeout and completion receipts are mutually exclusive")
        if timeout is not None and annotated is not None and timeout < annotated:
            raise ValueError("timeout cannot precede the annotated utterance end")


@dataclass(frozen=True, slots=True)
class LatencyDistribution:
    """Nearest-rank latency summary.

    For ``n`` sorted samples, percentile ``p`` selects the 1-based item at
    ``ceil(p * n)``.  Empty distributions report ``None`` rather than inventing
    zero latency.
    """

    count: int
    p50_ms: int | None
    p95_ms: int | None
    max_ms: int | None


@dataclass(frozen=True, slots=True)
class EndpointingSummary:
    trace_count: int
    endpoint_latency: LatencyDistribution
    playback_latency: LatencyDistribution
    premature_cutoff_count: int
    false_split_count: int
    timeout_count: int
    false_activation_count: int


def _distribution(samples: Iterable[int]) -> LatencyDistribution:
    ordered = sorted(samples)
    if not ordered:
        return LatencyDistribution(count=0, p50_ms=None, p95_ms=None, max_ms=None)

    def nearest_rank(percentile: float) -> int:
        return ordered[ceil(percentile * len(ordered)) - 1]

    return LatencyDistribution(
        count=len(ordered),
        p50_ms=nearest_rank(0.50),
        p95_ms=nearest_rank(0.95),
        max_ms=ordered[-1],
    )


def evaluate_endpointing_traces(traces: Iterable[EndpointingTrace]) -> EndpointingSummary:
    """Evaluate immutable receipts without consulting a live provider.

    Endpoint latency runs from annotated utterance end to provider turn-end.
    Premature (negative) turn-ends are classified as false splits and excluded
    from latency.  Playback latency runs from first-audio receipt to the local
    playback receipt, isolating local delivery from model response time.
    """

    materialized = tuple(traces)
    if any(not isinstance(trace, EndpointingTrace) for trace in materialized):
        raise TypeError("traces must contain EndpointingTrace records")
    ids = tuple(trace.trace_id for trace in materialized)
    if len(ids) != len(set(ids)):
        raise ValueError("trace_id values must be unique")

    endpoint_latencies: list[int] = []
    playback_latencies: list[int] = []
    premature_cutoffs = 0
    false_splits = 0
    timeouts = 0
    false_activations = 0

    for trace in materialized:
        annotated = trace.annotated_utterance_end_ms
        activation_receipts = (
            trace.provider_speech_stop_ms,
            trace.provider_turn_end_ms,
            trace.first_audio_ms,
            trace.local_playback_ms,
        )
        if annotated is None:
            false_activations += int(any(receipt is not None for receipt in activation_receipts))
        else:
            stop = trace.provider_speech_stop_ms
            turn = trace.provider_turn_end_ms
            premature_cutoffs += int(stop is not None and stop < annotated)
            false_splits += int(turn is not None and turn < annotated)
            if turn is not None and turn >= annotated:
                endpoint_latencies.append(turn - annotated)

        if trace.first_audio_ms is not None and trace.local_playback_ms is not None:
            playback_latencies.append(trace.local_playback_ms - trace.first_audio_ms)
        timeouts += int(trace.timed_out_at_ms is not None)

    return EndpointingSummary(
        trace_count=len(materialized),
        endpoint_latency=_distribution(endpoint_latencies),
        playback_latency=_distribution(playback_latencies),
        premature_cutoff_count=premature_cutoffs,
        false_split_count=false_splits,
        timeout_count=timeouts,
        false_activation_count=false_activations,
    )
