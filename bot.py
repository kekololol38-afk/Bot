from flask import Flask, request
import telebot
import sqlite3
import os

TOKEN = "8554161920:AAF1HJmmNo1aVEwR3DYWNvJH-AOV52zKDgg"
ADMINS = [7411827400]
PAYMENT_USERNAME = "@moonlllu"

bot = telebot.TeleBot(TOKEN)
app = Flask(__name__)

# ------------------ DATABASE ------------------
conn = sqlite3.connect("bot.db", check_same_thread=False)
cur = conn.cursor()

cur.execute("""
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    balance INTEGER DEFAULT 0
)
""")
conn.commit()

def get_balance(user_id):
    cur.execute("SELECT balance FROM users WHERE user_id=?", (user_id,))
    row = cur.fetchone()
    if not row:
        cur.execute("INSERT INTO users (user_id, balance) VALUES (?, ?)", (user_id, 0))
        conn.commit()
        return 0
    return row[0]

def add_balance(user_id, amount):
    get_balance(user_id)
    cur.execute("UPDATE users SET balance = balance + ? WHERE user_id=?", (amount, user_id))
    conn.commit()

def remove_balance(user_id, amount):
    cur.execute("UPDATE users SET balance = balance - ? WHERE user_id=?", (amount, user_id))
    conn.commit()

# ------------------ START ------------------
@bot.message_handler(commands=['start'])
def start(message):
    get_balance(message.chat.id)
    bot.send_message(message.chat.id, "👋 Бот работает через WEBHOOK")

# ------------------ BALANCE ------------------
@bot.message_handler(commands=['balance'])
def balance(message):
    bal = get_balance(message.chat.id)
    bot.send_message(message.chat.id, f"💰 Баланс: {bal} ⭐")

# ------------------ SLOT ------------------
@bot.message_handler(commands=['slot'])
def slot(message):
    uid = message.chat.id
    result = bot.send_dice(message.chat.id, emoji="🎰").dice.value

    if result == 64:
        add_balance(uid, 50)
        bot.send_message(uid, "+50 ⭐")
    elif result >= 40:
        add_balance(uid, 10)
        bot.send_message(uid, "+10 ⭐")
    else:
        bot.send_message(uid, "❌")

# ------------------ WEBHOOK ROUTE ------------------
@app.route(f"/{TOKEN}", methods=["POST"])
def webhook():
    json_str = request.get_data().decode("utf-8")
    update = telebot.types.Update.de_json(json_str)
    bot.process_new_updates([update])
    return "OK", 200

# ------------------ SET WEBHOOK ------------------
@app.route("/")
def index():
    bot.remove_webhook()
    bot.set_webhook(url=f"https://YOUR-DOMAIN.com/{TOKEN}")
    return "Webhook set!", 200

# ------------------ RUN ------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))