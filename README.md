# OpenClaw Admin Bot

Telegram-бот для мониторинга и аварийного управления сервером OpenClaw.

## Возможности

### Панель управления (кнопки в Telegram)
- **СТОП / СТАРТ / РЕСТАРТ** — управление контейнером OpenClaw Gateway
- **Статус** — состояние всех Docker-контейнеров
- **Здоровье** — HTTP healthcheck эндпоинта `/healthz`
- **Логи** — последние 20 строк логов контейнера
- **Система** — диск, RAM, нагрузка
- **Fail2ban** — текущий статус SSH-защиты
- **Бэкапы** — состояние резервных копий

### Команды
- `/start` — открыть панель управления
- `/report` — полный отчёт о состоянии сервера

### Автоматический мониторинг (фоновые алерты)
Бот проактивно отправляет уведомления при проблемах:

| Проверка | Интервал | Алерт |
|---|---|---|
| Контейнеры Docker | 60 сек | 🔴 при падении, 🟢 при восстановлении |
| OpenClaw healthcheck | 120 сек | 🔴 если HTTP != 200 |
| Fail2ban баны | 30 сек | 🛡 при новом бане IP |
| Диск | 5 мин | 🔴 если занято >= 80% |
| RAM / нагрузка | 5 мин | 🔴 если RAM >= 90% или load высокий |
| Бэкап конфигов | ежедневно 07:10 UTC | ⚠️ если не выполнен |
| Бэкап MongoDB | ежедневно 03:10 UTC | 🔴 если провалился |

**Антиспам:** повторный алерт одного типа не чаще чем раз в 30 минут.

## Установка

### Требования
- Ubuntu 22.04+ / Debian 12+
- Docker
- Python 3.10+
- Telegram Bot (создать через [@BotFather](https://t.me/BotFather))
- Пользователь с sudo NOPASSWD и доступом к Docker

### Быстрая установка
```bash
git clone https://github.com/guzhovpro-code/openclaw-admin-bot.git
cd openclaw-admin-bot
bash install.sh
```

### Ручная установка

1. Установи зависимости:
```bash
sudo pip3 install "python-telegram-bot[job-queue]" --break-system-packages
```

2. Скопируй файлы:
```bash
mkdir -p /home/deploy/admin-bot
cp bot.py /home/deploy/admin-bot/
```

3. Создай `.env`:
```bash
cat > /home/deploy/admin-bot/.env << EOF
ADMIN_BOT_TOKEN=токен-от-BotFather
ALLOWED_TELEGRAM_ID=твой-telegram-id
EOF
chmod 600 /home/deploy/admin-bot/.env
```

4. Создай systemd-сервис:
```bash
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

5. Проверь:
```bash
sudo systemctl status openclaw-admin-bot
```

## Настройка

### Контейнеры для мониторинга
В файле `bot.py` измени список `CONTAINERS`:
```python
CONTAINERS = [
    "repo-openclaw-gateway-1",
    "repo-openclaw-cli-1",
    "root-traefik-1",
    "root-n8n-1",
]
```

### Игнорируемые контейнеры
Контейнеры, которые должны быть остановлены (не вызывают алерт):
```python
EXPECTED_DOWN = {"repo-openclaw-cli-1"}
```

### Пороги алертов
```python
DISK_THRESHOLD = 80   # процент заполненности диска
RAM_THRESHOLD = 90    # процент использования RAM
LOAD_FACTOR = 4.0     # load > ядра * factor
ALERT_COOLDOWN = 1800 # антиспам, секунды
```

### Пути к логам
```python
FAIL2BAN_LOG = "/var/log/fail2ban.log"
CONFIG_BACKUP_LOG = "/var/log/openclaw-backup.log"
MONGO_BACKUP_LOG = "/var/log/mongo_backup.log"
```

## Как узнать свой Telegram ID
Отправь `/start` боту [@userinfobot](https://t.me/userinfobot) — он покажет твой ID.

## Лицензия
MIT
