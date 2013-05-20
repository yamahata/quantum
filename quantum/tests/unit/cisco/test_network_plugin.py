# Copyright (c) 2012 OpenStack Foundation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import contextlib
import inspect
import logging
import mock

from quantum.api.v2 import base
from quantum.common import exceptions as q_exc
from quantum import context
from quantum.db import l3_db
from quantum.manager import QuantumManager
from quantum.plugins.cisco.common import cisco_constants as const
from quantum.plugins.cisco.common import cisco_exceptions as c_exc
from quantum.plugins.cisco.common import config as cisco_config
from quantum.plugins.cisco.db import nexus_db_v2
from quantum.plugins.cisco.models import virt_phy_sw_v2
from quantum.plugins.openvswitch.common import config as ovs_config
from quantum.plugins.openvswitch import ovs_db_v2
from quantum.tests.unit import test_db_plugin

LOG = logging.getLogger(__name__)


class CiscoNetworkPluginV2TestCase(test_db_plugin.QuantumDbPluginV2TestCase):

    _plugin_name = 'quantum.plugins.cisco.network_plugin.PluginV2'

    def setUp(self):
        # Use a mock netconf client
        self.mock_ncclient = mock.Mock()
        self.patch_obj = mock.patch.dict('sys.modules',
                                         {'ncclient': self.mock_ncclient})
        self.patch_obj.start()

        super(CiscoNetworkPluginV2TestCase, self).setUp(self._plugin_name)
        self.port_create_status = 'DOWN'
        self.addCleanup(self.patch_obj.stop)

    def _get_plugin_ref(self):
        plugin_obj = QuantumManager.get_plugin()
        if getattr(plugin_obj, "_master"):
            plugin_ref = plugin_obj
        else:
            plugin_ref = getattr(plugin_obj, "_model").\
                _plugins[const.VSWITCH_PLUGIN]

        return plugin_ref


class TestCiscoBasicGet(CiscoNetworkPluginV2TestCase,
                        test_db_plugin.TestBasicGet):
    pass


class TestCiscoV2HTTPResponse(CiscoNetworkPluginV2TestCase,
                              test_db_plugin.TestV2HTTPResponse):

    pass


