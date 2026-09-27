"""
tg2disbot (режим БОТА) — копирует посты из Telegram-канала в Discord.

Работает через Telegram Bot API (getUpdates), без пользовательского аккаунта.
Требование: бот должен быть АДМИНИСТРАТОРОМ канала-источника.

Один запуск = разбор всех накопившихся постов и отправка их в Discord.
Скрипт ничего не хранит: подтверждённые посты Telegram сам больше не отдаёт
(offset подтверждается на его стороне). Поэтому подходит для запуска по cron
(в т.ч. GitHub Actions) — просто дёргайте его периодически.

Поддерживаемые типы постов:
  * текст, фото, видео, документы, аудио, гиф (animation), альбомы;
  * кружки (video_note)     -> обычное видео (.mp4);
  * голосовые (voice)       -> нативное voice message в Discord (с фолбэком на .ogg-вложение);
  * опросы (poll)           -> нативный опрос в Discord (таймер POLL_HOURS, по умолчанию 24ч).

Файлы <=20 МБ пересылаются вложением; больше 20 МБ Bot API скачать не может —
для них ставится ссылка на исходный пост в Telegram.
"""

import base64
import json
import os
import sys
import time

import requests

FAILURES = []  # накопленные ошибки отправки -> прогон завершится с ошибкой (красный run)

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

TOKEN = os.environ["TG_BOT_TOKEN"]
DISCORD_BOT_TOKEN = os.environ["DISCORD_BOT_TOKEN"]
DISCORD_CHANNEL_ID = os.environ["DISCORD_CHANNEL_ID"]
SOURCE_CHAT = os.environ.get("TG_SOURCE_CHAT_ID")  # необязательный фильтр (id или @username)
MAX_FILE_BYTES = int(float(os.environ.get("MAX_FILE_MB", "20")) * 1024 * 1024)
POLL_HOURS = int(os.environ.get("POLL_HOURS", "24"))  # таймер опроса в Discord

API = f"https://api.telegram.org/bot{TOKEN}"
DISCORD_API = f"https://discord.com/api/v10/channels/{DISCORD_CHANNEL_ID}/messages"
DISCORD_HEADERS = {"Authorization": f"Bot {DISCORD_BOT_TOKEN}"}
DISCORD_MSG_LIMIT = 2000
DISCORD_FILES_LIMIT = 10
FLAG_VOICE_MESSAGE = 1 << 13  # IS_VOICE_MESSAGE

# обычные вложения (кружок отдаём как видео; voice/poll обрабатываются отдельно)
MEDIA_KEYS = ("photo", "video", "video_note", "document", "audio", "animation")


# ---------- Telegram ----------

def get_updates(offset=None):
    params = {"timeout": 0, "allowed_updates": json.dumps(["channel_post"])}
    if offset is not None:
        params["offset"] = offset
    r = requests.get(f"{API}/getUpdates", params=params, timeout=60)
    r.raise_for_status()
    return r.json().get("result", [])


def extract_media(msg):
    """Возвращает dict(file_id, size, name) для обычного медиа поста или None."""
    if "photo" in msg:
        p = msg["photo"][-1]  # самый большой размер
        return {"file_id": p["file_id"], "size": p.get("file_size", 0), "name": None}
    if "video_note" in msg:  # кружок -> обычное видео (имени файла в API нет)
        f = msg["video_note"]
        return {"file_id": f["file_id"], "size": f.get("file_size", 0), "name": "video_note.mp4"}
    for k in ("video", "document", "audio", "animation"):
        if k in msg:
            f = msg[k]
            return {"file_id": f["file_id"], "size": f.get("file_size", 0), "name": f.get("file_name")}
    return None


def download_file(file_id):
    """Скачивает файл через Bot API. Работает только для файлов <=20 МБ."""
    r = requests.get(f"{API}/getFile", params={"file_id": file_id}, timeout=60).json()
    if not r.get("ok"):
        return None, None
    path = r["result"]["file_path"]
    data = requests.get(f"https://api.telegram.org/file/bot{TOKEN}/{path}", timeout=180).content
    return data, os.path.basename(path)


