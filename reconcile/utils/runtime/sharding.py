import copy
from collections.abc import Iterable
from dataclasses import dataclass
from typing import (
    Protocol,
)

from pydantic import BaseModel

from reconcile.gql_definitions.common.clusters_minimal import ClusterV1
from reconcile.gql_definitions.fragments.minimal_ocm_organization import (
    MinimalOCMOrganization,
)
from reconcile.gql_definitions.integrations.integrations import (
    AWSAccountShardingV1,
    AWSAccountShardSpecOverrideV1,
    CloudflareDNSZoneShardingV1,
    CloudflareDNSZoneShardSpecOverrideV1,
    IntegrationManagedV1,
    IntegrationShardingV1,
    IntegrationSpecV1,
    JiraBoardShardingV1,
    JiraBoardShardSpecOverrideV1,
    OCMOrganizationShardingV1,
    OCMOrganizationShardSpecOverrideV1,
    OpenshiftClusterShardingV1,
    OpenshiftClusterShardSpecOverrideV1,
    StaticShardingV1,
    StaticSubShardingV1,
    SubShardingV1,
)
from reconcile.gql_definitions.sharding import aws_accounts as sharding_aws_accounts
from reconcile.gql_definitions.sharding import jira_boards as sharding_jira_boards
from reconcile.gql_definitions.sharding import (
    ocm_organization as sharding_ocm_organization,
)
from reconcile.gql_definitions.terraform_cloudflare_dns import (
    terraform_cloudflare_zones,
)
from reconcile.gql_definitions.terraform_cloudflare_dns.terraform_cloudflare_zones import (
    CloudflareDnsZoneV1,
)
from reconcile.typed_queries.clusters_minimal import get_clusters_minimal
from reconcile.utils import gql
from reconcile.utils.runtime.meta import IntegrationMeta


class ShardSpec(BaseModel):
    # Base Sharding
    shards: str | None = ""
    shard_id: str | None = ""
    shard_spec_overrides: (
        AWSAccountShardSpecOverrideV1
        | OpenshiftClusterShardSpecOverrideV1
        | CloudflareDNSZoneShardSpecOverrideV1
        | OCMOrganizationShardSpecOverrideV1
        | None
    ) = None

    # Key sharding
    shard_key: str = ""
    shard_name_suffix: str = ""
    extra_args: str = ""

    def add_extra_args(self, args: str) -> None:
        if args:
            self.extra_args += f" {args}"


class ShardingStrategy(Protocol):
    def build_integration_shards(
        self,
        integration_meta: IntegrationMeta,
        integration_managed: IntegrationManagedV1,
    ) -> list[ShardSpec]:
        pass


class SubShardingStrategy(Protocol):
    @staticmethod
    def create_sub_shards(
        base_shard: ShardSpec, sub_sharding: SubShardingV1
    ) -> list[ShardSpec]:
        pass


class StaticShardingStrategy:
    IDENTIFIER = "static"

    def build_integration_shards(
        self, _: IntegrationMeta, integration_managed: IntegrationManagedV1
    ) -> list[ShardSpec]:
        shards = 1
        if integration_managed.sharding and isinstance(
            integration_managed.sharding, StaticShardingV1
        ):
            shards = integration_managed.sharding.shards

        return [
            ShardSpec(
                shard_id=str(s),
                shards=str(shards),
                shard_name_suffix=f"-{s}" if shards > 1 else "",
                extra_args=integration_managed.spec.extra_args or "",
            )
            for s in range(0, shards)
        ]

    @staticmethod
    def create_sub_shards(
        base_shard: ShardSpec, sub_sharding: SubShardingV1
    ) -> list[ShardSpec]:
        if base_shard.shard_id or base_shard.shards:
            raise ValueError(
                "Static sub_sharding can only be applied to Key based sharding"
            )
        num_shards = 1
        if isinstance(sub_sharding, StaticSubShardingV1):
            num_shards = sub_sharding.shards
        shards: list[ShardSpec] = []
        for s in range(0, num_shards):
            new_shard = copy.deepcopy(base_shard)
            if new_shard.shard_spec_overrides and isinstance(
                new_shard.shard_spec_overrides, OpenshiftClusterShardSpecOverrideV1
            ):
                new_shard.shard_spec_overrides.sub_sharding = None
            new_shard.shard_id = str(s)
            new_shard.shards = str(num_shards)
            new_shard.shard_name_suffix += f"-{s}"
            shards.append(new_shard)
        return shards