class TestCiscoPortsV2(CiscoNetworkPluginV2TestCase,
                       test_db_plugin.TestPortsV2):

    def setUp(self):
        """Configure for end-to-end quantum testing using a mock ncclient.

        This setup includes:
        - Configure the OVS plugin to use VLANs in the range of 1000-1100.
        - Configure the Cisco plugin model to use the real Nexus driver.
        - Configure the Nexus sub-plugin to use an imaginary switch
          at 1.1.1.1.

        """
        self.addCleanup(mock.patch.stopall)

        self.vlan_start = 1000
        self.vlan_end = 1100
        range_str = 'physnet1:%d:%d' % (self.vlan_start,
                                        self.vlan_end)
        nexus_driver = ('quantum.plugins.cisco.nexus.'
                        'cisco_nexus_network_driver_v2.CiscoNEXUSDriver')

        config = {
            ovs_config: {
                'OVS': {'bridge_mappings': 'physnet1:br-eth1',
                        'network_vlan_ranges': [range_str],
                        'tenant_network_type': 'vlan'}
            },
            cisco_config: {
                'CISCO': {'nexus_driver': nexus_driver},
            }
        }

        for module in config:
            for group in config[module]:
                for opt in config[module][group]:
                    module.cfg.CONF.set_override(opt,
                                                 config[module][group][opt],
                                                 group)
            self.addCleanup(module.cfg.CONF.reset)

        self.switch_ip = '1.1.1.1'
        nexus_config = {(self.switch_ip, 'username'): 'admin',
                        (self.switch_ip, 'password'): 'mySecretPassword',
                        (self.switch_ip, 'ssh_port'): 22,
                        (self.switch_ip, 'testhost'): '1/1'}
        mock.patch.dict(cisco_config.nexus_dictionary, nexus_config).start()

        patches = {
            '_should_call_create_net': True,
            '_get_instance_host': 'testhost'
        }
        for func in patches:
            mock_sw = mock.patch.object(
                virt_phy_sw_v2.VirtualPhysicalSwitchModelV2,
                func).start()
            mock_sw.return_value = patches[func]

        super(TestCiscoPortsV2, self).setUp()

    @contextlib.contextmanager
    def _patch_ncclient(self, attr, value):
        """Configure an attribute on the mock ncclient module.

        This method can be used to inject errors by setting a side effect
        or a return value for an ncclient method.

        :param attr: ncclient attribute (typically method) to be configured.
        :param value: Value to be configured on the attribute.

        """
        # Configure attribute.
        config = {attr: value}
        self.mock_ncclient.configure_mock(**config)
        # Continue testing
        yield
        # Unconfigure attribute
        config = {attr: None}
        self.mock_ncclient.configure_mock(**config)

    @contextlib.contextmanager
    def _create_port_res(self, fmt=None, no_delete=False,
                         **kwargs):
        """Create a network, subnet, and port and yield the result.

        Create a network, subnet, and port, yield the result,
        then delete the port, subnet, and network.

        :param fmt: Format to be used for API requests.
        :param no_delete: If set to True, don't delete the port at the
                          end of testing.
        :param kwargs: Keyword args to be passed to self._create_port.

        """
        with self.subnet() as subnet:
            net_id = subnet['subnet']['network_id']
            res = self._create_port(fmt, net_id, **kwargs)
            port = self.deserialize(fmt, res)
            try:
                yield res
            finally:
                if not no_delete:
                    self._delete('ports', port['port']['id'])

    def _assertExpectedHTTP(self, status, exc):
        """Confirm that an HTTP status corresponds to an expected exception.

        Confirm that an HTTP status which has been returned for an
        quantum API request matches the HTTP status corresponding
        to an expected exception.

        :param status: HTTP status
        :param exc: Expected exception

        """
        if exc in base.FAULT_MAP:
            expected_http = base.FAULT_MAP[exc].code
        else:
            expected_http = 500
        self.assertEqual(status, expected_http)

    def test_create_ports_bulk_emulated_plugin_failure(self):
        real_has_attr = hasattr

        #ensures the API choose the emulation code path
        def fakehasattr(item, attr):
            if attr.endswith('__native_bulk_support'):
                return False
            return real_has_attr(item, attr)

        with mock.patch('__builtin__.hasattr',
                        new=fakehasattr):
            plugin_ref = self._get_plugin_ref()
            orig = plugin_ref.create_port
            with mock.patch.object(plugin_ref,
                                   'create_port') as patched_plugin:

                def side_effect(*args, **kwargs):
                    return self._do_side_effect(patched_plugin, orig,
                                                *args, **kwargs)

                patched_plugin.side_effect = side_effect
                with self.network() as net:
                    res = self._create_port_bulk(self.fmt, 2,
                                                 net['network']['id'],
                                                 'test',
                                                 True)
                    # We expect a 500 as we injected a fault in the plugin
                    self._validate_behavior_on_bulk_failure(res, 'ports', 500)

    def test_create_ports_bulk_native_plugin_failure(self):
        if self._skip_native_bulk:
            self.skipTest("Plugin does not support native bulk port create")
        ctx = context.get_admin_context()
        with self.network() as net:
            plugin_ref = self._get_plugin_ref()
            orig = plugin_ref.create_port
            with mock.patch.object(plugin_ref,
                                   'create_port') as patched_plugin:

                def side_effect(*args, **kwargs):
                    return self._do_side_effect(patched_plugin, orig,
                                                *args, **kwargs)

                patched_plugin.side_effect = side_effect
                res = self._create_port_bulk(self.fmt, 2, net['network']['id'],
                                             'test', True, context=ctx)
                # We expect a 500 as we injected a fault in the plugin
                self._validate_behavior_on_bulk_failure(res, 'ports', 500)

    def test_nexus_connect_fail(self):
        """Test failure to connect to a Nexus switch.

        While creating a network, subnet, and port, simulate a connection
        failure to a nexus switch. Confirm that the expected HTTP code
        is returned for the create port operation.

        """
        with self._patch_ncclient('manager.connect.side_effect',
                                  AttributeError):
            with self._create_port_res(self.fmt, no_delete=True,
                                       name='myname') as res:
                self._assertExpectedHTTP(res.status_int,
                                         c_exc.NexusConnectFailed)

    def test_nexus_config_fail(self):
        """Test a Nexus switch configuration failure.

        While creating a network, subnet, and port, simulate a nexus
        switch configuration error. Confirm that the expected HTTP code
        is returned for the create port operation.

        """
        with self._patch_ncclient(
            'manager.connect.return_value.edit_config.side_effect',
            AttributeError):
            with self._create_port_res(self.fmt, no_delete=True,
                                       name='myname') as res:
                self._assertExpectedHTTP(res.status_int,
                                         c_exc.NexusConfigFailed)

    def test_get_seg_id_fail(self):
        """Test handling of a NetworkSegmentIDNotFound exception.

        Test the Cisco NetworkSegmentIDNotFound exception by simulating
        a return of None by the OVS DB get_network_binding method
        during port creation.

        """
        orig = ovs_db_v2.get_network_binding

        def _return_none_if_nexus_caller(self, *args, **kwargs):
            def _calling_func_name(offset=0):
                """Get name of the calling function 'offset' frames back."""
                return inspect.stack()[1 + offset][3]
            if (_calling_func_name(1) == '_get_segmentation_id' and
                _calling_func_name(2) == '_invoke_nexus_for_net_create'):
                return None
            else:
                return orig(self, *args, **kwargs)

        with mock.patch.object(ovs_db_v2, 'get_network_binding',
                               new=_return_none_if_nexus_caller):
            with self._create_port_res(self.fmt, no_delete=True,
                                       name='myname') as res:
                self._assertExpectedHTTP(res.status_int,
                                         c_exc.NetworkSegmentIDNotFound)

    def test_nexus_host_non_configured(self):
        """Test handling of a NexusComputeHostNotConfigured exception.

        Test the Cisco NexusComputeHostNotConfigured exception by using
        a fictitious host name during port creation.

        """
        with mock.patch.object(virt_phy_sw_v2.VirtualPhysicalSwitchModelV2,
                               '_get_instance_host') as mock_get_instance:
            mock_get_instance.return_value = 'fictitious_host'
            with self._create_port_res(self.fmt, no_delete=True,
                                       name='myname') as res:
                self._assertExpectedHTTP(res.status_int,
                                         c_exc.NexusComputeHostNotConfigured)

    def test_nexus_bind_fail_rollback(self):
        """Test for proper rollback following add Nexus DB binding failure.

        Test that the Cisco Nexus plugin correctly rolls back the vlan
        configuration on the Nexus switch when add_nexusport_binding fails
        within the plugin's create_port() method.

        """
        with mock.patch.object(nexus_db_v2, 'add_nexusport_binding',
                               side_effect=KeyError):
            with self._create_port_res(self.fmt, no_delete=True,
                                       name='myname') as res:
                # Confirm that the last configuration sent to the Nexus
                # switch was a removal of vlan from the test interface.
                last_nexus_cfg = (self.mock_ncclient.manager.connect().
                                  edit_config.mock_calls[-1][2]['config'])
                self.assertTrue('<vlan>' in last_nexus_cfg)
                self.assertTrue('<remove>' in last_nexus_cfg)
                self._assertExpectedHTTP(res.status_int, KeyError)

    def test_model_delete_port_rollback(self):
        """Test for proper rollback for OVS plugin delete port failure.

        Test that the nexus port configuration is rolled back (restored)
        by the Cisco model plugin when there is a failure in the OVS
        plugin for a delete port operation.

        """
        with self._create_port_res(self.fmt, name='myname') as res:

            # After port is created, we should have one binding for this
            # vlan/nexus switch.
            port = self.deserialize(self.fmt, res)
            start_rows = nexus_db_v2.get_nexusvlan_binding(self.vlan_start,
                                                           self.switch_ip)
            self.assertEqual(len(start_rows), 1)

            # Inject an exception in the OVS plugin delete_port
            # processing, and attempt a port deletion.
            inserted_exc = q_exc.Conflict
            expected_http = base.FAULT_MAP[inserted_exc].code
            with mock.patch.object(l3_db.L3_NAT_db_mixin,
                                   'disassociate_floatingips',
                                   side_effect=inserted_exc):
                self._delete('ports', port['port']['id'],
                             expected_code=expected_http)

            # Confirm that the Cisco model plugin has restored
            # the nexus configuration for this port after deletion failure.
            end_rows = nexus_db_v2.get_nexusvlan_binding(self.vlan_start,
                                                         self.switch_ip)
            self.assertEqual(start_rows, end_rows)

    def test_nexus_delete_port_rollback(self):
        """Test for proper rollback for nexus plugin delete port failure.

        Test for rollback (i.e. restoration) of a VLAN entry in the
        nexus database whenever the nexus plugin fails to reconfigure the
        nexus switch during a delete_port operation.

        """
        with self._create_port_res(self.fmt, name='myname') as res:

            port = self.deserialize(self.fmt, res)

            # Check that there is only one binding in the nexus database
            # for this VLAN/nexus switch.
            start_rows = nexus_db_v2.get_nexusvlan_binding(self.vlan_start,
                                                           self.switch_ip)
            self.assertEqual(len(start_rows), 1)

            # Simulate a Nexus switch configuration error during
            # port deletion.
            with self._patch_ncclient(
                'manager.connect.return_value.edit_config.side_effect',
                AttributeError):
                self._delete('ports', port['port']['id'],
                             base.FAULT_MAP[c_exc.NexusConfigFailed].code)

            # Confirm that the binding has been restored (rolled back).
            end_rows = nexus_db_v2.get_nexusvlan_binding(self.vlan_start,
                                                         self.switch_ip)
            self.assertEqual(start_rows, end_rows)