def post_link(msg):
    chat, mid = msg["chat"], msg["message_id"]
    if chat.get("username"):
        return f"https://t.me/{chat['username']}/{mid}"
    cid = str(chat["id"])
    cid = cid[4:] if cid.startswith("-100") else cid.lstrip("-")
    return f"https://t.me/c/{cid}/{mid}"


# ---------- Discord ----------

def discord_request(payload, files=None):
    """Низкоуровневая отправка: multipart при наличии файлов, иначе JSON. Ретрай на 429."""
    if files:
        data = {"payload_json": json.dumps(payload)}
        file_args = []
        for i, f in enumerate(files):
            name, blob = f[0], f[1]
            ctype = f[2] if len(f) > 2 else "application/octet-stream"
            file_args.append((f"files[{i}]", (name, blob, ctype)))
        resp = requests.post(DISCORD_API, headers=DISCORD_HEADERS,
                             data=data, files=file_args, timeout=180)
    else:
        resp = requests.post(DISCORD_API, headers={**DISCORD_HEADERS, "Content-Type": "application/json"},
                             json=payload, timeout=60)
    if resp.status_code == 429:
        time.sleep(float(resp.json().get("retry_after", 1)) + 0.5)
        return discord_request(payload, files)
    return resp


def discord_post(content, files=None):
    resp = discord_request({"content": content}, files or None)
    if resp.status_code >= 400:
        msg = f"[discord] ошибка {resp.status_code}: {resp.text[:300]}"
        print(msg); FAILURES.append(msg)
        return False
    return True


def chunk_text(text, size=DISCORD_MSG_LIMIT):
    chunks, cur = [], ""
    for line in text.split("\n"):
        while len(line) > size:
            if cur:
                chunks.append(cur); cur = ""
            chunks.append(line[:size]); line = line[size:]
        add = line if not cur else "\n" + line
        if len(cur) + len(add) > size:
            chunks.append(cur); cur = line
        else:
            cur += add
    if cur:
        chunks.append(cur)
    return chunks or [""]


def send_poll(msg):
    """Telegram-опрос -> нативный опрос Discord с таймером POLL_HOURS."""
    poll = msg["poll"]
    answers = [{"poll_media": {"text": (o["text"] or " ")[:55]}} for o in poll["options"][:10]]
    payload = {"poll": {
        "question": {"text": poll["question"][:300]},
        "answers": answers,
        "duration": POLL_HOURS,
        "allow_multiselect": poll.get("allows_multiple_answers", False),
    }}
    resp = discord_request(payload)
    if resp.status_code >= 400:
        m = f"[discord] опрос #{msg['message_id']} не отправлен {resp.status_code}: {resp.text[:300]}"
        print(m); FAILURES.append(m)
        discord_post("📊 Опрос — смотрите в Telegram:\n" + post_link(msg))  # фолбэк ссылкой
    else:
        print(f"[send] опрос #{msg['message_id']} -> Discord poll ({POLL_HOURS}ч)")


def send_voice(msg):
    """Голосовое TG -> voice message Discord; при отказе — обычное .ogg-вложение."""
    v = msg["voice"]
    size = v.get("file_size", 0)
    caption = (msg.get("caption") or "").strip()
    if size and size > MAX_FILE_BYTES:
        discord_post("🎤 Голосовое сообщение (>20 МБ) — в Telegram:\n" + post_link(msg))
        return
    blob, _ = download_file(v["file_id"])
    if blob is None:
        discord_post("🎤 Голосовое сообщение — в Telegram:\n" + post_link(msg))
        return

    duration = int(v.get("duration", 1)) or 1
    # Bot API не отдаёт настоящую форму волны — генерируем плейсхолдер (только для UI).
    waveform = base64.b64encode(bytes((i * 11) % 256 for i in range(48))).decode()
    payload = {"flags": FLAG_VOICE_MESSAGE, "attachments": [{
        "id": 0, "filename": "voice-message.ogg",
        "duration_secs": duration, "waveform": waveform,
    }]}
    resp = discord_request(payload, [("voice-message.ogg", blob, "audio/ogg")])
    if resp.status_code >= 400:
        # не считаем провалом: деградируем на обычное .ogg-вложение (+ подпись)
        print(f"[warn] voice #{msg['message_id']} -> фолбэк на вложение ({resp.status_code}): {resp.text[:200]}")
        discord_post(caption, [("voice-message.ogg", blob, "audio/ogg")])
        return
    print(f"[send] голосовое #{msg['message_id']} -> voice message")
    if caption:
        discord_post(caption)


