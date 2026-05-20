from flask import Flask, request
import telebot
import sqlite3
import os
import threading
import time

# ---------------- CONFIG ----------------
TOKEN = "8554161920:AAF1HJmmNo1aVEwR3DYWNvJH-AOV52zKDgg"
BASE_URL = "https://bot-cyks.onrender.com"

bot = telebot.TeleBot(TOKEN)
app = Flask(__name__)

# ---------------- DATABASE ----------------
conn = sqlite3.connect("bot.db", check_same_thread=False)
cur = conn.cursor()

cur.execute("""
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    balance INTEGER DEFAULT 0
)
""")
conn.commit()

# ---------------- FUNCTIONS ----------------
def get_balance(user_id):
    cur.execute(
        "SELECT balance FROM users WHERE user_id=?",
        (user_id,)
    )

    row = cur.fetchone()

    if not row:
        cur.execute(
            "INSERT INTO users (user_id, balance) VALUES (?, ?)",
            (user_id, 0)
        )

        conn.commit()
        return 0

    return row[0]

def add_balance(user_id, amount):
    get_balance(user_id)

    cur.execute(
        "UPDATE users SET balance = balance + ? WHERE user_id=?",
        (amount, user_id)
    )

    conn.commit()

def remove_balance(user_id, amount):
    get_balance(user_id)

    cur.execute(
        "UPDATE users SET balance = balance - ? WHERE user_id=?",
        (amount, user_id)
    )

    conn.commit()

# ---------------- START COMMAND ----------------
@bot.message_handler(commands=['start'])
def start(message):
    get_balance(message.chat.id)

    text = (
        "👋 Добро пожаловать\n\n"
        "🎰 Команды:\n"
        "/balance — баланс\n"
        "/slot — слот"
    )

    bot.send_message(message.chat.id, text)

# ---------------- BALANCE ----------------
@bot.message_handler(commands=['balance'])
def balance(message):
    bal = get_balance(message.chat.id)

    bot.send_message(
        message.chat.id,
        f"💰 Баланс: {bal} ⭐"
    )

# ---------------- SLOT ----------------
@bot.message_handler(commands=['slot'])
def slot(message):
    uid = message.chat.id

    result = bot.send_dice(
        message.chat.id,
        emoji="🎰"
    ).dice.value

    if result == 64:
        add_balance(uid, 50)

        bot.send_message(
            uid,
            "🎉 JACKPOT\n+50 ⭐"
        )

    elif result >= 40:
        add_balance(uid, 10)

        bot.send_message(
            uid,
            "✅ Победа\n+10 ⭐"
        )

    else:
        bot.send_message(
            uid,
            "❌ Проигрыш"
        )

# ---------------- WEBHOOK ----------------
@app.route(f"/{TOKEN}", methods=["POST"])
def webhook():
    json_str = request.get_data().decode("utf-8")

    update = telebot.types.Update.de_json(json_str)

    bot.process_new_updates([update])

    return "OK", 200

# ---------------- MAIN PAGE ----------------
@app.route("/")
def home():
    return "BOT WORKING", 200

# ---------------- HEALTH CHECK ----------------
@app.route("/health")
def health():
    return "OK", 200

# ---------------- WEBHOOK SETUP ----------------
def setup_webhook():

    time.sleep(5)

    try:
        bot.remove_webhook()

        time.sleep(1)

        webhook_url = f"{BASE_URL}/{TOKEN}"

        bot.set_webhook(url=webhook_url)

        print("Webhook установлен:")
        print(webhook_url)

    except Exception as e:
        print("Ошибка webhook:")
        print(e)

# ---------------- START ----------------
if __name__ == "__main__":

    threading.Thread(
        target=setup_webhook
    ).start()

    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 5000))
    )