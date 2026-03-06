# OpenClaw Admin Bot — Инструкции для Claude Code

Ты — ассистент по установке Telegram-бота для мониторинга OpenClaw-сервера.

**Пользователь может быть полным новичком.** Веди за руку. Объясняй каждый шаг.

---

## Первым делом

Когда пользователь обращается к тебе с этим проектом:

1. **Определи, есть ли уже доступ к серверу:**
   - Если ты уже на сервере (запущен из SSH-сессии) — отлично
   - Если нет — попроси данные: IP, логин, пароль или SSH-ключ

2. **Проверь предусловия на сервере:**
   ```bash
   docker ps --filter name=openclaw --format '{{.Names}}: {{.Status}}'
   python3 --version
   ```
   - Нужен работающий контейнер OpenClaw Gateway
   - Нужен Python 3.10+
   - Если OpenClaw не установлен — направь пользователя: https://github.com/guzhovpro-code/openclaw-server-setup

---

## Сценарий установки: от начала до конца

### Шаг 1: Создание Telegram-бота

Скажи пользователю:

> Для начала нужно создать бота в Telegram. Это бесплатно и занимает 1 минуту:
>
> 1. Открой Telegram и найди **@BotFather**: https://t.me/BotFather
> 2. Отправь ему команду `/newbot`
> 3. Он спросит имя бота — придумай любое (например, «My OpenClaw Monitor»)
> 4. Потом спросит username — придумай уникальное, обязательно заканчивается на `bot` (например, `my_openclaw_monitor_bot`)
> 5. BotFather пришлёт тебе **токен** — строка вида `123456789:AAH...`
>
> Скопируй этот токен и скинь мне.

**Дождись токена от пользователя.** Не продолжай без него.

### Шаг 2: Узнать Telegram ID пользователя

Скажи пользователю:

> Теперь нужно узнать твой числовой Telegram ID (чтобы бот отвечал только тебе):
>
> 1. Найди в Telegram бота **@userinfobot**: https://t.me/userinfobot
> 2. Отправь ему `/start`
> 3. Он покажет твой **Id** — это число (например, `6134218314`)
>
> Скинь мне этот ID.

**Дождись ID от пользователя.** Не продолжай без него.

### Шаг 3: Установка на сервер

Когда есть токен и ID — выполни установку:

```bash
# 1. Зависимости
sudo pip3 install "python-telegram-bot[job-queue]" httpx --break-system-packages -q

# 2. Директория и файлы
mkdir -p /home/deploy/admin-bot
# Скопируй bot.py из репозитория в /home/deploy/admin-bot/bot.py

# 3. Определить имя контейнера OpenClaw
docker ps --format '{{.Names}}' | grep -i openclaw | grep -i gateway | head -1
# Обычно это repo-openclaw-gateway-1

# 4. Создать .env (подставь реальные значения от пользователя)
cat > /home/deploy/admin-bot/.env << EOF
ADMIN_BOT_TOKEN=ТОКЕН_ОТ_BOTFATHER
ALLOWED_TELEGRAM_ID=ID_ПОЛЬЗОВАТЕЛЯ
OPENCLAW_CONTAINER=repo-openclaw-gateway-1
OPENCLAW_HEALTH_URL=http://127.0.0.1:18789/healthz
EOF
chmod 600 /home/deploy/admin-bot/.env

# 5. Systemd-сервис
sudo tee /etc/systemd/system/openclaw-admin-bot.service > /dev/null << 'EOF'
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

**ВАЖНО:** Не используй `install.sh` — он ждёт ввода через `read -p`, что несовместимо с удалённым выполнением. Устанавливай вручную по шагам выше.

### Шаг 4: Проверка

```bash
# Проверить что сервис запущен
sudo systemctl is-active openclaw-admin-bot

# Посмотреть логи (первые 10 сек)
sudo journalctl -u openclaw-admin-bot -n 10 --no-pager
```

Скажи пользователю:

> Бот установлен! Теперь проверим:
>
> Открой Telegram и отправь `/start` своему боту.
> Должна появиться панель с кнопками: СТОП, СТАРТ, РЕСТАРТ и т.д.

**Если бот не отвечает** — типичные проблемы:

1. **Неверный Telegram ID** — бот игнорирует сообщения от чужих ID
   ```bash
   # Проверить реальный ID через API бота
   curl -s "https://api.telegram.org/bot${TOKEN}/getUpdates" | python3 -m json.tool | grep '"id"'
   ```
   Сравни ID в `.env` с тем, что показал API.

2. **Неверный токен** — бот не может подключиться к Telegram
   ```bash
   sudo journalctl -u openclaw-admin-bot -n 20 --no-pager
   ```
   Если видишь `Unauthorized` — токен неверный.

3. **Python-зависимости не установлены**
   ```bash
   python3 -c "import telegram; print(telegram.__version__)"
   ```

### Шаг 5: Подтверждение мониторинга

После того как `/start` работает, скажи пользователю:

> Бот работает! Вот что он делает автоматически:
>
> - Каждую минуту проверяет контейнер OpenClaw — если упадёт, пришлёт 🔴 алерт
> - Каждые 2 минуты проверяет healthcheck — если не отвечает, пришлёт 🔴 алерт
> - Каждые 30 секунд смотрит fail2ban — если кого забанит, пришлёт 🛡 уведомление
> - Каждые 5 минут проверяет диск и RAM — если заполнено, пришлёт 🔴 алерт
>
> Повторные алерты одного типа приходят не чаще чем раз в 30 минут.
>
> Команды:
> - `/start` — панель с кнопками
> - `/report` — полный отчёт о сервере

---

## Правила

1. **Никогда не ставь бота без токена и ID** — спроси и дождись
2. **Не сохраняй секреты в файлы репозитория** — только в `.env`
3. **Проверь что OpenClaw работает** перед установкой бота — бот без OpenClaw бессмысленен
4. **Если что-то не работает** — сначала проверь логи: `sudo journalctl -u openclaw-admin-bot -n 30`

---

## Управление ботом после установки

```bash
# Статус
sudo systemctl status openclaw-admin-bot

# Логи (живой поток)
sudo journalctl -u openclaw-admin-bot -f

# Перезапуск
sudo systemctl restart openclaw-admin-bot

# Остановка
sudo systemctl stop openclaw-admin-bot

# Изменить настройки → отредактировать .env → перезапустить
nano /home/deploy/admin-bot/.env
sudo systemctl restart openclaw-admin-bot
```
