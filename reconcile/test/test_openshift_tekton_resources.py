# Setting ignore[misc] for setters on TstData:
# see https://github.com/python/mypy/issues/9160
# type: ignore[misc]
from typing import Any
from unittest import TestCase
from unittest.mock import patch, create_autospec

from reconcile.queries import PIPELINES_PROVIDERS_QUERY
from reconcile import openshift_tekton_resources as otr
from reconcile.utils import gql

from .fixtures import Fixtures

MODULE = 'reconcile.openshift_tekton_resources'


class TstUnsupportedGqlQueryError(Exception):
    pass


class TstData:
    '''Class to add data to tests in setUp. It will be used by mocks'''
    def __init__(self):
        self._providers = []
        self._saas_files = []

    @property
    def providers(self) -> list[dict[str, Any]]:
        return self._providers

    @property
    def saas_files(self) -> list[dict[str, Any]]:
        return self._saas_files

    @providers.setter  # type: ignore[no-redef, attr-defined]
    def providers(self, providers: list[dict[str, Any]]) -> None:
        if not isinstance(providers, list):
            raise TypeError(f'Expecting list, have {type(providers)}')
        self._providers = providers

    @saas_files.setter  # type: ignore[no-redef, attr-defined]
    def saas_files(self, saas_files: list[dict[str, Any]]) -> None:
        if not isinstance(saas_files, list):
            raise TypeError(f'Expecting list, have {type(saas_files)}')
        self._saas_files = saas_files


