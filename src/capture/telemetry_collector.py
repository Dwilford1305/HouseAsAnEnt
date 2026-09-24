"""Stage 1 Data Capture: harvest IT infrastructure health telemetry.

This module probes household-controlled assets only: one ICMP echo to the
configured router, and one read of the local Docker daemon. Each observation
is a dictionary that keeps the complete, unedited probe payload (ELT).

When a probe times out or the daemon is unreachable — including an isolated
CI runner with no Docker socket — that component is replaced by the matching
row in ``data/raw/mock_telemetry_logs.json``. A failed probe does not discard
a successful one, and it does not abort the batch.

Department: IT Infrastructure (network ping and container telemetry).
No warehouse I/O and no staging export live here.
"""

from __future__ import annotations

import json
import logging
import os
import pprint
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Final, List, Optional, Pattern, Union

from dotenv import load_dotenv


# Resolve paths from this file so the script is deterministic regardless of CWD.
PROJECT_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
MOCK_TELEMETRY_PATH: Final[Path] = (
    PROJECT_ROOT / "data" / "raw" / "mock_telemetry_logs.json"
)

# 12-factor config: the router address lives in .env, never as a literal in source.
load_dotenv(dotenv_path=PROJECT_ROOT / ".env")
ROUTER_IP: Final[str] = os.getenv("ROUTER_IP", "127.0.0.1")

LOGGER: Final[logging.Logger] = logging.getLogger(__name__)

# iputils prints "time=4.12 ms". The optional "<" covers "time<1 ms".
_PING_LATENCY_MS: Final[Pattern[str]] = re.compile(
    r"time[=<](?P<latency>\d+(?:\.\d+)?)\s*ms",
    re.IGNORECASE,
)

# Wall-clock cap. ``ping -c 1`` can still block until the kernel gives up
# on an unreachable host, which would stall the capture batch.
_PING_TIMEOUT_SECONDS: Final[int] = 5

MetricValue = Union[int, float, None]


class TelemetryObservation(dict):
    """Stage 1 capture payload for one infrastructure metric.

    Typed as a dict subclass so ``pprint`` and JSON callers see a plain
    mapping, while attribute names stay documented for reviewers.

    Keys:
        timestamp: UTC collection time, ISO 8601 with a ``Z`` suffix.
        source_component: Probe identity (``router_ping`` or ``docker_stats``).
        metric_name: Measured field (``latency_ms`` or ``active_containers``).
        metric_value: Parsed number, or ``None`` when the probe has no sample.
        status: ``ONLINE`` / ``TIMEOUT`` for ICMP, ``HEALTHY`` for the daemon.
        raw_payload: Complete, unedited stdout or daemon JSON document.
    """


class TelemetryCaptureError(Exception):
    """A live probe could not produce an observation and must use the fixture."""


def _utc_timestamp() -> str:
    """Return the current UTC time as an ISO 8601 string ending in ``Z``.

    Returns:
        A timestamp such as ``2026-09-23T22:00:00Z``.
    """
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ping_stdout(stdout: str) -> tuple[Optional[float], str]:
    """Read round-trip latency and reachability from one ping transcript.

    Args:
        stdout: Complete standard output of ``ping -c 1``, unmodified.

    Returns:
        A pair of latency in milliseconds (``None`` when no sample was
        printed) and status ``ONLINE`` or ``TIMEOUT``.
    """
    match = _PING_LATENCY_MS.search(stdout)
    if match is None:
        # No RTT sample means the echo was not answered.
        return None, "TIMEOUT"
    return float(match.group("latency")), "ONLINE"


