#!/usr/bin/env python3
"""OpenClaw Admin Bot v3.0 — мульти-провайдер + защита от ложных алертов."""

import os
import asyncio
import time
import datetime
import calendar
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

# Расходы — ключи провайдеров
OPENAI_ADMIN_KEY = os.environ.get("OPENAI_ADMIN_KEY", "")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
COST_DAILY_LIMIT = float(os.environ.get("COST_DAILY_LIMIT", "10.0"))
COST_CHECK_INTERVAL = int(os.environ.get("COST_CHECK_INTERVAL", "300"))
ALERT_AUTO_DELETE = int(os.environ.get("ALERT_AUTO_DELETE", "3600"))

# Ретраи — сколько подряд неудач до алерта
HEALTH_FAIL_THRESHOLD = 3
COST_FAIL_THRESHOLD = 2

# Ступенчатые пороги расходов (% от лимита)
COST_WARN_THRESHOLDS = [
    (0.50, "🟡", "50%"),
    (0.75, "🟡", "75%"),
    (0.90, "🟠", "90%"),
    (1.00, "🔴", "100%"),
]

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
_health_fail_count: int = 0
_openai_api_was_ok: bool = True
_openai_fail_count: int = 0
_f2b_offset: int = 0

# Очистка чата — отслеживание сообщений
_prev_data_msg_id: int | None = None
_prev_kb_msg_id: int | None = None

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


async def _delete_msg(bot, msg_id: int):
    """Безопасное удаление сообщения (игнорирует ошибки)."""
    try:
        await bot.delete_message(chat_id=CHAT_ID, message_id=msg_id)
    except Exception:
        pass


async def _delete_msg_job(ctx: CallbackContext):
    """Удаление сообщения по расписанию."""
    await _delete_msg(ctx.bot, ctx.job.data)


async def send_alert(ctx: CallbackContext, text: str,
                     delete_after: int | None = None):
    """Отправить алерт и запланировать автоудаление."""
    if delete_after is None:
        delete_after = ALERT_AUTO_DELETE
    msg = await ctx.bot.send_message(chat_id=CHAT_ID, text=text)
    if delete_after > 0:
        ctx.job_queue.run_once(
            _delete_msg_job, when=delete_after, data=msg.message_id,
        )


# ── Клавиатура ────────────────────────────────────────────────


def keyboard() -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton("📊 Дашборд", callback_data="dashboard"),
            InlineKeyboardButton("💰 Расходы", callback_data="costs"),
        ],
        [
            InlineKeyboardButton("🔄 Перезапуск", callback_data="restart_ask"),
        ],
    ]
    return InlineKeyboardMarkup(rows)


# ── Форматирование дашборда ───────────────────────────────────

_MONTHS_RU = {
    1: "янв", 2: "фев", 3: "мар", 4: "апр", 5: "май", 6: "июн",
    7: "июл", 8: "авг", 9: "сен", 10: "окт", 11: "ноя", 12: "дек",
}


def _fmt_uptime(started_at: str) -> str:
    """ISO-дату старта → '3д 14ч 22мин'."""
    try:
        start = datetime.datetime.fromisoformat(started_at.replace("Z", "+00:00"))
        delta = datetime.datetime.now(datetime.timezone.utc) - start
        total_sec = int(delta.total_seconds())
        if total_sec < 60:
            return f"{total_sec} сек"
        days = total_sec // 86400
        hours = (total_sec % 86400) // 3600
        mins = (total_sec % 3600) // 60
        parts = []
        if days:
            parts.append(f"{days}д")
        if hours:
            parts.append(f"{hours}ч")
        parts.append(f"{mins}мин")
        return " ".join(parts)
    except Exception:
        return "?"


async def _fmt_disk() -> str:
    """'42% занято (12 ГБ свободно)'."""
    raw = await sh("df / --output=pcent,avail | tail -1")
    try:
        parts = raw.split()
        pct = parts[0].rstrip("%")
        avail_kb = int(parts[1])
        if avail_kb >= 1_048_576:
            avail = f"{avail_kb / 1_048_576:.1f} ГБ"
        else:
            avail = f"{avail_kb // 1024} МБ"
        return f"{pct}% занято ({avail} свободно)"
    except Exception:
        return raw.strip()


