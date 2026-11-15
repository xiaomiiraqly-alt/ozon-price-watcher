"""Real Chromium HTTPS/cookie regression; Ozon and SMS are replaced by a fixture.

Run separately: pip install '.[browser]' && playwright install chromium
               python tests/browser_login.py
"""
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import ipaddress
from pathlib import Path
import ssl
import sys
import tempfile
from unittest.mock import patch

from aiohttp import web
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from playwright.async_api import async_playwright

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.login_web import COOKIE, build_app  # noqa: E402
from app.storage import Store  # noqa: E402


def local_tls(root:Path) -> ssl.SSLContext:
    key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
    name=x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,"localhost")])
    now=datetime.now(timezone.utc)
    cert=(x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
          .serial_number(x509.random_serial_number()).not_valid_before(now-timedelta(minutes=1))
          .not_valid_after(now+timedelta(days=1)).add_extension(x509.SubjectAlternativeName([
              x509.IPAddress(ipaddress.IPv4Address("127.0.0.1")),x509.DNSName("localhost")]),critical=False)
          .sign(key,hashes.SHA256()))
    (root/"key.pem").write_bytes(key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()))
    (root/"cert.pem").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    tls=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(root/"cert.pem",root/"key.pem")
    return tls


async def main():
    with tempfile.TemporaryDirectory() as tmp:
        root=Path(tmp)
        store=Store(str(root/"test.db"),owner_id=111)
        token=store.issue_login_token(111)
        class API:
            async def call(self,tool,**args):
                return {"session_status":{"signed_in":False},"start_login":{"stage":"code_requested"},
                        "submit_login_code":{"signed_in":True}}[tool]
        @asynccontextmanager
        async def gateway(*args):yield API()
        runner=web.AppRunner(build_app(store,"internal","a"*48),access_log=None)
        landing_runner=None
        await runner.setup()
        try:
            site=web.TCPSite(runner,"127.0.0.1",0,ssl_context=local_tls(root))
            await site.start()
            origin=f"https://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
            landing=web.Application()
            async def link(request):
                return web.Response(text=f'<a href="{origin}/login?t={token}">Open login</a>',content_type="text/html")
            landing.router.add_get("/",link)
            landing_runner=web.AppRunner(landing,access_log=None)
            await landing_runner.setup()
            landing_site=web.TCPSite(landing_runner,"127.0.0.1",0)
            await landing_site.start()
            port=landing_site._server.sockets[0].getsockname()[1]
            with patch("app.login_web.connect_gateway",gateway):
                async with async_playwright() as playwright:
                    browser=await playwright.chromium.launch()
                    try:
                        context=await browser.new_context(ignore_https_errors=True)
                        page=await context.new_page()
                        # Different sites, not just different ports: reproduce a
                        # link opened outside the login site (e.g. Telegram).
                        await page.goto(f"http://localhost:{port}/")
                        await page.get_by_role("link",name="Open login").click()
                        await page.wait_for_url(origin+"/login")
                        await page.get_by_role("heading",name="Подключение своего Ozon").wait_for()
                        cookie=next(c for c in await context.cookies() if c["name"]==COOKIE)
                        assert cookie["secure"] and cookie["httpOnly"] and cookie["sameSite"]=="Lax"
                        await page.locator("input[name=login]").fill("+79001234567")
                        await page.get_by_role("button",name="Получить код от Ozon").click()
                        await page.get_by_role("heading",name="Введите код Ozon").wait_for()
                        await page.locator("input[name=code]").fill("123456")
                        await page.get_by_role("button",name="Подключить Ozon").click()
                        await page.get_by_role("heading",name="Ozon подключён",exact=True).wait_for()
                        assert not any(c["name"]==COOKIE for c in await context.cookies())
                        print("HTTPS cross-site login, token redirect, Secure/HttpOnly/Lax cookie, OTP form and cookie cleanup: OK")
                    finally:await browser.close()
        finally:
            if landing_runner:await landing_runner.cleanup()
            await runner.cleanup()


if __name__=="__main__":asyncio.run(main())