class TestOpenshiftTektonResources(TestCase):

    def _test_deploy_resources_in_task(self, desired_resources, task_name,
                                       deploy_resources) -> None:
        '''Helper method to test if deploy resources have been properly set'''
        for dr in desired_resources:
            if dr['name'] == task_name:
                task = dr['value'].body
                for step in task['spec']['steps']:
                    if step['name'] == otr.DEFAULT_DEPLOY_RESOURCES_STEP_NAME:
                        self.assertEqual(step['resources'], deploy_resources)
                break

    def mock_gql_get_resource(self, path: str) -> dict[str, str]:
        '''Mock for GqlApi.get_resources using fixtures'''
        content = self.fxt.get(path)
        return {'path': path,
                'content': content,
                'sha256sum': ''}  # we do not need it for these tests

    def mock_gql_query(self, query: str) -> dict[str, Any]:
        '''Mock for GqlApi.query using test_data set in setUp'''
        if query == otr.SAAS_FILES_QUERY:
            return {'saas_files': self.test_data.saas_files}
        elif query == PIPELINES_PROVIDERS_QUERY:
            return {'pipelines_providers': self.test_data.providers}
        else:
            raise TstUnsupportedGqlQueryError("Unsupported query")

    def setUp(self) -> None:
        self.test_data = TstData()

        self.fxt = Fixtures('openshift_tekton_resources')

        # Common fixtures
        self.saas1 = self.fxt.get_json('saas1.json')
        self.saas2 = self.fxt.get_json('saas2.json')
        self.saas2_wr = self.fxt.get_json('saas2-with-resources.json')
        self.provider1 = self.fxt.get_json('provider1.json')
        self.provider2_wr = self.fxt.get_json('provider2-with-resources.json')

        # Patcher for GqlApi methods
        self.gql_patcher = patch.object(gql, 'get_api', autospec=True)
        self.gql = self.gql_patcher.start()
        gqlapi_mock = create_autospec(gql.GqlApi)
        self.gql.return_value = gqlapi_mock
        gqlapi_mock.query.side_effect = self.mock_gql_query
        gqlapi_mock.get_resource.side_effect = self.mock_gql_get_resource

    def tearDown(self) -> None:
        """ cleanup patches created in self.setUp"""
        self.gql_patcher.stop()

    def test_get_one_saas_file(self) -> None:
        self.test_data.saas_files = [self.saas1, self.saas2]
        saas_files = otr.fetch_saas_files(self.saas1['name'])
        self.assertEqual(saas_files, [self.saas1])

    def test_fetch_tkn_providers(self) -> None:
        self.test_data.saas_files = [self.saas1, self.saas2]
        self.test_data.providers = [self.provider1, self.provider2_wr]

        tkn_providers = otr.fetch_tkn_providers(None)
        keys_expected = set([self.provider1['name'],
                             self.provider2_wr['name']])
        self.assertEqual(tkn_providers.keys(), keys_expected)

    def test_duplicate_providers(self) -> None:
        self.test_data.saas_files = [self.saas1]
        self.test_data.providers = [self.provider1, self.provider1]
        msg = r'There are duplicates in tekton providers names: provider1'
        self.assertRaisesRegex(otr.OpenshiftTektonResourcesBadConfigError, msg,
                               otr.fetch_tkn_providers, None)

    def test_fetch_desired_resources(self) -> None:
        self.test_data.saas_files = [self.saas1, self.saas2, self.saas2_wr]
        self.test_data.providers = [self.provider1, self.provider2_wr]

        desired_resources = otr.fetch_desired_resources(
            otr.fetch_tkn_providers(None))

        # we have one task per namespace and a pipeline + task per saas file
        self.assertEqual(len(desired_resources), 8)

    def test_fetch_desired_resources_names(self) -> None:
        self.test_data.saas_files = [self.saas1]
        self.test_data.providers = [self.provider1]
        desired_resources = otr.fetch_desired_resources(
            otr.fetch_tkn_providers(None))

        expected_task_names = set([
            'o-push-gateway-openshift-saas-deploy-task-status-metric',
            'o-openshift-saas-deploy-saas1'])
        expected_pipeline_name = 'o-openshift-saas-deploy-saas1'

        task_names = set()
        for dr in desired_resources:
            body = dr['value'].body
            if body['kind'] == 'Task':
                task_names.add(body['metadata']['name'])
            else:
                pipeline_name = body['metadata']['name']

        self.assertEqual(task_names, expected_task_names)
        self.assertEqual(pipeline_name, expected_pipeline_name)

    # we check we have what we need in tkn_providers. This test should
    # be removed when this integration controls all tekton resources
    def test_managed_resources_from_desired_resources(self) -> None:
        self.test_data.saas_files = [self.saas1, self.saas2, self.saas2_wr]
        self.test_data.providers = [self.provider1, self.provider2_wr]

        tkn_providers = otr.fetch_tkn_providers(None)
        _ = otr.fetch_desired_resources(tkn_providers)
        p1_managed = tkn_providers[self.provider1['name']]['namespace'][
            'managedResourceNames']
        p2_managed = tkn_providers[self.provider2_wr['name']]['namespace'][
            'managedResourceNames']

        self.assertEqual(len(p1_managed), 2)
        self.assertEqual(len(p2_managed), 2)

        # 1 namespace task, 1 saas file task, 1 saas file pipeline
        for managed in p1_managed:
            if managed['resource'] == 'Task':
                self.assertEqual(len(managed['resourceNames']), 2)
            else:
                self.assertEqual(len(managed['resourceNames']), 1)

        # 1 namespace task, 2 saas file tasks, 2 saas file pipelines
        for managed in p2_managed:
            if managed['resource'] == 'Task':
                self.assertEqual(len(managed['resourceNames']), 3)
            else:
                self.assertEqual(len(managed['resourceNames']), 2)

    def test_set_deploy_resources_default(self) -> None:
        self.test_data.saas_files = [self.saas1]
        self.test_data.providers = [self.provider1]
        desired_resources = otr.fetch_desired_resources(
            otr.fetch_tkn_providers(None))

        # we need to locate the onePerSaasFile task in the desired resources
        # we could be very strict and find the onePerSaasFile task in
        # self.provider1 or just use the actual structure of the fixtures
        task_name = otr.build_one_per_saas_file_tkn_object_name(
            template_name=self.provider1['taskTemplates'][0]['name'],
            saas_file_name=self.saas1['name'])
        self._test_deploy_resources_in_task(desired_resources, task_name,
                                            otr.DEFAULT_DEPLOY_RESOURCES)

    def test_set_deploy_resources_from_provider(self) -> None:
        self.test_data.saas_files = [self.saas2]
        self.test_data.providers = [self.provider2_wr]
        desired_resources = otr.fetch_desired_resources(
            otr.fetch_tkn_providers(None))

        task_name = otr.build_one_per_saas_file_tkn_object_name(
            template_name=self.provider2_wr['taskTemplates'][0]['name'],
            saas_file_name=self.saas2['name'])
        self._test_deploy_resources_in_task(
            desired_resources, task_name, self.provider2_wr['deployResources'])

    def test_set_deploy_resources_from_saas_file(self) -> None:
        self.test_data.saas_files = [self.saas2_wr]
        self.test_data.providers = [self.provider2_wr]
        desired_resources = otr.fetch_desired_resources(
            otr.fetch_tkn_providers(None))

        task_name = otr.build_one_per_saas_file_tkn_object_name(
            template_name=self.provider2_wr['taskTemplates'][0]['name'],
            saas_file_name=self.saas2['name'])
        self._test_deploy_resources_in_task(
            desired_resources, task_name, self.saas2_wr['deployResources'])

    def test_task_templates_name_duplicates(self) -> None:
        self.provider4_wtd = \
            self.fxt.get_json('provider4-with-task-duplicates.json')
        self.saas4 = self.fxt.get_json('saas4.json')
        self.test_data.saas_files = [self.saas4]
        self.test_data.providers = [self.provider4_wtd]

        msg = r'There are duplicates in task templates names in tekton ' \
              r'provider provider4-with-task-duplicates'
        self.assertRaisesRegex(otr.OpenshiftTektonResourcesBadConfigError, msg,
                               otr.fetch_desired_resources,
                               otr.fetch_tkn_providers(None))

    def test_task_templates_unknown_task(self) -> None:
        self.provider5_wut = \
            self.fxt.get_json('provider5-with-unknown-task.json')
        self.saas5 = self.fxt.get_json('saas5.json')
        self.test_data.saas_files = [self.saas5]
        self.test_data.providers = [self.provider5_wut]

        msg = r'Unknown task this-is-an-unknown-task in pipeline template ' \
              r'openshift-saas-deploy'
        self.assertRaisesRegex(otr.OpenshiftTektonResourcesBadConfigError, msg,
                               otr.fetch_desired_resources,
                               otr.fetch_tkn_providers(None))

    @patch(f'{MODULE}.DEFAULT_DEPLOY_RESOURCES_STEP_NAME', 'unknown-step')
    def test_task_templates_unknown_deploy_resources_step(self) -> None:
        self.test_data.saas_files = [self.saas1]
        self.test_data.providers = [self.provider1]
        msg = r'Cannot find a step named unknown-step to set resources in ' \
              r'task template openshift-saas-deploy'
        self.assertRaisesRegex(otr.OpenshiftTektonResourcesBadConfigError, msg,
                               otr.fetch_desired_resources,
                               otr.fetch_tkn_providers(None))

    @patch(f'{MODULE}.RESOURCE_MAX_LENGTH', 1)
    def test_task_templates_resource_too_long(self) -> None:
        self.test_data.saas_files = [self.saas1]
        self.test_data.providers = [self.provider1]
        msg = r'name o-openshift-saas-deploy-saas1 is longer than 1 characters'
        self.assertRaisesRegex(otr.OpenshiftTektonResourcesNameTooLongError,
                               msg, otr.fetch_desired_resources,
                               otr.fetch_tkn_providers(None))