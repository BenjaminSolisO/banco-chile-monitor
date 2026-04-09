#!/usr/bin/env python3
"""
Monitor de alertas Banco de Chile via IMAP IDLE → Telegram.

Requisitos:
    pip install imapclient requests

Configuración de Gmail:
    1. Activar IMAP en Gmail: Configuración → Ver toda la conf. → Reenvío e IMAP
    2. Activar verificación en 2 pasos en tu cuenta Google
    3. Generar una "Contraseña de aplicación":
       myaccount.google.com → Seguridad → Contraseñas de aplicaciones
    4. Exportar variables de entorno antes de ejecutar:
          export GMAIL_USER="tu@gmail.com"
          export GMAIL_PASSWORD="xxxx xxxx xxxx xxxx"   # contraseña de app (16 chars)

Uso:
    python gmail_banco_chile_monitor.py
"""

import email
import email.utils
import logging
import os
import re
import time
from email.header import decode_header
from datetime import datetime, timezone

import requests
from imapclient import IMAPClient

# ─── Configuración ────────────────────────────────────────────────────────────

IMAP_HOST = "imap.gmail.com"
IMAP_PORT = 993

GMAIL_USER     = os.environ.get("GMAIL_USER", "")      # tu@gmail.com
GMAIL_PASSWORD = os.environ.get("GMAIL_PASSWORD", "")  # contraseña de aplicación

TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
TELEGRAM_API_URL = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"

# Dominios aceptados — solo emails cuyo remitente termine en uno de estos
BANCO_CHILE_DOMAINS = ["@bancochile.cl", "@banchile.cl"]

# Emails recibidos hace más de estos minutos se ignoran (evita ruido al reiniciar)
MAX_AGE_MINUTES = 15

IDLE_TIMEOUT   = 29 * 60   # 29 min — el servidor puede cortar a los 30 min
RECONNECT_WAIT = 5          # segundos antes de reconectar tras un error

# ─── Logging ──────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


# ─── Telegram ─────────────────────────────────────────────────────────────────

def send_telegram(text: str) -> bool:
    """Envía un mensaje HTML a Telegram. Retorna True si fue exitoso."""
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
    }
    try:
        resp = requests.post(TELEGRAM_API_URL, json=payload, timeout=10)
        resp.raise_for_status()
        log.info("Notificacion Telegram enviada.")
        return True
    except requests.RequestException as exc:
        log.error(f"Error enviando a Telegram: {exc}")
        return False


# ─── Utilidades de email ───────────────────────────────────────────────────────

def decode_header_str(value: str) -> str:
    """Decodifica un header de email (base64, quoted-printable, etc.)."""
    parts = decode_header(value or "")
    result = []
    for part, charset in parts:
        if isinstance(part, bytes):
            result.append(part.decode(charset or "utf-8", errors="replace"))
        else:
            result.append(str(part))
    return "".join(result)


def get_body(msg: email.message.Message) -> str:
    """
    Extrae el cuerpo del email priorizando text/plain.
    Si solo hay HTML, lo retorna sin parsear (los patrones regex funcionan igual).
    """
    plain = ""
    html  = ""

    if msg.is_multipart():
        for part in msg.walk():
            ct  = part.get_content_type()
            cd  = str(part.get("Content-Disposition", ""))
            if "attachment" in cd:
                continue
            charset = part.get_content_charset() or "utf-8"
            payload = part.get_payload(decode=True)
            if payload is None:
                continue
            decoded = payload.decode(charset, errors="replace")
            if ct == "text/plain" and not plain:
                plain = decoded
            elif ct == "text/html" and not html:
                html = decoded
    else:
        charset = msg.get_content_charset() or "utf-8"
        payload = msg.get_payload(decode=True)
        if payload:
            plain = payload.decode(charset, errors="replace")

    return plain or html


def strip_html_tags(text: str) -> str:
    """Elimina tags HTML para facilitar el parseo con regex."""
    return re.sub(r"<[^>]+>", " ", text)


# ─── Detección y extracción ────────────────────────────────────────────────────

