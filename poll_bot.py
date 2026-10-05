"""
tg2disbot (режим БОТА) — копирует посты из Telegram-канала в Discord.

Работает через Telegram Bot API (getUpdates), без пользовательского аккаунта.
Требование: бот должен быть АДМИНИСТРАТОРОМ канала-источника.

Два режима (переключаются переменной LOOP_MINUTES):
  * LOOP_MINUTES=0 (по умолч.) — разовый проход: забрать накопившееся и выйти
    (удобно локально или под внешний cron);
  * LOOP_MINUTES>0 — long polling: держим процесс заданное время и получаем посты
    почти мгновенно (getUpdates висит открытым до появления апдейта). Так работает
    в GitHub Actions: один прогон крутится ~5.5ч, следующий встаёт встык (concurrency).

Скрипт ничего не хранит: подтверждённые посты Telegram сам больше не отдаёт
(offset подтверждается на его стороне). Важно: одновременно апдейты должен тянуть
только ОДИН процесс (иначе getUpdates вернёт 409) — за этим следит concurrency.

Поддерживаемые типы постов:
  * текст, фото, видео, документы, аудио, гиф (animation), альбомы;
  * кружки (video_note)     -> обычное видео (.mp4);
  * голосовые (voice)       -> нативное voice message в Discord (с фолбэком на .ogg-вложение);
  * опросы (poll)           -> нативный опрос в Discord (таймер POLL_HOURS, по умолчанию 24ч).

Форматирование Telegram (жирный, курсив, ссылки-под-текстом, цитаты, код, спойлеры...)
переводится в Discord-markdown. Анонсы стримов (пост со ссылкой на kick.com / w.tv /
goodgame.ru) уходят в отдельный канал DISCORD_ANNOUNCE_CHANNEL_ID с пингом @everyone.

Файлы <=20 МБ пересылаются вложением; больше 20 МБ Bot API скачать не может —
для них ставится ссылка на исходный пост в Telegram.
"""

import base64
import json
import os
import re
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
# канал для анонсов стримов (пост со ссылкой на стрим-площадку) — с пингом @everyone
DISCORD_ANNOUNCE_CHANNEL_ID = os.environ.get("DISCORD_ANNOUNCE_CHANNEL_ID") or "1550093981113778206"
SOURCE_CHAT = os.environ.get("TG_SOURCE_CHAT_ID")  # необязательный фильтр (id или @username)
MAX_FILE_BYTES = int(float(os.environ.get("MAX_FILE_MB", "20")) * 1024 * 1024)
POLL_HOURS = int(os.environ.get("POLL_HOURS", "24"))  # таймер опроса в Discord
# Long polling: держим процесс до LOOP_MINUTES, каждый getUpdates висит до LONG_POLL_SECONDS.
# LOOP_MINUTES=0 (по умолчанию) -> разовый проход и выход (удобно локально / под cron).
LOOP_MINUTES = int(os.environ.get("LOOP_MINUTES", "0"))
LONG_POLL_SECONDS = int(os.environ.get("LONG_POLL_SECONDS", "50"))

API = f"https://api.telegram.org/bot{TOKEN}"
DISCORD_API = "https://discord.com/api/v10/channels/{}/messages"
DISCORD_HEADERS = {"Authorization": f"Bot {DISCORD_BOT_TOKEN}"}
DISCORD_MSG_LIMIT = 2000
DISCORD_FILES_LIMIT = 10
FLAG_VOICE_MESSAGE = 1 << 13  # IS_VOICE_MESSAGE
NO_MENTIONS = {"parse": []}           # текст из TG никого не пингует
PING_EVERYONE = {"parse": ["everyone"]}

# Ссылка на стрим-площадку (в т.ч. поддомены) -> пост считается анонсом
ANNOUNCE_RE = re.compile(r"(?<![\w.-])(?:[\w-]+\.)*(?:kick\.com|w\.tv|goodgame\.ru)(?![\w-])", re.I)

# обычные вложения (кружок отдаём как видео; voice/poll обрабатываются отдельно)
MEDIA_KEYS = ("photo", "video", "video_note", "document", "audio", "animation")


