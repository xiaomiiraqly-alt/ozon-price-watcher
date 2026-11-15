"""Offline, size-bounded import of AWG client profiles and share keys.

No network requests are made; keys never leave this process. A key is treated
as an untrusted configuration, not as shell code. Only known wg-quick options
are allowed into the generated profile; hooks and arbitrary directives are
rejected. The converter never logs the source input or private keys.
"""
from __future__ import annotations

import base64
import binascii
import configparser
import ipaddress
import json
import re
import zlib
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

MAX_SOURCE = 150_000
MAX_DECOMPRESSED = 250_000

INTERFACE_FIELDS = (
    "PrivateKey", "Address", "MTU", "ListenPort",
    "Jc", "Jmin", "Jmax", "S1", "S2", "S3", "S4",
    "H1", "H2", "H3", "H4", "I1", "I2", "I3", "I4", "I5",
    "HeaderProtectionKey", "ContentPaddingAddition", "RandomTrailers",
    "DisableCookies",
    # AWG 3.x protocol timers. They may be exact integers or randomized ranges.
    "RekeyAfterTime", "RekeyTimeout", "RejectAfterTime",
    "KeepaliveTimeout", "MaxHandshakeAttempts",
)
AWG3_TIMER_FIELDS = ("RekeyAfterTime", "RekeyTimeout", "RejectAfterTime", "KeepaliveTimeout")
PEER_FIELDS = ("PublicKey", "PresharedKey", "Endpoint", "PersistentKeepalive")
PREFIXES = ("awg://", "amneziawg://", "wg://", "wireguard://")


