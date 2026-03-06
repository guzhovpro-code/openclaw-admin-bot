#!/usr/bin/env python3
"""OpenClaw Admin Bot — управление контейнером и мониторинг сервера."""

import os
import asyncio
import time
import datetime
import logging

import httpx
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    ContextTypes, CallbackContext,
)

# ── Конфигурация ──────────────────────────────────────────────
# Все параметры читаются из .env или используют значения по умолчанию.

BOT_TOKEN = os.environ["ADMIN_BOT_TOKEN"]
CHAT_ID = int(os.environ["ALLOWED_TELEGRAM_ID"])

# Имя контейнера OpenClaw Gateway (можно переопределить в .env)
CONTAINER = os.environ.get("OPENCLAW_CONTAINER", "repo-openclaw-gateway-1")

# URL healthcheck-эндпоинта OpenClaw
HEALTH_URL = os.environ.get("OPENCLAW_HEALTH_URL", "http://127.0.0.1:18789/healthz")

# Пороги для алертов
DISK_THRESHOLD = int(os.environ.get("DISK_THRESHOLD", "80"))
RAM_THRESHOLD = int(os.environ.get("RAM_THRESHOLD", "90"))
LOAD_FACTOR = float(os.environ.get("LOAD_FACTOR", "4.0"))

# Антиспам: повторный алерт не чаще чем раз в N секунд
ALERT_COOLDOWN = int(os.environ.get("ALERT_COOLDOWN", "1800"))

# Путь к логу fail2ban (если fail2ban установлен)
FAIL2BAN_LOG = os.environ.get("FAIL2BAN_LOG", "/var/log/fail2ban.log")

# ── Дедупликация алертов ──────────────────────────────────────


class AlertDedup:
    def __init__(self, cooldown: int = ALERT_COOLDOWN):
        self._cd = cooldown
        self._last: dict[str, float] = {}

    def should_alert(self, key: str) -> bool:
        now = time.monotonic()
        if now - self._last.get(key, 0) >= self._cd:
            self._last[key] = now
            return True
        return False

    def reset(self, key: str):
        self._last.pop(key, None)

    def prune(self):
        now = time.monotonic()
        stale = [k for k, v in self._last.items() if now - v > self._cd * 2]
        for k in stale:
            del self._last[k]


dedup = AlertDedup()

# ── Состояние мониторинга ─────────────────────────────────────

_container_was_up: bool = True
_health_was_ok: bool = True
_f2b_offset: int = 0

# ── Утилиты ───────────────────────────────────────────────────


def authorized(update: Update) -> bool:
    return update.effective_user.id == CHAT_ID


async def sh(cmd: str, timeout: int = 30) -> str:
    try:
        proc = await asyncio.create_subprocess_shell(
            cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return (out or b"").decode().strip() or (err or b"").decode().strip() or "(нет вывода)"
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:
            pass
        return "ОШИБКА: таймаут"
    except Exception as e:
        return f"ОШИБКА: {e}"


def keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("СТОП", callback_data="stop"),
            InlineKeyboardButton("СТАРТ", callback_data="start"),
            InlineKeyboardButton("РЕСТАРТ", callback_data="restart"),
        ],
        [
            InlineKeyboardButton("Статус", callback_data="status"),
            InlineKeyboardButton("Здоровье", callback_data="health"),
        ],
        [
            InlineKeyboardButton("Логи (20)", callback_data="logs"),
            InlineKeyboardButton("Система", callback_data="system"),
        ],
        [
            InlineKeyboardButton("Fail2ban", callback_data="f2b"),
        ],
    ])


# ── Команды ───────────────────────────────────────────────────


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return
    await update.message.reply_text(
        "Панель управления OpenClaw\nВыбери действие:",
        reply_markup=keyboard(),
    )


