import os
import re
import json
import time
import html
import requests
import feedparser
import calendar

from pathlib import Path
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv

# =====================================
# CONFIGURACION
# =====================================

BASE_DIR = Path(__file__).parent
load_dotenv(BASE_DIR / ".env")

WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")

if not WEBHOOK_URL:
    raise SystemExit("No se encontro el webhook en .env")

FEEDS = {
    "InfoFortniteES": "https://fxtwitter.com/InfoFortniteES/feed.xml",
    "Fortnite_ES": "https://fixupx.com/Fortnite_ES/feed.xml"
}

STATE_FILE = BASE_DIR / "seen_posts.json"

# Revisar cada 5 minutos en ejecucion local
POLL_SECONDS = 300

# Zona horaria de Peru (UTC-5)
PERU_TIMEZONE = timezone(timedelta(hours=-5))


# =====================================
# MEMORIA DE PUBLICACIONES
# =====================================

def load_state():
    if not STATE_FILE.exists():
        return {}

    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        print("No se pudo leer el historial. Se creara uno nuevo.")
        return {}


def save_state(state):
    temp_file = STATE_FILE.with_suffix(".tmp")
    temp_file.write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )
    temp_file.replace(STATE_FILE)


def get_post_id(post):
    return post.get("id") or post.get("link") or post.get("title")


# =====================================
# EXTRAER TEXTO E IMAGEN
# =====================================

def clean_text(raw):
    raw = re.sub(r"<br\s*/?>", "\n", raw, flags=re.I)
    raw = re.sub(r"</p\s*>", "\n\n", raw, flags=re.I)
    raw = re.sub(r"<[^>]+>", "", raw)

    return html.unescape(raw).strip()


def get_image(post):
    # Imagenes adjuntas al RSS
    for enclosure in post.get("enclosures", []):
        url = enclosure.get("href") or enclosure.get("url")
        content_type = enclosure.get("type", "").lower()

        if url and (
            content_type.startswith("image/")
            or re.search(r"\.(jpg|jpeg|png|gif|webp)(\?|$)", url, re.I)
        ):
            return url

    # Miniaturas de videos
    thumbnails = post.get("media_thumbnail", [])

    if thumbnails:
        return thumbnails[0].get("url")

    # Buscar una imagen dentro del HTML
    summary = post.get("summary", "")
    match = re.search(
        r"""<img[^>]+src=["']([^"']+)""",
        summary,
        re.I
    )

    if match:
        return match.group(1)

    return None


# =====================================
# ENVIAR NOTICIA A DISCORD
# =====================================

def send_news(account, post):
    title = post.get("title", "Nueva publicacion")
    link = post.get("link", "")
    summary = post.get("summary", "")

    description = clean_text(summary) if summary else title

    if len(description) > 3900:
        description = description[:3897] + "..."

    # Convertir la fecha UTC a hora de Peru
    published = post.get("published_parsed") or post.get("updated_parsed")

    embed = {
        "author": {
            "name": account,
            "url": "https://x.com/" + account
        },
        "title": title[:256],
        "url": link,
        "description": description or "Nueva publicacion.",
        "color": 5793266,
        "footer": {
            "text": "Noticias de Fortnite"
        }
    }

    if published:
        date = datetime(
            *published[:6],
            tzinfo=timezone.utc
        ).astimezone(PERU_TIMEZONE)

        embed["timestamp"] = date.isoformat()
        embed["footer"]["text"] = (
            "Noticias de Fortnite | "
            + date.strftime("%d/%m/%Y - %H:%M")
            + " (Perú)"
        )

    image = get_image(post)

    if image:
        embed["image"] = {"url": image}

    payload = {
        "username": "Noticias de Fortnite",
        "embeds": [embed],
        "allowed_mentions": {"parse": []}
    }

    response = requests.post(
        WEBHOOK_URL,
        json=payload,
        timeout=20
    )

    response.raise_for_status()


# =====================================
# COMPROBAR NOTICIAS NUEVAS
# =====================================

def check_feeds(state):
    for account, url in FEEDS.items():
        print(f"\nComprobando {account}...")

        try:
            # Intentar consultar el RSS hasta 3 veces
            for intento in range(3):
                try:
                    response = requests.get(
                        url,
                        headers={"User-Agent": "Mozilla/5.0"},
                        timeout=25
                    )

                    if response.status_code == 404 and intento < 2:
                        print(f"404 en {account}. Reintentando...")
                        time.sleep(5)
                        continue

                    response.raise_for_status()
                    break

                except requests.RequestException:
                    if intento == 2:
                        raise

                    print(f"Error temporal en {account}. Reintentando...")
                    time.sleep(5)

            feed = feedparser.parse(response.content)

            if not feed.entries:
                print("No se encontraron publicaciones.")
                continue

            if account not in state:
                state[account] = []
                save_state(state)
                print("Historial inicializado.")

            seen = set(state[account])
            cutoff = datetime.now(timezone.utc) - timedelta(minutes=30)

            new_posts = []
            ignored_old = []

            # Ordenar desde la mas antigua a la mas reciente
            for post in reversed(feed.entries):
                post_id = get_post_id(post)

                if not post_id or post_id in seen:
                    continue

                published = (
                    post.get("published_parsed")
                    or post.get("updated_parsed")
                )

                if not published:
                    ignored_old.append(post_id)
                    continue

                published_time = datetime.fromtimestamp(
                    calendar.timegm(published),
                    tz=timezone.utc
                )

                if published_time < cutoff:
                    ignored_old.append(post_id)
                    continue

                new_posts.append((published_time, post))

            # Registrar publicaciones antiguas
            if ignored_old:
                state[account].extend(ignored_old)
                state[account] = state[account][-500:]
                save_state(state)

            if not new_posts:
                print("No hay noticias nuevas de los ultimos 30 minutos.")
                continue

            # Enviar publicaciones recientes
            for published_time, post in new_posts:
                post_id = get_post_id(post)

                try:
                    send_news(account, post)

                    print(
                        "Noticia enviada:",
                        post.get("title", "Sin titulo")
                    )

                    state[account].append(post_id)
                    state[account] = state[account][-500:]
                    save_state(state)

                    seen.add(post_id)

                except requests.RequestException as error:
                    print("Error enviando noticia:", error)
                    break

        except requests.RequestException as error:
            print(f"Error consultando {account}: {error}")


# =====================================
# INICIAR BOT
# =====================================

def main():
    print("=================================")
    print("       X NEWS BOT")
    print("=================================")
    print("Bot iniciado correctamente.")
    print("Revisando cuentas de Fortnite...")

    state = load_state()

    # En GitHub: ejecutar una sola comprobacion
    if os.getenv("GITHUB_ACTIONS") == "true":
        check_feeds(state)
        print("\nComprobacion finalizada.")
        return

    # En tu PC: mantener el funcionamiento normal
    while True:
        check_feeds(state)

        print(
            f"\nProxima comprobacion en "
            f"{POLL_SECONDS // 60} minutos."
        )

        try:
            time.sleep(POLL_SECONDS)

        except KeyboardInterrupt:
            print("\nBot detenido.")
            break


if __name__ == "__main__":
    main()
