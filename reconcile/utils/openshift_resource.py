# ruff: noqa: SIM114
import base64
import contextlib
import copy
import datetime
import hashlib
import json
import re
from collections.abc import Mapping
from threading import Lock

import semver
from pydantic import BaseModel

from reconcile.external_resources.meta import SECRET_UPDATED_AT
from reconcile.utils.metrics import GaugeMetric

SECRET_MAX_KEY_LENGTH = 253


class ResourceKeyExistsError(Exception):
    pass


class ResourceNotManagedError(Exception):
    pass


class ConstructResourceError(Exception):
    def __init__(self, msg):
        super().__init__("error constructing openshift resource: " + str(msg))


# Regexes for kubernetes objects fields which have to adhere to DNS-1123
DNS_SUBDOMAIN_MAX_LENGTH = 253
DNS_SUBDOMAIN_RE = re.compile(
    r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?(\.[a-z0-9]([-a-z0-9]*[a-z0-9])?)*$"
)
DNS_LABEL_MAX_LENGTH = 63
DNS_LABEL_RE = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
DNS_NAMES_URL = (
    "https://kubernetes.io/docs/concepts/overview/working-with-objects/names/"
)

IGNORABLE_DATA_FIELDS = ["service-ca.crt"]
# these labels existance and/or value is determined by a controller running
# on the cluster. we need to ignore their existance in the current state,
# otherwise we will deal with constant reconciliation
CONTROLLER_MANAGED_LABELS: dict[str, set[str | re.Pattern]] = {
    "ManagedCluster": {
        "clusterID",
        "managed-by",
        "openshiftVersion",
        re.compile(r"feature.open-cluster-management.io/.*"),
    }
}

QONTRACT_ANNOTATION_INTEGRATION = "qontract.integration"
QONTRACT_ANNOTATION_INTEGRATION_VERSION = "qontract.integration_version"
QONTRACT_ANNOTATION_SHA256SUM = "qontract.sha256sum"
QONTRACT_ANNOTATION_UPDATE = "qontract.update"
QONTRACT_ANNOTATION_CALLER_NAME = "qontract.caller_name"

QONTRACT_ANNOTATIONS = {
    QONTRACT_ANNOTATION_INTEGRATION,
    QONTRACT_ANNOTATION_INTEGRATION_VERSION,
    QONTRACT_ANNOTATION_SHA256SUM,
    QONTRACT_ANNOTATION_UPDATE,
    QONTRACT_ANNOTATION_CALLER_NAME,
}