async def _fmt_ram() -> str:
    """'67% (2.1 / 3.2 ГБ)'."""
    raw = await sh("free -m | awk '/Mem:/{print $2, $3}'")
    try:
        total_mb, used_mb = map(int, raw.split())
        pct = int(used_mb / total_mb * 100)
        return f"{pct}% ({used_mb / 1024:.1f} / {total_mb / 1024:.1f} ГБ)"
    except Exception:
        return raw.strip()


async def _fmt_f2b() -> str:
    """'3 IP забанено' или 'не активен'."""
    raw = await sh("sudo fail2ban-client status sshd 2>/dev/null")
    try:
        for line in raw.split("\n"):
            if "Currently banned" in line:
                n = int(line.split(":")[-1].strip())
                if n == 0:
                    return "нет банов"
                ip_word = "IP" if n == 1 else "IP"
                return f"{n} {ip_word} забанено"
        return "не активен"
    except Exception:
        return "?"


async def _fmt_backup_config() -> str:
    """Статус бэкапа конфигов — по логу с датами."""
    today = datetime.date.today().isoformat()
    raw = await sh(f"tail -5 {CONFIG_BACKUP_LOG} 2>/dev/null")
    if not raw.strip() or raw == "(нет вывода)":
        return "⚠️ лог недоступен"
    if "ERROR" in raw.upper() or "FAILED" in raw.upper():
        return "❌ ошибка"
    if today in raw:
        for line in reversed(raw.split("\n")):
            if today in line:
                try:
                    # [config-backup 2026-03-08 04:00] → 04:00
                    time_part = line.split(today)[1].strip().split("]")[0].strip()
                    return f"✅ сегодня {time_part}"
                except Exception:
                    return "✅ сегодня"
        return "✅ сегодня"
    return "❌ нет записи за сегодня"


async def _fmt_backup_mongo() -> str:
    """Статус бэкапа MongoDB — по rclone логу (без дат, проверяем свежесть файла)."""
    # Проверяем когда лог последний раз менялся
    age = await sh(f"stat -c %Y {MONGO_BACKUP_LOG} 2>/dev/null")
    if not age.strip().isdigit():
        return "⚠️ лог недоступен"
    last_mod = int(age.strip())
    now = int(datetime.datetime.now().timestamp())
    hours_ago = (now - last_mod) / 3600
    if hours_ago > 48:
        return "❌ давно не обновлялся"
    # Проверяем содержимое на ошибки
    raw = await sh(f"tail -5 {MONGO_BACKUP_LOG} 2>/dev/null")
    if "ERROR" in raw.upper() or "FAILED" in raw.upper():
        return "❌ ошибка"
    if "100%" in raw or "Transferred" in raw:
        if hours_ago < 24:
            return "✅ ок"
        return f"✅ {int(hours_ago)}ч назад"
    return "✅ ок"


async def _check_openai_api() -> bool:
    """Проверить доступность OpenAI API."""
    if OPENAI_API_KEY:
        # Project key → /v1/models
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    "https://api.openai.com/v1/models",
                    headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
                    params={"limit": 1},
                )
                return resp.status_code == 200
        except Exception:
            return False
    elif OPENAI_ADMIN_KEY:
        # Admin key → /v1/organization/costs (быстрая проверка)
        _, ok = await get_today_cost()
        return ok
    return True  # Нечего проверять


