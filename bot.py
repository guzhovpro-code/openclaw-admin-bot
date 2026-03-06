#!/usr/bin/env python3
"""OpenClaw Admin Bot v2 — управление контейнером и мониторинг сервера."""

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

BOT_TOKEN = os.environ["ADMIN_BOT_TOKEN"]
CHAT_ID = int(os.environ["ALLOWED_TELEGRAM_ID"])

PRIMARY = "repo-openclaw-gateway-1"
CONTAINERS = [
    "repo-openclaw-gateway-1",
    "repo-openclaw-cli-1",
    "root-traefik-1",
    "root-n8n-1",
    "main-icambio-finance",
    "usdt-icambio-finance",
]
EXPECTED_DOWN = {"repo-openclaw-cli-1"}

HEALTH_URL = "http://127.0.0.1:18789/healthz"

DISK_THRESHOLD = 80
RAM_THRESHOLD = 90
LOAD_FACTOR = 4.0

ALERT_COOLDOWN = 1800  # 30 мин

FAIL2BAN_LOG = "/var/log/fail2ban.log"
CONFIG_BACKUP_LOG = "/var/log/openclaw-backup.log"
MONGO_BACKUP_LOG = "/var/log/mongo_backup.log"

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

_container_was_up: dict[str, bool] = {}
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
            InlineKeyboardButton("Бэкапы", callback_data="backups"),
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
        sh("docker ps -a --format '{{.Names}}: {{.Status}}'"),
        sh("df -h / --output=pcent,avail | tail -1"),
        sh("free -m | awk '/Mem:/{printf \"%d/%d МБ (%.0f%% занято)\", $3,$2,$3/$2*100}'"),
        sh("uptime | awk -F'load average:' '{print $2}'"),
        sh(f"curl -s -o /dev/null -w '%{{http_code}}' {HEALTH_URL}"),
        sh("sudo fail2ban-client status sshd 2>/dev/null | grep -E 'Currently|Total'"),
        sh(f"tail -2 {CONFIG_BACKUP_LOG} 2>/dev/null"),
        sh(f"tail -3 {MONGO_BACKUP_LOG} 2>/dev/null"),
    )

    text = (
        "📊 ОТЧЁТ СЕРВЕРА\n\n"
        f"🐳 Контейнеры:\n{results[0]}\n\n"
        f"💾 Диск: {results[1].strip()}\n"
        f"🧠 RAM: {results[2]}\n"
        f"⚡ Нагрузка:{results[3]}\n\n"
        f"🏥 OpenClaw Health: HTTP {results[4]}\n\n"
        f"🛡 Fail2ban:\n{results[5]}\n\n"
        f"📦 Бэкап конфигов:\n{results[6]}\n\n"
        f"📦 Бэкап MongoDB:\n{results[7]}"
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
        result = f"⛔ ОСТАНОВЛЕН\n{await sh(f'docker stop {PRIMARY}')}"

    elif action == "start":
        await q.edit_message_text("Запускаю...")
        result = f"✅ ЗАПУЩЕН\n{await sh(f'docker start {PRIMARY}')}"

    elif action == "restart":
        await q.edit_message_text("Перезапускаю...")
        result = f"🔄 ПЕРЕЗАПУЩЕН\n{await sh(f'docker restart {PRIMARY}')}"

    elif action == "status":
        result = await sh("docker ps -a --format '{{.Names}}: {{.Status}}'")

    elif action == "health":
        status = await sh(f"docker ps --filter name={PRIMARY} --format '{{{{.Status}}}}'")
        code = await sh(f"curl -s -o /dev/null -w '%{{http_code}}' {HEALTH_URL}")
        result = f"Контейнер: {status}\nHTTP: {code}"

    elif action == "logs":
        raw = await sh(f"docker logs --tail 20 {PRIMARY}")
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
        result = await sh("sudo fail2ban-client status sshd 2>/dev/null")

    elif action == "backups":
        cfg = await sh(f"tail -2 {CONFIG_BACKUP_LOG} 2>/dev/null")
        mongo = await sh(f"tail -3 {MONGO_BACKUP_LOG} 2>/dev/null")
        result = f"📦 Конфиги:\n{cfg}\n\n📦 MongoDB:\n{mongo}"

    await q.edit_message_text(result[:4096])
    await ctx.bot.send_message(chat_id=q.message.chat_id, text="Выбери действие:", reply_markup=keyboard())


# ── Фоновый мониторинг ────────────────────────────────────────


async def job_containers(ctx: CallbackContext):
    output = await sh("docker ps -a --format '{{.Names}}|{{.Status}}'")
    for line in output.strip().split("\n"):
        if "|" not in line:
            continue
        name, status = line.split("|", 1)
        name = name.strip()
        if name not in CONTAINERS or name in EXPECTED_DOWN:
            continue
        is_up = status.strip().startswith("Up")
        was_up = _container_was_up.get(name, True)

        if not is_up and was_up:
            if dedup.should_alert(f"container:{name}"):
                await ctx.bot.send_message(
                    chat_id=CHAT_ID,
                    text=f"🔴 АЛЕРТ: Контейнер {name} УПАЛ\nСтатус: {status.strip()}",
                )
        elif is_up and not was_up:
            dedup.reset(f"container:{name}")
            await ctx.bot.send_message(
                chat_id=CHAT_ID,
                text=f"🟢 ВОССТАНОВЛЕНО: Контейнер {name} снова работает",
            )
        _container_was_up[name] = is_up


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
        # Проверка ротации лога
        size_out = await sh(f"sudo wc -c < {FAIL2BAN_LOG} 2>/dev/null")
        current_size = int(size_out.strip()) if size_out.strip().isdigit() else 0
        if current_size < _f2b_offset:
            _f2b_offset = 0

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
    # RAM
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

    # Нагрузка
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


async def job_backup_config(ctx: CallbackContext):
    today = datetime.date.today().isoformat()
    output = await sh(f"tail -5 {CONFIG_BACKUP_LOG} 2>/dev/null")
    if "ERROR" in output.upper() or "error" in output:
        if dedup.should_alert("backup:config:error"):
            await ctx.bot.send_message(
                chat_id=CHAT_ID,
                text=f"🔴 АЛЕРТ: Бэкап конфигов — ошибки\n{output[:500]}",
            )
    elif today not in output:
        if dedup.should_alert("backup:config"):
            await ctx.bot.send_message(
                chat_id=CHAT_ID,
                text=f"⚠️ Бэкап конфигов: нет записи за {today}\n{output[:500]}",
            )


async def job_backup_mongo(ctx: CallbackContext):
    today = datetime.date.today().isoformat()
    output = await sh(f"tail -10 {MONGO_BACKUP_LOG} 2>/dev/null")
    if "FAILED" in output.upper() and today in output:
        if dedup.should_alert("backup:mongo"):
            await ctx.bot.send_message(
                chat_id=CHAT_ID,
                text=f"🔴 АЛЕРТ: Бэкап MongoDB ПРОВАЛИЛСЯ\n{output[:500]}",
            )


# ── Инициализация ─────────────────────────────────────────────


async def post_init(app: Application) -> None:
    await app.bot.set_my_commands([
        ("start", "Панель управления"),
        ("report", "Полный отчёт"),
    ])

    jq = app.job_queue
    jq.run_repeating(job_containers, interval=60, first=10)
    jq.run_repeating(job_health, interval=120, first=15)
    jq.run_repeating(job_fail2ban, interval=30, first=5)
    jq.run_repeating(job_disk, interval=300, first=20)
    jq.run_repeating(job_system, interval=300, first=25)

    jq.run_daily(
        job_backup_config,
        time=datetime.time(hour=7, minute=10, tzinfo=datetime.timezone.utc),
    )
    jq.run_daily(
        job_backup_mongo,
        time=datetime.time(hour=3, minute=10, tzinfo=datetime.timezone.utc),
    )

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