class AWSAccountShardingStrategy:
    IDENTIFIER = "per-aws-account"

    def __init__(
        self,
        aws_accounts: Iterable[sharding_aws_accounts.AWSAccountV1] | None = None,
    ):
        if not aws_accounts:
            self.aws_accounts = (
                sharding_aws_accounts.query(query_func=gql.get_api().query).aws_accounts
                or []
            )
        else:
            self.aws_accounts = list(aws_accounts)

    def filter_objects(
        self, integration: str
    ) -> list[sharding_aws_accounts.AWSAccountV1]:
        return [
            a
            for a in self.aws_accounts
            if (
                not a.disable
                or not a.disable.integrations
                or (
                    a.disable.integrations and integration not in a.disable.integrations
                )
            )
        ]

    def get_shard_spec_overrides(
        self, sharding: IntegrationShardingV1 | None
    ) -> dict[str, AWSAccountShardSpecOverrideV1]:
        spos: dict[str, AWSAccountShardSpecOverrideV1] = {}

        if isinstance(sharding, AWSAccountShardingV1) and sharding.shard_spec_overrides:
            for sp in sharding.shard_spec_overrides:
                spos[sp.shard.name] = sp
        return spos

    def check_integration_sharding_params(self, meta: IntegrationMeta) -> None:
        if "--account-name" not in meta.args:
            raise ValueError(
                f"integration {meta.name} does not support the provided argument. "
                " --account-name is required by the 'per-aws-account' sharding "
                "strategy."
            )

    def build_shard_spec(
        self,
        aws_account: sharding_aws_accounts.AWSAccountV1,
        integration_spec: IntegrationSpecV1,
        spo: AWSAccountShardSpecOverrideV1 | None,
    ) -> ShardSpec:
        return ShardSpec(
            shard_key=aws_account.name,
            shard_name_suffix=f"-{aws_account.name}",
            extra_args=(integration_spec.extra_args or "")
            + f" --account-name {aws_account.name}",
            shard_spec_overrides=spo,
        )

    def build_integration_shards(
        self,
        integration_meta: IntegrationMeta,
        integration_managed: IntegrationManagedV1,
    ) -> list[ShardSpec]:
        self.check_integration_sharding_params(integration_meta)
        spos = self.get_shard_spec_overrides(integration_managed.sharding)
        shards = []
        for c in self.filter_objects(integration_meta.name):
            spo = spos.get(c.name)
            base_shard = self.build_shard_spec(c, integration_managed.spec, spo)
            shards.append(base_shard)
        return shards


class OCMOrganizationShardingStrategy:
    IDENTIFIER = "per-ocm-organization"

    def __init__(
        self,
        ocm_organizations: Iterable[MinimalOCMOrganization] | None = None,
    ):
        if not ocm_organizations:
            self.ocm_organizations = (
                sharding_ocm_organization.query(
                    query_func=gql.get_api().query
                ).ocm_organizations
                or []
            )
        else:
            self.ocm_organizations = list(ocm_organizations)

    def get_shard_spec_overrides(
        self, sharding: IntegrationShardingV1 | None
    ) -> dict[str, OCMOrganizationShardSpecOverrideV1]:
        spos: dict[str, OCMOrganizationShardSpecOverrideV1] = {}

        if (
            isinstance(sharding, OCMOrganizationShardingV1)
            and sharding.shard_spec_overrides
        ):
            for sp in sharding.shard_spec_overrides or []:
                spos[sp.shard.name] = sp
        return spos

    def check_integration_sharding_params(self, meta: IntegrationMeta) -> None:
        if "--org-id" not in meta.args:
            raise ValueError(
                f"the integration {meta.name} does not support the required argument "
                " --org-id for the 'per-ocm-organization' sharding strategy."
            )

    def build_shard_spec(
        self,
        org: MinimalOCMOrganization,
        integration_spec: IntegrationSpecV1,
        spo: OCMOrganizationShardSpecOverrideV1 | None,
    ) -> ShardSpec:
        return ShardSpec(
            shard_key=org.org_id,
            shard_name_suffix=f"-{org.org_id.lower()}",
            extra_args=(integration_spec.extra_args or "") + f" --org-id {org.org_id}",
            shard_spec_overrides=spo,
        )

    def build_integration_shards(
        self,
        integration_meta: IntegrationMeta,
        integration_managed: IntegrationManagedV1,
    ) -> list[ShardSpec]:
        self.check_integration_sharding_params(integration_meta)
        spos = self.get_shard_spec_overrides(integration_managed.sharding)
        shards = []
        for org in self.ocm_organizations:
            spo = spos.get(org.name)
            base_shard = self.build_shard_spec(org, integration_managed.spec, spo)
            shards.append(base_shard)
        return shards