async def build_dashboard() -> str:
    """Собрать красивый дашборд."""
    # Собираем данные параллельно
    gather_tasks = [
        sh(f"docker ps --filter name={PRIMARY} --format '{{{{.Status}}}}'"),
        sh(f"docker inspect --format '{{{{.State.StartedAt}}}}' {PRIMARY} 2>/dev/null"),
        get_today_cost(),
        _fmt_disk(),
        _fmt_ram(),
        _fmt_f2b(),
        _fmt_backup_config(),
        _fmt_backup_mongo(),
    ]
    if OPENROUTER_API_KEY:
        gather_tasks.append(get_openrouter_usage())

    results = await asyncio.gather(*gather_tasks)

    container_status = results[0]
    started_at = results[1]
    today_cost, api_ok = results[2]
    disk = results[3]
    ram = results[4]
    f2b = results[5]
    backup_cfg = results[6]
    backup_mongo = results[7]
    or_data = results[8] if OPENROUTER_API_KEY else None

    is_up = container_status.strip().startswith("Up")

    lines = ["📊 ДАШБОРД", "━━━━━━━━━━━━━━━━━"]

    # OpenClaw статус
    if is_up:
        uptime = _fmt_uptime(started_at.strip())
        lines.append(f"🤖 OpenClaw: ✅ работает")
        lines.append(f"   ⏱ аптайм: {uptime}")
    else:
        lines.append(f"🤖 OpenClaw: ❌ не работает")

    lines.append("")

    # Суммарные расходы
    total_today = 0.0
    cost_parts = []

    if OPENAI_ADMIN_KEY or OPENAI_API_KEY:
        api_emoji = "✅" if api_ok else "❌"
        lines.append(f"🌐 OpenAI API: {api_emoji}")
        if api_ok:
            total_today += today_cost
            cost_parts.append(f"OpenAI ${today_cost:.2f}")

    if or_data and or_data["ok"]:
        or_daily = or_data["daily"] or 0
        total_today += or_daily
        cost_parts.append(f"OpenRouter ${or_daily:.2f}")

    lines.append(f"💰 Расходы сегодня: ${total_today:.2f}")
    if cost_parts:
        lines.append(f"   ({' + '.join(cost_parts)})")
    lines.append("")

    # Ресурсы
    lines.append(f"💾 Диск: {disk}")
    lines.append(f"🧠 Память: {ram}")
    lines.append(f"🛡 Fail2ban: {f2b}")

    # Бэкапы
    lines.append(f"📦 Бэкап конфигов: {backup_cfg}")
    lines.append(f"📦 Бэкап MongoDB: {backup_mongo}")

    return "\n".join(lines)


# ── OpenAI Usage ──────────────────────────────────────────────


def _pretty_model(name: str) -> str:
    """gpt-5.4-2026-03-05 → GPT-5.4, gpt-4o-transcribe → Транскрипция"""
    n = name.lower()
    if "gpt-5.4" in n:
        return "GPT-5.4"
    if "gpt-5.2" in n:
        return "GPT-5.2"
    if "mini" in n and "transcribe" in n:
        return "Транскрипция (мини)"
    if "transcribe" in n:
        return "Транскрипция"
    if "whisper" in n:
        return "Распознавание речи"
    return name.split("-2026")[0].split("-2025")[0].upper()


def _pretty_date(iso: str) -> str:
    """2026-03-06 → 6 мар"""
    try:
        d = datetime.date.fromisoformat(iso[:10])
        return f"{d.day} {_MONTHS_RU.get(d.month, '?')}"
    except Exception:
        return iso[:10]


def _fmt_tokens(n: int) -> str:
    """7430012 → 7.4 млн, 23795 → 24 тыс, 500 → 500"""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f} млн"
    if n >= 1_000:
        return f"{n // 1_000} тыс"
    return str(n)