# ---------- Telegram ----------

def get_updates(offset=None, timeout=0):
    # timeout>0 -> long polling: Telegram держит запрос открытым до появления апдейта.
    params = {"timeout": timeout, "allowed_updates": json.dumps(["channel_post"])}
    if offset is not None:
        params["offset"] = offset
    r = requests.get(f"{API}/getUpdates", params=params, timeout=timeout + 15)
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
    """Скачивает файл через Bot API (только <=20 МБ). None,None при ошибке/сбое сети."""
    try:
        r = requests.get(f"{API}/getFile", params={"file_id": file_id}, timeout=60).json()
        if not r.get("ok"):
            return None, None
        path = r["result"]["file_path"]
        data = requests.get(f"https://api.telegram.org/file/bot{TOKEN}/{path}", timeout=180).content
        return data, os.path.basename(path)
    except requests.exceptions.RequestException as e:
        print(f"[warn] не удалось скачать файл: {e}")
        return None, None


def post_link(msg):
    chat, mid = msg["chat"], msg["message_id"]
    if chat.get("username"):
        return f"https://t.me/{chat['username']}/{mid}"
    cid = str(chat["id"])
    cid = cid[4:] if cid.startswith("-100") else cid.lstrip("-")
    return f"https://t.me/c/{cid}/{mid}"

def is_announce(msg):
    """Анонс стрима: в тексте/подписи или в ссылках-под-текстом есть kick.com / w.tv / goodgame.ru."""
    text = msg.get("text") or msg.get("caption") or ""
    urls = [e.get("url", "") for e in (msg.get("entities") or msg.get("caption_entities") or [])]
    return any(ANNOUNCE_RE.search(s) for s in [text, *urls])


# ---------- Telegram-разметка -> Discord-markdown ----------

# стили, которые оборачивают текст парой маркеров
_WRAP = {"bold": "**", "italic": "*", "underline": "__", "strikethrough": "~~",
         "spoiler": "||", "code": "`"}
# сущности, внутри которых текст нельзя экранировать (иначе сломаются ссылки/адреса)
_RAW = {"url", "email", "code", "pre", "mention", "hashtag", "cashtag", "bot_command", "phone_number"}
_ESCAPE_RE = re.compile(r"([\\*_~`|])")
_LINE_START_RE = re.compile(r"(^|\n)([#>])", re.M)
_URL_RE = re.compile(r"https?://\S+|(?:[\w-]+\.)+[a-z]{2,}/\S*", re.I)
_NL16 = "\n".encode("utf-16-le")


def _escape(text):
    """Экранирует символы, которые Discord иначе воспримет как разметку (ссылки не трогаем)."""
    out, pos = [], 0
    for m in _URL_RE.finditer(text):
        out.append(_ESCAPE_RE.sub(r"\\\1", text[pos:m.start()]))
        out.append(m.group())
        pos = m.end()
    out.append(_ESCAPE_RE.sub(r"\\\1", text[pos:]))
    return _LINE_START_RE.sub(r"\1\\\2", "".join(out))


def _u16(text):
    return text.encode("utf-16-le")


def _s16(b):
    return b.decode("utf-16-le")


def _render(buf, start, end, entities):
    """Рендерит участок buf[start:end] (единицы UTF-16) с вложенными сущностями."""
    out, pos, i = [], start, 0
    while i < len(entities):
        e = entities[i]
        e_start, e_end = e["offset"], e["offset"] + e["length"]
        # сущности, лежащие внутри текущей (Telegram гарантирует вложенность)
        j = i + 1
        while j < len(entities) and entities[j]["offset"] < e_end:
            j += 1
        if e_start > pos:
            out.append(_escape(_s16(buf[pos * 2:e_start * 2])))
        out.append(_render_entity(buf, e, entities[i + 1:j]))
        pos, i = max(pos, e_end), j
    if end > pos:
        out.append(_escape(_s16(buf[pos * 2:end * 2])))
    return "".join(out)


