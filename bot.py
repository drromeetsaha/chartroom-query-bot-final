import os
import re
import html
import json
import time
import threading
import telebot
from telebot import types
from datetime import datetime, timedelta
import pytz
from stock_tags import tags as stock_tags

BOT_TOKEN = os.environ["BOT_TOKEN"]  # set in Railway Variables
bot = telebot.TeleBot(BOT_TOKEN)

CHAT_ID = -1002599963619
DOC_ID = 570458138
DISCUSSION_TOPIC = 20972
QUERY_TOPIC = 5210

IST = pytz.timezone('Asia/Kolkata')

NSE_HOLIDAYS = [
    "2026-01-15", "2026-01-26", "2026-03-03", "2026-03-26",
    "2026-03-31", "2026-04-03", "2026-04-14", "2026-05-01",
    "2026-05-28", "2026-06-26", "2026-09-14", "2026-10-02",
    "2026-10-20", "2026-11-10", "2026-11-24", "2026-12-25",
]

WINDOW_END_MINUTES = 15 * 60 + 30   # queries are taken 12:00 AM - 3:30 PM IST
NOTE_LIFETIME = 60                  # rejection notes delete themselves after a minute
OFFER_LIFETIME = 10 * 60            # "forward at the next window?" buttons expire

# Weekly counts and saved queries live on a Railway volume so restarts don't reset them
DB_FILE = "/app/data/query_bot_db.json" if os.path.isdir("/app/data") else "query_bot_db.json"
lock = threading.Lock()
offers = {}      # offer message id -> pending yes/no choice
deletions = []   # (delete_at, message_id) of notes to remove


def load_db():
    try:
        with open(DB_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        data = {}
    data.setdefault("user_queries", {})
    data.setdefault("saved", [])
    data.setdefault("forwarded", {})  # forwarded message id -> who asked, so Doc's reply can tag them
    return data


def save_db():
    tmp = DB_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=1)
    os.replace(tmp, DB_FILE)


db = load_db()


def get_current_ist():
    return datetime.now(IST)


def is_trading_day(d):
    return d.weekday() < 5 and d.strftime("%Y-%m-%d") not in NSE_HOLIDAYS


def in_query_window(now):
    return is_trading_day(now.date()) and now.hour * 60 + now.minute <= WINDOW_END_MINUTES


def next_window_start(now):
    """12:00 AM IST of the next trading day after today."""
    d = now.date() + timedelta(days=1)
    while not is_trading_day(d):
        d += timedelta(days=1)
    return IST.localize(datetime(d.year, d.month, d.day, 0, 0))


def week_start_of(dt):
    """Monday of dt's week as YYYY-MM-DD."""
    return (dt.date() - timedelta(days=dt.weekday())).strftime("%Y-%m-%d")


def has_query_in_week(user_id, week_start):
    data = db["user_queries"].get(str(user_id), {})
    return data.get("week_start") == week_start and data.get("week_count", 0) >= 1


def record_query(user_id, week_start):
    data = db["user_queries"].setdefault(str(user_id), {})
    if data.get("week_start") != week_start:
        data["week_start"] = week_start
        data["week_count"] = 0
    data["week_count"] = data.get("week_count", 0) + 1
    save_db()


def when_text(target, now):
    if target.date() == now.date() + timedelta(days=1):
        return f"12:00 AM tomorrow ({target.strftime('%A')})"
    return f"12:00 AM on {target.strftime('%A')}"


def send_note(message, text, lifetime=NOTE_LIFETIME, markup=None):
    """Reply in Discussion; the note deletes itself after `lifetime` seconds."""
    sent = bot.send_message(
        CHAT_ID, text,
        message_thread_id=DISCUSSION_TOPIC,
        reply_to_message_id=message.message_id,
        reply_markup=markup,
    )
    if lifetime:
        with lock:
            deletions.append((time.time() + lifetime, sent.message_id))
    return sent


def remember_forward(sent, user_id, username, query_text=""):
    """Keeps who asked each forwarded query (and which stock) for 30 days, so the member can be tagged under Doc's reply."""
    with lock:
        db["forwarded"][str(sent.message_id)] = {"user_id": user_id, "name": username, "ts": time.time(),
                                                 "tags": stock_tags(query_text)}
        cut = time.time() - 30 * 86400
        db["forwarded"] = {k: v for k, v in db["forwarded"].items() if v["ts"] > cut}
        save_db()


def forward_query(username, query_text, saved_at=None, user_id=None):
    text = f"📌 Query from {username}\n\n{query_text}"
    if saved_at:
        text += f"\n\n(saved on {saved_at})"
    # saved queries go out at midnight, so they arrive silently
    sent = bot.send_message(CHAT_ID, text, message_thread_id=QUERY_TOPIC, disable_notification=bool(saved_at))
    if user_id:
        remember_forward(sent, user_id, username, query_text)


