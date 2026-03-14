import telebot
from datetime import datetime
import pytz

BOT_TOKEN = "8677858964:AAH3Y7Z2j7bj-vEaSojneK4Byr8qUeVl4Hc"
bot = telebot.TeleBot(BOT_TOKEN)

CHAT_ID = -1002599963619
DISCUSSION_TOPIC = 20972
QUERY_TOPIC = 5210

IST = pytz.timezone('Asia/Kolkata')
user_queries = {}

NSE_HOLIDAYS = [
    "2026-01-15", "2026-01-26", "2026-03-03", "2026-03-26",
    "2026-03-31", "2026-04-03", "2026-04-14", "2026-05-01",
    "2026-05-28", "2026-06-26", "2026-09-14", "2026-10-02",
    "2026-10-20", "2026-11-10", "2026-11-24", "2026-12-25",
]

def get_current_ist():
    return datetime.now(IST)

def is_market_open_today():
    today = get_current_ist()
    date_str = today.strftime("%Y-%m-%d")
    if today.weekday() in [5, 6]:
        return False
    if date_str in NSE_HOLIDAYS:
        return False
    return True

def is_within_query_time():
    now = get_current_ist()
    hour = now.hour
    minute = now.minute
    current_time_minutes = hour * 60 + minute
    start_time_minutes = 0
    end_time_minutes = 15 * 60 + 30
    return start_time_minutes <= current_time_minutes <= end_time_minutes

def has_query_today(user_id):
    today = get_current_ist().strftime("%Y-%m-%d")
    if user_id not in user_queries:
        return False
    if user_queries[user_id]["date"] != today:
        user_queries[user_id] = {"count": 0, "date": today}
        return False
    return user_queries[user_id]["count"] >= 1

def record_query(user_id):
    today = get_current_ist().strftime("%Y-%m-%d")
    user_queries[user_id] = {"count": 1, "date": today}

@bot.message_handler(func=lambda message: '#query' in message.text.lower() and message.message_thread_id == DISCUSSION_TOPIC)
def handle_query(message):
    user_id = message.from_user.id
    username = message.from_user.username or message.from_user.first_name
    
    print(f"\n🔍 Caught #query from @{username}")
    
    if not is_market_open_today():
        try:
            bot.send_message(
                CHAT_ID,
                f"📅 @{username} - Query sessions are only on market trading days!\nPlease post your query on the next trading day.",
                message_thread_id=DISCUSSION_TOPIC,
                reply_to_message_id=message.message_id
            )
            print(f"⚠️ Market closed today - rejected query from @{username}")
        except Exception as e:
            print(f"Error: {e}")
        return
    
    if not is_within_query_time():
        try:
            bot.send_message(
                CHAT_ID,
                f"⏰ @{username} - Query time window has closed!\nQueries are accepted from 12:00 AM to 3:30 PM IST only.\nPlease ask your query tomorrow.",
                message_thread_id=DISCUSSION_TOPIC,
                reply_to_message_id=message.message_id
            )
            print(f"⏰ Outside query hours - rejected query from @{username}")
        except Exception as e:
            print(f"Error: {e}")
        return
    
    if has_query_today(user_id):
        try:
            bot.send_message(
                CHAT_ID,
                f"⚠️ @{username} - You already posted a query today!\nYou can post again tomorrow after 12:00 AM IST.",
                message_thread_id=DISCUSSION_TOPIC,
                reply_to_message_id=message.message_id
            )
            print(f"⚠️ Daily limit exceeded for @{username}")
        except Exception as e:
            print(f"Error: {e}")
        return
    
    query_text = message.text.replace("#query", "").replace("#Query", "").strip()
    
    forward_text = (
        f"📌 **Query from @{username}**\n\n"
        f"{query_text}\n\n"
        f"_User ID: {user_id}_"
    )
    
    try:
        print(f"   Sending to Query topic...")
        bot.send_message(CHAT_ID, forward_text, message_thread_id=QUERY_TOPIC, parse_mode="Markdown")
        print(f"   ✅ Sent to Query topic!")
        
        print(f"   Sending confirmation...")
        bot.send_message(CHAT_ID, f"✅ @{username} - Your query forwarded to Query topic!", message_thread_id=DISCUSSION_TOPIC, reply_to_message_id=message.message_id)
        print(f"   ✅ Confirmation sent!")
        
        record_query(user_id)
        print(f"✅ Successfully processed query: {query_text[:50]}...\n")
        
    except Exception as e:
        print(f"❌ ERROR: {e}\n")

@bot.message_handler(commands=['start'])
def start(message):
    bot.reply_to(message, "🤖 ChartRoom Query Bot is active!")

@bot.message_handler(commands=['stats'])
def stats(message):
    today = get_current_ist().strftime("%Y-%m-%d")
    count = len([uid for uid, data in user_queries.items() if data["date"] == today])
    bot.reply_to(message, f"📊 Queries received today: {count}")

print("🚀 ChartRoom Query Bot is running...")
print(f"Chat ID: {CHAT_ID}")
print(f"Discussion Topic: {DISCUSSION_TOPIC}")
print(f"Query Topic: {QUERY_TOPIC}")
print(f"Query Window: 12:00 AM to 3:30 PM IST")
print(f"Market Days Only (Mon-Fri, excluding NSE holidays)")
print("="*60)
bot.infinity_polling()
