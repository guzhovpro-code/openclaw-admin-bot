# OpenClaw Admin Bot

Telegram-бот для мониторинга и аварийного управления сервером [OpenClaw](https://github.com/nicepkg/openclaw).

Позволяет удалённо управлять контейнером OpenClaw Gateway, следить за состоянием сервера и получать автоматические уведомления при сбоях — прямо в Telegram.

## Возможности

### Панель управления (кнопки в Telegram)
- **СТОП / СТАРТ / РЕСТАРТ** — управление контейнером OpenClaw Gateway
- **Статус** — состояние контейнера
- **Здоровье** — HTTP healthcheck эндпоинта `/healthz`
- **Логи** — последние 20 строк логов контейнера
- **Система** — диск, RAM, нагрузка
- **Fail2ban** — текущий статус SSH-защиты (если установлен)

### Команды
- `/start` — открыть панель управления
- `/report` — полный отчёт о состоянии сервера

### Автоматический мониторинг
Бот проактивно отправляет уведомления **только при проблемах**:

| Проверка | Интервал | Алерт |
|---|---|---|
| Контейнер OpenClaw | 60 сек | 🔴 при падении, 🟢 при восстановлении |
| Healthcheck `/healthz` | 120 сек | 🔴 если HTTP ≠ 200 |
| Fail2ban баны | 30 сек | 🛡 при бане нового IP |
| Диск | 5 мин | 🔴 если занято ≥ 80% |
| RAM / нагрузка | 5 мин | 🔴 если RAM ≥ 90% или load высокий |

Антиспам: повторный алерт одного типа не чаще чем раз в 30 минут.

## Безопасность

- Бот **не открывает никаких портов** на сервере
- Работает через long polling — делает исходящие HTTPS-запросы к Telegram API
- Доступ ограничен одним Telegram ID (жёсткая проверка в коде)
- Секреты хранятся в `.env` файле (не попадают в git)

## Установка

### Требования
- VPS с Ubuntu 22.04+ / Debian 12+
- Работающий контейнер OpenClaw Gateway
- Python 3.10+

### Способ 1: Через Claude Code (рекомендуется)

Откройте Claude Code и отправьте:

> Установи Telegram-бота для мониторинга моего OpenClaw-сервера по инструкциям из https://github.com/guzhovpro-code/openclaw-admin-bot
>
> Данные сервера:
> - IP: _(ваш IP)_
> - Логин: deploy
> - Пароль или SSH-ключ: _(...) _

Claude Code проведёт через создание бота в Telegram и установку. Вам нужно будет только создать бота через @BotFather и скинуть токен.

### Способ 2: Вручную

**Шаг 1.** Создай Telegram-бота: открой [@BotFather](https://t.me/BotFather), отправь `/newbot`, скопируй токен.

**Шаг 2.** Узнай свой Telegram ID: отправь `/start` боту [@userinfobot](https://t.me/userinfobot).

**Шаг 3.** Установи на сервер:
```bash
git clone https://github.com/guzhovpro-code/openclaw-admin-bot.git
cd openclaw-admin-bot
bash install.sh
```

**Шаг 4.** Отправь `/start` своему боту в Telegram.

## Настройка

Все параметры задаются в `.env` файле:

```bash
# Обязательные
ADMIN_BOT_TOKEN=токен
ALLOWED_TELEGRAM_ID=числовой-id

# Опциональные (значения по умолчанию)
OPENCLAW_CONTAINER=repo-openclaw-gateway-1   # имя Docker-контейнера
OPENCLAW_HEALTH_URL=http://127.0.0.1:18789/healthz  # URL healthcheck
DISK_THRESHOLD=80        # порог диска (%)
RAM_THRESHOLD=90         # порог RAM (%)
LOAD_FACTOR=4.0          # порог нагрузки (load > ядра × factor)
ALERT_COOLDOWN=1800      # антиспам алертов (секунды)
FAIL2BAN_LOG=/var/log/fail2ban.log  # путь к логу fail2ban
```

## Лицензия
MIT