def _render_entity(buf, e, inner):
    start, end = e["offset"], e["offset"] + e["length"]
    t = e["type"]
    raw = _s16(buf[start * 2:end * 2])
    if t in _RAW:
        if t == "code":
            return f"``{raw}``" if "`" in raw else f"`{raw}`"
        if t == "pre":
            return f"```{e.get('language', '')}\n{raw.rstrip(chr(10))}\n```"
        return raw
    body = _render(buf, start, end, inner)
    if t == "text_link":
        url = e.get("url", "")
        return url if raw.strip() == url else f"[{body}]({url})"
    if t in ("blockquote", "expandable_blockquote"):
        quote = "\n".join("> " + line for line in body.split("\n"))
        # цитата в Discord работает только с начала строки и до её конца
        before = "\n" if start and buf[start * 2 - 2:start * 2] != _NL16 else ""
        after = "\n" if end * 2 < len(buf) and buf[end * 2:end * 2 + 2] != _NL16 else ""
        return before + quote + after
    mark = _WRAP.get(t)
    if not mark or not body.strip():
        return body  # custom_emoji, text_mention и пр. — просто текст
    # Discord не распознаёт "** текст **": выносим пробелы/переносы за маркеры
    lead = body[:len(body) - len(body.lstrip())]
    trail = body[len(body.rstrip()):]
    return f"{lead}{mark}{body.strip()}{mark}{trail}"


def tg_to_markdown(msg):
    """Текст/подпись поста с Telegram-сущностями -> Discord-markdown."""
    text = msg.get("text") or msg.get("caption") or ""
    entities = msg.get("entities") or msg.get("caption_entities") or []
    if not text:
        return ""
    entities = sorted(entities, key=lambda e: (e["offset"], -e["length"]))
    buf = _u16(text)
    return _render(buf, 0, len(buf) // 2, entities)


# ---------- Discord ----------

def discord_request(payload, files=None, channel=None, _attempt=1):
    """Отправка в Discord: multipart при файлах, иначе JSON.
    Ретрай на 429 и на сетевых сбоях. Возвращает Response или None (сеть недоступна)."""
    url = DISCORD_API.format(channel or DISCORD_CHANNEL_ID)
    payload = {"allowed_mentions": NO_MENTIONS, **payload}
    try:
        if files:
            data = {"payload_json": json.dumps(payload)}
            file_args = []
            for i, f in enumerate(files):
                name, blob = f[0], f[1]
                ctype = f[2] if len(f) > 2 else "application/octet-stream"
                file_args.append((f"files[{i}]", (name, blob, ctype)))
            resp = requests.post(url, headers=DISCORD_HEADERS,
                                 data=data, files=file_args, timeout=180)
        else:
            resp = requests.post(url, headers={**DISCORD_HEADERS, "Content-Type": "application/json"},
                                 json=payload, timeout=60)
    except requests.exceptions.RequestException as e:
        if _attempt <= 3:
            time.sleep(2 * _attempt)
            return discord_request(payload, files, channel, _attempt + 1)
        print(f"[warn] сеть Discord недоступна после ретраев: {e}")
        return None
    if resp.status_code == 429:
        time.sleep(float(resp.json().get("retry_after", 1)) + 0.5)
        return discord_request(payload, files, channel, _attempt)
    return resp


def discord_post(content, files=None, channel=None, mentions=None):
    payload = {"content": content}
    if mentions:
        payload["allowed_mentions"] = mentions
    resp = discord_request(payload, files or None, channel)
    if resp is None or resp.status_code >= 400:
        msg = f"[discord] ошибка {resp.status_code if resp else 'network'}: {resp.text[:300] if resp else '-'}"
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
    if resp is None or resp.status_code >= 400:
        code = resp.status_code if resp else "network"
        m = f"[discord] опрос #{msg['message_id']} не отправлен {code}: {resp.text[:300] if resp else '-'}"
        print(m); FAILURES.append(m)
        discord_post("📊 Опрос — смотрите в Telegram:\n" + post_link(msg))  # фолбэк ссылкой
    else:
        print(f"[send] опрос #{msg['message_id']} -> Discord poll ({POLL_HOURS}ч)")


def send_voice(msg):
    """Голосовое TG -> voice message Discord; при отказе — обычное .ogg-вложение."""
    v = msg["voice"]
    size = v.get("file_size", 0)
    caption = tg_to_markdown(msg).strip()
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
    if resp is None or resp.status_code >= 400:
        # не считаем провалом: деградируем на обычное .ogg-вложение (+ подпись)
        code = resp.status_code if resp else "network"
        print(f"[warn] voice #{msg['message_id']} -> фолбэк на вложение ({code}): {resp.text[:200] if resp else '-'}")
        discord_post(caption, [("voice-message.ogg", blob, "audio/ogg")])
        return
    print(f"[send] голосовое #{msg['message_id']} -> voice message")
    if caption:
        discord_post(caption)


def send_media_group(posts):
    """Текст + обычные вложения (в т.ч. альбомы и кружки-как-видео).
    Анонс стрима -> отдельный канал с @everyone."""
    announce = any(is_announce(m) for m in posts)
    channel = DISCORD_ANNOUNCE_CHANNEL_ID if announce else None
    texts, files, links = [], [], []
    for msg in posts:
        t = tg_to_markdown(msg).strip()
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
          f"файлов {len(files)}, ссылок {len(links)}" + (" [анонс -> @everyone]" if announce else ""))

    if announce:
        text = "@everyone\n" + text
    chunks = chunk_text(text) if text else [""]
    batches = [files[i:i + DISCORD_FILES_LIMIT]
               for i in range(0, len(files), DISCORD_FILES_LIMIT)] or [[]]
    first = True
    for batch in batches:
        if first:
            discord_post(chunks.pop(0) if chunks else "", batch, channel,
                         PING_EVERYONE if announce else None)
        else:
            discord_post("", batch, channel)
        first = False
    for c in chunks:
        discord_post(c, None, channel)


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


