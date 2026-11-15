"""Minimal HTTPS-only self-service Ozon login portal for invited family members.

Bot issues a 10-minute one-time link. The link is exchanged for a Secure,
HttpOnly cookie and disappears from the address bar before any login form.
One-time Ozon codes are never sent to Telegram or persisted in SQLite.
"""
from __future__ import annotations

import html
import asyncio
import os
import re
import secrets

from aiohttp import web

from app.gateway_client import GatewayError, connect_gateway
from app.storage import Store

COOKIE="ozw_login"


def page(title:str,body:str,csrf:str="") -> web.Response:
    csrf_field=f'<input type="hidden" name="csrf" value="{html.escape(csrf,quote=True)}">' if csrf else ''
    document=f'''<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)} — Ozon Watcher</title>
<style>body{{font:16px/1.5 system-ui,sans-serif;background:#f5f5f5;color:#151515;max-width:450px;margin:10vh auto;padding:24px}}
main{{background:white;padding:25px;border:1px solid #ddd;border-radius:12px}}
label{{display:block;margin-top:18px}}input{{width:100%;padding:12px;margin:6px 0 16px;box-sizing:border-box;font:inherit}}
button{{width:100%;padding:12px;background:#1754c3;color:white;border:0;border-radius:8px;font:inherit;cursor:pointer}}
small{{color:#666}}.error{{color:#b00020}}</style></head><body><main><h2>{html.escape(title)}</h2>{body.replace('%%CSRF%%',csrf_field)}
<p><small>Код Ozon передаётся только по HTTPS на ваш сервер, а не через Telegram. Доступ к аккаунту: только чтение.</small></p>
</main></body></html>'''
    return web.Response(text=document,content_type="text/html",charset="utf-8")