class OpenshiftClusterShardingStrategy:
    IDENTIFIER = "per-openshift-cluster"

    def __init__(self, clusters: Iterable[ClusterV1] | None = None):
        if not clusters:
            self.clusters = get_clusters_minimal()
        else:
            self.clusters = list(clusters)

        self.sub_sharding_strategies = {
            StaticShardingStrategy.IDENTIFIER: StaticShardingStrategy
        }

    def filter_objects(self, integration: str) -> list[ClusterV1]:
        return [
            c
            for c in self.clusters
            if (
                not c.disable
                or not c.disable.integrations
                or (
                    c.disable.integrations and integration not in c.disable.integrations
                )
            )
        ]

    def get_shard_spec_overrides(
        self, sharding: IntegrationShardingV1 | None
    ) -> dict[str, OpenshiftClusterShardSpecOverrideV1]:
        spos: dict[str, OpenshiftClusterShardSpecOverrideV1] = {}

        if (
            isinstance(sharding, OpenshiftClusterShardingV1)
            and sharding.shard_spec_overrides
        ):
            for sp in sharding.shard_spec_overrides:
                spos[sp.shard.name] = sp
        return spos

    def check_integration_sharding_params(self, meta: IntegrationMeta) -> None:
        if "--cluster-name" not in meta.args:
            raise ValueError(
                f"integration {meta.name} does not support the provided argument. "
                " --cluster-name is required by the 'per-openshift-cluster' sharding "
                "strategy."
            )

    def build_shard_spec(
        self,
        cluster: ClusterV1,
        integration_spec: IntegrationSpecV1,
        spo: OpenshiftClusterShardSpecOverrideV1 | None,
    ) -> ShardSpec:
        return ShardSpec(
            shard_key=cluster.name,
            shard_name_suffix=f"-{cluster.name}",
            extra_args=(integration_spec.extra_args or "")
            + f" --cluster-name {cluster.name}",
            shard_spec_overrides=spo,
        )

    def build_sub_shards(
        self, base_shard: ShardSpec, spo: OpenshiftClusterShardSpecOverrideV1 | None
    ) -> list[ShardSpec]:
        sub_shards = []
        if spo and spo.sub_sharding and spo.sub_sharding.strategy:
            if spo.sub_sharding.strategy not in self.sub_sharding_strategies:
                raise ValueError(
                    "Subsharding strategy not allowed by {self.__class__.__name__}"
                )
            c = self.sub_sharding_strategies[spo.sub_sharding.strategy]
            sub_shards = c.create_sub_shards(base_shard, spo.sub_sharding)
        return sub_shards

    def build_integration_shards(
        self,
        integration_meta: IntegrationMeta,
        integration_managed: IntegrationManagedV1,
    ) -> list[ShardSpec]:
        self.check_integration_sharding_params(integration_meta)
        spos = self.get_shard_spec_overrides(integration_managed.sharding)
        shards = []
        for c in self.filter_objects(integration_meta.name):
            spo = spos.get(c.name)
            base_shard = self.build_shard_spec(c, integration_managed.spec, spo)
            sub_shards = self.build_sub_shards(base_shard, spo)
            if sub_shards:
                shards.extend(sub_shards)
            else:
                shards.append(base_shard)
        return shards