def _decode_b64(payload: str) -> bytes:
    payload = payload.strip().rstrip("=")
    if not payload or len(payload) > MAX_SOURCE or not re.fullmatch(r"[A-Za-z0-9_+/-]+", payload):
        raise ValueError("Неверный формат ключа подключения")
    try:
        out = base64.b64decode(payload.replace("-", "+").replace("_", "/") + "=" * (-len(payload) % 4), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("Не удалось прочитать Base64-ключ") from exc
    if len(out) > MAX_SOURCE:
        raise ValueError("Ключ превышает допустимый размер")
    return out


def _decompress(payload: bytes) -> bytes:
    # AmneziaVPN's vpn:// key uses Qt's qCompress: 4-byte BE size + zlib.
    if len(payload) > 6 and payload[4] == 0x78:
        expected_size = int.from_bytes(payload[:4], "big")
        packed = payload[4:]
    elif payload[:1] == b"x":
        expected_size = None
        packed = payload
    else:
        return payload  # A few third-party generators use plain JSON.
    if expected_size is not None and expected_size > MAX_DECOMPRESSED:
        raise ValueError("Слишком большой распакованный конфиг")
    try:
        inflater = zlib.decompressobj()
        result = inflater.decompress(packed, MAX_DECOMPRESSED + 1)
        if len(result) > MAX_DECOMPRESSED or inflater.unconsumed_tail or not inflater.eof:
            raise ValueError("Слишком большой или неполный конфиг")
        result += inflater.flush()
        if len(result) > MAX_DECOMPRESSED or inflater.unused_data:
            raise ValueError("Некорректный сжатый конфиг")
    except zlib.error as exc:
        raise ValueError("Не удалось распаковать vpn:// ключ") from exc
    if expected_size is not None and len(result) != expected_size:
        raise ValueError("Размер распакованного конфига не совпадает")
    return result


def _from_vpn_key(link: str) -> str:
    encoded = link[len("vpn://"):].split("#", 1)[0]
    try:
        blob = _decompress(_decode_b64(encoded))
        obj = json.loads(blob.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Не удалось прочитать конфигурацию в vpn:// ключе") from exc
    if not isinstance(obj, dict) or not isinstance(obj.get("containers"), list):
        raise ValueError("В vpn:// нет контейнеров подключения")
    for entry in obj["containers"]:
        if not isinstance(entry, dict) or entry.get("container") not in {"amnezia-awg", "amnezia-awg2", "amneziawg"}:
            continue
        proto = entry.get("awg") or entry.get("amnezia-awg") or entry.get("amneziawg")
        if not isinstance(proto, dict):
            continue
        last = proto.get("last_config")
        if isinstance(last, str):
            try:
                last = json.loads(last)
            except json.JSONDecodeError as exc:
                raise ValueError("Не удалось прочитать настройки AWG из vpn://") from exc
        if not isinstance(last, dict):
            continue
        if isinstance(last.get("config"), str) and "[Interface]" in last["config"]:
            return last["config"]
        # Alternative Amnezia exporters have the structured fields but omit raw INI.
        fields = last
        required = ("client_priv_key", "client_ip", "server_pub_key")
        if not all(isinstance(fields.get(key), str) and fields[key] for key in required):
            continue
        host = fields.get("hostName") or obj.get("hostName")
        port = fields.get("port") or proto.get("port")
        if not isinstance(host, str) or not host or not str(port).isdigit():
            continue
        interface = {
            "PrivateKey": fields["client_priv_key"],
            "Address": fields["client_ip"],
        }
        for key in INTERFACE_FIELDS:
            if key in fields and key not in interface and fields[key] is not None:
                interface[key] = str(fields[key])
        if fields.get("mtu"):
            interface["MTU"] = str(fields["mtu"])
        if ":" in host and not host.startswith("["):
            host=f"[{host}]"
        peer = {
            "PublicKey": fields["server_pub_key"],
            "Endpoint": f"{host}:{port}",
            "AllowedIPs": "0.0.0.0/0",
        }
        if fields.get("psk_key"):
            peer["PresharedKey"] = str(fields["psk_key"])
        if fields.get("persistent_keep_alive"):
            peer["PersistentKeepalive"] = str(fields["persistent_keep_alive"])
        return _ini(interface, peer)
    raise ValueError("В vpn:// не найден профиль AmneziaWG с настройками клиента")


def _from_awg_key(link: str) -> str:
    scheme, rest = link.split("://", 1)
    rest = rest.split("#", 1)[0]
    if not rest:
        raise ValueError("Пустой AWG-ключ")
    # First form: base64 of a complete .conf. No username@host authority.
    if "@" not in rest and "?" not in rest:
        try:
            plain = _decode_b64(rest).decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ValueError("Ссылка не содержит UTF-8 AWG-конфигурацию") from exc
        if "[Interface]" not in plain:
            raise ValueError("Ссылка не содержит конфигурацию [Interface]")
        return plain
    # Second form: awg://PRIVATE_KEY@host:port?publickey=...&address=... etc.
    parsed = urlsplit(f"{scheme}://{rest}")
    if not parsed.username or not parsed.hostname or not parsed.port:
        raise ValueError("В AWG-ссылке отсутствуют ключ, адрес или порт")
    q = {key.lower(): values[-1] for key, values in parse_qs(parsed.query, keep_blank_values=False).items()}
    if not q.get("publickey") or not q.get("address"):
        raise ValueError("В AWG-ссылке отсутствуют publickey или address")
    interface: dict[str, str] = {"PrivateKey": unquote(parsed.username), "Address": q["address"]}
    mapping = {name.lower(): name for name in INTERFACE_FIELDS if name not in {"PrivateKey", "Address"}}
    mapping.update({"keepalive": "PersistentKeepalive"})
    for k, name in mapping.items():
        if k in q and name != "PersistentKeepalive":
            interface[name] = q[k]
    host=f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
    peer = {"PublicKey": q["publickey"], "Endpoint": f"{host}:{parsed.port}", "AllowedIPs": "0.0.0.0/0"}
    for source, target in (("presharedkey", "PresharedKey"), ("keepalive", "PersistentKeepalive"), ("persistentkeepalive", "PersistentKeepalive")):
        if q.get(source):
            peer[target] = q[source]
    return _ini(interface, peer)


def _ini(interface: dict[str, str], peer: dict[str, str]) -> str:
    return "[Interface]\n" + "\n".join(f"{k} = {v}" for k, v in interface.items()) + "\n\n[Peer]\n" + "\n".join(f"{k} = {v}" for k, v in peer.items()) + "\n"


def _check_key(value: str, name: str) -> None:
    try:
        decoded = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError(f"Неверный {name} в профиле AWG") from exc
    if len(decoded) != 32:
        raise ValueError(f"Неверная длина {name} в профиле AWG")


def _safe_profile(raw: str) -> str:
    if len(raw.encode("utf-8")) > MAX_SOURCE:
        raise ValueError("Конфигурация слишком большая")
    parser = configparser.ConfigParser(interpolation=None, strict=True)
    parser.optionxform = str
    try:
        parser.read_string(raw)
    except configparser.Error as exc:
        raise ValueError("Неверный формат конфигурации [Interface]/[Peer]") from exc
    if set(parser.sections()) != {"Interface", "Peer"}:
        raise ValueError("Ожидается один клиентский профиль с [Interface] и [Peer]")
    interface = dict(parser.items("Interface"))
    peer = dict(parser.items("Peer"))
    for key in interface:
        if key not in INTERFACE_FIELDS and key not in {"AllowedIPs", "DNS", "Table", "SaveConfig"}:
            raise ValueError(f"Неизвестная или опасная директива Interface: {key}")
    for key in peer:
        if key not in PEER_FIELDS and key != "AllowedIPs":
            raise ValueError(f"Неизвестная директива Peer: {key}")
    # Unlike arbitrary wg-quick hooks, these AWG 3.x keys are safe numeric
    # configuration options. Reject malformed input rather than passing it to
    # the VPN client; do not drop the values, which can change wire behaviour.
    for key in (*AWG3_TIMER_FIELDS, "MaxHandshakeAttempts"):
        if key not in interface:
            continue
        value = interface[key]
        if not re.fullmatch(r"[0-9]{1,6}(?:-[0-9]{1,6})?", value):
            raise ValueError(f"Неверное значение AWG 3.x: {key}")
        bounds = [int(part) for part in value.split("-")]
        upper_limit = 10_000 if key == "MaxHandshakeAttempts" else 86_400
        if min(bounds) < 1 or max(bounds) > upper_limit or bounds != sorted(bounds):
            raise ValueError(f"Неверный диапазон AWG 3.x: {key}")
    if not all(interface.get(field) for field in ("PrivateKey", "Address")):
        raise ValueError("В профиле отсутствуют PrivateKey или Address")
    if not all(peer.get(field) for field in ("PublicKey", "Endpoint")):
        raise ValueError("В профиле отсутствуют PublicKey или Endpoint")
    for key, source in (("PrivateKey", interface), ("PublicKey", peer), ("PresharedKey", peer), ("HeaderProtectionKey", interface)):
        if source.get(key):
            # HeaderProtectionKey is an opaque AWG3 value; 32-byte base64 is expected.
            _check_key(source[key], key)
    try:
        addresses = [ipaddress.ip_interface(x.strip()) for x in interface["Address"].split(",")]
        if not any(x.version == 4 for x in addresses):
            raise ValueError("В профиле должен быть IPv4 адрес клиента")
    except ValueError as exc:
        raise ValueError("В профиле неверный IPv4 адрес клиента") from exc
    interface["Address"] = ", ".join(str(x) for x in addresses if x.version == 4)
    # Endpoint: valid hostname/IP (IPv4 or [IPv6]) and UDP port. Avoid log/command injection.
    endpoint = peer["Endpoint"]
    m = re.fullmatch(r"(\[[0-9a-fA-F:]+\]|[A-Za-z0-9._-]+):([0-9]{1,5})", endpoint)
    if not m or not 1 <= int(m.group(2)) <= 65535:
        raise ValueError("В профиле неверный Endpoint")
    for source in (interface, peer):
        for key, value in source.items():
            if any(ch in value for ch in "\r\n\0"):
                raise ValueError(f"Недопустимый символ в {key}")
    # Safety: no DNS, scripts, SaveConfig, or Table=off imported; full-tunnel
    # applies only inside the client's Docker network namespace.
    return _ini({key: interface[key] for key in INTERFACE_FIELDS if interface.get(key)}, {
        **{key: peer[key] for key in PEER_FIELDS if peer.get(key)},
        "AllowedIPs": "0.0.0.0/0",
    })


def import_awg_profile(user_input: str) -> str:
    """Return sanitized wg-quick profile from pasted URL, INI or local file path."""
    source = user_input.strip()
    if not source:
        raise ValueError("Нужна ссылка AWG или путь к .conf файлу")
    if source.startswith("vpn://"):
        raw = _from_vpn_key(source)
    elif source.startswith(PREFIXES):
        raw = _from_awg_key(source)
    elif source.startswith("[Interface]"):
        raw = source
    elif source.startswith(("http://", "https://")):
        raise ValueError("Веб-ссылки не загружаются: нужна vpn:// или awg:// ссылка, либо локальный .conf файл")
    else:
        path = Path(source).expanduser()
        if not path.is_file():
            raise ValueError("Неподдерживаемый формат ссылки. Используйте vpn://, awg:// или путь к .conf")
        if path.stat().st_size > MAX_SOURCE:
            raise ValueError("Файл профиля слишком большой")
        raw = path.read_text(encoding="utf-8-sig")
    return _safe_profile(raw)