async def fetch_openai_costs(days: int = 2) -> str:
    """Получить расходы OpenAI за последние N дней — понятным языком."""
    if not OPENAI_ADMIN_KEY:
        return "⚠️ OPENAI_ADMIN_KEY не задан в .env"

    now = datetime.datetime.now(datetime.timezone.utc)
    start = now - datetime.timedelta(days=days)
    start_ts = int(start.timestamp())
    end_ts = int(now.timestamp())

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            costs_resp = await client.get(
                "https://api.openai.com/v1/organization/costs",
                params={
                    "start_time": start_ts,
                    "end_time": end_ts,
                    "bucket_width": "1d",
                    "group_by[]": "line_item",
                },
                headers={"Authorization": f"Bearer {OPENAI_ADMIN_KEY}"},
            )
            usage_resp = await client.get(
                "https://api.openai.com/v1/organization/usage/completions",
                params={
                    "start_time": start_ts,
                    "end_time": end_ts,
                    "bucket_width": "1d",
                    "group_by[]": "model",
                },
                headers={"Authorization": f"Bearer {OPENAI_ADMIN_KEY}"},
            )

        if costs_resp.status_code != 200:
            return f"❌ Ошибка API: {costs_resp.status_code}\n{costs_resp.text[:200]}"

        costs_data = costs_resp.json()
        usage_data = usage_resp.json()

        # ── Собираем расходы по дням, группируя по модели ──
        day_costs: dict[str, dict[str, float]] = {}
        day_totals: dict[str, float] = {}

        for bucket in costs_data.get("data", []):
            day = bucket.get("start_time_iso", "?")[:10]
            if day not in day_costs:
                day_costs[day] = {}
                day_totals[day] = 0.0
            for r in bucket.get("results", []):
                amount = float(r.get("amount", {}).get("value", 0))
                raw_item = r.get("line_item", "?")
                model_key = raw_item.split(",")[0].strip() if "," in raw_item else raw_item
                model_name = _pretty_model(model_key)
                day_costs[day][model_name] = day_costs[day].get(model_name, 0) + amount
                day_totals[day] += amount

        # ── Собираем данные о токенах ──
        day_usage: dict[str, dict[str, dict]] = {}

        for bucket in usage_data.get("data", []):
            day = bucket.get("start_time_iso", "?")[:10]
            if day not in day_usage:
                day_usage[day] = {}
            for r in bucket.get("results", []):
                model_name = _pretty_model(r.get("model", "?"))
                inp = r.get("input_tokens", 0)
                cached = r.get("input_cached_tokens", 0)
                out = r.get("output_tokens", 0)
                reqs = r.get("num_model_requests", 0)
                if model_name not in day_usage[day]:
                    day_usage[day][model_name] = {"reqs": 0, "inp": 0, "cached": 0, "out": 0}
                d = day_usage[day][model_name]
                d["reqs"] += reqs
                d["inp"] += inp
                d["cached"] += cached
                d["out"] += out

        # ── Формируем красивый вывод ──
        lines = ["💰 РАСХОДЫ НА ИИ", ""]
        total_all = 0.0

        for day in sorted(day_costs.keys()):
            dt = day_totals.get(day, 0)
            total_all += dt
            lines.append(f"📅 {_pretty_date(day)} — ${dt:.2f}")

            sorted_models = sorted(day_costs[day].items(), key=lambda x: -x[1])
            for i, (model, cost) in enumerate(sorted_models):
                if cost < 0.005:
                    continue
                is_last = (i == len(sorted_models) - 1)
                prefix = "└" if is_last else "├"
                lines.append(f"  {prefix} {model}: ${cost:.2f}")

                usage = day_usage.get(day, {}).get(model, {})
                reqs = usage.get("reqs", 0)
                inp = usage.get("inp", 0)
                cached = usage.get("cached", 0)
                if reqs > 0:
                    detail_prefix = "  " if is_last else "│ "
                    avg_inp = inp // reqs
                    parts = [f"{reqs} запр."]
                    parts.append(f"~{_fmt_tokens(avg_inp)}/запрос")
                    if cached > 0 and inp > 0:
                        cache_pct = int(cached / inp * 100)
                        parts.append(f"{cache_pct}% из кеша")
                    lines.append(f"  {detail_prefix}  {', '.join(parts)}")

            lines.append("")

        # Итог + бюджет
        lines.append(f"💵 Итого: ${total_all:.2f}")
        lines.append(f"📊 Лимит: ${COST_DAILY_LIMIT:.0f}/день")

        # Остаток на сегодня
        today_str = now.strftime("%Y-%m-%d")
        today_cost = day_totals.get(today_str, 0.0)
        remaining = max(0, COST_DAILY_LIMIT - today_cost)
        lines.append(f"💳 Остаток на сегодня: ~${remaining:.2f} из ${COST_DAILY_LIMIT:.0f}")

        # Средний расход + прогноз на месяц
        n_days = max(len(day_totals), 1)
        avg_daily = total_all / n_days
        lines.append(f"📈 Средний расход: ~${avg_daily:.2f}/день")
        days_in_month = calendar.monthrange(now.year, now.month)[1]
        monthly_forecast = avg_daily * days_in_month
        lines.append(f"📅 Прогноз на месяц: ~${monthly_forecast:.0f}")

        if today_cost >= COST_DAILY_LIMIT:
            lines.append(f"🔴 Дневной лимит превышен!")

        return "\n".join(lines)
    except Exception as e:
        return f"❌ Ошибка: {e}"


