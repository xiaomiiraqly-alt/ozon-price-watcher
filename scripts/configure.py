#!/usr/bin/env python3
"""Secure interactive installation wizard: Telegram token, owner, AWG share link."""
from __future__ import annotations

import argparse
import getpass
import os
import termios
import re
import secrets
import sys
import tempfile
from pathlib import Path

# This file can run from any working directory with no third-party packages.
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.awg_import import import_awg_profile  # noqa: E402

TOKEN_RE = re.compile(r"^[0-9]{5,}:[A-Za-z0-9_-]{20,}$")
USER_RE = re.compile(r"^[0-9]{1,20}$")
DOMAIN_RE = re.compile(r"^(?=.{4,253}$)[a-zA-Z0-9-]+(?:\.[a-zA-Z0-9-]+)+$")


def read_hidden_link(prompt: str) -> str:
    """Read past Linux TTY canonical 4096-byte limit, with echo disabled.

    Some AWG 3.x vpn:// exports are long; getpass() uses canonical input and
    can silently truncate a paste. Read one character at a time in noncanonical
    mode, restoring terminal state even on Ctrl-C or errors.
    """
    with open("/dev/tty", "rb", buffering=0) as tty:
        fd = tty.fileno()
        original = termios.tcgetattr(fd)
        mode = termios.tcgetattr(fd)
        mode[3] &= ~(termios.ICANON | termios.ECHO)
        mode[6][termios.VMIN] = 1
        mode[6][termios.VTIME] = 0
        chars = bytearray()
        try:
            sys.stderr.write(prompt)
            sys.stderr.flush()
            termios.tcsetattr(fd, termios.TCSANOW, mode)
            while len(chars) <= 150_000:
                char = os.read(fd, 1)
                if char in (b"\r", b"\n", b""):
                    break
                if char in (b"\x7f", b"\x08"):
                    if chars:
                        chars.pop()
                else:
                    chars.extend(char)
            else:
                raise ValueError("Ключ превышает допустимый размер")
        finally:
            termios.tcsetattr(fd, termios.TCSANOW, original)
            sys.stderr.write("\n")
            sys.stderr.flush()
    return chars.decode("utf-8").removeprefix("\x1b[200~").removesuffix("\x1b[201~")


def env_values(raw: str) -> dict[str, str]:
    result = {}
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        result[key] = val
    return result


def render_env(existing: str, token: str, owner: str, domain: str | None = None) -> str:
    # Preserve non-secret customization without introducing duplicate entries.
    initial = env_values((ROOT / ".env.example").read_text(encoding="utf-8"))
    initial.update(env_values(existing))
    initial.update({
        "TELEGRAM_BOT_TOKEN": token,
        "TELEGRAM_USER_ID": owner,
        "TELEGRAM_PROXY_URL": "socks5://awg-client:1080",
        "OZON_GATEWAY_URL": "http://ozon-gateway:8085",
        "GATEWAY_TOKEN": initial.get("GATEWAY_TOKEN") or secrets.token_urlsafe(48),
        "DATABASE_PATH": "/data/watcher.sqlite3",
    })
    if domain is not None:
        domain = domain.strip().lower()
        if domain and not DOMAIN_RE.fullmatch(domain):
            raise ValueError("Домен должен выглядеть как ozon.example.com (без https:// и /)")
        initial["LOGIN_DOMAIN"] = domain
        initial["LOGIN_BASE_URL"] = f"https://{domain}" if domain else ""
    return "# Local secrets: chmod 600, never commit\n" + "\n".join(f"{k}={v}" for k, v in initial.items()) + "\n"


def write_private(path: Path, data: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temp = tempfile.mkstemp(prefix=".install-", dir=str(path.parent))
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
        os.chmod(path, 0o600)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def configure(token: str, owner: str, key: str, root: Path = ROOT, domain: str | None = None) -> None:
    if not TOKEN_RE.fullmatch(token):
        raise ValueError("Неверный формат Telegram-токена от @BotFather")
    if not USER_RE.fullmatch(owner) or int(owner) < 1:
        raise ValueError("Нужен числовой Telegram User ID")
    profile = import_awg_profile(key)
    env = root / ".env"
    original = env.read_text(encoding="utf-8") if env.is_file() else ""
    new_env = render_env(original, token, owner, domain)
    conf = root / "data/awg-client/wg_confs/awg0.conf"
    # Never overwrite the previous known-good configuration on parse failure.
    write_private(conf, profile)
    write_private(env, new_env)


def main() -> int:
    argparse.ArgumentParser(description="Interactive personal Ozon watcher setup").parse_args()
    if not sys.stdin.isatty():
        print("Установка требует интерактивного терминала (SSH с -t).", file=sys.stderr)
        return 2
    print("Ozon Price Watcher — первоначальная настройка")
    print("Ни токен, ни AWG-ключ, ни приватные ключи не будут выводиться в терминал.")
    print("Получите отдельного клиента в AWG-Router: vpn:// или awg://, либо .conf файл.")
    existing_file = ROOT / ".env"
    previous = env_values(existing_file.read_text(encoding="utf-8")) if existing_file.exists() else {}
    current_token = previous.get("TELEGRAM_BOT_TOKEN", "")
    while True:
        token = getpass.getpass("Токен Telegram от @BotFather" + (" (Enter = оставить прежний)" if current_token else "") + ": ").strip() or current_token
        if TOKEN_RE.fullmatch(token):
            break
        print("Неверный формат токена. Проверьте значение в @BotFather.")
    old_id = previous.get("TELEGRAM_USER_ID", "")
    while True:
        owner = input(f"Ваш числовой Telegram User ID" + (f" [{old_id}]" if old_id else "") + ": ").strip() or old_id
        if USER_RE.fullmatch(owner) and int(owner) > 0:
            break
        print("Введите положительный числовой User ID (не @username).")
    previous_domain = previous.get("LOGIN_DOMAIN", "")
    print("Чтобы близкие входили в Ozon со своего телефона, нужен HTTPS-домен.")
    print("Можно пропустить сейчас; /connect заработает после настройки HTTPS.")
    while True:
        domain = input("Домен входа (например ozon.example.com)" + (f" [{previous_domain}]" if previous_domain else "") + ": ").strip() or previous_domain
        if not domain or DOMAIN_RE.fullmatch(domain):
            break
        print("Введите только имя домена без схемы https:// и без пути.")
    old_profile = ROOT / "data/awg-client/wg_confs/awg0.conf"
    while True:
        instruction = "Ссылка AWG vpn:// или awg:// (ввод скрыт)"
        if old_profile.is_file():
            instruction += " (Enter = оставить прежний профиль)"
        link = read_hidden_link(instruction + ": ").strip()
        if not link and old_profile.is_file():
            link = str(old_profile)
        try:
            configure(token, owner, link, domain=domain)
            break
        except ValueError as exc:
            print(f"Профиль не сохранён: {exc}")
    print("Настройки записаны локально: .env и data/awg-client/wg_confs/awg0.conf")
    if domain: print("HTTPS-адрес входа:", "https://"+domain+"/login")
    else: print("Веб-вход пока выключен: задайте домен и HTTPS при установке.")
    print("Права на оба файла: 600. Их содержимое не нужно передавать в Telegram или GitHub.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