# "#query" used as advice, not as a query: "Pls use #query stock name", "Try #query mcx", "#query krke dalo"
INSTRUCTION_RE = re.compile(r"\b(use|try|post|ask|pls|please|plz|via|through|bhejo|dalo|daalo)\s+(?:in\s+|at\s+|on\s+|it\s+)?#query\b"
                            r"|#query\s+(?:karo|kro|krke|karke|kar\s+ke|kar\s+do|me|mein|pe|topic)\b", re.I)


def is_real_query(message):
    """Doc never asks queries (his "#query" is always advice to a member); members' advice to each other is skipped too."""
    if message.from_user.id == DOC_ID:
        return False
    replying_to_member = (message.reply_to_message is not None and message.reply_to_message.from_user is not None
                          and message.reply_to_message.from_user.id not in (message.from_user.id, DOC_ID)  # replies to Doc are real queries
                          and message.reply_to_message.message_id != DISCUSSION_TOPIC)
    if replying_to_member and INSTRUCTION_RE.search(message.text):
        return False
    return True


@bot.message_handler(func=lambda message: message.text and '#query' in message.text.lower()
                     and message.message_thread_id == DISCUSSION_TOPIC and is_real_query(message))
def handle_query(message):
    user_id = message.from_user.id
    # "@username" when the member has one; otherwise their first name (an "@Ravi" would tag nobody)
    username = f"@{message.from_user.username}" if message.from_user.username else (message.from_user.first_name or "a member")
    now = get_current_ist()
    query_text = message.text.replace("#query", "").replace("#Query", "").replace("#QUERY", "").strip()

    print(f"\n🔍 Caught #query from {username}")

    open_now = in_query_window(now)
    target = now if open_now else next_window_start(now)
    week = week_start_of(target)

    with lock:
        saved = next((s for s in db["saved"] if s["user_id"] == user_id and not s.get("sent")
                      and s["target"] > now.timestamp()), None)
        used_week = has_query_in_week(user_id, week)

    try:
        if saved:
            what = " ".join(saved["text"].split())
            what = (what[:30].rstrip() + "…") if len(what) > 30 else what
            day = datetime.fromtimestamp(saved["target"], IST)
            day = f"tomorrow ({day.strftime('%A')})" if day.date() == now.date() + timedelta(days=1) else day.strftime("%A")
            send_note(message, f"🙏 You've already saved one query ({what}) for {day}. "
                               f"It's 1 query per week, so this one can't be added.")
            return
        if used_week:
            send_note(message, "🙏 You've already used this week's query. Your next one opens on Monday.\n"
                               "Meanwhile, #view stockname shows Doc's latest views.")
            print(f"⚠️ Weekly limit reached for {username}")
            return
        if open_now:
            forward_query(username, query_text, user_id=user_id)
            # Record only after a successful forward, so a failed send doesn't use up the weekly query
            with lock:
                record_query(user_id, week)
            print(f"✅ Forwarded query: {query_text[:50]}...")
            return

        # Outside the window: ask whether to forward it when the next window opens
        reason = ("Today's query window has closed (12:00 AM–3:30 PM on market days)."
                  if is_trading_day(now.date()) else "Queries are taken on market days only.")
        markup = types.InlineKeyboardMarkup()
        markup.row(types.InlineKeyboardButton("✅ Yes, forward it", callback_data="qsave"),
                   types.InlineKeyboardButton("No, thanks", callback_data="qskip"))
        offer = send_note(message, f"🙏 {reason} Shall I forward this query to Doc at {when_text(target, now)}?",
                          lifetime=OFFER_LIFETIME, markup=markup)
        with lock:
            offers[offer.message_id] = {"user_id": user_id, "username": username, "text": query_text,
                                        "target": target.timestamp(), "asked": now.strftime("%d %b, %I:%M %p")}
        print(f"🕒 Outside window - offered to save query from {username}")
    except Exception as e:
        print(f"❌ ERROR: {e}\n")


def mention(entry):
    """'@username' for members with one; otherwise a name link that still notifies them."""
    name = entry["name"]
    if name.startswith("@"):
        return name
    return f'<a href="tg://user?id={entry["user_id"]}">{html.escape(name)}</a>'


albums = {}  # album id -> Doc's messages in it; an album arrives as one message per photo


@bot.message_handler(content_types=["text", "photo", "document"],
                     func=lambda m: m.message_thread_id == QUERY_TOPIC and m.from_user and m.from_user.id == DOC_ID)
def handle_doc_answer(message):
    """Doc answered a forwarded query. If his answer doesn't tag the member, tag them under it, so they're
    notified that their query was answered (Doc then never has to type the "#Symbol @member" header)."""
    if message.media_group_id:
        # wait for the whole album (the caption may be on any photo), then tag once
        with lock:
            first = message.media_group_id not in albums
            albums.setdefault(message.media_group_id, []).append(message)
        if first:
            threading.Timer(4, lambda g=message.media_group_id: answer_done(albums.pop(g, []))).start()
        return
    answer_done([message])