def is_banco_chile_alert(sender: str) -> bool:
    """Solo acepta emails cuyo remitente sea estrictamente del dominio Banco de Chile."""
    sender_l = sender.lower()
    return any(domain in sender_l for domain in BANCO_CHILE_DOMAINS)


def is_recent(date_str: str) -> bool:
    """Devuelve True si el email fue recibido hace menos de MAX_AGE_MINUTES minutos."""
    try:
        dt = email.utils.parsedate_to_datetime(date_str)
        # Normalizar a UTC para comparar
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - dt).total_seconds() / 60
        return age <= MAX_AGE_MINUTES
    except Exception:
        return True  # si no se puede parsear, dejar pasar


def extract_alert_info(subject: str, body: str) -> dict:
    """
    Extrae monto y comercio de una alerta del Banco de Chile.

    Retorna siempre un dict con claves 'monto' y 'comercio'
    (pueden ser 'N/D' si no se pudo extraer).
    """
    # Unir subject + body limpio para simplificar la búsqueda
    clean_body = strip_html_tags(body)
    text = f"{subject}\n{clean_body}"

    # ── Monto ──────────────────────────────────────────────────────────────────
    # Formatos frecuentes en alertas bancarias chilenas:
    #   "$ 1.234.567"  "$1.234"  "CLP 12.500"  "por $5.000"  "monto: $999"
    monto_patterns = [
        r'(?:monto|cargo|compra|valor)[:\s]+\$?\s*([\d\.,]+)',
        r'por\s+\$\s*([\d\.,]+)',
        r'\$\s*([\d\.,]+)',
        r'CLP\s+([\d\.,]+)',
        r'([\d\.,]+)\s*CLP',
    ]
    monto = None
    for pat in monto_patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            monto = m.group(1).strip()
            break

    # ── Comercio ───────────────────────────────────────────────────────────────
    # Formatos frecuentes: "en RIPLEY", "comercio: Falabella", "TIENDA: PedidosYa"
    comercio_patterns = [
        r'(?:comercio|establecimiento|tienda|local)[:\s]+([^\n\r<]{3,50})',
        r'en\s+([A-ZÁÉÍÓÚÑ][A-Za-záéíóúñÁÉÍÓÚÑ0-9 &\.\-]{2,40})(?:\s*[\n\r,\.\<])',
        r'(?:en|at)\s+([A-Z][A-Za-z0-9 &\.\-]{2,40})',
    ]
    comercio = None
    for pat in comercio_patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            comercio = m.group(1).strip()
            break

    return {
        "monto":    monto    or "N/D",
        "comercio": comercio or "N/D",
    }


DIAS_ES   = ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"]
MESES_ES  = ["enero", "febrero", "marzo", "abril", "mayo", "junio",
             "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre"]

def format_date_es(date_str: str) -> str:
    """Convierte un header Date de email a 'Miércoles 7 de abril'."""
    try:
        dt = email.utils.parsedate_to_datetime(date_str)
        dia   = DIAS_ES[dt.weekday()]
        mes   = MESES_ES[dt.month - 1]
        return f"{dia} {dt.day} de {mes}"
    except Exception:
        return date_str


def build_telegram_message(subject: str, sender: str, date: str, info: dict) -> str:
    monto      = info["monto"]
    fecha      = format_date_es(date)

    if monto == "N/D":
        return (
            f"<b>Alerta Banco de Chile</b>\n"
            f"<b>Fecha:</b> {fecha}\n"
            f"<b>Asunto:</b> {subject}\n\n"
            f"¿Qué compraste?\n"
            f"¿Dónde?"
        )

    return (
        f"<b>Alerta Banco de Chile</b>\n"
        f"<b>Monto:</b> ${monto}\n"
        f"<b>Fecha:</b> {fecha}\n\n"
        f"¿Qué compraste?\n"
        f"¿Dónde?"
    )


# ─── Procesamiento de mensajes ─────────────────────────────────────────────────