def send_media_group(posts):
    """Текст + обычные вложения (в т.ч. альбомы и кружки-как-видео)."""
    texts, files, links = [], [], []
    for msg in posts:
        t = msg.get("text") or msg.get("caption")
        if t:
            texts.append(t)
        media = extract_media(msg)
        if not media:
            continue
        if media["size"] and media["size"] <= MAX_FILE_BYTES:
            blob, fname = download_file(media["file_id"])
            if blob is not None:
                files.append((media["name"] or fname or "file", blob))
                continue
            print(f"[warn] пост #{msg['message_id']}: файл не скачался -> ссылка")
        else:
            print(f"[info] пост #{msg['message_id']}: файл >{MAX_FILE_BYTES // 1048576}МБ -> ссылка")
        links.append(post_link(msg))  # слишком большой или не скачался

    text = "\n\n".join(texts).strip()
    if links:
        text = (text + "\n\n" if text else "") + \
               "📎 Файлы больше 20 МБ — смотрите в Telegram:\n" + "\n".join(links)
    if not text and not files:
        ids = ",".join(str(m.get("message_id")) for m in posts)
        keys = sorted(set(k for m in posts for k in m))
        print(f"[warn] нечего отправить для поста(ов) #{ids} — тип не поддержан? поля: {keys}")
        return
    print(f"[send] пост(ы) #{','.join(str(m['message_id']) for m in posts)}: "
          f"файлов {len(files)}, ссылок {len(links)}")

    chunks = chunk_text(text) if text else [""]
    batches = [files[i:i + DISCORD_FILES_LIMIT]
               for i in range(0, len(files), DISCORD_FILES_LIMIT)] or [[]]
    first = True
    for batch in batches:
        discord_post(chunks.pop(0) if (first and chunks) else "", batch)
        first = False
    for c in chunks:
        discord_post(c)


def send_group(posts):
    """Маршрутизация группы постов по типу (опрос/голосовое — всегда одиночные)."""
    if len(posts) == 1:
        msg = posts[0]
        if "poll" in msg:
            return send_poll(msg)
        if "voice" in msg:
            return send_voice(msg)
    send_media_group(posts)


def group_albums(posts):
    """Склеивает подряд идущие посты с одинаковым media_group_id."""
    groups, buf, gid = [], [], object()
    for p in posts:
        cur = p.get("media_group_id")
        if buf and cur == gid and cur is not None:
            buf.append(p)
        else:
            if buf:
                groups.append(buf)
            buf, gid = [p], cur
    if buf:
        groups.append(buf)
    return groups


def main():
    total = 0
    while True:
        updates = get_updates()  # накопившиеся, ещё не подтверждённые
        posts = [u["channel_post"] for u in updates if "channel_post" in u]
        if SOURCE_CHAT:
            posts = [p for p in posts
                     if str(p["chat"].get("id")) == str(SOURCE_CHAT)
                     or ("@" + str(p["chat"].get("username", "")) == SOURCE_CHAT)]
        for group in group_albums(posts):
            send_group(group)
            total += len(group)
        if not updates:
            break
        get_updates(offset=updates[-1]["update_id"] + 1)  # подтверждаем разобранное
        if len(updates) < 100:
            break
    print(f"[ok] обработано постов: {total}")
    if FAILURES:
        print(f"[FAIL] ошибок отправки в Discord: {len(FAILURES)} — прогон помечен как упавший")
        sys.exit(1)


if __name__ == "__main__":
    main()