class CloudflareDnsZoneShardingStrategy:
    """
    This provides a new sharding strategy that each shard is targeting a Cloudflare zone.
    It uses the combination of the Cloudflare account name and the zone's identifier as the unique sharding key.
    """

    IDENTIFIER = "per-cloudflare-dns-zone"

    def __init__(self, cloudflare_zones: Iterable[CloudflareDnsZoneV1] | None = None):
        if not cloudflare_zones:
            self.cloudflare_zones = (
                terraform_cloudflare_zones.query(query_func=gql.get_api().query).zones
                or []
            )
        else:
            self.cloudflare_zones = list(cloudflare_zones)

    def _get_shard_key(self, dns_zone: CloudflareDnsZoneV1) -> str:
        return f"{dns_zone.account.name}-{dns_zone.identifier}"

    def get_shard_spec_overrides(
        self, sharding: IntegrationShardingV1 | None
    ) -> dict[str, CloudflareDNSZoneShardSpecOverrideV1]:
        spos: dict[str, CloudflareDNSZoneShardSpecOverrideV1] = {}

        if (
            isinstance(sharding, CloudflareDNSZoneShardingV1)
            and sharding.shard_spec_overrides
        ):
            for override in sharding.shard_spec_overrides:
                key = f"{override.shard.zone}-{override.shard.identifier}"
                spos[key] = override
        return spos

    def check_integration_sharding_params(self, meta: IntegrationMeta) -> None:
        if "--zone-name" not in meta.args:
            raise ValueError(
                f"integration {meta.name} does not support the provided argument. "
                f"--zone-name is required by the '{self.IDENTIFIER}' sharding "
                "strategy."
            )

    def build_shard_spec(
        self,
        dns_zone: CloudflareDnsZoneV1,
        integration_spec: IntegrationSpecV1,
        spo: CloudflareDNSZoneShardSpecOverrideV1 | None,
    ) -> ShardSpec:
        return ShardSpec(
            shard_key=self._get_shard_key(dns_zone),
            shard_name_suffix=f"-{self._get_shard_key(dns_zone)}",
            extra_args=(integration_spec.extra_args or "")
            + f" --zone-name {dns_zone.identifier}",
            shard_spec_overrides=spo,
        )

    def build_integration_shards(
        self,
        integration_meta: IntegrationMeta,
        integration_managed: IntegrationManagedV1,
    ) -> list[ShardSpec]:
        self.check_integration_sharding_params(integration_meta)
        spos = self.get_shard_spec_overrides(integration_managed.sharding)
        shards = []
        for zone in self.cloudflare_zones or []:
            spo = spos.get(self._get_shard_key(zone))
            base_shard = self.build_shard_spec(zone, integration_managed.spec, spo)
            shards.append(base_shard)
        return shards


class JiraBoardShardingStrategy:
    IDENTIFIER = "per-jira-board"

    def __init__(
        self,
        jira_boards: Iterable[sharding_jira_boards.JiraBoardV1] | None = None,
    ):
        if not jira_boards:
            self.jira_boards = (
                sharding_jira_boards.query(query_func=gql.get_api().query).jira_boards
                or []
            )
        else:
            self.jira_boards = list(jira_boards)

    def get_shard_spec_overrides(
        self, sharding: IntegrationShardingV1 | None
    ) -> dict[str, JiraBoardShardSpecOverrideV1]:
        spos: dict[str, JiraBoardShardSpecOverrideV1] = {}

        if isinstance(sharding, JiraBoardShardingV1) and sharding.shard_spec_overrides:
            for sp in sharding.shard_spec_overrides or []:
                spos[sp.shard.name] = sp
        return spos

    def check_integration_sharding_params(self, meta: IntegrationMeta) -> None:
        if "--jira-board-name" not in meta.args:
            raise ValueError(
                f"the integration {meta.name} does not support the required argument "
                " --jira-board-name for the 'per-jira-board' sharding strategy."
            )

    def build_shard_spec(
        self,
        jira_board: sharding_jira_boards.JiraBoardV1,
        integration_spec: IntegrationSpecV1,
        spo: JiraBoardShardSpecOverrideV1 | None,
    ) -> ShardSpec:
        return ShardSpec(
            shard_key=jira_board.name,
            shard_name_suffix=f"-{jira_board.name.lower()}",
            extra_args=(integration_spec.extra_args or "")
            + f" --jira-board-name {jira_board.name}",
            shard_spec_overrides=spo,
        )

    def build_integration_shards(
        self,
        integration_meta: IntegrationMeta,
        integration_managed: IntegrationManagedV1,
    ) -> list[ShardSpec]:
        self.check_integration_sharding_params(integration_meta)
        spos = self.get_shard_spec_overrides(integration_managed.sharding)
        shards = []
        for board in self.jira_boards:
            spo = spos.get(board.name)
            base_shard = self.build_shard_spec(board, integration_managed.spec, spo)
            shards.append(base_shard)
        return shards


@dataclass
class IntegrationShardManager:
    strategies: dict[str, ShardingStrategy]
    integration_runtime_meta: dict[str, IntegrationMeta]

    def build_integration_shards(
        self, integration: str, integration_spec: IntegrationManagedV1
    ) -> list[ShardSpec]:
        shards: list[ShardSpec] = []

        sharding = integration_spec.sharding
        if not sharding:
            sharding = StaticShardingV1(strategy="static", shards=1)

        integration_meta = self.integration_runtime_meta.get(integration)
        if not integration_meta:
            # workaround until we can get metadata for non cli.py based integrations
            integration_meta = IntegrationMeta(
                name=integration, args=[], short_help=None
            )

        shards = self.strategies[sharding.strategy].build_integration_shards(
            integration_meta, integration_spec
        )
        return shards
