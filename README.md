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
- Linux (Ubuntu 22.04+ / Debian 12+)
- Docker с запущенным OpenClaw Gateway
- Python 3.10+
- Пользователь с доступом к Docker

### Шаг 1: Создай Telegram-бота
1. Открой [@BotFather](https://t.me/BotFather) в Telegram
2. Отправь `/newbot`, задай имя
3. Скопируй токен (формат: `123456:ABC-...`)

### Шаг 2: Узнай свой Telegram ID
Отправь `/start` боту [@userinfobot](https://t.me/userinfobot) — он покажет твой числовой ID.

### Шаг 3: Установи бота на сервер

**Автоматически:**
```bash
git clone https://github.com/guzhovpro-code/openclaw-admin-bot.git
cd openclaw-admin-bot
bash install.sh
```

**Вручную:**
```bash
# Зависимости
sudo pip3 install "python-telegram-bot[job-queue]" --break-system-packages

# Файлы
mkdir -p /home/deploy/admin-bot
cp bot.py /home/deploy/admin-bot/

# Конфиг
cat > /home/deploy/admin-bot/.env << EOF
ADMIN_BOT_TOKEN=токен-от-BotFather
ALLOWED_TELEGRAM_ID=твой-telegram-id
EOF
chmod 600 /home/deploy/admin-bot/.env

# Systemd-сервис
sudo tee /etc/systemd/system/openclaw-admin-bot.service << 'EOF'
[Unit]
Description=OpenClaw Admin Telegram Bot
After=network.target docker.service

[Service]
Type=simple
User=deploy
WorkingDirectory=/home/deploy/admin-bot
EnvironmentFile=/home/deploy/admin-bot/.env
Environment=PYTHONUNBUFFERED=1
ExecStart=/usr/bin/python3 /home/deploy/admin-bot/bot.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable openclaw-admin-bot
sudo systemctl start openclaw-admin-bot
```

### Шаг 4: Проверь
```bash
sudo systemctl status openclaw-admin-bot
```
Отправь `/start` своему боту в Telegram.

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
