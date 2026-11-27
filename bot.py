import os
import json
import time
import requests

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

# Perfiles de X que consulta el bot
ACCOUNTS = [
    "InfoFortniteES",
    "Fortnite_ES"
]

# API publica de FxTwitter (JSON)
API_BASE_URL = "https://api.fxtwitter.com/2/profile"
API_POST_COUNT = 100

STATE_FILE = BASE_DIR / "seen_posts.json"

# En ejecucion local, revisar cada 5 minutos
POLL_SECONDS = 300

# Filtro de publicaciones recientes
POST_MAX_AGE_MINUTES = 30

# Reintentos para errores temporales de red/API
MAX_ATTEMPTS = 3
RETRY_DELAY_SECONDS = 5

# Zona horaria de Peru (UTC-5)
PERU_TIMEZONE = timezone(timedelta(hours=-5))


# =====================================
# MEMORIA DE PUBLICACIONES
# =====================================

def load_state():
    if not STATE_FILE.exists():
        return {}

    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
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
    post_id = post.get("id")
    return str(post_id) if post_id is not None else None


# =====================================
# FECHAS Y MULTIMEDIA DE LA API
# =====================================

def get_post_datetime(post):
    """Obtiene la fecha de la publicacion en UTC."""
    timestamp = post.get("created_timestamp")

    if timestamp is not None:
        try:
            return datetime.fromtimestamp(float(timestamp), tz=timezone.utc)
        except (TypeError, ValueError, OverflowError, OSError):
            pass

    created_at = post.get("created_at")
    if created_at:
        try:
            parsed = datetime.fromisoformat(str(created_at).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)
        except ValueError:
            pass

    return None


def get_image(post):
    """Busca una imagen o miniatura en los datos multimedia de FxTwitter."""
    media = post.get("media") or {}

    # Fotos e imagenes/GIF
    photos = media.get("photos") or []
    for photo in photos:
        image_url = photo.get("url")
        if image_url:
            return image_url

    # Miniatura de un video o GIF
    videos = media.get("videos") or []
    for video in videos:
        thumbnail_url = video.get("thumbnail_url")
        if thumbnail_url:
            return thumbnail_url

    # Video externo, si la API incluye miniatura
    external = media.get("external") or {}
    return external.get("thumbnail_url")


# =====================================
# CONSULTAR LA API DE FXTWITTER
# =====================================

def fetch_posts(account):
    url = f"{API_BASE_URL}/{account}/statuses"

    # El filtro de 30 minutos se aplica localmente mas abajo.
    # Asi evitamos depender del parametro opcional 'since' de la API.
    params = {
        "count": API_POST_COUNT,
        "lang": "es"
    }

    headers = {
        "User-Agent": "FortniteNewsDiscordBot/1.0",
        "Accept": "application/json"
    }

    for intento in range(MAX_ATTEMPTS):
        try:
            response = requests.get(
                url,
                params=params,
                headers=headers,
                timeout=25
            )

            # 204 significa que no hay contenido para esta consulta.
            if response.status_code == 204:
                return []

            # 404 no se interpreta como "no hay noticias".
            # Se devuelve None para que check_feeds omita esta cuenta.
            if response.status_code == 404:
                detail = "cuenta no encontrada o cronologia no disponible"
                try:
                    error_data = response.json()
                    if isinstance(error_data, dict):
                        detail = (
                            error_data.get("message")
                            or error_data.get("error")
                            or error_data.get("reason")
                            or detail
                        )
                except ValueError:
                    pass

                print(f"La API devolvio 404 para {account}: {detail}.")
                return None

            # Reintentar errores temporales o limite de solicitudes.
            if response.status_code == 429 or response.status_code >= 500:
                if intento < MAX_ATTEMPTS - 1:
                    retry_after = response.headers.get("Retry-After")
                    try:
                        wait_seconds = min(max(float(retry_after), 1), 30)
                    except (TypeError, ValueError):
                        wait_seconds = RETRY_DELAY_SECONDS

                    print(
                        f"Error HTTP {response.status_code} en {account}. "
                        f"Reintentando en {wait_seconds:g} segundos..."
                    )
                    time.sleep(wait_seconds)
                    continue

            response.raise_for_status()

            data = response.json()
            if not isinstance(data, dict):
                raise ValueError("La respuesta de FxTwitter no es un objeto JSON valido.")

            api_code = data.get("code", response.status_code)

            # La API tambien puede indicar 204 dentro del JSON.
            if api_code == 204:
                return []

            # Un 404 dentro del JSON tambien es un fallo de consulta.
            if api_code == 404:
                print(
                    f"La API devolvio 404 para {account}: "
                    "cuenta no encontrada o cronologia no disponible."
                )
                return None

            if api_code != 200:
                raise ValueError(f"Codigo inesperado de FxTwitter: {api_code}")

            results = data.get("results", [])
            if not isinstance(results, list):
                raise ValueError(
                    "La respuesta de FxTwitter no contiene una lista 'results'."
                )

            # Cada resultado suele ser una publicacion individual.
            return [
                post for post in results
                if isinstance(post, dict) and post.get("type", "status") == "status"
            ]

        except (requests.RequestException, ValueError) as error:
            if intento == MAX_ATTEMPTS - 1:
                raise

            print(
                f"Error temporal consultando {account}: {error}. "
                f"Reintentando en {RETRY_DELAY_SECONDS} segundos..."
            )
            time.sleep(RETRY_DELAY_SECONDS)

    return None


