#!/data/data/com.termux/files/usr/bin/bash
# Jalankan bot di Termux (atau Linux biasa). Butuh file .env di folder ini.
cd "$(dirname "$0")"
[ -f .env ] || { echo "File .env belum ada. Salin dari .env.example"; exit 1; }
set -a; . ./.env; set +a
command -v termux-wake-lock >/dev/null && termux-wake-lock
while true; do
  python bot.py
  echo "Bot berhenti, restart dalam 5 detik... (Ctrl+C untuk keluar)"
  sleep 5
done