def answer_done(messages):
    head = (next((m.text or m.caption for m in messages if m.text or m.caption), "") or "").strip().split("\n")[0][:120]
    with lock:  # Doc's answers of the last 3 days, for the 3:30 PM check
        db.setdefault("doc_heads", []).append([time.time(), head.lower()])
        db["doc_heads"] = [h for h in db["doc_heads"] if h[0] > time.time() - 3 * 86400]
    key = next((str(m.reply_to_message.message_id) for m in messages
                if m.reply_to_message and str(m.reply_to_message.message_id) in db["forwarded"]), None)
    if key is None:
        key = open_query_for(messages)
    if key:
        tag_member(key, messages)


def _words(text):
    return {w.lower() for w in re.findall(r"#(\w+)", text or "")}


def open_query_for(messages):
    """Doc answered by replying to his old analysis, or without a reply ("#Groww Akash As per our last analysis...").
    The open query (last 3 days, not yet tagged) whose stock tag or member name his first line names."""
    text = next((m.text or m.caption for m in messages if m.text or m.caption), "") or ""
    head = text.strip().split("\n")[0][:120].lower()
    stocks = _words(head)
    if not stocks:  # no "#Stock" of his own: take it from the old analysis he replied under
        r = next((m.reply_to_message for m in messages if m.reply_to_message), None)
        stocks = _words(((r.text or r.caption or "") if r else "").split("\n")[0])
    cut = time.time() - 3 * 86400
    by_name, by_stock = [], []
    for key, e in db["forwarded"].items():
        if e.get("tagged") or e["ts"] < cut:
            continue
        name = e["name"].lstrip("@").lower()
        if len(name) >= 3 and re.search(r"(?<!\w)@?" + re.escape(name) + r"(?!\w)", head):
            by_name.append(key)
        elif any(a == b or len(min(a, b, key=len)) >= 4 and (a.startswith(b) or b.startswith(a))
                 for a in _words(e.get("tags", "")) for b in stocks):
            by_stock.append(key)  # "#Bajajhind" and a query tagged "#Bajajhindustansugar"
    found = by_name if by_name else by_stock
    return found[0] if len(found) == 1 else None


def tag_member(key, messages):
    with lock:
        entry = db["forwarded"].get(key)
        if not entry or entry.get("tagged") or not messages:
            return  # tagged once per query, even if Doc answers in several messages
        entry["tagged"] = True
        save_db()
    for message in messages:
        answer = (message.text or message.caption or "").lower()
        if entry["name"].startswith("@") and entry["name"].lower() in answer:
            return  # Doc already tagged them
        for e in (message.entities or message.caption_entities or []):
            if e.type == "text_mention" and e.user and e.user.id == entry["user_id"]:
                return
    try:
        head = (entry.get("tags", "") + " " + mention(entry)).strip()  # "#Kpittech @amitdelta19", Doc's own style
        bot.send_message(CHAT_ID, f"{head} \U0001f64f Doc has answered your query \U0001f446",
                         message_thread_id=QUERY_TOPIC, reply_to_message_id=messages[0].message_id, parse_mode="HTML")
        print(f"🔔 Tagged {entry['name']} under Doc's answer")
    except Exception as e:
        print(f"❌ Could not tag under Doc's answer: {e}")


@bot.callback_query_handler(func=lambda call: call.data in ("qsave", "qskip"))
def handle_offer_choice(call):
    offer_id = call.message.message_id
    with lock:
        offer = offers.get(offer_id)
    if not offer:
        bot.answer_callback_query(call.id, "This option has expired.")
        return
    if call.from_user.id != offer["user_id"]:
        bot.answer_callback_query(call.id, "🙏 Only the member who asked can choose this.")
        return
    with lock:
        offers.pop(offer_id, None)
    try:
        if call.data == "qskip":
            bot.answer_callback_query(call.id, "👍 No problem.")
            bot.delete_message(CHAT_ID, offer_id)
            return
        target = datetime.fromtimestamp(offer["target"], IST)
        week = week_start_of(target)
        with lock:
            if has_query_in_week(offer["user_id"], week):
                text = "🙏 You've already used that week's query, so this one can't be saved."
            else:
                record_query(offer["user_id"], week)
                db["saved"].append({"user_id": offer["user_id"], "username": offer["username"], "display": True, "text": offer["text"],
                                    "target": offer["target"], "asked": offer["asked"], "sent": False})
                save_db()
                text = f"✅ Done! Your query will be forwarded to Doc at {when_text(target, get_current_ist())}."
            deletions.append((time.time() + NOTE_LIFETIME, offer_id))
        bot.answer_callback_query(call.id)
        bot.edit_message_text(text, CHAT_ID, offer_id)
        print(f"💾 Query from {offer['username']}: {text}")
    except Exception as e:
        print(f"❌ ERROR: {e}\n")