def build_app(store:Store, gateway_url:str, gateway_token:str) -> web.Application:
    if len(gateway_token)<32:
        raise ValueError("GATEWAY_TOKEN is required")

    # There is one Ozon login state per user, even when two browser tabs POST.
    user_locks:dict[int,asyncio.Lock]={}

    @web.middleware
    async def serial_login(request:web.Request,handler):
        if request.method=="POST":
            session=store.web_session(request.cookies.get(COOKIE,""))
            if session:
                lock=user_locks.setdefault(int(session["user_id"]),asyncio.Lock())
                async with lock:
                    return await handler(request)
        return await handler(request)

    @web.middleware
    async def security_headers(request:web.Request, handler):
        headers={
            "Cache-Control":"no-store", "Pragma":"no-cache", "Referrer-Policy":"no-referrer",
            "X-Content-Type-Options":"nosniff", "X-Frame-Options":"DENY",
            "Content-Security-Policy":"default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'",
            "Strict-Transport-Security":"max-age=15552000; includeSubDomains",
        }
        try:
            response=await handler(request)
        except web.HTTPException as exc:
            exc.headers.update(headers)
            raise
        response.headers.update(headers)
        return response

    app=web.Application(middlewares=[security_headers,serial_login],client_max_size=1024)
    routes=web.RouteTableDef()

    def active_session(request:web.Request):
        sid=request.cookies.get(COOKIE,"")
        data=store.web_session(sid)
        return sid,data

    def protected_post(request:web.Request,session,fields:dict):
        if not session or session["attempts"] >= 6:
            raise web.HTTPForbidden()
        if not secrets.compare_digest(session["csrf"].encode(),str(fields.get("csrf","")).encode()):
            raise web.HTTPForbidden()

    @routes.get("/health")
    async def health(request:web.Request):
        return web.Response(text="ok")

    @routes.get("/login")
    async def login(request:web.Request):
        token=request.query.get("t","")
        if token:
            redeemed=store.consume_login_token(token)
            if not redeemed:
                return page("Ссылка недействительна","<p>Ссылка использована или срок её действия истёк. Запросите /connect в Telegram повторно.</p>")
            sid,_=redeemed
            response=web.HTTPSeeOther(location="/login")
            # A link opened from Telegram is a cross-site top-level navigation.
            # Strict cookies are omitted on the following 303 redirect in Chrome
            # and other browsers, so the session appears lost immediately.
            # Lax permits safe GET navigation while CSRF protects form POSTs.
            response.set_cookie(COOKIE,sid,secure=True,httponly=True,samesite="Lax",max_age=1200,path="/login")
            raise response
        sid,session=active_session(request)
        if not session:
            return page("Нужна новая ссылка","<p>Вернитесь в Telegram и отправьте /connect.</p>")
        if session["stage"]=="code":
            return page("Введите код Ozon",'''<form action="/login/confirm" method="post">%%CSRF%%
<label>Одноразовый код из SMS, приложения или email</label><input name="code" type="text" inputmode="numeric" pattern="[0-9]{4,10}" maxlength="10" autocomplete="one-time-code" required>
<button type="submit">Подключить Ozon</button></form>''',session["csrf"])
        return page("Подключение своего Ozon",'''<form action="/login/start" method="post">%%CSRF%%
<label>Телефон или email вашего аккаунта Ozon</label>
<input name="login" type="text" maxlength="160" autocomplete="username" required>
<button type="submit">Получить код от Ozon</button></form>''',session["csrf"])

    @routes.post("/login/start")
    async def start(request:web.Request):
        sid,session=active_session(request)
        fields=dict(await request.post())
        protected_post(request,session,fields)
        if session["stage"]!="start":raise web.HTTPBadRequest()
        login=str(fields.get("login","")).strip()
        if len(login)>160 or not re.fullmatch(r"[a-zA-Z0-9@+._()\- ]{4,160}",login):
            return page("Неверный ввод","<p class=error>Укажите номер телефона или email.</p>")
        if store.web_attempt(sid)>6:raise web.HTTPTooManyRequests()
        try:
            async with connect_gateway(gateway_url,gateway_token,int(session["user_id"])) as api:
                status=await api.call("session_status")
                if not isinstance(status,dict):raise GatewayError("Invalid session status")
                if status.get("signed_in"):
                    store.end_web_session(sid)
                    return page("Ozon уже подключён","<p>Чтобы подключить другой аккаунт, сначала выполните /disconnect confirm в Telegram.</p>")
                result=await api.call("start_login",login=login)
                if not isinstance(result,dict) or result.get("stage")!="code_requested":
                    raise GatewayError("Login code was not requested")
        except GatewayError:
            return page("Не удалось отправить код","<p class=error>Не удалось выполнить вход. Попробуйте повторно или запросите новую ссылку.</p>")
        store.web_stage(sid,"code")
        raise web.HTTPSeeOther(location="/login")

    @routes.post("/login/confirm")
    async def confirm(request:web.Request):
        sid,session=active_session(request)
        fields=dict(await request.post())
        protected_post(request,session,fields)
        if session["stage"]!="code":raise web.HTTPBadRequest()
        code=str(fields.get("code","")).strip()
        if not re.fullmatch(r"[0-9]{4,10}",code):
            return page("Неверный код","<p class=error>Введите цифровой код из сообщения Ozon.</p>")
        if store.web_attempt(sid)>6:raise web.HTTPTooManyRequests()
        try:
            async with connect_gateway(gateway_url,gateway_token,int(session["user_id"])) as api:
                result=await api.call("submit_login_code",code=code)
        except GatewayError:
            return page("Ошибка авторизации","<p class=error>Код не принят или истёк. Начните подключение заново через /connect.</p>")
        if not isinstance(result,dict) or not result.get("signed_in"):
            return page("Вход не выполнен","<p class=error>Ozon не подтвердил авторизацию. Запросите новую ссылку через /connect.</p>")
        store.end_web_session(sid)
        response=page("Ozon подключён","<p>Аккаунт подключён. Вернитесь в Telegram и отправьте /sync.</p>")
        response.del_cookie(COOKIE,path="/login")
        return response

    app.add_routes(routes)
    return app


def main():
    gateway_token=os.environ.get("GATEWAY_TOKEN","")
    owner_id=int(os.environ["TELEGRAM_USER_ID"])
    app=build_app(Store(os.environ.get("DATABASE_PATH","/data/watcher.sqlite3"),owner_id),
                  os.environ.get("OZON_GATEWAY_URL","http://ozon-gateway:8085"),gateway_token)
    web.run_app(app,host="0.0.0.0",port=8086,access_log=None)

if __name__=="__main__":main()
