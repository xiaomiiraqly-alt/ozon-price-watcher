"""Share-link conversion, CLI configuration and secret-handling regression tests."""
from __future__ import annotations

import base64
import importlib.util
import json
import tempfile
import unittest
import zlib
from pathlib import Path
from urllib.parse import quote

from app.awg_import import import_awg_profile

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("installer", ROOT / "scripts" / "configure.py")
assert spec and spec.loader
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


def key(c: bytes) -> str:
    return base64.b64encode(c * 32).decode("ascii")


CLIENT = f"""[Interface]
PrivateKey = {key(b'a')}
Address = 10.100.1.8/32
DNS = 9.9.9.9
Table = off
MTU = 1280
Jc = 5
H1 = 1000-2000
I1 = <b 0x160301><r 5>
RandomTrailers = on
HeaderProtectionKey = {key(b'c')}

[Peer]
PublicKey = {key(b'b')}
PresharedKey = {key(b'd')}
Endpoint = 127.0.0.1:47083
AllowedIPs = 10.1.0.0/16, ::/0
PersistentKeepalive = 25
"""


def url_from_config(config: str) -> str:
    data = config.encode()
    return "awg://" + base64.urlsafe_b64encode(data).decode().rstrip("=") + "#my-bot"


def vpn_uri(config: str) -> str:
    nested = json.dumps({"config": config})
    envelope = {"containers": [{"container": "amnezia-awg", "awg": {"last_config": nested}}], "defaultContainer": "amnezia-awg"}
    data = json.dumps(envelope).encode()
    return "vpn://" + base64.urlsafe_b64encode(len(data).to_bytes(4, "big") + zlib.compress(data)).decode().rstrip("=")


class ImportTests(unittest.TestCase):
    def test_raw_conf_sanitized_and_full_tunnel(self):
        p = import_awg_profile(CLIENT)
        self.assertIn("AllowedIPs = 0.0.0.0/0", p)
        self.assertIn("H1 = 1000-2000", p)
        self.assertIn("I1 = <b 0x160301><r 5>", p)
        self.assertIn("RandomTrailers = on", p)
        self.assertNotIn("Table =", p)
        self.assertNotIn("DNS =", p)
        self.assertNotIn("::/0", p)

    def test_awg3_timers_preserved_in_raw_and_share_links(self):
        config = CLIENT.replace("RandomTrailers = on", """RandomTrailers = on
RekeyAfterTime = 100-145
RekeyTimeout = 4-10
RejectAfterTime = 170-210
KeepaliveTimeout = 8-22
MaxHandshakeAttempts = 12-28""")
        for source in (config, url_from_config(config), vpn_uri(config)):
            profile = import_awg_profile(source)
            for line in ("RekeyAfterTime = 100-145", "RekeyTimeout = 4-10",
                         "RejectAfterTime = 170-210", "KeepaliveTimeout = 8-22",
                         "MaxHandshakeAttempts = 12-28"):
                self.assertIn(line, profile)

    def test_awg3_single_integer_values(self):
        config = CLIENT.replace("RandomTrailers = on", "RandomTrailers = on\nRekeyAfterTime = 120\nMaxHandshakeAttempts = 18")
        profile = import_awg_profile(config)
        self.assertIn("RekeyAfterTime = 120", profile)
        self.assertIn("MaxHandshakeAttempts = 18", profile)

    def test_awg3_timers_reject_invalid_values(self):
        for invalid in ("1; echo hacked", "20-10", "0", "1-9999999", "-20"):
            with self.subTest(invalid=invalid), self.assertRaisesRegex(ValueError, "AWG 3.x"):
                import_awg_profile(CLIENT.replace("RandomTrailers = on", f"RandomTrailers = on\nRekeyAfterTime = {invalid}"))

    def test_awg_base64_link(self):
        self.assertEqual(import_awg_profile(url_from_config(CLIENT)), import_awg_profile(CLIENT))

    def test_amnezia_qcompress_vpn_uri(self):
        self.assertEqual(import_awg_profile(vpn_uri(CLIENT)), import_awg_profile(CLIENT))

    def test_structured_vpn_uri_without_ini(self):
        fields = {"client_priv_key": key(b'a'), "client_ip": "10.4.0.2/32", "server_pub_key": key(b'b'), "port": 47083, "hostName": "127.0.0.1", "Jc": "4", "S1": "12"}
        envelope = {"containers": [{"container": "amnezia-awg", "awg": {"last_config": json.dumps(fields)}}]}
        json_data = json.dumps(envelope).encode()
        uri = "vpn://" + base64.urlsafe_b64encode(len(json_data).to_bytes(4,"big") + zlib.compress(json_data)).decode().rstrip("=")
        profile = import_awg_profile(uri)
        self.assertIn("Endpoint = 127.0.0.1:47083", profile)
        self.assertIn("Jc = 4", profile)

    def test_uri_with_key_host(self):
        uri = f"awg://{quote(key(b'a'), safe='')}@127.0.0.1:47083?publickey={quote(key(b'b'), safe='')}&address=10.4.0.2%2F32&jc=4&keepalive=25"
        profile = import_awg_profile(uri)
        self.assertIn("PrivateKey = ", profile)
        self.assertIn("PersistentKeepalive = 25", profile)

    def test_uri_preserves_bracketed_ipv6_endpoint(self):
        uri=f"awg://{quote(key(b'a'),safe='')}@[2001:db8::1]:47083?publickey={quote(key(b'b'),safe='')}&address=10.4.0.2%2F32"
        self.assertIn("Endpoint = [2001:db8::1]:47083",import_awg_profile(uri))

    def test_file_path(self):
        with tempfile.TemporaryDirectory() as d:
            file = Path(d) / "client.conf"
            file.write_text(CLIENT)
            self.assertEqual(import_awg_profile(str(file)), import_awg_profile(CLIENT))

    def test_rejects_hook(self):
        with self.assertRaisesRegex(ValueError, "опасная директива"):
            import_awg_profile(CLIENT.replace("DNS = 9.9.9.9", "PostUp = curl example.com"))

    def test_rejects_web_link(self):
        with self.assertRaisesRegex(ValueError, "Веб-ссылки"):
            import_awg_profile("https://localhost/private-config")

    def test_rejects_enormous_compressed_vpn_uri(self):
        blob = b"{}"
        fake = (99999999).to_bytes(4, "big") + zlib.compress(blob)
        with self.assertRaisesRegex(ValueError, "слишком большой|Слишком большой"):
            import_awg_profile("vpn://" + base64.urlsafe_b64encode(fake).decode().rstrip("="))

    def test_rejects_bad_public_key(self):
        with self.assertRaisesRegex(ValueError, "PublicKey"):
            import_awg_profile(CLIENT.replace("PublicKey = "+key(b'b'), "PublicKey = abc"))


