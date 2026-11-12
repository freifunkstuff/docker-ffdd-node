from pathlib import Path
import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import gateway_role
import gateway_control
import exit_dns


class GatewayRoleControlTests(unittest.TestCase):
    def test_role_off_never_reads_identity_or_enables_services(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            for name in gateway_role.SERVICES:(root/name).mkdir()
            with patch.dict(os.environ,{'NODE_ROLE':'node'}),patch.object(gateway_role,'load_state') as load:
                self.assertFalse(gateway_role.prepare_role(root,root/'runtime'))
            load.assert_not_called()
            self.assertFalse((root/'runtime/enabled').exists())
            self.assertTrue(all((root/name/'down').exists() for name in gateway_role.SERVICES))

    def test_role_requires_existing_secrets_unless_explicitly_initialized(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            for name in gateway_role.SERVICES:(root/name).mkdir()
            with patch.dict(os.environ,{'NODE_ROLE':'gateway','BACKBONE_ENABLED':'0','GATEWAY_ALLOW_IDENTITY_GENERATION':'false'}),patch.object(gateway_role,'load_state',return_value={}):
                with self.assertRaisesRegex(ValueError,'pre-existing'):gateway_role.prepare_role(root,root/'runtime')
            with patch.dict(os.environ,{'NODE_ROLE':'gateway','BACKBONE_ENABLED':'0','GATEWAY_ALLOW_IDENTITY_GENERATION':'true'}):
                self.assertTrue(gateway_role.prepare_role(root,root/'runtime'))
            self.assertTrue((root/'runtime/enabled').exists())

    def test_announcement_requires_explicit_request_and_health_and_withdraws(self):
        calls=[]
        def run(args,**kwargs):calls.append(args);return subprocess.CompletedProcess(args,0,'','')
        controller=gateway_control.AnnouncementController(run)
        controller.apply(True,False)
        self.assertNotIn('-g',calls[-1])
        controller.apply(False,True)
        self.assertFalse(controller.advertised)
        controller.apply(True,True)
        self.assertIn('-g',calls[-1]);self.assertTrue(controller.advertised)
        controller.apply(False,True)
        self.assertIn('-r',calls[-1]);self.assertFalse(controller.advertised)

    def test_gateway_community_dns_survives_exit_loss_without_public_fallback(self):
        with patch.dict(os.environ,{'NODE_ROLE':'gateway'}):
            config=exit_dns.dns_configuration({'enabled':True,'exits':[]},'10.200.3.149')
        self.assertIn('no-resolv',config)
        self.assertIn('server=/ffdd/10.200.0.4@10.200.3.149',config)
        self.assertNotIn('server=10.64.',config)
        self.assertNotIn('resolv-file',config)