def process_uid(client: IMAPClient, uid: int) -> None:
    """Descarga y procesa un email por su UID."""
    try:
        data = client.fetch([uid], ["RFC822"])
        raw  = data[uid][b"RFC822"]
        msg  = email.message_from_bytes(raw)

        sender  = decode_header_str(msg.get("From", ""))
        subject = decode_header_str(msg.get("Subject", ""))
        date    = msg.get("Date", "")

        log.info(f"Nuevo mensaje — De: {sender!r} | Asunto: {subject!r}")

        if not is_banco_chile_alert(sender):
            log.debug("Remitente no es Banco de Chile, ignorado.")
            return

        if not is_recent(date):
            log.info("Email demasiado antiguo, ignorado.")
            return

        body = get_body(msg)
        info = extract_alert_info(subject, body)
        text = build_telegram_message(subject, sender, date, info)
        send_telegram(text)

    except Exception as exc:
        log.error(f"Error procesando UID {uid}: {exc}")


# ─── Loop principal con IMAP IDLE ─────────────────────────────────────────────

def monitor() -> None:
    if not GMAIL_USER or not GMAIL_PASSWORD:
        log.error(
            "Faltan credenciales. Exporta GMAIL_USER y GMAIL_PASSWORD "
            "como variables de entorno antes de ejecutar."
        )
        raise SystemExit(1)

    log.info(f"Conectando a {IMAP_HOST} como {GMAIL_USER} …")
    send_telegram("<b>Monitor Banco de Chile activo</b>")

    while True:  # loop de reconexion
        try:
            with IMAPClient(IMAP_HOST, port=IMAP_PORT, ssl=True) as client:
                client.login(GMAIL_USER, GMAIL_PASSWORD)
                client.select_folder("INBOX")
                log.info("Sesion abierta. Entrando en IDLE …")

                # Registrar el UID más alto actual como marca de agua.
                all_uids = client.search(["ALL"])
                watermark = max(all_uids) if all_uids else 0
                log.info(f"Marca de agua inicial: UID {watermark}.")

                # Recuperación al arrancar: procesar emails recientes perdidos durante
                # un posible reinicio (is_recent() filtra los más viejos de 15 min).
                today_str = datetime.now().strftime("%d-%b-%Y")
                startup_uids = client.search(["SINCE", today_str])
                if startup_uids:
                    log.info(f"Revisando {len(startup_uids)} email(s) de hoy al arrancar …")
                    for uid in startup_uids:
                        process_uid(client, uid)

                # Entrar en modo IDLE
                client.idle()
                idle_start = time.monotonic()

                while True:
                    elapsed   = time.monotonic() - idle_start
                    remaining = IDLE_TIMEOUT - elapsed

                    if remaining <= 0:
                        # Refrescar el IDLE antes de que el servidor corte
                        log.debug("Refrescando IDLE …")
                        client.idle_done()
                        client.idle()
                        idle_start = time.monotonic()
                        continue

                    # Esperar actividad del servidor (tick cada 60 s máximo).
                    # Aunque no llegue notificación IDLE, el timeout actúa como poll.
                    responses = client.idle_check(timeout=min(remaining, 60))
                    if responses:
                        log.debug(f"Respuesta IDLE: {responses}")

                    client.idle_done()

                    # Buscar siempre, con o sin evento IDLE
                    today_str = datetime.now().strftime("%d-%b-%Y")
                    all_current = client.search(["SINCE", today_str])
                    new_uids = [uid for uid in all_current if uid > watermark]
                    if new_uids:
                        log.info(f"{len(new_uids)} mensaje(s) nuevo(s) hoy.")
                    for uid in new_uids:
                        process_uid(client, uid)
                        watermark = max(watermark, uid)

                    client.idle()
                    idle_start = time.monotonic()

        except KeyboardInterrupt:
            log.info("Detenido por el usuario.")
            send_telegram("<b>Monitor Banco de Chile detenido</b>")
            break
        except Exception as exc:
            log.error(f"Error de conexion: {exc}. Reintentando en {RECONNECT_WAIT}s …")
            time.sleep(RECONNECT_WAIT)


# ─── Entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    monitor()
