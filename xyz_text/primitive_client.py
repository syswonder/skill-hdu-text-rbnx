# SPDX-License-Identifier: MIT
"""Atlas discovery + gRPC calls to the plotter primitive.

The text skill never touches PLC sockets. It resolves the three primitive contracts through Atlas, then calls the generated gRPC stubs:

    robonix/primitive/plotter/trajectory
    robonix/primitive/plotter/home
    robonix/primitive/plotter/geometry

The mirrored contract files under ``capabilities/primitive/plotter``
exist only so codegen emits these stubs. They are not declared as
provided capabilities. Global motion parameters are owned by the
primitive (MCP ``plotter_set_params``); this skill does not push
SET_PARAM per job.
"""
from __future__ import annotations

import importlib
import threading
import time
from dataclasses import dataclass
from functools import lru_cache

import grpc

from robonix_api import ATLAS
from robonix_api.atlas_types import GrpcParams, Transport

REQUIRED_CONTRACTS = {
    "trajectory": "robonix/primitive/plotter/trajectory",
    "home": "robonix/primitive/plotter/home",
    "geometry": "robonix/primitive/plotter/geometry",
}


class PrimitiveDiscoveryError(RuntimeError):
    """One or more primitive contracts were not available in Atlas."""


class PrimitiveCallError(RuntimeError):
    """A primitive gRPC call failed (unreachable, timeout, upstream error)."""


@dataclass(frozen=True)
class Binding:
    endpoint: str
    service_name: str
    method: str


@dataclass(frozen=True)
class Geometry:
    """Authoritative machine geometry reported by the plotter primitive."""

    x_min: float
    x_max: float
    y_min: float
    y_max: float
    z_up: float
    z_down: float


@lru_cache(maxsize=None)
def _generated_module(names: tuple[str, ...]):
    errors: list[str] = []
    for name in names:
        try:
            return importlib.import_module(name)
        except ImportError as exc:
            errors.append(f"{name}: {exc}")
    raise RuntimeError(
        "generated gRPC code not found (run scripts/build.sh): " + "; ".join(errors)
    )


def _pb2():
    return _generated_module(("plotter_pb2", "robonix_contracts_pb2"))


def _pb2_grpc():
    return _generated_module(("robonix_contracts_pb2_grpc", "plotter_pb2_grpc"))


def _binding(channel, contract_id: str) -> Binding:
    params = getattr(channel, "params", None)
    if not isinstance(params, GrpcParams) or not params.service_name or not params.method:
        raise PrimitiveDiscoveryError(
            f"Atlas returned no gRPC binding metadata for {contract_id}"
        )
    return Binding(
        endpoint=str(channel.endpoint),
        service_name=params.service_name,
        method=params.method,
    )


def resolve_endpoints(
    skill,
    *,
    timeout_s: float = 0.0,
    poll_s: float = 1.0,
) -> dict[str, Binding]:
    """Resolve all required primitive contracts, polling until timeout.

    ``timeout_s <= 0`` makes a single attempt. ``skill`` is the Robonix
    ``Skill`` instance; Atlas owns channel bookkeeping, so only the
    endpoint and gRPC binding metadata are retained.
    """
    budget = max(0.0, float(timeout_s))
    deadline = time.monotonic() + budget
    resolved: dict[str, Binding] = {}
    last_errors: dict[str, str] = {}

    while True:
        for key, contract_id in REQUIRED_CONTRACTS.items():
            if key in resolved:
                continue
            try:
                capability = ATLAS.find_unique_capability(
                    contract_id=contract_id,
                    transport=Transport.GRPC,
                )
                channel = skill.connect_capability(
                    capability, contract_id, Transport.GRPC
                )
                resolved[key] = _binding(channel, contract_id)
                last_errors.pop(key, None)
            except Exception as exc:  # noqa: BLE001 - retry until deadline
                last_errors[key] = str(exc)

        missing = [key for key in REQUIRED_CONTRACTS if key not in resolved]
        if not missing:
            return resolved

        remaining = deadline - time.monotonic()
        if remaining <= 0.0:
            detail = "; ".join(
                f"{REQUIRED_CONTRACTS[key]}: {last_errors.get(key, 'not found')}"
                for key in missing
            )
            raise PrimitiveDiscoveryError(
                f"plotter primitive contracts not available: {detail}"
            )
        time.sleep(min(max(0.05, poll_s), remaining))