class TestCiscoNetworksV2(CiscoNetworkPluginV2TestCase,
                          test_db_plugin.TestNetworksV2):

    def test_create_networks_bulk_emulated_plugin_failure(self):
        real_has_attr = hasattr

        def fakehasattr(item, attr):
            if attr.endswith('__native_bulk_support'):
                return False
            return real_has_attr(item, attr)

        plugin_ref = self._get_plugin_ref()
        orig = plugin_ref.create_network
        #ensures the API choose the emulation code path
        with mock.patch('__builtin__.hasattr',
                        new=fakehasattr):
            with mock.patch.object(plugin_ref,
                                   'create_network') as patched_plugin:
                def side_effect(*args, **kwargs):
                    return self._do_side_effect(patched_plugin, orig,
                                                *args, **kwargs)
                patched_plugin.side_effect = side_effect
                res = self._create_network_bulk(self.fmt, 2, 'test', True)
                LOG.debug("response is %s" % res)
                # We expect a 500 as we injected a fault in the plugin
                self._validate_behavior_on_bulk_failure(res, 'networks', 500)

    def test_create_networks_bulk_native_plugin_failure(self):
        if self._skip_native_bulk:
            self.skipTest("Plugin does not support native bulk network create")
        plugin_ref = self._get_plugin_ref()
        orig = plugin_ref.create_network
        with mock.patch.object(plugin_ref,
                               'create_network') as patched_plugin:

            def side_effect(*args, **kwargs):
                return self._do_side_effect(patched_plugin, orig,
                                            *args, **kwargs)

            patched_plugin.side_effect = side_effect
            res = self._create_network_bulk(self.fmt, 2, 'test', True)
            # We expect a 500 as we injected a fault in the plugin
            self._validate_behavior_on_bulk_failure(res, 'networks', 500)