def collect_icmp_ping(target_ip: str = ROUTER_IP) -> TelemetryObservation:
    """Send one ICMP echo and project latency onto the capture schema.

    Args:
        target_ip: Router address from ``ROUTER_IP``, defaulting to loopback.

    Returns:
        An observation whose ``raw_payload`` is the full ping stdout.

    Raises:
        TelemetryCaptureError: If the ping binary is missing, the probe
            exceeds the wall-clock cap, or the echo times out. Callers
            substitute the mock fixture instead of failing the batch.
    """
    try:
        # List argv, never shell=True: the env value is one argument, not a command.
        completed: subprocess.CompletedProcess[str] = subprocess.run(
            ["ping", "-c", "1", target_ip],
            capture_output=True,
            text=True,
            timeout=_PING_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise TelemetryCaptureError(
            f"ICMP probe to {target_ip} did not complete: {exc}"
        ) from exc

    # ELT: keep the transcript even when we later decide it is a timeout.
    raw_payload: str = completed.stdout
    latency_ms, status = parse_ping_stdout(raw_payload)
    if completed.returncode != 0 or status == "TIMEOUT":
        raise TelemetryCaptureError(
            f"ICMP probe to {target_ip} timed out (exit {completed.returncode})."
        )

    return TelemetryObservation(
        timestamp=_utc_timestamp(),
        source_component="router_ping",
        metric_name="latency_ms",
        metric_value=latency_ms,
        status=status,
        raw_payload=raw_payload,
    )


def collect_docker_health() -> TelemetryObservation:
    """Read active container count and daemon health from the local socket.

    The Docker SDK is imported inside this function so a runner without the
    package still imports the module and degrades to the fixture.

    Returns:
        An observation whose ``raw_payload`` is the full ``/info`` JSON
        document returned by the daemon, with no keys removed.

    Raises:
        TelemetryCaptureError: If the SDK is missing or the daemon refuses
            the connection.
    """
    try:
        import docker
        from docker.errors import DockerException
    except ImportError as exc:
        raise TelemetryCaptureError(
            "Docker SDK is not installed; cannot read the local daemon."
        ) from exc

    try:
        client = docker.from_env()
        # ping() is the daemon liveness check; info() is the full document.
        client.ping()
        daemon_info: Dict[str, object] = client.info()
    except DockerException as exc:
        raise TelemetryCaptureError(
            f"Docker daemon is inaccessible: {exc}"
        ) from exc

    active_containers: int = int(daemon_info.get("ContainersRunning", 0))
    # default=str keeps non-JSON daemon fields (datetimes) without dropping them.
    raw_payload: str = json.dumps(daemon_info, default=str)

    return TelemetryObservation(
        timestamp=_utc_timestamp(),
        source_component="docker_stats",
        metric_name="active_containers",
        metric_value=active_containers,
        status="HEALTHY",
        raw_payload=raw_payload,
    )


def _observation_from_mock_row(row: Dict[str, object]) -> TelemetryObservation:
    """Project one fixture row onto the Stage 1 capture schema.

    Args:
        row: One object from ``mock_telemetry_logs.json``.

    Returns:
        An observation. ``raw_payload`` is copied verbatim from the fixture.

    Raises:
        KeyError: If the row is missing a field the capture schema requires.
    """
    telemetry_type: str = str(row["telemetry_type"])
    if telemetry_type == "router_ping":
        metric_name = "latency_ms"
        metric_value: MetricValue = float(str(row["latency_ms"]))
    elif telemetry_type == "docker_stats":
        metric_name = "active_containers"
        metric_value = int(str(row["active_containers"]))
    else:
        raise KeyError(f"Unknown telemetry_type in mock fixture: {telemetry_type}")

    raw_payload = row["raw_payload"]
    if not isinstance(raw_payload, str):
        raise KeyError("Mock raw_payload must be the original string document.")

    return TelemetryObservation(
        timestamp=str(row["timestamp"]),
        source_component=telemetry_type,
        metric_name=metric_name,
        metric_value=metric_value,
        status=str(row["status"]),
        raw_payload=raw_payload,
    )


def load_mock_telemetry(
    mock_path: Path = MOCK_TELEMETRY_PATH,
) -> List[TelemetryObservation]:
    """Load the synthetic telemetry fixture used when a live probe fails.

    Args:
        mock_path: Path to ``data/raw/mock_telemetry_logs.json``.

    Returns:
        Capture observations in fixture order.

    Raises:
        FileNotFoundError: If the fixture is missing.
        json.JSONDecodeError: If the fixture is not valid JSON.
    """
    if not mock_path.is_file():
        raise FileNotFoundError(
            f"Telemetry fixture not found at {mock_path}. "
            "Place mock_telemetry_logs.json in data/raw/."
        )

    raw_document: str = mock_path.read_text(encoding="utf-8")
    rows: object = json.loads(raw_document)
    if not isinstance(rows, list):
        raise json.JSONDecodeError("Fixture root must be a list.", raw_document, 0)

    observations: List[TelemetryObservation] = []
    for row in rows:
        if isinstance(row, dict):
            observations.append(_observation_from_mock_row(row))
    return observations


def _fallback_observation(source_component: str) -> TelemetryObservation:
    """Return the fixture row for one probe after a live capture failure.

    Args:
        source_component: ``router_ping`` or ``docker_stats``.

    Returns:
        The matching mock observation.

    Raises:
        TelemetryCaptureError: If the fixture has no row for that component.
    """
    for observation in load_mock_telemetry():
        if observation["source_component"] == source_component:
            return observation
    raise TelemetryCaptureError(
        f"Mock fixture has no {source_component} row at {MOCK_TELEMETRY_PATH}."
    )


def collect_infrastructure_telemetry() -> List[TelemetryObservation]:
    """Collect router ping and Docker health, degrading per probe.

    Returns:
        Two observations in probe order (ICMP, then Docker). A probe that
        times out or cannot reach its source is replaced by its mock row.
    """
    observations: List[TelemetryObservation] = []

    try:
        observations.append(collect_icmp_ping(ROUTER_IP))
    except TelemetryCaptureError as exc:
        # Graceful degradation: a timeout is an ingestion miss, not a crash.
        LOGGER.warning("ICMP capture degraded to mock fixture. Reason: %s", exc)
        observations.append(_fallback_observation("router_ping"))

    try:
        observations.append(collect_docker_health())
    except TelemetryCaptureError as exc:
        LOGGER.warning("Docker capture degraded to mock fixture. Reason: %s", exc)
        observations.append(_fallback_observation("docker_stats"))

    return observations


def main() -> None:
    """Print the Stage 1 telemetry batch as a list of ELT dictionaries."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s | %(name)s | %(message)s",
    )
    print("\n=== IT Infrastructure Telemetry (Stage 1 Capture) ===")
    try:
        observations: List[TelemetryObservation] = collect_infrastructure_telemetry()
    except (OSError, json.JSONDecodeError, TelemetryCaptureError) as exc:
        # The fixture itself is the last resort; a missing file is logged, not raised.
        LOGGER.error("Telemetry batch could not be captured. Reason: %s", exc)
        return

    pprint.pprint(observations, width=100, sort_dicts=False)


if __name__ == "__main__":
    main()
