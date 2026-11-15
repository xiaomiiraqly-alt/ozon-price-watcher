"""Family-only Telegram bot: independent Ozon accounts and private price alerts."""
from __future__ import annotations

import asyncio
import html
import logging

from aiogram import Bot, Dispatcher, F
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import Message

from app.config import Config
from app.gateway_client import GatewayError, connect_gateway
from app.monitor import Monitor
from app.storage import Notice, Store

logging.basicConfig(level=logging.INFO,format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger=logging.getLogger(__name__)

HELP="""<b>Ozon Price Watcher — семейный бот</b>

/connect — безопасно подключить свой аккаунт Ozon
/status — состояние своего аккаунта
/sync — проверить цены сейчас
/items [cart|order|manual] [страница] — свои товары
/add ССЫЛКА — добавить товар вручную
/default 100 — свой общий порог уведомлений
/limit SKU 250 — свой порог для артикула
/limit #ID 250 — порог конкретной позиции
/limit #ID default — сбросить порог
/pause SKU, /resume SKU — приостановить/возобновить уведомления
/remove SKU — удалить ручное наблюдение
/mode bank|regular — режим сравнения цены
/disconnect confirm — отвязать Ozon и удалить историю своих товаров

Только владельцу: /invite, /users, /revoke ID.

У каждого пользователя отдельная сессия Ozon, история и уведомления. Одноразовые коды Ozon вводятся только на защищённом сайте входа, не в Telegram."""


def fmt_money(num:int|None)->str:
    return f"{num:,} ₽".replace(","," ") if num is not None else "—"


def fmt_item(row:dict)->str:
    state={"cart":"Корзина","order":"Заказ","manual":"Отдельно"}.get(row["source_type"],"Товар")
    title=html.escape(str(row["title"])[:90])
    threshold=row["threshold"] if row["threshold"] is not None else "по умолчанию"
    muted="🔕 " if row["muted"] else ""
    active="" if row["active"] else f" [ {html.escape(row['state'])} ]"
    return (f"{muted}<b>#{row['id']} {title}</b>{active}\n"
            f"{state} · <code>{row['sku']}</code> · порог {threshold} ₽\n"
            f"База: {fmt_money(row['base_price'])} → сейчас: {fmt_money(row['last_price'])}")


def alert_text(n:Notice)->str:
    from urllib.parse import quote
    link="https://www.ozon.ru/product/"+quote(n.sku,safe="")+"/"
    source={"cart":"в корзине","order":"в оформленном заказе","manual":"в наблюдении"}[n.source_type]
    return (f"📉 <b>Подешевел товар {source}</b>\n"
            f'<a href="{link}">{html.escape(n.title[:130])}</a>\n'
            f"Было: <b>{fmt_money(n.base_price)}</b>\n"
            f"Сейчас: <b>{fmt_money(n.current_price)}</b>\n"
            f"Экономия: <b>{fmt_money(n.base_price-n.current_price)}</b>\n"
            f"Порог: {fmt_money(n.threshold)}\n"
            "Для оформленного заказа стоимость автоматически не пересчитывается.")


def setup_handlers(dp:Dispatcher,store:Store,config:Config,bot:Bot) -> dict[int,Monitor]:
    monitors:dict[int,Monitor]={}

    def is_private(message:Message)->bool:
        return bool(message.chat.type=="private" and message.from_user)

    def allowed(message:Message)->bool:
        return bool(is_private(message) and store.is_allowed(message.from_user.id))

    def mine(message:Message)->Store:
        return store.for_user(message.from_user.id)

    def monitor(user_id:int)->Monitor:
        if user_id not in monitors:
            user_store=store.for_user(user_id)
            async def notify(n:Notice):
                # Recheck after sync; a revoked member cannot receive alerts.
                if store.is_allowed(n.user_id):
                    await bot.send_message(n.user_id,alert_text(n),parse_mode=ParseMode.HTML,disable_web_page_preview=True)
            monitors[user_id]=Monitor(user_store,config.gateway_url,config.gateway_token,
                user_store.get_setting("price_mode",config.price_mode),notify)
        return monitors[user_id]

    @dp.message(Command("start"))
    async def start(message:Message)->None:
        if not is_private(message):return
        args=(message.text or "").split(maxsplit=1)
        uid=message.from_user.id
        if len(args)>1 and args[1].startswith("invite_") and not store.is_allowed(uid):
            if store.accept_invite(args[1][7:],uid,message.from_user.full_name or ""):
                monitor(uid)
                await message.answer("Доступ предоставлен. Для подключения своего Ozon отправьте /connect.\n\n"+HELP,
                                     parse_mode=ParseMode.HTML)
                return
            await message.answer("Ссылка приглашения уже использована или устарела. Запросите новую у владельца.")
            return
        if allowed(message):
            await message.answer(HELP,parse_mode=ParseMode.HTML)
        else:
            await message.answer("Бот доступен только по личному приглашению владельца.")

    @dp.message(Command("help"))
    async def help_cmd(message:Message):
        if allowed(message):await message.answer(HELP,parse_mode=ParseMode.HTML)

    @dp.message(Command("invite"))
    async def invite(message:Message):
        if not allowed(message) or message.from_user.id!=config.owner_id:return
        token=store.make_invite(config.owner_id)
        me=await bot.get_me()
        link=f"https://t.me/{me.username}?start=invite_{token}"
        await message.answer("Одноразовое приглашение, действительно 7 дней:\n"+link+"\nОтправьте ссылку близкому человеку.",disable_web_page_preview=True)

    @dp.message(Command("users"))
    async def users(message:Message):
        if not allowed(message) or message.from_user.id!=config.owner_id:return
        lines=[f"{row['user_id']} · {html.escape(row['display_name'] or ('владелец' if row['role']=='owner' else 'пользователь'))}" for row in store.active_users()]
        await message.answer("Пользователи:\n"+"\n".join(lines),parse_mode=ParseMode.HTML)

    @dp.message(Command("revoke"))
    async def revoke(message:Message):
        if not allowed(message) or message.from_user.id!=config.owner_id:return
        args=(message.text or "").split()
        if len(args)!=2 or not args[1].isdigit():
            await message.answer("Формат: /revoke TELEGRAM_ID")
            return
        try:
            uid=int(args[1])
            if not 0<uid<2**63:raise ValueError()
        except ValueError:
            await message.answer("Нужен положительный числовой Telegram ID.")
            return
        try:
            changed=store.revoke(config.owner_id,uid)
        except PermissionError:
            await message.answer("Нельзя удалить владельца.")
            return
        if not changed:
            await message.answer("Участник не найден или уже отключён.")
            return
        old_monitor=monitors.pop(uid,None)
        if old_monitor:await old_monitor.stop()
        store.erase_user_data(uid)
        try:
            async with connect_gateway(config.gateway_url,config.gateway_token,uid) as api:
                await api.drop_account()
        except GatewayError:
            await message.answer(f"Доступ для {uid} закрыт, база очищена. ВНИМАНИЕ: профиль Ozon на диске не удалён — требуется очистка data/ozon/users/{uid} после остановки ozon-gateway.")
        else:
            await message.answer(f"Доступ для {uid} закрыт. История и профиль Ozon удалены.")

    @dp.message(Command("connect"))
    async def connect(message:Message):
        if not allowed(message):return
        if not config.login_base_url:
            await message.answer("Веб-вход пока не включён. Владельцу нужно задать HTTPS-адрес LOGIN_BASE_URL на сервере.")
            return
        monitor(message.from_user.id)
        token=store.issue_login_token(message.from_user.id)
        await message.answer("Ваша личная ссылка для подключения Ozon (одноразовая, действует 10 минут):\n"
                             f"{config.login_base_url}/login?t={token}\n"
                             "Открывайте только по HTTPS. Код из Ozon вводите на открывшейся странице, не в Telegram.",
                             disable_web_page_preview=True)

    @dp.message(Command("disconnect"))
    async def disconnect(message:Message):
        if not allowed(message):return
        if (message.text or "").split()[-1]!="confirm":
            await message.answer("Для удаления своей Ozon-сессии и истории наблюдений отправьте /disconnect confirm")
            return
        uid=message.from_user.id
        try:
            await monitor(uid).disconnect()
        except GatewayError:
            await message.answer("Не удалось удалить профиль Ozon. Попробуйте ещё раз; данные не менялись.")
            return
        monitors.pop(uid,None)
        await message.answer("Ваш Ozon отключён, история и настройки удалены. Для нового подключения используйте /connect.")

    @dp.message(Command("status"))
    async def status(message:Message):
        if not allowed(message):return
        s=mine(message);m=monitor(message.from_user.id)
        report=[f"Порог: {s.default_threshold} ₽",f"Режим цен: {m.price_mode}",
                f"Интервал: {config.interval//60} мин",f"Последняя проверка: {s.get_setting('last_sync','не проводилась')}"]
        try:
            async with connect_gateway(config.gateway_url,config.gateway_token,s.user_id) as api:
                result=await api.call("session_status")
            if not isinstance(result,dict):raise GatewayError("Invalid session status")
            report.append("Ozon: "+("подключён" if result.get("signed_in") else "не подключён, /connect"))
        except GatewayError:
            report.append("Ozon: сервис недоступен")
        await message.answer("\n".join(report))

    @dp.message(Command("sync"))
    async def sync(message:Message):
        if not allowed(message):return
        await message.answer("Проверяю вашу корзину и заказы.")
        result=await monitor(message.from_user.id).sync()
        await message.answer(result.summary())

    @dp.message(Command("items"))
    async def items(message:Message):
        if not allowed(message):return
        args=(message.text or "").split()
        kind=None;page=1
        if len(args)>1:
            if args[1] in ("cart","order","manual"):
                kind=args[1]
                if len(args)>2 and args[2].isascii() and args[2].isdigit():page=max(1,int(args[2]))
            elif args[1].isascii() and args[1].isdigit():page=max(1,int(args[1]))
            else:
                await message.answer("Формат: /items [cart|order|manual] [страница]")
                return
        rows=mine(message).list_items(kind)
        if not rows:
            await message.answer("Пока нет товаров. /sync или /add ССЫЛКА.")
            return
        start=(page-1)*12
        if start>=len(rows):
            await message.answer("Такой страницы нет.")
            return
        title=f"<b>Мои товары ({page} из {(len(rows)+11)//12})</b>\n\n"
        out=title+"\n\n".join(fmt_item(dict(r)) for r in rows[start:start+12])
        if start+12<len(rows):out+=f"\n\nСледующая: /items {kind+' ' if kind else ''}{page+1}"
        await message.answer(out,parse_mode=ParseMode.HTML,disable_web_page_preview=True)

    @dp.message(Command("default"))
    async def default(message:Message):
        if not allowed(message):return
        try:
            value=int((message.text or "").split()[1]);mine(message).set_default_threshold(value)
        except (IndexError,ValueError):
            await message.answer("Формат: /default 100 (от 1 до 1 000 000 ₽)")
            return
        await message.answer(f"Ваш общий порог: {value} ₽.")

    @dp.message(Command("limit"))
    async def limit(message:Message):
        if not allowed(message):return
        args=(message.text or "").split()
        if len(args)!=3 or (not args[1].isdigit() and not(args[1].startswith("#") and args[1][1:].isdigit())):
            await message.answer("Формат: /limit SKU 250 или /limit #ID default")
            return
        try:
            threshold=None if args[2].lower()=="default" else int(args[2])
            changed=mine(message).set_threshold(args[1],threshold)
        except ValueError:
            await message.answer("Порог 1–1 000 000 ₽ или default.")
            return
        await message.answer(f"Изменено ваших позиций: {changed}.")

    @dp.message(Command("pause","resume"))
    async def mute(message:Message):
        if not allowed(message):return
        args=(message.text or "").split()
        if len(args)!=2:
            await message.answer("Формат: /pause SKU или /resume #ID")
            return
        paused=args[0].split("@")[0]=="/pause"
        count=mine(message).set_muted(args[1],paused)
        await message.answer(f"Изменено ваших позиций: {count}.")

    @dp.message(Command("remove"))
    async def remove(message:Message):
        if not allowed(message):return
        args=(message.text or "").split()
        if len(args)!=2:
            await message.answer("Формат: /remove SKU (только ручные наблюдения)")
            return
        await message.answer(f"Удалено ваших наблюдений: {mine(message).forget_manual(args[1])}.")

    @dp.message(Command("mode"))
    async def mode(message:Message):
        if not allowed(message):return
        args=(message.text or "").split()
        if len(args)!=2 or args[1] not in ("bank","regular"):
            await message.answer("Формат: /mode bank или /mode regular")
            return
        s=mine(message)
        s.set_setting("price_mode",args[1])
        monitor(message.from_user.id).price_mode=args[1]
        await message.answer(f"Ваш режим сравнения: {args[1]}.")

    @dp.message(Command("add"))
    async def add(message:Message):
        if not allowed(message):return
        parts=(message.text or "").split(maxsplit=1)
        if len(parts)!=2:
            await message.answer("Формат: /add https://www.ozon.ru/product/.../")
            return
        try:
            result=await monitor(message.from_user.id).add_manual(parts[1])
        except Exception:
            logger.exception("Adding product failed for user=%s",message.from_user.id)
            await message.answer("Не удалось добавить товар. Проверьте ссылку и подключение Ozon.")
            return
        await message.answer(result)

    @dp.message(F.text)
    async def unknown(message:Message):
        if allowed(message):await message.answer("Неизвестная команда. /help")

    return monitors


async def scheduled_sync(store:Store,monitors:dict[int,Monitor],seconds:int)->None:
    await asyncio.sleep(10)
    while True:
        for user in store.active_users():
            uid=int(user["user_id"])
            # Never sync revoked users or share one member's account with another.
            if not store.is_allowed(uid):continue
            monitor=monitors.get(uid)
            if monitor is None:continue  # bootstrap below creates monitors on startup
            try:
                report=await monitor.sync()
                logger.info("Periodic sync user=%s: %s",uid,report.summary())
            except Exception:
                logger.exception("Periodic sync failed user=%s",uid)
        await asyncio.sleep(seconds)


async def main()->None:
    cfg=Config.from_env()
    store=Store(cfg.db_path,owner_id=cfg.owner_id)
    bot=Bot(token=cfg.bot_token,session=AiohttpSession(proxy=cfg.telegram_proxy_url))
    dp=Dispatcher()
    monitors=setup_handlers(dp,store,cfg,bot)
    # Bootstrap every enrolled member so scheduled checks do not depend on /start.
    for member in store.active_users():
        uid=int(member["user_id"])
        s=store.for_user(uid)
        async def notify(n:Notice):
            if store.is_allowed(n.user_id):
                await bot.send_message(n.user_id,alert_text(n),parse_mode=ParseMode.HTML,disable_web_page_preview=True)
        monitors[uid]=Monitor(s,cfg.gateway_url,cfg.gateway_token,s.get_setting("price_mode",cfg.price_mode),notify)
    worker=asyncio.create_task(scheduled_sync(store,monitors,cfg.interval))
    try:
        await bot.delete_webhook(drop_pending_updates=False)
        await dp.start_polling(bot)
    finally:
        worker.cancel()
        try:await worker
        except asyncio.CancelledError:pass
        await bot.session.close()

if __name__=="__main__":asyncio.run(main())