class TestCiscoSubnetsV2(CiscoNetworkPluginV2TestCase,
                         test_db_plugin.TestSubnetsV2):

    def test_create_subnets_bulk_emulated_plugin_failure(self):
        real_has_attr = hasattr

        #ensures the API choose the emulation code path
        def fakehasattr(item, attr):
            if attr.endswith('__native_bulk_support'):
                return False
            return real_has_attr(item, attr)

        with mock.patch('__builtin__.hasattr',
                        new=fakehasattr):
            plugin_ref = self._get_plugin_ref()
            orig = plugin_ref.create_subnet
            with mock.patch.object(plugin_ref,
                                   'create_subnet') as patched_plugin:

                def side_effect(*args, **kwargs):
                    self._do_side_effect(patched_plugin, orig,
                                         *args, **kwargs)

                patched_plugin.side_effect = side_effect
                with self.network() as net:
                    res = self._create_subnet_bulk(self.fmt, 2,
                                                   net['network']['id'],
                                                   'test')
                # We expect a 500 as we injected a fault in the plugin
                self._validate_behavior_on_bulk_failure(res, 'subnets', 500)

    def test_create_subnets_bulk_native_plugin_failure(self):
        if self._skip_native_bulk:
            self.skipTest("Plugin does not support native bulk subnet create")
        plugin_ref = self._get_plugin_ref()
        orig = plugin_ref.create_subnet
        with mock.patch.object(plugin_ref,
                               'create_subnet') as patched_plugin:
            def side_effect(*args, **kwargs):
                return self._do_side_effect(patched_plugin, orig,
                                            *args, **kwargs)

            patched_plugin.side_effect = side_effect
            with self.network() as net:
                res = self._create_subnet_bulk(self.fmt, 2,
                                               net['network']['id'],
                                               'test')

                # We expect a 500 as we injected a fault in the plugin
                self._validate_behavior_on_bulk_failure(res, 'subnets', 500)


class TestCiscoPortsV2XML(TestCiscoPortsV2):
    fmt = 'xml'


class TestCiscoNetworksV2XML(TestCiscoNetworksV2):
    fmt = 'xml'


class TestCiscoSubnetsV2XML(TestCiscoSubnetsV2):
    fmt = 'xml'