class OpenshiftResource:
    def __init__(
        self,
        body,
        integration,
        integration_version,
        error_details="",
        caller_name=None,
        validate_k8s_object=True,
    ):
        self.body = body
        self.integration = integration
        self.integration_version = integration_version
        self.error_details = error_details
        self.caller_name = caller_name
        if validate_k8s_object:
            self.verify_valid_k8s_object()

    def __eq__(self, other):
        return self.obj_intersect_equal(self.body, other.body)

    def obj_intersect_equal(self, obj1, obj2, depth=0):
        # obj1 == d_item
        # obj2 == c_item
        if obj1.__class__ != obj2.__class__:
            return False

        if isinstance(obj1, dict):
            for obj1_k, obj1_v in obj1.items():
                obj2_v = obj2.get(obj1_k, None)
                if obj2_v is None:
                    if obj1_v:
                        return False
                if self.ignorable_field(obj1_k):
                    pass
                elif self.ignorable_key_value_pair(obj1_k, obj1_v):
                    pass
                elif depth == 0 and obj1_k == "status":
                    pass
                elif obj1_k == "labels":
                    diff = [
                        k
                        for k in obj2_v
                        if k not in obj1_v
                        and not OpenshiftResource.is_controller_managed_label(
                            self.kind, k
                        )
                    ]
                    if diff or not self.obj_intersect_equal(obj1_v, obj2_v, depth + 1):
                        return False
                elif obj1_k in {"data", "matchLabels"}:
                    diff = [
                        k
                        for k in obj2_v
                        if k not in obj1_v and k not in IGNORABLE_DATA_FIELDS
                    ]
                    if diff or not self.obj_intersect_equal(obj1_v, obj2_v, depth + 1):
                        return False
                elif obj1_k == "env":
                    for v in obj2_v or []:
                        if "name" in v and len(v) == 1:
                            v["value"] = ""
                    if not self.obj_intersect_equal(obj1_v, obj2_v, depth + 1):
                        return False
                elif obj1_k == "cpu":
                    equal = self.cpu_equal(obj1_v, obj2_v)
                    if not equal:
                        return False
                elif obj1_k == "apiVersion":
                    valid = self.api_version_mutation(obj1_v, obj2_v)
                    if not valid:
                        return False
                elif obj1_k == "imagePullSecrets":
                    # remove default pull secrets added by k8s
                    obj2_v_clean = [s for s in obj2_v if "-dockercfg-" not in s["name"]]
                    if not self.obj_intersect_equal(obj1_v, obj2_v_clean, depth + 1):
                        return False
                elif not self.obj_intersect_equal(obj1_v, obj2_v, depth + 1):
                    return False

        elif isinstance(obj1, list):
            if len(obj1) != len(obj2):
                return False
            for index, item in enumerate(obj1):
                if not self.obj_intersect_equal(item, obj2[index], depth + 1):
                    return False

        elif obj1 != obj2:
            return False

        return True

    @staticmethod
    def ignorable_field(val):
        ignorable_fields = [
            "kubectl.kubernetes.io/last-applied-configuration",
            "creationTimestamp",
            "resourceVersion",
            "generation",
            "selfLink",
            "uid",
            "fieldRef",
        ]
        return val in ignorable_fields

    @staticmethod
    def ignorable_key_value_pair(key, val):
        ignorable_key_value_pair = {"annotations": None, "divisor": "0"}
        return bool(
            key in ignorable_key_value_pair and ignorable_key_value_pair[key] == val
        )

    @staticmethod
    def cpu_equal(val1, val2):
        # normalize both to string
        with contextlib.suppress(Exception):
            val1 = f"{int(float(val1) * 1000)}m"
        with contextlib.suppress(Exception):
            val2 = f"{int(float(val2) * 1000)}m"
        return val1 == val2

    @staticmethod
    def api_version_mutation(val1, val2):
        # required temporarily, pending response on
        # https://redhat.service-now.com/surl.do?n=INC1224482
        if val1 == "apps/v1" and val2 == "extensions/v1beta1":
            return True
        if val1 == "extensions/v1beta1" and val2 == "apps/v1":
            return True
        if val1 == "networking.k8s.io/v1" and val2 == "extensions/v1beta1":
            return True
        return val1 == val2

    @property
    def name(self):
        # PipelineRun name can be empty when creating
        if self.kind == "PipelineRun" and "name" not in self.body["metadata"]:
            return self.body["metadata"]["generateName"][:-1]
        else:
            return self.body["metadata"]["name"]

    @property
    def kind(self):
        return self.body["kind"]

    @property
    def annotations(self):
        return self.body["metadata"].get("annotations", {})

    @property
    def kind_and_group(self):
        return fully_qualified_kind(self.kind, self.body["apiVersion"])

    @property
    def caller(self):
        try:
            return (
                self.caller_name
                or self.body["metadata"]["annotations"]["qontract.caller_name"]
            )
        except KeyError:
            return None

    def verify_valid_k8s_object(self):
        try:
            assert self.name
            assert self.kind
        except (KeyError, TypeError) as e:
            msg = f"resource invalid data ({e.__class__.__name__}). details: {self.error_details}"
            raise ConstructResourceError(msg) from None

        if self.kind not in {
            "Role",
            "RoleBinding",
            "ClusterRole",
            "ClusterRoleBinding",
        } and (
            not DNS_SUBDOMAIN_RE.match(self.name)
            or not len(self.name) <= DNS_SUBDOMAIN_MAX_LENGTH
        ):
            msg = (
                f'The {self.kind} "{self.name}" is invalid: '
                + f'metadata.name: Invalid value: "{self.name}". '
                + "This field must adhere to DNS-1123 subdomain names spec."
                + f"More info can be found at {DNS_NAMES_URL}."
            )
            raise ConstructResourceError(msg)

        # All objects that have a spec.template.spec.containers[]
        try:
            containers = self.body["spec"]["template"]["spec"]["containers"]
            if not isinstance(containers, list):
                msg = (
                    f'The {self.kind} "{self.name}" is invalid: '
                    + "spec.template.spec.containers is not a list"
                )
                raise ConstructResourceError(msg)
            for c in containers:
                cname = c.get("name", None)
                if cname is None:
                    msg = (
                        f'The {self.kind} "{self.name}" is invalid: '
                        + "an item in spec.template.spec.containers was "
                        + "found without a required name field"
                    )
                    raise ConstructResourceError(msg)
                if (
                    not DNS_LABEL_RE.match(cname)
                    or not len(cname) <= DNS_LABEL_MAX_LENGTH
                ):
                    msg = (
                        f'The {self.kind} "{self.name}" is invalid: '
                        + "an container in spec.template.spec.containers "
                        + f"was found with an invalid name ({cname}). More "
                        + f"info at {DNS_NAMES_URL}."
                    )
                    raise ConstructResourceError(msg)
        except KeyError:
            pass

    @staticmethod
    def is_controller_managed_label(kind, label) -> bool:
        for il in CONTROLLER_MANAGED_LABELS.get(kind, []):
            if isinstance(il, str) and il == label:
                return True
            if isinstance(il, re.Pattern) and re.search(il, label):
                return True
        return False

    def has_qontract_annotations(self):
        try:
            annotations = self.body["metadata"]["annotations"]

            assert annotations[QONTRACT_ANNOTATION_INTEGRATION] == self.integration

            integration_version = annotations[QONTRACT_ANNOTATION_INTEGRATION_VERSION]
            assert (
                semver.VersionInfo.parse(integration_version).major
                == semver.VersionInfo.parse(self.integration_version).major
            )

            assert annotations[QONTRACT_ANNOTATION_SHA256SUM] is not None
        except KeyError:
            return False
        except AssertionError:
            return False
        except ValueError:
            # raised by semver.VersionInfo.parse
            return False

        return True

    def has_owner_reference(self):
        return bool(self.body["metadata"].get("ownerReferences", []))

    def has_valid_sha256sum(self):
        try:
            current_sha256sum = self.body["metadata"]["annotations"][
                "qontract.sha256sum"
            ]
            return current_sha256sum == self.sha256sum()
        except KeyError:
            return False

    def annotate(self, canonicalize=True):
        """
        Creates a OpenshiftResource with the qontract annotations, and removes
        unneeded Openshift fields.

        Returns:
            openshift_resource: new OpenshiftResource object with
                annotations.
        """
        body = self.canonicalize(self.body) if canonicalize else self.body

        sha256sum = self.calculate_sha256sum(self.serialize(body))

        # create new body object
        body = copy.deepcopy(self.body)

        # create annotations if not present
        body["metadata"].setdefault("annotations", {})
        if body["metadata"]["annotations"] is None:
            body["metadata"]["annotations"] = {}

        annotations = body["metadata"]["annotations"]

        # add qontract annotations
        annotations[QONTRACT_ANNOTATION_INTEGRATION] = self.integration
        annotations[QONTRACT_ANNOTATION_INTEGRATION_VERSION] = self.integration_version
        annotations[QONTRACT_ANNOTATION_SHA256SUM] = sha256sum
        now = datetime.datetime.utcnow().replace(microsecond=0).isoformat()
        annotations[QONTRACT_ANNOTATION_UPDATE] = now
        if self.caller_name:
            annotations[QONTRACT_ANNOTATION_CALLER_NAME] = self.caller_name

        return OpenshiftResource(body, self.integration, self.integration_version)

    def sha256sum(self):
        body = self.annotate().body

        annotations = body["metadata"]["annotations"]
        return annotations["qontract.sha256sum"]

    def to_json(self):
        return self.serialize(self.body)

    @staticmethod
    def canonicalize(body):
        body = copy.deepcopy(body)

        # create annotations if not present
        body["metadata"].setdefault("annotations", {})
        if body["metadata"]["annotations"] is None:
            body["metadata"]["annotations"] = {}
        annotations = body["metadata"]["annotations"]

        # remove openshift specific params
        body["metadata"].pop("creationTimestamp", None)
        body["metadata"].pop("resourceVersion", None)
        body["metadata"].pop("generation", None)
        body["metadata"].pop("selfLink", None)
        body["metadata"].pop("uid", None)
        body["metadata"].pop("namespace", None)
        body["metadata"].pop("managedFields", None)
        annotations.pop("kubectl.kubernetes.io/last-applied-configuration", None)

        # remove status
        body.pop("status", None)

        # remove controller managed labels
        labels = body["metadata"].get("labels", {})
        for label in set(labels.keys()):
            if OpenshiftResource.is_controller_managed_label(body["kind"], label):
                labels.pop(label)

        # Default fields for specific resource types
        # ConfigMaps and Secrets are by default Opaque
        if body["kind"] in {"ConfigMap", "Secret"} and body.get("type") == "Opaque":
            body.pop("type")

        if body["kind"] == "Secret":
            string_data = body.pop("stringData", None)
            if string_data:
                body.setdefault("data", {})
                for k, v in string_data.items():
                    v = base64_encode_secret_field_value(str(v))
                    body["data"][k] = v

        if body["kind"] == "Deployment":
            annotations.pop("deployment.kubernetes.io/revision", None)

        if body["kind"] == "Route":
            if body["spec"].get("wildcardPolicy") == "None":
                body["spec"].pop("wildcardPolicy")
            # remove tls-acme specific params from Route
            if "kubernetes.io/tls-acme" in annotations:
                annotations.pop(
                    "kubernetes.io/tls-acme-awaiting-authorization-owner", None
                )
                annotations.pop(
                    "kubernetes.io/tls-acme-awaiting-authorization-at-url", None
                )
                if "tls" in body["spec"]:
                    tls = body["spec"]["tls"]
                    tls.pop("key", None)
                    tls.pop("certificate", None)
            subdomain = body["spec"].get("subdomain", None)
            if not subdomain:
                body["spec"].pop("subdomain", None)

        if body["kind"] == "ServiceAccount":
            if "imagePullSecrets" in body:
                # remove default pull secrets added by k8s
                if imagepullsecrets := [
                    s
                    for s in body.pop("imagePullSecrets")
                    if "-dockercfg-" not in s["name"]
                ]:
                    body["imagePullSecrets"] = imagepullsecrets
            if "secrets" in body:
                body.pop("secrets")

        if body["kind"] == "Role":
            for rule in body["rules"]:
                if "resources" in rule:
                    rule["resources"].sort()

                if "verbs" in rule:
                    rule["verbs"].sort()

                if (
                    "attributeRestrictions" in rule
                    and not rule["attributeRestrictions"]
                ):
                    rule.pop("attributeRestrictions")

        if body["kind"] == "OperatorGroup":
            annotations.pop("olm.providedAPIs", None)

        if body["kind"] == "RoleBinding":
            if "groupNames" in body:
                body.pop("groupNames")
            if "userNames" in body:
                body.pop("userNames")
            if "roleRef" in body:
                if "namespace" in body["roleRef"]:
                    body["roleRef"].pop("namespace")
                if (
                    "apiGroup" in body["roleRef"]
                    and body["roleRef"]["apiGroup"] in body["apiVersion"]
                ):
                    body["roleRef"].pop("apiGroup")
                if "kind" in body["roleRef"]:
                    body["roleRef"].pop("kind")
            for subject in body["subjects"]:
                if "namespace" in subject:
                    subject.pop("namespace")
                if "apiGroup" in subject and (
                    not subject["apiGroup"] or subject["apiGroup"] in body["apiVersion"]
                ):
                    subject.pop("apiGroup")

        if body["kind"] == "ClusterRoleBinding":
            if "userNames" in body:
                body.pop("userNames")
            if "roleRef" in body:
                if (
                    "apiGroup" in body["roleRef"]
                    and body["roleRef"]["apiGroup"] in body["apiVersion"]
                ):
                    body["roleRef"].pop("apiGroup")
                if "kind" in body["roleRef"]:
                    body["roleRef"].pop("kind")
            if "groupNames" in body:
                body.pop("groupNames")
        if body["kind"] == "Service":
            spec = body["spec"]
            if spec.get("sessionAffinity") == "None":
                spec.pop("sessionAffinity")
            if spec.get("type") == "ClusterIP":
                spec.pop("clusterIP", None)

        # remove qontract specific params
        for a in QONTRACT_ANNOTATIONS:
            annotations.pop(a, None)

        # Remove external resources annotation used for optimistic locking
        annotations.pop(SECRET_UPDATED_AT, None)
        return body

    @staticmethod
    def serialize(body):
        return json.dumps(body, sort_keys=True)

    @staticmethod
    def calculate_sha256sum(body):
        m = hashlib.sha256()
        m.update(body.encode("utf-8"))
        return m.hexdigest()