async def cmd_report(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return
    await update.message.reply_text("Собираю отчёт...")

    results = await asyncio.gather(
        sh(f"docker ps -a --filter name={CONTAINER} --format '{{{{.Names}}}}: {{{{.Status}}}}'"),
        sh("df -h / --output=pcent,avail | tail -1"),
        sh("free -m | awk '/Mem:/{printf \"%d/%d МБ (%.0f%% занято)\", $3,$2,$3/$2*100}'"),
        sh("uptime | awk -F'load average:' '{print $2}'"),
        sh(f"curl -s -o /dev/null -w '%{{http_code}}' {HEALTH_URL}"),
        sh("sudo fail2ban-client status sshd 2>/dev/null | grep -E 'Currently|Total' || echo 'fail2ban не установлен'"),
    )

    text = (
        "📊 ОТЧЁТ СЕРВЕРА\n\n"
        f"🐳 Контейнер:\n{results[0]}\n\n"
        f"💾 Диск: {results[1].strip()}\n"
        f"🧠 RAM: {results[2]}\n"
        f"⚡ Нагрузка:{results[3]}\n\n"
        f"🏥 OpenClaw Health: HTTP {results[4]}\n\n"
        f"🛡 Fail2ban:\n{results[5]}"
    )
    await update.message.reply_text(text[:4096])


# ── Обработчик кнопок ─────────────────────────────────────────


async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q.from_user.id != CHAT_ID:
        await q.answer("Нет доступа")
        return
    await q.answer()

    action = q.data
    result = ""

    if action == "stop":
        await q.edit_message_text("Останавливаю...")
        result = f"⛔ ОСТАНОВЛЕН\n{await sh(f'docker stop {CONTAINER}')}"

    elif action == "start":
        await q.edit_message_text("Запускаю...")
        result = f"✅ ЗАПУЩЕН\n{await sh(f'docker start {CONTAINER}')}"

    elif action == "restart":
        await q.edit_message_text("Перезапускаю...")
        result = f"🔄 ПЕРЕЗАПУЩЕН\n{await sh(f'docker restart {CONTAINER}')}"

    elif action == "status":
        result = await sh(f"docker ps -a --filter name={CONTAINER} --format '{{{{.Names}}}}: {{{{.Status}}}}'")

    elif action == "health":
        status = await sh(f"docker ps --filter name={CONTAINER} --format '{{{{.Status}}}}'")
        code = await sh(f"curl -s -o /dev/null -w '%{{http_code}}' {HEALTH_URL}")
        result = f"Контейнер: {status}\nHTTP: {code}"

    elif action == "logs":
        raw = await sh(f"docker logs --tail 20 {CONTAINER}")
        text = f"```\n{raw[:3900]}\n```"
        await q.edit_message_text(text, parse_mode="Markdown")
        await ctx.bot.send_message(chat_id=q.message.chat_id, text="Выбери действие:", reply_markup=keyboard())
        return

    elif action == "system":
        disk = await sh("df -h / --output=pcent,size,avail | tail -1")
        mem = await sh("free -m | awk '/Mem:/{printf \"%d/%d МБ занято\", $3,$2}'")
        load = await sh("uptime")
        result = f"💾 Диск: {disk.strip()}\n🧠 RAM: {mem}\n⚡ {load}"

    elif action == "f2b":
        result = await sh("sudo fail2ban-client status sshd 2>/dev/null || echo 'fail2ban не установлен'")

    await q.edit_message_text(result[:4096])
    await ctx.bot.send_message(chat_id=q.message.chat_id, text="Выбери действие:", reply_markup=keyboard())


# ── Фоновый мониторинг ────────────────────────────────────────


async def job_container(ctx: CallbackContext):
    global _container_was_up
    output = await sh(f"docker ps --filter name={CONTAINER} --format '{{{{.Status}}}}'")
    is_up = output.startswith("Up") if output and output != "(нет вывода)" else False

    if not is_up and _container_was_up:
        if dedup.should_alert("container:down"):
            status = await sh(f"docker ps -a --filter name={CONTAINER} --format '{{{{.Status}}}}'")
            await ctx.bot.send_message(
                chat_id=CHAT_ID,
                text=f"🔴 АЛЕРТ: Контейнер {CONTAINER} УПАЛ\nСтатус: {status}",
            )
    elif is_up and not _container_was_up:
        dedup.reset("container:down")
        await ctx.bot.send_message(
            chat_id=CHAT_ID,
            text=f"🟢 ВОССТАНОВЛЕНО: Контейнер {CONTAINER} снова работает",
        )
    _container_was_up = is_up


async def job_health(ctx: CallbackContext):
    global _health_was_ok
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(HEALTH_URL, timeout=10)
            ok = resp.status_code == 200
    except Exception:
        ok = False

    if not ok and _health_was_ok:
        if dedup.should_alert("health:openclaw"):
            await ctx.bot.send_message(
                chat_id=CHAT_ID,
                text=f"🔴 АЛЕРТ: OpenClaw healthcheck НЕ ОТВЕЧАЕТ\n{HEALTH_URL}",
            )
    elif ok and not _health_was_ok:
        dedup.reset("health:openclaw")
        await ctx.bot.send_message(
            chat_id=CHAT_ID,
            text="🟢 ВОССТАНОВЛЕНО: OpenClaw healthcheck OK",
        )
    _health_was_ok = ok


async def job_fail2ban(ctx: CallbackContext):
    global _f2b_offset
    try:
        size_out = await sh(f"sudo wc -c < {FAIL2BAN_LOG} 2>/dev/null")
        current_size = int(size_out.strip()) if size_out.strip().isdigit() else 0

        if current_size == 0:
            return  # fail2ban не установлен или лог пуст

        if current_size < _f2b_offset:
            _f2b_offset = 0  # лог ротирован

        if _f2b_offset == 0:
            _f2b_offset = current_size
            return

        proc = await asyncio.create_subprocess_shell(
            f"sudo tail -c +{_f2b_offset + 1} {FAIL2BAN_LOG} 2>/dev/null",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        _f2b_offset = current_size
        new_data = out.decode(errors="replace")

        for line in new_data.split("\n"):
            if "Ban" in line and "NOTICE" in line:
                parts = line.strip().split()
                ip = parts[-1] if parts else "?"
                if dedup.should_alert(f"f2b:{ip}"):
                    await ctx.bot.send_message(
                        chat_id=CHAT_ID,
                        text=f"🛡 FAIL2BAN: Забанен {ip}\n{line.strip()[:200]}",
                    )
    except Exception as e:
        logging.warning(f"fail2ban check: {e}")


async def job_disk(ctx: CallbackContext):
    output = await sh("df / --output=pcent | tail -1")
    try:
        pct = int(output.strip().rstrip("%"))
    except ValueError:
        return
    if pct >= DISK_THRESHOLD:
        if dedup.should_alert("disk:/"):
            avail = await sh("df -h / --output=avail | tail -1")
            await ctx.bot.send_message(
                chat_id=CHAT_ID,
                text=f"🔴 АЛЕРТ: Диск заполнен на {pct}%\nСвободно: {avail.strip()}",
            )
    else:
        dedup.reset("disk:/")


async def job_system(ctx: CallbackContext):
    mem = await sh("free | awk '/Mem:/{printf \"%d %d\", $3, $2}'")
    try:
        used, total = map(int, mem.split())
        pct = int(used / total * 100)
        if pct >= RAM_THRESHOLD:
            if dedup.should_alert("ram:high"):
                await ctx.bot.send_message(
                    chat_id=CHAT_ID,
                    text=f"🔴 АЛЕРТ: RAM {pct}% ({used // 1024} / {total // 1024} МБ)",
                )
        else:
            dedup.reset("ram:high")
    except ValueError:
        pass

    load_out = await sh("cat /proc/loadavg")
    nproc = await sh("nproc")
    try:
        load5 = float(load_out.split()[1])
        cores = int(nproc.strip())
        thresh = cores * LOAD_FACTOR
        if load5 > thresh:
            if dedup.should_alert("load:high"):
                await ctx.bot.send_message(
                    chat_id=CHAT_ID,
                    text=f"🔴 АЛЕРТ: Нагрузка {load5} (порог: {thresh}, ядер: {cores})",
                )
        else:
            dedup.reset("load:high")
    except (ValueError, IndexError):
        pass


# ── Инициализация ─────────────────────────────────────────────


async def post_init(app: Application) -> None:
    await app.bot.set_my_commands([
        ("start", "Панель управления"),
        ("report", "Полный отчёт"),
    ])

    jq = app.job_queue
    jq.run_repeating(job_container, interval=60, first=10)
    jq.run_repeating(job_health, interval=120, first=15)
    jq.run_repeating(job_fail2ban, interval=30, first=5)
    jq.run_repeating(job_disk, interval=300, first=20)
    jq.run_repeating(job_system, interval=300, first=25)

    async def prune(ctx: CallbackContext):
        dedup.prune()
    jq.run_repeating(prune, interval=3600, first=3600)

    logging.info("Бот запущен, мониторинг активен")


# ── Точка входа ───────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)


def main():
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("report", cmd_report))
    app.add_handler(CallbackQueryHandler(on_button))
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