def answered(e):
    """Doc tagged them, the bot tagged them under his answer, or a later message of his names the member or the stock."""
    if e.get("tagged"):
        return True
    name = e["name"].lstrip("@").lower()
    stocks = _words(e.get("tags", ""))
    for ts, head in db.get("doc_heads", []):
        if ts < e["ts"]:
            continue
        if len(name) >= 3 and name in head:
            return True
        if any(a == b or len(min(a, b, key=len)) >= 4 and (a.startswith(b) or b.startswith(a)) for a in stocks for b in _words(head)):
            return True
    return False


def remind_doc(now):
    """3:30 PM on market days, only if a query from today is still without Doc's answer: a private note with links."""
    today = now.strftime("%Y-%m-%d")
    if not is_trading_day(now.date()) or now.hour * 60 + now.minute < WINDOW_END_MINUTES or db.get("reminded") == today:
        return
    with lock:
        db["reminded"] = today
        save_db()
        missed = [(k, e) for k, e in db["forwarded"].items()
                  if datetime.fromtimestamp(e["ts"], IST).strftime("%Y-%m-%d") == today and not answered(e)]
    if not missed:
        return
    lines = [f'• <a href="https://t.me/c/{str(CHAT_ID)[4:]}/{QUERY_TOPIC}/{k}">{html.escape(e.get("tags") or "Query")} '
             f'({html.escape(e["name"])})</a>' for k, e in sorted(missed, key=lambda x: x[1]["ts"])]
    try:
        bot.send_message(DOC_ID, f"\U0001f64f Doc, {len(missed)} {'query' if len(missed) == 1 else 'queries'} from today "
                                 f"may still be waiting for your answer:\n" + "\n".join(lines),
                         parse_mode="HTML", disable_web_page_preview=True)
        print(f"🔔 Reminded Doc of {len(missed)} open queries")
    except Exception as e:
        print(f"❌ Could not send Doc the reminder (has he pressed Start in the bot's chat?): {e}")


def housekeeping():
    """Every 20 s: forward saved queries that are due, and remove notes and offers whose time is up."""
    while True:
        try:
            now_ts = time.time()
            with lock:
                due = [s for s in db["saved"] if not s.get("sent") and s["target"] <= now_ts]
                to_delete = {mid for t, mid in deletions if t <= now_ts}
                deletions[:] = [(t, mid) for t, mid in deletions if t > now_ts]
            for s in due:
                # queries saved before 27 Oct stored the bare name and were always shown with "@"
                name = s["username"] if s.get("display") else f"@{s['username']}"
                forward_query(name, s["text"], saved_at=s["asked"], user_id=s.get("user_id"))
                with lock:
                    s["sent"] = True
                    db["saved"] = [x for x in db["saved"] if not x.get("sent") or now_ts - x["target"] < 7 * 86400]
                    save_db()
                print(f"📤 Forwarded saved query from @{s['username']}")
            for mid in to_delete:
                with lock:
                    offers.pop(mid, None)
                try:
                    bot.delete_message(CHAT_ID, mid)
                except Exception:
                    pass  # already deleted
            remind_doc(get_current_ist())
        except Exception as e:
            print(f"❌ Housekeeping error: {e}")
        time.sleep(20)


@bot.message_handler(commands=['start'])
def start(message):
    bot.reply_to(message, "🤖 ChartRoom Query Bot is active!")


@bot.message_handler(commands=['stats'])
def stats(message):
    week_start = week_start_of(get_current_ist())
    count = len([uid for uid, data in db["user_queries"].items() if data.get("week_start") == week_start])
    pending = len([s for s in db["saved"] if not s.get("sent")])
    bot.reply_to(message, f"📊 Queries this week: {count}\n💾 Saved for the next window: {pending}")


def main():
    print("🚀 ChartRoom Query Bot is running...")
    print(f"Chat ID: {CHAT_ID}")
    print(f"Discussion Topic: {DISCUSSION_TOPIC}")
    print(f"Query Topic: {QUERY_TOPIC}")
    print(f"Query Window: 12:00 AM to 3:30 PM IST")
    print(f"Limit: 1 query per person per week (resets Monday)")
    print(f"Market Days Only (Mon-Fri, excluding NSE holidays); late queries can be saved for the next window")
    print(f"DB file: {DB_FILE} ({len(db['user_queries'])} members, {len([s for s in db['saved'] if not s.get('sent')])} saved queries)")
    print("="*60)
    threading.Thread(target=housekeeping, daemon=True).start()
    bot.remove_webhook()
    bot.infinity_polling(allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    main()
