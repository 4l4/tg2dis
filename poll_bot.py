"""
tg2disbot (режим БОТА) — копирует посты из Telegram-канала в Discord.

Работает через Telegram Bot API (getUpdates), без пользовательского аккаунта.
Требование: бот должен быть АДМИНИСТРАТОРОМ канала-источника.

Один запуск = разбор всех накопившихся постов и отправка их в Discord.
Скрипт ничего не хранит: подтверждённые посты Telegram сам больше не отдаёт
(offset подтверждается на его стороне). Поэтому подходит для запуска по cron
(в т.ч. GitHub Actions) — просто дёргайте его периодически.

Файлы <=20 МБ пересылаются вложением; больше 20 МБ Bot API скачать не может —
для них ставится ссылка на исходный пост в Telegram.
"""

import json
import os

import requests

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

API = f"https://api.telegram.org/bot{TOKEN}"
DISCORD_API = f"https://discord.com/api/v10/channels/{DISCORD_CHANNEL_ID}/messages"
DISCORD_HEADERS = {"Authorization": f"Bot {DISCORD_BOT_TOKEN}"}
DISCORD_MSG_LIMIT = 2000
DISCORD_FILES_LIMIT = 10
MEDIA_KEYS = ("video", "document", "audio", "animation", "voice", "video_note")


# ---------- Telegram ----------

def get_updates(offset=None):
    params = {"timeout": 0, "allowed_updates": json.dumps(["channel_post"])}
    if offset is not None:
        params["offset"] = offset
    r = requests.get(f"{API}/getUpdates", params=params, timeout=60)
    r.raise_for_status()
    return r.json().get("result", [])


def extract_media(msg):
    """Возвращает (file_id, size, filename) для медиа в посте или None."""
    if "photo" in msg:
        p = msg["photo"][-1]  # самый большой размер
        return p["file_id"], p.get("file_size", 0), None
    for k in MEDIA_KEYS:
        if k in msg:
            f = msg[k]
            return f["file_id"], f.get("file_size", 0), f.get("file_name")
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


def discord_post(content, files):
    data = {"payload_json": json.dumps({"content": content})}
    file_args = [(f"files[{i}]", (name, blob)) for i, (name, blob) in enumerate(files)]
    resp = requests.post(DISCORD_API, headers=DISCORD_HEADERS,
                         data=data, files=file_args or None, timeout=180)
    if resp.status_code == 429:
        import time
        time.sleep(float(resp.json().get("retry_after", 1)) + 0.5)
        return discord_post(content, files)
    if resp.status_code >= 400:
        print(f"[discord] ошибка {resp.status_code}: {resp.text}")


def send_group(posts):
    """posts — список постов одного альбома (или один пост списком)."""
    texts, files, links = [], [], []
    for msg in posts:
        t = msg.get("text") or msg.get("caption")
        if t:
            texts.append(t)
        media = extract_media(msg)
        if not media:
            continue
        file_id, size, name = media
        if size and size <= MAX_FILE_BYTES:
            blob, fname = download_file(file_id)
            if blob is not None:
                files.append((name or fname or "file", blob))
                continue
        links.append(post_link(msg))  # слишком большой или не скачался

    text = "\n\n".join(texts).strip()
    if links:
        text = (text + "\n\n" if text else "") + \
               "📎 Файлы больше 20 МБ — смотрите в Telegram:\n" + "\n".join(links)
    if not text and not files:
        return

    chunks = chunk_text(text) if text else [""]
    batches = [files[i:i + DISCORD_FILES_LIMIT]
               for i in range(0, len(files), DISCORD_FILES_LIMIT)] or [[]]
    first = True
    for batch in batches:
        discord_post(chunks.pop(0) if (first and chunks) else "", batch)
        first = False
    for c in chunks:
        discord_post(c, [])


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


if __name__ == "__main__":
    main()
