# OpenClaw Admin Bot v2.1

Telegram-бот для мониторинга и управления сервером [OpenClaw](https://github.com/nicepkg/openclaw).

Чистый дашборд, понятный без технического опыта. Скриншот из бота = контент для подписчиков.

## Возможности

### Панель управления (кнопки в Telegram)
- **📊 Дашборд** — единая сводка: статус OpenClaw, аптайм, API, расходы, диск, RAM, fail2ban
- **💰 Расходы** — детализация расходов OpenAI по дням и моделям (нужен Admin API ключ)
- **🔄 Перезапуск** — перезапуск контейнера с подтверждением

### Команды
- `/start` — открыть панель управления
- `/report` — полный технический отчёт (для администратора)

### Автоматический мониторинг

| Проверка | Интервал | Алерт |
|---|---|---|
| Контейнер OpenClaw | 60 сек | 🔴 при падении, 🟢 при восстановлении |
| Healthcheck `/healthz` | 120 сек | 🔴 если HTTP ≠ 200 |
| Fail2ban баны | 30 сек | 🛡 при бане нового IP |
| Диск | 5 мин | 🔴 если занято ≥ 80% |
| RAM / нагрузка | 5 мин | 🔴 если RAM ≥ 90% |
| Расходы OpenAI | 5 мин | 🟡 50%, 🟡 75%, 🟠 90%, 🔴 100% лимита |
| API OpenAI | 5 мин | 🔴 недоступен, 🟢 восстановлен |

### Чистый чат
- Предыдущие ответы автоматически удаляются — в чате максимум 2 сообщения
- Фоновые алерты автоудаляются через 1 час
- Критичные алерты — через 2 часа
- Уведомления о восстановлении — через 30 мин

## Пример дашборда

```
📊 ДАШБОРД
━━━━━━━━━━━━━━━━━
🤖 OpenClaw: ✅ работает
   ⏱ аптайм: 3д 14ч 22мин

🌐 API OpenAI: ✅ доступен
💰 Расходы: $4.12 из $10 (осталось $5.88)

💾 Диск: 42% занято (12 ГБ свободно)
🧠 Память: 67% (2.1 / 3.2 ГБ)
🛡 Fail2ban: 3 IP забанено
```

## Безопасность

- Бот **не открывает никаких портов** на сервере
- Работает через long polling — исходящие HTTPS-запросы к Telegram API
- Доступ ограничен одним Telegram ID
- Секреты в `.env` файле (не попадают в git)

## Установка

### Требования
- VPS с Ubuntu 22.04+ / Debian 12+
- Работающий контейнер OpenClaw Gateway
- Python 3.10+

### Через Claude Code (рекомендуется)

> Установи Telegram-бота для мониторинга моего OpenClaw-сервера по инструкциям из https://github.com/guzhovpro-code/openclaw-admin-bot

### Вручную

```bash
git clone https://github.com/guzhovpro-code/openclaw-admin-bot.git
cd openclaw-admin-bot
bash install.sh
```

## Настройка

Все параметры в `.env` файле:

```bash
# Обязательные
ADMIN_BOT_TOKEN=токен
ALLOWED_TELEGRAM_ID=числовой-id

# Опциональные
OPENCLAW_CONTAINER=repo-openclaw-gateway-1
OPENCLAW_HEALTH_URL=http://127.0.0.1:18789/healthz
DISK_THRESHOLD=80
RAM_THRESHOLD=90
LOAD_FACTOR=4.0
ALERT_COOLDOWN=1800
FAIL2BAN_LOG=/var/log/fail2ban.log

# Мониторинг расходов OpenAI
OPENAI_ADMIN_KEY=sk-admin-...   # Admin API ключ
COST_DAILY_LIMIT=10             # лимит $/день
COST_CHECK_INTERVAL=300         # интервал проверки (сек)
ALERT_AUTO_DELETE=3600          # автоудаление алертов (сек)
```

### OpenAI Admin API ключ

Для 💰 Расходы и мониторинга API нужен **Admin API ключ**:

1. Откройте https://platform.openai.com/settings/organization/admin-keys
2. Нажмите «Create new admin key»
3. Скопируйте ключ (`sk-admin-...`)
4. Добавьте в `.env` как `OPENAI_ADMIN_KEY`

Без ключа — бот работает без расходов и мониторинга API.

## Лицензия
MIT
