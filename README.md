# Telegram GitHub Manager Bot

## Deploy ke Railway
1. Push folder ini ke repo GitHub (private disarankan).
2. Railway → **New Project → Deploy from GitHub repo**.
3. Tab **Variables**, isi:
   - `BOT_TOKEN` – dari @BotFather
   - `GITHUB_TOKEN` – classic PAT dengan scope `repo`
   - `ALLOWED_USER_IDS` – ID Telegram kamu (cek via @userinfobot)
4. (Opsional) Tambah **Volume** di `/data` + variabel `DATA_DIR=/data` agar repo/branch aktif tidak hilang saat redeploy.
5. Deploy. Buka bot, kirim `/start`.

## Catatan
- Bot jalan sebagai worker (long polling), tidak perlu domain/port.
- Batas Telegram Bot API: terima file ≤ 20 MB, kirim file ≤ 50 MB.
- `/dl` dengan branch ber-slash: `/dl owner/repo feature/x`.
- Jalankan lokal: `pip install -r requirements.txt && export $(cat .env | xargs) && python bot.py`

## Jalankan di Termux (hapus versi lama, pasang versi baru)
```
pkill -f start.sh; pkill -f "python bot.py"; tmux kill-session -t bot 2>/dev/null
cp ~/telegram-github-bot/.env ~/env.bak
rm -rf ~/telegram-github-bot ~/telegram-github-bot.zip
cp ~/storage/downloads/telegram-github-bot.zip ~ && cd ~
unzip telegram-github-bot.zip
cp ~/env.bak ~/telegram-github-bot/.env
cd telegram-github-bot && pip install -r requirements.txt
tmux new -s bot
bash start.sh
```