async def get_today_cost() -> tuple[float, bool]:
    """Получить стоимость за текущий день. Возвращает (сумма, api_ok)."""
    if not OPENAI_ADMIN_KEY:
        return 0.0, True
    now = datetime.datetime.now(datetime.timezone.utc)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    start_ts = int(start.timestamp())
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                "https://api.openai.com/v1/organization/costs",
                params={"start_time": start_ts, "bucket_width": "1d"},
                headers={"Authorization": f"Bearer {OPENAI_ADMIN_KEY}"},
            )
        if resp.status_code != 200:
            return 0.0, False
        total = 0.0
        for bucket in resp.json().get("data", []):
            for r in bucket.get("results", []):
                total += float(r.get("amount", {}).get("value", 0))
        return total, True
    except Exception:
        return 0.0, False


# ── OpenRouter Usage ──────────────────────────────────────────


async def get_openrouter_usage() -> dict[str, float | None]:
    """Получить расходы OpenRouter. Возвращает dict с daily/monthly/total."""
    if not OPENROUTER_API_KEY:
        return {"daily": None, "monthly": None, "total": None, "ok": False}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                "https://openrouter.ai/api/v1/auth/key",
                headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}"},
            )
        if resp.status_code != 200:
            return {"daily": None, "monthly": None, "total": None, "ok": False}
        data = resp.json().get("data", {})
        return {
            "daily": data.get("usage_daily", 0),
            "monthly": data.get("usage_monthly", 0),
            "total": data.get("usage", 0),
            "ok": True,
        }
    except Exception:
        return {"daily": None, "monthly": None, "total": None, "ok": False}


async def fetch_all_costs(days: int = 2) -> str:
    """Сводка расходов по всем провайдерам."""
    now = datetime.datetime.now(datetime.timezone.utc)
    lines = ["💰 РАСХОДЫ НА ИИ", ""]

    grand_today = 0.0
    grand_monthly = 0.0

    # ── OpenAI ──
    if OPENAI_ADMIN_KEY:
        openai_text = await fetch_openai_costs(days=days)
        # Извлекаем данные из get_today_cost для суммарного подсчёта
        today_cost, api_ok = await get_today_cost()
        if api_ok:
            grand_today += today_cost
        lines.append("━━ OpenAI ━━")
        # Показываем только ключевые строки из openai_text
        for line in openai_text.split("\n"):
            if line and not line.startswith("💰 РАСХОДЫ"):
                lines.append(line)
        lines.append("")

    # ── OpenRouter ──
    if OPENROUTER_API_KEY:
        or_data = await get_openrouter_usage()
        lines.append("━━ OpenRouter ━━")
        if or_data["ok"]:
            daily = or_data["daily"] or 0
            monthly = or_data["monthly"] or 0
            grand_today += daily
            grand_monthly += monthly
            lines.append(f"📅 Сегодня: ${daily:.2f}")
            lines.append(f"📅 За месяц: ${monthly:.2f}")
        else:
            lines.append("⚠️ API недоступен")
        lines.append("")

    # ── Недоступные провайдеры ──
    lines.append(f"ℹ️ Без API: Google, Deepgram, Perplexity")
    lines.append("")

    # ── Итог ──
    lines.append("━━ ИТОГО ━━")
    lines.append(f"💵 Сегодня: ~${grand_today:.2f}")

    return "\n".join(lines)


# ── Команды ───────────────────────────────────────────────────


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    global _prev_kb_msg_id
    if not authorized(update):
        return
    msg = await update.message.reply_text(
        "Панель управления OpenClaw\nВыбери действие:",
        reply_markup=keyboard(),
    )
    _prev_kb_msg_id = msg.message_id