def handle_updates(updates):
    """Разбирает пачку апдейтов -> Discord. Возвращает число обработанных постов."""
    posts = [u["channel_post"] for u in updates if "channel_post" in u]
    if SOURCE_CHAT:
        posts = [p for p in posts
                 if str(p["chat"].get("id")) == str(SOURCE_CHAT)
                 or ("@" + str(p["chat"].get("username", "")) == SOURCE_CHAT)]
    for group in group_albums(posts):
        send_group(group)
    return len(posts)


def main():
    deadline = time.time() + LOOP_MINUTES * 60 if LOOP_MINUTES > 0 else None
    offset = None  # None -> Telegram отдаст все ещё не подтверждённые апдейты
    total = 0
    while True:
        # в цикле (deadline задан) — long polling; в разовом режиме — короткий запрос
        wait = LONG_POLL_SECONDS if deadline else 0
        try:
            updates = get_updates(offset=offset, timeout=wait)
        except requests.exceptions.RequestException as e:
            # транзиентный сетевой сбой Telegram: не роняем цикл, ждём и повторяем
            print(f"[warn] getUpdates сеть: {e}; повтор через 5с")
            if deadline is None:
                break
            time.sleep(5)
            continue
        if updates:
            total += handle_updates(updates)
            offset = updates[-1]["update_id"] + 1  # подтвердится следующим getUpdates

        if deadline is None:                 # разовый режим: дренаж и выход
            if not updates or len(updates) < 100:
                break
        elif time.time() >= deadline:        # цикл: пора завершаться (следующий прогон встанет встык)
            break

    if offset is not None:                   # финально подтверждаем последнюю пачку
        try:
            get_updates(offset=offset, timeout=0)
        except Exception as e:
            print(f"[warn] не удалось подтвердить offset при выходе: {e}")

    print(f"[ok] обработано постов: {total}")
    if FAILURES:
        print(f"[FAIL] ошибок отправки в Discord: {len(FAILURES)} — прогон помечен как упавший")
        sys.exit(1)


if __name__ == "__main__":
    main()