class InstallationTests(unittest.TestCase):
    def test_configuration_writes_private_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            installer.configure("123456:abcdefghijklmnopqrstuvwxyzABCDEF123", "987654", vpn_uri(CLIENT), root)
            env = root / ".env"
            conf = root / "data/awg-client/wg_confs/awg0.conf"
            self.assertIn("TELEGRAM_BOT_TOKEN=123456:", env.read_text())
            self.assertIn("TELEGRAM_USER_ID=987654", env.read_text())
            self.assertIn("AllowedIPs = 0.0.0.0/0", conf.read_text())
            self.assertEqual(env.stat().st_mode & 0o777, 0o600)
            self.assertEqual(conf.stat().st_mode & 0o777, 0o600)

    def test_domain_and_gateway_secret(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            installer.configure("123456:abcdefghijklmnopqrstuvwxyzABCDEF123", "987654", CLIENT, root, domain="ozon.example.org")
            values = installer.env_values((root / ".env").read_text())
            self.assertEqual(values["LOGIN_DOMAIN"], "ozon.example.org")
            self.assertEqual(values["LOGIN_BASE_URL"], "https://ozon.example.org")
            self.assertGreaterEqual(len(values["GATEWAY_TOKEN"]),32)
            previous = values["GATEWAY_TOKEN"]
            installer.configure("123456:abcdefghijklmnopqrstuvwxyzABCDEF123", "987654", CLIENT, root, domain="ozon.example.org")
            self.assertEqual(installer.env_values((root / ".env").read_text())["GATEWAY_TOKEN"],previous)

    def test_invalid_domain(self):
        with tempfile.TemporaryDirectory() as tmp:
            for domain in ("http://bad.invalid","ozon.-invalid.org","ozon.invalid-.org","a"*64+".org"):
                with self.subTest(domain=domain),self.assertRaisesRegex(ValueError,"Домен"):
                    installer.configure("123456:abcdefghijklmnopqrstuvwxyzABCDEF123", "987654", CLIENT, Path(tmp), domain=domain)

    def test_oversized_owner_is_rejected_before_writing(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                installer.configure("123456:abcdefghijklmnopqrstuvwxyzABCDEF123","9223372036854775808",CLIENT,Path(tmp))
            self.assertFalse((Path(tmp)/".env").exists())

    def test_short_existing_gateway_secret_is_replaced(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            (root/".env").write_text("GATEWAY_TOKEN=short\n")
            installer.configure("123456:abcdefghijklmnopqrstuvwxyzABCDEF123","987654",CLIENT,root)
            self.assertGreaterEqual(len(installer.env_values((root/".env").read_text())["GATEWAY_TOKEN"]),32)

    def test_invalid_link_does_not_replace_previous_secrets(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            installer.configure("123456:abcdefghijklmnopqrstuvwxyzABCDEF123", "987654", CLIENT, root)
            conf = root / "data/awg-client/wg_confs/awg0.conf"
            old = conf.read_bytes()
            old_env = (root / ".env").read_bytes()
            with self.assertRaises(ValueError):
                installer.configure("123456:abcdefghijklmnopqrstuvwxyzABCDEF123", "987654", "vpn://invalid", root)
            self.assertEqual(conf.read_bytes(), old)
            self.assertEqual((root / ".env").read_bytes(), old_env)


if __name__ == "__main__":
    unittest.main()