async def cmd_report(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Полный технический отчёт (для администратора, не для скриншотов)."""
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
        _fmt_backup_config(),
        _fmt_backup_mongo(),
    )

    text = (
        "📊 ОТЧЁТ СЕРВЕРА\n\n"
        f"🐳 Контейнеры:\n{results[0]}\n\n"
        f"💾 Диск: {results[1].strip()}\n"
        f"🧠 RAM: {results[2]}\n"
        f"⚡ Нагрузка:{results[3]}\n\n"
        f"🏥 OpenClaw Health: HTTP {results[4]}\n\n"
        f"🛡 Fail2ban:\n{results[5]}\n\n"
        f"📦 Бэкап конфигов: {results[6]}\n"
        f"📦 Бэкап MongoDB: {results[7]}"
    )
    await update.message.reply_text(text[:4096])


# ── Обработчик кнопок ─────────────────────────────────────────


async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    global _prev_data_msg_id, _prev_kb_msg_id
    q = update.callback_query
    if q.from_user.id != CHAT_ID:
        await q.answer("Нет доступа")
        return
    await q.answer()

    action = q.data
    result = ""

    # ── Перезапуск с подтверждением ──
    if action == "restart_ask":
        confirm_kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Да, перезапустить", callback_data="restart_yes"),
                InlineKeyboardButton("❌ Отмена", callback_data="restart_no"),
            ]
        ])
        await q.edit_message_text("Перезапустить OpenClaw?", reply_markup=confirm_kb)
        return

    if action == "restart_yes":
        await q.edit_message_text("🔄 Перезапускаю OpenClaw...")
        output = await sh(f"docker restart {PRIMARY}")
        result = f"🔄 ПЕРЕЗАПУЩЕН\n{output}"

    elif action == "restart_no":
        await q.edit_message_text("Отменено.")
        # Удалить сообщение отмены и показать меню
        if _prev_data_msg_id:
            await _delete_msg(ctx.bot, _prev_data_msg_id)
        _prev_data_msg_id = q.message.message_id
        kb_msg = await ctx.bot.send_message(
            chat_id=CHAT_ID, text="Выбери действие:", reply_markup=keyboard(),
        )
        _prev_kb_msg_id = kb_msg.message_id
        return

    elif action == "dashboard":
        await q.edit_message_text("📊 Загружаю дашборд...")
        result = await build_dashboard()

    elif action == "costs":
        await q.edit_message_text("💰 Загружаю расходы...")
        result = await fetch_all_costs(days=2)

    else:
        result = "Неизвестная команда"

    # ── Очистка чата: удалить предыдущий ответ ──
    if _prev_data_msg_id:
        await _delete_msg(ctx.bot, _prev_data_msg_id)

    # Текущее сообщение (от кнопки) обновляем данными
    await q.edit_message_text(result[:4096])
    _prev_data_msg_id = q.message.message_id

    # Новое сообщение с клавиатурой
    kb_msg = await ctx.bot.send_message(
        chat_id=CHAT_ID, text="Выбери действие:", reply_markup=keyboard(),
    )
    _prev_kb_msg_id = kb_msg.message_id


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
                await send_alert(
                    ctx,
                    f"🔴 Контейнер {name} УПАЛ\nСтатус: {status.strip()}",
                    delete_after=7200,  # 2 часа — критический алерт
                )
        elif is_up and not was_up:
            dedup.reset(f"container:{name}")
            await send_alert(
                ctx,
                f"🟢 Контейнер {name} снова работает",
                delete_after=1800,  # 30 мин — восстановление
            )
        _container_was_up[name] = is_up


async def job_health(ctx: CallbackContext):
    global _health_was_ok, _health_fail_count
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(HEALTH_URL, timeout=10)
            ok = resp.status_code == 200
    except Exception:
        ok = False

    if not ok:
        _health_fail_count += 1
        if _health_was_ok and _health_fail_count >= HEALTH_FAIL_THRESHOLD:
            if dedup.should_alert("health:openclaw"):
                await send_alert(
                    ctx,
                    f"🔴 OpenClaw healthcheck НЕ ОТВЕЧАЕТ ({_health_fail_count} проверок подряд)",
                    delete_after=7200,
                )
            _health_was_ok = False
    else:
        _health_fail_count = 0
        if not _health_was_ok:
            dedup.reset("health:openclaw")
            await send_alert(
                ctx,
                "🟢 OpenClaw healthcheck восстановлен",
                delete_after=1800,
            )
            _health_was_ok = True


async def job_fail2ban(ctx: CallbackContext):
    global _f2b_offset
    try:
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
                    await send_alert(ctx, f"🛡 Fail2ban: забанен {ip}")
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
            await send_alert(
                ctx,
                f"🔴 Диск заполнен на {pct}%\nСвободно: {avail.strip()}",
                delete_after=7200,
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
                await send_alert(
                    ctx,
                    f"🔴 RAM: {pct}% ({used // 1024} / {total // 1024} МБ)",
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
                await send_alert(
                    ctx,
                    f"🔴 Нагрузка: {load5:.1f} (порог: {thresh:.0f})",
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
            await send_alert(ctx, f"🔴 Бэкап конфигов: ошибка в логе")
    elif today not in output:
        if dedup.should_alert("backup:config"):
            await send_alert(ctx, f"⚠️ Бэкап конфигов: нет записи за сегодня")


async def job_backup_mongo(ctx: CallbackContext):
    today = datetime.date.today().isoformat()
    output = await sh(f"tail -10 {MONGO_BACKUP_LOG} 2>/dev/null")
    if "FAILED" in output.upper() and today in output:
        if dedup.should_alert("backup:mongo"):
            await send_alert(ctx, f"🔴 Бэкап MongoDB: ПРОВАЛИЛСЯ")


async def job_cost_alert(ctx: CallbackContext):
    """Проверка расходов — ступенчатые пороги + детекция API (с ретраями)."""
    global _openai_api_was_ok, _openai_fail_count
    if not OPENAI_ADMIN_KEY:
        return

    cost, api_ok = await get_today_cost()

    # ── Детекция состояния API (с ретраями) ──
    if not api_ok:
        _openai_fail_count += 1
        if _openai_api_was_ok and _openai_fail_count >= COST_FAIL_THRESHOLD:
            if dedup.should_alert("openai:api_down"):
                await send_alert(
                    ctx,
                    "🔴 OpenAI API недоступен — проверьте квоту или баланс",
                    delete_after=7200,
                )
            _openai_api_was_ok = False
    else:
        _openai_fail_count = 0
        if not _openai_api_was_ok:
            dedup.reset("openai:api_down")
            for pct, _, _ in COST_WARN_THRESHOLDS:
                dedup.reset(f"cost:daily:{int(pct * 100)}")
            await send_alert(
                ctx,
                "🟢 OpenAI API восстановлен",
                delete_after=3600,
            )
            _openai_api_was_ok = True

    # ── Ступенчатые пороги расходов ──
    if api_ok:
        highest_hit = None
        for pct, emoji, label in COST_WARN_THRESHOLDS:
            threshold = COST_DAILY_LIMIT * pct
            if cost >= threshold:
                highest_hit = (pct, emoji, label)
            else:
                dedup.reset(f"cost:daily:{int(pct * 100)}")

        if highest_hit:
            pct, emoji, label = highest_hit
            key = f"cost:daily:{int(pct * 100)}"
            if dedup.should_alert(key):
                await send_alert(
                    ctx,
                    f"{emoji} OpenAI: ${cost:.2f} — {label} лимита (${COST_DAILY_LIMIT:.0f}/день)",
                )


# ── Инициализация ─────────────────────────────────────────────


async def post_init(app: Application) -> None:
    await app.bot.set_my_commands([
        ("start", "Панель управления"),
        ("report", "Полный техотчёт"),
    ])

    jq = app.job_queue
    jq.run_repeating(job_containers, interval=60, first=10)
    jq.run_repeating(job_health, interval=120, first=15)
    jq.run_repeating(job_fail2ban, interval=30, first=5)
    jq.run_repeating(job_disk, interval=300, first=20)
    jq.run_repeating(job_system, interval=300, first=25)

    # Расходы OpenAI — каждые 5 мин (по умолчанию)
    if OPENAI_ADMIN_KEY:
        jq.run_repeating(job_cost_alert, interval=COST_CHECK_INTERVAL, first=60)
        logging.info(
            f"Мониторинг расходов: каждые {COST_CHECK_INTERVAL}с, "
            f"лимит ${COST_DAILY_LIMIT}/день"
        )

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

    logging.info("Бот v3.0 запущен, мониторинг активен")


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