def fully_qualified_kind(kind: str, api_version: str) -> str:
    if "/" in api_version:
        group = api_version.split("/")[0]  # noqa: PLC0207
        return f"{kind}.{group}"
    return kind


class OpenshiftResourceBaseMetric(BaseModel):
    "Base class Openshift Resource metrics"

    integration: str


class OpenshiftResourceInventoryGauge(OpenshiftResourceBaseMetric, GaugeMetric):
    "Inventory Gauge"

    cluster: str
    namespace: str
    kind: str
    state: str

    @classmethod
    def name(cls) -> str:
        return "qontract_reconcile_openshift_resource_inventory"


class ResourceInventory:
    def __init__(self):
        self._clusters = {}
        self._error_registered = False
        self._error_registered_clusters = {}
        self._lock = Lock()

    def initialize_resource_type(
        self,
        cluster,
        namespace,
        resource_type,
        managed_names: list[str] | None = None,
    ):
        self._clusters.setdefault(cluster, {})
        self._clusters[cluster].setdefault(namespace, {})
        self._clusters[cluster][namespace].setdefault(
            resource_type,
            {
                "current": {},
                "desired": {},
                "use_admin_token": {},
                "managed_names": managed_names,
            },
        )

    def is_cluster_present(self, cluster: str) -> bool:
        return cluster in self._clusters

    def add_desired_resource(
        self,
        cluster: str,
        namespace: str,
        resource: OpenshiftResource,
        privileged: bool = False,
    ) -> None:
        if resource.kind_and_group in self._clusters[cluster][namespace]:
            kind = resource.kind_and_group
        else:
            kind = resource.kind
        self.add_desired(
            cluster=cluster,
            namespace=namespace,
            resource_type=kind,
            name=resource.name,
            value=resource,
            privileged=privileged,
        )

    def add_desired(
        self, cluster, namespace, resource_type, name, value, privileged=False
    ):
        # privileged permissions to apply resources to clusters are managed on
        # a per-namespace level in qontract-schema namespace files, but are
        # tracked on a per-resource level in ResourceInventory and the
        # state-specs that lead up to add_desired calls. while this is a
        # mismatch between schema and implementation for now, it will enable
        # us to implement per-resource configuration in the future
        with self._lock:
            # fail if the name of the resource is not within the managed names if they are defined
            managed_names = self._clusters[cluster][namespace][resource_type][
                "managed_names"
            ]
            if managed_names is not None and name not in managed_names:
                raise ResourceNotManagedError(name)

            desired = self._clusters[cluster][namespace][resource_type]["desired"]
            if name in desired:
                raise ResourceKeyExistsError(name)
            desired[name] = value
            admin_token_usage = self._clusters[cluster][namespace][resource_type][
                "use_admin_token"
            ]
            admin_token_usage[name] = privileged

    def get_desired(self, cluster, namespace, resource_type, name):
        try:
            return self._clusters[cluster][namespace][resource_type]["desired"][name]
        except KeyError:
            return None

    def get_desired_by_type(self, cluster, namespace, resource_type):
        try:
            return self._clusters[cluster][namespace][resource_type]["desired"]
        except KeyError:
            return None

    def get_current(self, cluster, namespace, resource_type, name):
        try:
            return self._clusters[cluster][namespace][resource_type]["current"][name]
        except KeyError:
            return None

    def add_current(self, cluster, namespace, resource_type, name, value):
        with self._lock:
            current = self._clusters[cluster][namespace][resource_type]["current"]
            current[name] = value

    def __iter__(self):
        for cluster_name, cluster in self._clusters.items():
            for namespace_name, namespace in cluster.items():
                for resource_type, resource in namespace.items():
                    yield (cluster_name, namespace_name, resource_type, resource)

    def register_error(self, cluster=None):
        self._error_registered = True
        if cluster is not None:
            self._error_registered_clusters[cluster] = True

    def has_error_registered(self, cluster=None):
        if cluster is not None:
            return self._error_registered_clusters.get(cluster, False)
        return self._error_registered


def build_secret(
    name: str,
    integration: str,
    integration_version: str,
    unencoded_data: Mapping[str, str],
    error_details: str = "",
    caller_name: str | None = None,
    annotations: Mapping[str, str] | None = None,
) -> OpenshiftResource:
    encoded_data = {
        k: base64_encode_secret_field_value(v) for k, v in unencoded_data.items()
    }

    body = {
        "apiVersion": "v1",
        "kind": "Secret",
        "type": "Opaque",
        "metadata": {"name": name, "annotations": annotations or {}},
        "data": encoded_data,
    }

    return OpenshiftResource(
        body,
        integration,
        integration_version,
        error_details=error_details,
        caller_name=caller_name,
    )


def base64_encode_secret_field_value(value: str) -> str:
    if not value:
        return ""
    return base64.b64encode(str(value).encode()).decode("utf-8")
