#!/usr/bin/env bash
# ============================================================
# OpenClaw Admin Bot — автоматическая установка
# Использование: bash install.sh
# ============================================================
set -euo pipefail

BOT_DIR="/home/deploy/admin-bot"
SERVICE="openclaw-admin-bot"

echo "=== OpenClaw Admin Bot — Установка ==="
echo ""

# 1. Зависимости
echo "[1/5] Установка зависимостей..."
sudo pip3 install "python-telegram-bot[job-queue]" httpx --break-system-packages -q

# 2. Директория и файлы
echo "[2/5] Копирование файлов..."
mkdir -p "$BOT_DIR"
cp bot.py "$BOT_DIR/bot.py"
chmod +x "$BOT_DIR/bot.py"

# 3. Env-файл
if [ ! -f "$BOT_DIR/.env" ]; then
    echo "[3/5] Настройка .env..."
    read -p "Telegram Bot Token: " BOT_TOKEN
    read -p "Твой Telegram User ID: " TG_ID
    cat > "$BOT_DIR/.env" << EOF
ADMIN_BOT_TOKEN=${BOT_TOKEN}
ALLOWED_TELEGRAM_ID=${TG_ID}
EOF
    chmod 600 "$BOT_DIR/.env"
    echo "  .env создан"
else
    echo "[3/5] .env уже существует, пропускаю"
fi

# 4. Systemd-сервис
echo "[4/5] Настройка systemd..."
sudo tee /etc/systemd/system/${SERVICE}.service > /dev/null << 'EOF'
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
sudo systemctl enable ${SERVICE}
sudo systemctl restart ${SERVICE}

# 5. Проверка
echo "[5/5] Проверка..."
sleep 3
if sudo systemctl is-active --quiet ${SERVICE}; then
    echo ""
    echo "✅ Бот запущен и работает!"
    echo "   Отправь /start боту в Telegram"
else
    echo ""
    echo "❌ Ошибка запуска. Проверь логи:"
    echo "   sudo journalctl -u ${SERVICE} -n 20"
fi