# =====================================
# ENVIAR NOTICIA A DISCORD
# =====================================

def send_news(account, post, published_time):
    text_content = (post.get("text") or "").strip()
    if not text_content:
        text_content = "Nueva publicacion."

    if len(text_content) > 3900:
        text_content = text_content[:3897] + "..."

    post_id = get_post_id(post)
    link = post.get("url") or (
        f"https://x.com/{account}/status/{post_id}"
        if post_id else f"https://x.com/{account}"
    )

    # Hora UTC convertida a Peru (UTC-5)
    peru_date = published_time.astimezone(PERU_TIMEZONE)

    embed = {
        "author": {
            "name": account,
            "url": f"https://x.com/{account}"
        },
        "title": f"Nueva publicacion de @{account}",
        "url": link,
        "description": text_content,
        "color": 5793266,
        "timestamp": published_time.astimezone(timezone.utc).isoformat(),
        "footer": {
            "text": (
                "Noticias de Fortnite | "
                + peru_date.strftime("%d/%m/%Y - %H:%M")
                + " (Perú)"
            )
        }
    }

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
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=POST_MAX_AGE_MINUTES)

    for account in ACCOUNTS:
        print(f"\nComprobando {account}...")

        try:
            posts = fetch_posts(account)

            # None significa que la API no pudo consultar la cuenta.
            # No se debe mostrar como si simplemente no hubiera noticias.
            if posts is None:
                print(
                    f"No se pudo consultar {account}. "
                    "Se omite esta comprobacion."
                )
                continue

            if account not in state or not isinstance(state.get(account), list):
                state[account] = []
                save_state(state)
                print("Historial inicializado.")

            seen = set(str(item) for item in state[account])
            new_posts = []

            for post in posts:
                post_id = get_post_id(post)
                if not post_id or post_id in seen:
                    continue

                published_time = get_post_datetime(post)
                if published_time is None:
                    print(f"Publicacion {post_id} ignorada: no tiene fecha valida.")
                    continue

                # Validacion local del filtro de 30 minutos.
                if published_time < cutoff:
                    continue

                new_posts.append((published_time, post))

            # Publicar desde la mas antigua hasta la mas reciente.
            new_posts.sort(key=lambda item: item[0])

            if not new_posts:
                print("No hay noticias nuevas de los ultimos 30 minutos.")
                continue

            for published_time, post in new_posts:
                post_id = get_post_id(post)

                try:
                    send_news(account, post, published_time)
                    print("Noticia enviada:", (post.get("text") or "")[:100])

                    state[account].append(post_id)
                    state[account] = state[account][-500:]
                    save_state(state)
                    seen.add(post_id)

                except requests.RequestException as error:
                    print("Error enviando noticia a Discord:", error)
                    break

        except (requests.RequestException, ValueError) as error:
            print(f"Error consultando {account} en la API de FxTwitter: {error}")


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

    # En GitHub Actions: una sola comprobacion por ejecucion.
    if os.getenv("GITHUB_ACTIONS") == "true":
        check_feeds(state)
        print("\nComprobacion finalizada.")
        return

    # En tu PC: mantener el funcionamiento continuo.
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