class PrimitiveClient:
    """Cached gRPC stubs for one set of resolved bindings."""

    def __init__(
        self, bindings: dict[str, Binding], *, default_timeout_s: float = 600.0
    ):
        missing = [key for key in REQUIRED_CONTRACTS if key not in bindings]
        if missing:
            raise ValueError(f"missing primitive bindings: {missing}")
        self._bindings = dict(bindings)
        self._default_timeout = float(default_timeout_s)
        self._lock = threading.Lock()
        self._channels: dict[str, object] = {}
        self._rpcs: dict[str, object] = {}

    def _rpc(self, key: str):
        with self._lock:
            rpc = self._rpcs.get(key)
            if rpc is not None:
                return rpc

            binding = self._bindings[key]
            channel = self._channels.get(binding.endpoint)
            if channel is None:
                channel = grpc.insecure_channel(
                    binding.endpoint, options=[("grpc.enable_http_proxy", 0)]
                )
                self._channels[binding.endpoint] = channel

            module = _pb2_grpc()
            stub_name = f"{binding.service_name}Stub"
            stub_class = getattr(module, stub_name, None)
            if stub_class is None:
                available = sorted(
                    name for name in vars(module) if name.endswith("Stub")
                )
                raise PrimitiveCallError(
                    f"generated stub {stub_name!r} not found; available: {available}"
                )
            rpc = getattr(stub_class(channel), binding.method, None)
            if rpc is None:
                raise PrimitiveCallError(
                    f"generated stub {stub_name!r} has no RPC {binding.method!r}"
                )
            self._rpcs[key] = rpc
            return rpc

    def _timeout(self, timeout_s: float | None) -> float:
        if timeout_s is None:
            return self._default_timeout
        value = float(timeout_s)
        return value if value > 0.0 else self._default_timeout

    def run_trajectory(self, waypoints, *, timeout_s: float | None = None):
        """Execute ``(x, y, z)`` waypoints through the primitive.

        The PLC has no per-point motion profile: the global speed,
        acceleration, and pen heights are whatever was last set through
        the primitive's ``set_params`` contract. This skill never changes
        them, so a user-selected profile survives every write.
        """
        pb2 = _pb2()
        request = pb2.Trajectory_Request()
        for waypoint in waypoints:
            request.waypoints.add(
                x=float(waypoint.x),
                y=float(waypoint.y),
                z=float(waypoint.z),
            )
        try:
            return self._rpc("trajectory")(
                request, timeout=self._timeout(timeout_s)
            )
        except grpc.RpcError as exc:
            raise PrimitiveCallError(
                f"trajectory call failed: {_rpc_error(exc)}"
            ) from exc

    def home(self, *, timeout_s: float | None = None):
        pb2 = _pb2()
        try:
            return self._rpc("home")(pb2.Home_Request(), timeout=self._timeout(timeout_s))
        except grpc.RpcError as exc:
            raise PrimitiveCallError(f"home call failed: {_rpc_error(exc)}") from exc

    def get_geometry(self, *, timeout_s: float | None = None) -> Geometry:
        """Read the machine geometry once: drawable area + pen heights.

        Static configuration, not feedback: the PLC has no readback, so
        this only differs between machines when the primitive config does.
        """
        pb2 = _pb2()
        try:
            response = self._rpc("geometry")(
                pb2.Geometry_Request(), timeout=self._timeout(timeout_s)
            )
        except grpc.RpcError as exc:
            raise PrimitiveCallError(
                f"geometry call failed: {_rpc_error(exc)}"
            ) from exc
        if not response.success:
            raise PrimitiveCallError(
                f"geometry call rejected: {response.message or 'unknown error'}"
            )
        return Geometry(
            x_min=float(response.x_min),
            x_max=float(response.x_max),
            y_min=float(response.y_min),
            y_max=float(response.y_max),
            z_up=float(response.z_up),
            z_down=float(response.z_down),
        )

    def close(self) -> None:
        with self._lock:
            for channel in self._channels.values():
                try:
                    channel.close()
                except Exception:  # noqa: BLE001 - teardown must not raise
                    pass
            self._channels.clear()
            self._rpcs.clear()


def _rpc_error(exc: grpc.RpcError) -> str:
    try:
        code = exc.code().name
        details = exc.details() or ""
    except Exception:  # noqa: BLE001 - some RpcError implementations are bare
        return str(exc)
    return f"{code}: {details}" if details else code