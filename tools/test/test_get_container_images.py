import pytest
from pytest_mock import MockerFixture

from reconcile.gql_definitions.common.namespaces import NamespaceV1
from reconcile.test.fixtures import Fixtures
from reconcile.utils.oc import OCNative
from reconcile.utils.oc_map import OCMap
from tools.cli_commands.container_images_report import (
    fetch_pods_images_from_namespaces,
)

fxt = Fixtures("container_images_report")


@pytest.fixture
def observability_pods() -> list[dict]:
    return fxt.get_anymarkup("app-sre-observability-stage-pods.yaml")


@pytest.fixture
def pipeline_pods() -> list[dict]:
    return fxt.get_anymarkup("app-sre-pipelines-pods.yaml")


@pytest.fixture
def namespaces() -> list[NamespaceV1]:
    pipelines_ns = fxt.get_anymarkup("app-sre-pipelines-namespace.yaml")
    observability_ns = fxt.get_anymarkup("app-sre-observability-stage-namespace.yaml")
    return [NamespaceV1(**observability_ns), NamespaceV1(**pipelines_ns)]


@pytest.fixture
def oc(
    mocker: MockerFixture,
    observability_pods: list[dict],
    pipeline_pods: list[dict],
) -> OCNative:
    oc = mocker.patch("reconcile.utils.oc.OCNative", autospec=True)
    oc.get_items.side_effect = [observability_pods, pipeline_pods]
    return oc


@pytest.fixture
def oc_map(mocker: MockerFixture, oc: OCNative) -> OCMap:
    oc_map = mocker.patch("reconcile.utils.oc_map.OCMap", autospec=True)
    oc_map.get_cluster.return_value = oc
    return oc_map


def test_fetch_no_filter(namespaces: list[NamespaceV1], oc_map: OCMap) -> None:
    images = fetch_pods_images_from_namespaces(
        namespaces=namespaces,
        oc_map=oc_map,
        thread_pool_size=2,
    )

    assert images == [
        {
            "name": "quay.io/app-sre/clamav",
            "namespaces": "app-sre-pipelines",
            "namespace_apps": "app-sre",
            "count": 1,
        },
        {
            "name": "quay.io/app-sre/internal-redhat-ca",
            "namespaces": "app-sre-observability-stage,app-sre-pipelines",
            "namespace_apps": "app-sre,app-sre-observability",
            "count": 3,
        },
        {
            "name": "quay.io/app-sre/prom-cloudwatch-exporter",
            "namespaces": "app-sre-observability-stage",
            "namespace_apps": "app-sre-observability",
            "count": 1,
        },
        {
            "name": "quay.io/prometheus/blackbox-exporter",
            "namespaces": "app-sre-observability-stage",
            "namespace_apps": "app-sre-observability",
            "count": 1,
        },
        {
            "name": "quay.io/redhat-appstudio/clamav-db",
            "namespaces": "app-sre-pipelines",
            "namespace_apps": "app-sre",
            "count": 1,
        },
        {
            "name": "quay.io/redhat-services-prod/app-sre-tenant/gitlab-project-exporter-main/gitlab-project-exporter-main",
            "namespaces": "app-sre-observability-stage",
            "namespace_apps": "app-sre-observability",
            "count": 1,
        },
        {
            "name": "quay.io/redhatproductsecurity/rapidast",
            "namespaces": "app-sre-pipelines",
            "namespace_apps": "app-sre",
            "count": 1,
        },
        {
            "name": "registry.redhat.io/openshift-pipelines/pipelines-entrypoint-rhel8",
            "namespaces": "app-sre-pipelines",
            "namespace_apps": "app-sre",
            "count": 3,
        },
    ]


def test_fetch_exclude_pattern(namespaces: list[NamespaceV1], oc_map: OCMap) -> None:
    images = fetch_pods_images_from_namespaces(
        namespaces=namespaces,
        oc_map=oc_map,
        thread_pool_size=2,
        exclude_pattern="quay.io/redhat|quay.io/app-sre",
    )
    assert images == [
        {
            "name": "quay.io/prometheus/blackbox-exporter",
            "namespaces": "app-sre-observability-stage",
            "namespace_apps": "app-sre-observability",
            "count": 1,
        },
        {
            "name": "registry.redhat.io/openshift-pipelines/pipelines-entrypoint-rhel8",
            "namespaces": "app-sre-pipelines",
            "namespace_apps": "app-sre",
            "count": 3,
        },
    ]


def test_fetch_include_pattern(namespaces: list[NamespaceV1], oc_map: OCMap) -> None:
    images = fetch_pods_images_from_namespaces(
        namespaces=namespaces,
        oc_map=oc_map,
        thread_pool_size=2,
        include_pattern="^registry.redhat.io",
    )
    assert images == [
        {
            "name": "registry.redhat.io/openshift-pipelines/pipelines-entrypoint-rhel8",
            "namespaces": "app-sre-pipelines",
            "namespace_apps": "app-sre",
            "count": 3,
        },
    ]


def test_fetch_exception(
    namespaces: list[NamespaceV1], mocker: MockerFixture, observability_pods: list[dict]
) -> None:
    oc = mocker.patch("reconcile.utils.oc.OCNative", autospec=True)
    oc.get_items.side_effect = [observability_pods, Exception("generic error")]
    oc_map = mocker.patch("reconcile.utils.oc_map.OCMap", autospec=True)
    oc_map.get_cluster.return_value = oc

    images = fetch_pods_images_from_namespaces(
        namespaces=namespaces,
        oc_map=oc_map,
        thread_pool_size=2,
    )

    assert images == [
        {
            "name": "quay.io/app-sre/internal-redhat-ca",
            "namespaces": "app-sre-observability-stage",
            "namespace_apps": "app-sre-observability",
            "count": 2,
        },
        {
            "name": "quay.io/app-sre/prom-cloudwatch-exporter",
            "namespaces": "app-sre-observability-stage",
            "namespace_apps": "app-sre-observability",
            "count": 1,
        },
        {
            "name": "quay.io/prometheus/blackbox-exporter",
            "namespaces": "app-sre-observability-stage",
            "namespace_apps": "app-sre-observability",
            "count": 1,
        },
        {
            "name": "quay.io/redhat-services-prod/app-sre-tenant/gitlab-project-exporter-main/gitlab-project-exporter-main",
            "namespaces": "app-sre-observability-stage",
            "namespace_apps": "app-sre-observability",
            "count": 1,
        },
        {
            "name": "error",
            "namespaces": "app-sre-pipelines/generic error",
            "namespace_apps": "",
            "count": 1,
        },
    ]
