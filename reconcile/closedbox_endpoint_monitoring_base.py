# ruff: noqa: N815
import json
import logging
from collections import defaultdict
from collections.abc import Callable
from dataclasses import field
from typing import Any
from urllib.parse import urlparse

from pydantic.dataclasses import dataclass

import reconcile.openshift_base as ob
from reconcile import queries
from reconcile.utils.defer import defer
from reconcile.utils.openshift_resource import (
    OpenshiftResource,
    ResourceInventory,
)

LOG = logging.getLogger(__name__)


class ClosedBoxReconcilerError(Exception):
    pass


@dataclass(frozen=True, eq=True)
class BlackboxMonitoringProvider:
    module: str
    # the namespace of a blackbox-exporter provider is mapped as dict
    # since its only use with ob.fetch_current_state is as a dict
    namespace: dict[str, Any] = field(compare=False, hash=False)
    exporterUrl: str


@dataclass(frozen=True, eq=True)
class SignalfxMonitoringProvier:
    # the namespace of a signalfx provider is mapped as dict
    # since its only use with ob.fetch_current_state is as a dict
    namespace: dict[str, Any] = field(compare=False, hash=False)
    exporterUrl: str
    targetFilterLabel: str


@dataclass(frozen=True, eq=True)
class EndpointMonitoringProvider:
    name: str
    provider: str
    description: str
    timeout: str | None = None
    checkInterval: str | None = None
    blackboxExporter: BlackboxMonitoringProvider | None = None
    signalFx: SignalfxMonitoringProvier | None = None
    metricLabels: str | None = None

    @property
    def namespace(self) -> dict[str, Any] | None:
        if self.blackboxExporter:
            return self.blackboxExporter.namespace

        if self.signalFx:
            return self.signalFx.namespace

        return None

    @property
    def metric_labels(self) -> dict[str, Any]:
        return json.loads(self.metricLabels) if self.metricLabels else {}


@dataclass
class Endpoint:
    name: str
    description: str
    url: str

    @dataclass
    class Monitoring:
        provider: EndpointMonitoringProvider

    monitoring: list[Monitoring]


def parse_prober_url(url: str) -> dict[str, str]:
    parsed_url = urlparse(url)
    if parsed_url.scheme not in {"http", "https"}:
        raise ValueError(
            f"the prober URL needs to be an http:// or https:// one but is {url}"
        )
    data = {"url": parsed_url.netloc, "scheme": parsed_url.scheme}
    if parsed_url.path:
        data["path"] = parsed_url.path
    return data


def get_endpoints(provider: str) -> dict[EndpointMonitoringProvider, list[Endpoint]]:
    endpoints: dict[EndpointMonitoringProvider, list[Endpoint]] = defaultdict(
        list[Endpoint]
    )
    for app in queries.get_service_monitoring_endpoints():
        for ep_data in app.get("endPoints") or []:
            monitoring = ep_data.get("monitoring")
            if monitoring:
                ep_data["monitoring"] = [
                    m for m in monitoring if m["provider"]["provider"] == provider
                ]
                ep = Endpoint(**ep_data)
                for mon in ep.monitoring:
                    endpoints[mon.provider].append(ep)
    return endpoints


def fill_desired_state(
    provider: EndpointMonitoringProvider,
    probe: OpenshiftResource,
    ri: ResourceInventory,
) -> None:
    if provider.namespace:
        ri.add_desired(
            cluster=provider.namespace["cluster"]["name"],
            namespace=provider.namespace["name"],
            resource_type=probe.kind,
            name=probe.name,
            value=probe,
        )


@defer
def run_for_provider(
    provider: str,
    probe_builder: Callable[
        [EndpointMonitoringProvider, list[Endpoint]], OpenshiftResource | None
    ],
    integration: str,
    integration_version: str,
    dry_run: bool,
    thread_pool_size: int,
    internal: bool,
    use_jump_host: bool,
    defer: Callable | None = None,
) -> None:
    # prepare
    desired_endpoints = get_endpoints(provider)
    namespaces = [p.namespace for p in desired_endpoints if p.namespace]

    if namespaces:
        ri, oc_map = ob.fetch_current_state(
            namespaces,
            thread_pool_size=thread_pool_size,
            internal=internal,
            use_jump_host=use_jump_host,
            integration=integration,
            integration_version=integration_version,
            override_managed_types=["Probe"],
        )
        if defer:
            defer(oc_map.cleanup)

        # reconcile
        for ep_mon_provider, endpoints in desired_endpoints.items():
            probe = probe_builder(ep_mon_provider, endpoints)
            if probe:
                fill_desired_state(ep_mon_provider, probe, ri)
        ob.publish_metrics(ri, integration)
        ob.realize_data(dry_run, oc_map, ri, thread_pool_size, recycle_pods=False)

        if ri.has_error_registered():
            raise ClosedBoxReconcilerError("ResourceInventory has registered errors")
