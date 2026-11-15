"""Static invariants for split egress: a configuration regression must fail tests."""
from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest.mock import patch

from app.config import Config

try:
    import yaml
except ImportError:  # yaml is an optional test dependency
    yaml = None

ROOT = Path(__file__).resolve().parents[1]


class BotProxyConfigTests(unittest.TestCase):
    def test_proxy_required(self):
        with patch.dict(os.environ, {
            'TELEGRAM_BOT_TOKEN': '123:TEST', 'TELEGRAM_USER_ID': '123456',
            'TELEGRAM_PROXY_URL': '',
        }, clear=True):
            with self.assertRaisesRegex(ValueError, 'TELEGRAM_PROXY_URL'):
                Config.from_env()

    def test_proxy_must_be_socks5(self):
        with patch.dict(os.environ, {
            'TELEGRAM_BOT_TOKEN': '123:TEST', 'TELEGRAM_USER_ID': '123456',
            'TELEGRAM_PROXY_URL': 'http://awg-client:1080',
        }, clear=True):
            with self.assertRaisesRegex(ValueError, 'TELEGRAM_PROXY_URL'):
                Config.from_env()

    def test_proxy_config(self):
        with patch.dict(os.environ, {
            'TELEGRAM_BOT_TOKEN': '123:TEST', 'TELEGRAM_USER_ID': '123456',
            'TELEGRAM_PROXY_URL': 'socks5://awg-client:1080', 'GATEWAY_TOKEN': 'a'*48,
        }, clear=True):
            self.assertEqual(Config.from_env().telegram_proxy_url, 'socks5://awg-client:1080')

    def test_invalid_owner_proxy_and_login_origin(self):
        values={'TELEGRAM_BOT_TOKEN':'123:TEST','TELEGRAM_USER_ID':'111','TELEGRAM_PROXY_URL':'socks5://awg-client:1080','GATEWAY_TOKEN':'a'*48}
        for key,value in (('TELEGRAM_USER_ID','9'*100),('TELEGRAM_USER_ID','²'),('TELEGRAM_PROXY_URL','socks5://'),('LOGIN_BASE_URL','https://'),('LOGIN_BASE_URL','https://example.org/path'),('LOGIN_BASE_URL','https://user:secret@example.org')):
            with self.subTest(key=key,value=value),patch.dict(os.environ,{**values,key:value},clear=True),self.assertRaises(ValueError):
                Config.from_env()


@unittest.skipIf(yaml is None, 'install PyYAML for Docker YAML tests')
class DockerEgressTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.compose = yaml.safe_load((ROOT / 'compose.yaml').read_text())
        cls.gost = yaml.safe_load((ROOT / 'deploy/gost.yaml').read_text())

    def test_bot_has_no_direct_internet_network(self):
        bot = self.compose['services']['bot']
        self.assertEqual(bot['networks'], ['private_backend'])
        self.assertTrue(self.compose['networks']['private_backend']['internal'])
        self.assertNotIn('network_mode', bot)

    def test_ozon_has_independent_direct_egress(self):
        mcp = self.compose['services']['ozon-gateway']
        self.assertIn('direct_egress', mcp['networks'])
        self.assertNotIn('network_mode', mcp)
        self.assertNotIn('ports', mcp)

    def test_family_login_exposes_only_loopback_http_and_optional_https(self):
        auth = self.compose['services']['login-web']
        self.assertEqual(auth['networks'], ['private_backend'])
        self.assertNotIn('ports', auth)
        publisher=self.compose['services']['login-publisher']
        self.assertEqual(publisher['ports'], ['127.0.0.1:8765:8080'])
        self.assertEqual(publisher['networks'], ['private_backend','web_publisher_egress'])
        self.assertNotIn('env_file',publisher)
        self.assertFalse(self.compose['networks']['web_publisher_egress'].get('internal',False))
        nginx=(ROOT/'deploy/login-publisher.conf').read_text()
        self.assertIn('proxy_pass http://login-web:8086;',nginx)
        self.assertIn('access_log off;',nginx)
        caddy = self.compose['services']['family-https']
        self.assertEqual(caddy['profiles'], ['family-https'])
        self.assertIn('80:80', caddy['ports'])
        self.assertIn('443:443', caddy['ports'])

    def test_gateway_not_given_telegram_bot_secret(self):
        gateway = self.compose['services']['ozon-gateway']
        self.assertNotIn('env_file', gateway)
        self.assertNotIn('TELEGRAM_BOT_TOKEN', gateway['environment'])
        caddy=self.compose['services']['family-https']
        self.assertNotIn('env_file',caddy)
        self.assertEqual(set(caddy['environment']),{'LOGIN_DOMAIN'})

    def test_telegram_proxy_uses_awg_namespace_only(self):
        proxy = self.compose['services']['telegram-proxy']
        self.assertEqual(proxy['network_mode'], 'service:awg-client')
        self.assertNotIn('ports', proxy)
        self.assertNotIn('ports', self.compose['services']['awg-client'])
        node = self.gost['chains'][0]['hops'][0]['nodes'][0]
        self.assertEqual(node['interface'], 'awg0')
        self.assertEqual(node['connector']['type'], 'virtual')
        self.assertEqual(node['dialer']['type'], 'virtual')

    def test_dns_also_uses_vpn(self):
        resolver = self.gost['resolvers'][0]
        self.assertEqual(resolver['nameservers'][0]['chain'], 'vpn-egress')


if __name__ == '__main__':
    unittest.main()
