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
import json
import logging
import os
import re
import threading
import time
from email.header import decode_header
from datetime import datetime, timezone, timedelta

import gspread
import requests
from google.oauth2.service_account import Credentials
from imapclient import IMAPClient

# ─── Configuración ────────────────────────────────────────────────────────────

IMAP_HOST = "imap.gmail.com"
IMAP_PORT = 993

GMAIL_USER     = os.environ.get("GMAIL_USER", "")      # tu@gmail.com
GMAIL_PASSWORD = os.environ.get("GMAIL_PASSWORD", "")  # contraseña de aplicación

TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
TELEGRAM_API_URL = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

GOOGLE_CREDENTIALS_JSON = os.environ.get("GOOGLE_CREDENTIALS_JSON", "")
GOOGLE_SHEET_ID         = os.environ.get("GOOGLE_SHEET_ID", "")

# Dominios aceptados — solo emails cuyo remitente termine en uno de estos
BANCO_CHILE_DOMAINS = ["@bancochile.cl", "@banchile.cl"]

# Emails recibidos hace más de estos minutos se ignoran (evita ruido al reiniciar)
MAX_AGE_MINUTES = 15

IDLE_TIMEOUT   = 29 * 60   # 29 min — el servidor puede cortar a los 30 min
RECONNECT_WAIT = 5          # segundos antes de reconectar tras un error

# ─── Estado global (compra pendiente de clasificar) ───────────────────────────

pending_purchase = None   # dict: {fecha, monto, comercio}
pending_lock     = threading.Lock()

# ─── Logging ──────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


# ─── Google Sheets ────────────────────────────────────────────────────────────

SHEET_HEADER = ["Fecha", "Monto", "Comercio (banco)", "¿Qué compraste?", "¿Dónde?"]


def get_sheet():
    scopes     = ["https://www.googleapis.com/auth/spreadsheets"]
    creds_dict = json.loads(GOOGLE_CREDENTIALS_JSON)
    creds      = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    gc         = gspread.authorize(creds)
    return gc.open_by_key(GOOGLE_SHEET_ID).sheet1


def ensure_header(sheet) -> None:
    """Crea la fila de encabezado si la hoja está vacía."""
    if not sheet.row_values(1):
        sheet.append_row(SHEET_HEADER)


def save_to_sheets(purchase: dict, que: str, donde: str) -> bool:
    try:
        sheet = get_sheet()
        ensure_header(sheet)
        sheet.append_row([
            purchase["fecha"],
            purchase["monto"],
            purchase["comercio"],
            que,
            donde,
        ])
        log.info("Gasto guardado en Google Sheets.")
        return True
    except Exception as exc:
        log.error(f"Error guardando en Sheets: {exc}")
        return False


def update_in_sheets(purchase: dict, que: str, donde: str) -> bool:
    try:
        sheet = get_sheet()
        all_rows = sheet.get_all_values()

        log.info(f"Buscando: fecha='{purchase['fecha']}' monto='{purchase['monto']}' comercio='{purchase['comercio']}'")
        log.info(f"Total de filas en Sheet: {len(all_rows)}")

        for idx, row in enumerate(all_rows[1:], start=2):
            log.debug(f"Fila {idx}: {row[0:3] if len(row) >= 3 else row}")
            if (len(row) >= 3 and
                row[0] == purchase["fecha"] and
                row[1] == purchase["monto"] and
                row[2] == purchase["comercio"]):
                sheet.update_cell(idx, 4, que)
                sheet.update_cell(idx, 5, donde)
                log.info(f"Gasto actualizado en Google Sheets (fila {idx}).")
                return True

        log.warning("No se encontró el registro anterior para actualizar.")
        return False
    except Exception as exc:
        log.error(f"Error actualizando en Sheets: {exc}")
        return False


# ─── Telegram ─────────────────────────────────────────────────────────────────

def send_telegram(text: str) -> bool:
    """Envía un mensaje HTML a Telegram. Retorna True si fue exitoso."""
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
    }
    try:
        resp = requests.post(f"{TELEGRAM_API_URL}/sendMessage", json=payload, timeout=10)
        resp.raise_for_status()
        log.info("Notificacion Telegram enviada.")
        return True
    except requests.RequestException as exc:
        log.error(f"Error enviando a Telegram: {exc}")
        return False


def get_telegram_updates(offset: int) -> list:
    try:
        resp = requests.get(
            f"{TELEGRAM_API_URL}/getUpdates",
            params={"offset": offset, "timeout": 30},
            timeout=35,
        )
        resp.raise_for_status()
        return resp.json().get("result", [])
    except requests.RequestException:
        return []


def handle_reply(text: str, is_edit: bool = False) -> None:
    global pending_purchase

    with pending_lock:
        if pending_purchase is None:
            return  # no hay compra pendiente, ignorar mensaje

        if "/" not in text:
            send_telegram(
                "Formato incorrecto. Responde así:\n"
                "<code>qué compraste / dónde</code>\n"
                "Ej: <code>ropa / Falabella</code>"
            )
            return

        parts    = text.split("/", 1)
        que      = parts[0].strip()
        donde    = parts[1].strip()
        purchase = pending_purchase

        if is_edit:
            pending_purchase = None
            ok = update_in_sheets(purchase, que, donde)
            if ok:
                send_telegram(
                    f"Actualizado en Google Sheets.\n"
                    f"<b>{que}</b> en <b>{donde}</b>"
                )
            else:
                send_telegram("Error al actualizar. Intenta de nuevo.")
                with pending_lock:
                    pending_purchase = purchase
        else:
            pending_purchase = None
            ok = save_to_sheets(purchase, que, donde)
            if ok:
                send_telegram(
                    f"Guardado en Google Sheets.\n"
                    f"<b>{que}</b> en <b>{donde}</b> — <b>${purchase['monto']}</b>"
                )
            else:
                send_telegram("Error al guardar en Google Sheets. Intenta responder de nuevo.")
                with pending_lock:
                    pending_purchase = purchase


def telegram_polling() -> None:
    """Hilo que escucha respuestas del usuario en Telegram (long polling)."""
    log.info("Polling de Telegram iniciado.")
    last_update_id = 0
    while True:
        updates = get_telegram_updates(last_update_id)
        for upd in updates:
            last_update_id = upd["update_id"] + 1

            # Detectar mensajes nuevos
            msg = upd.get("message")
            if msg:
                text    = msg.get("text", "").strip()
                chat_id = str(msg.get("chat", {}).get("id", ""))
                if chat_id == str(TELEGRAM_CHAT_ID) and text:
                    log.info(f"Mensaje nuevo recibido: {text}")
                    handle_reply(text, is_edit=False)

            # Detectar mensajes editados
            edited_msg = upd.get("edited_message")
            if edited_msg:
                text    = edited_msg.get("text", "").strip()
                chat_id = str(edited_msg.get("chat", {}).get("id", ""))
                if chat_id == str(TELEGRAM_CHAT_ID) and text:
                    log.info(f"Mensaje EDITADO recibido: {text}")
                    handle_reply(text, is_edit=True)

        time.sleep(2)


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


DIAS_ES    = ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"]
MESES_ES   = ["enero", "febrero", "marzo", "abril", "mayo", "junio",
              "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre"]
MESES_IMAP = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
              "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def imap_since() -> str:
    """Fecha de ayer en formato IMAP (ej: 08-Apr-2026), sin depender del locale del sistema.
    Usar ayer en vez de hoy cubre el desfase entre UTC del servidor y la hora local de Chile:
    emails que llegan antes de medianoche UTC tienen fecha interna del día anterior."""
    d = datetime.now() - timedelta(days=1)
    return f"{d.day:02d}-{MESES_IMAP[d.month - 1]}-{d.year}"

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
    monto = info["monto"]
    fecha = format_date_es(date)

    base = f"<b>Alerta Banco de Chile</b>\n<b>Fecha:</b> {fecha}\n"
    if monto != "N/D":
        base += f"<b>Monto:</b> ${monto}\n"
    else:
        base += f"<b>Asunto:</b> {subject}\n"

    base += "\nResponde: <code>qué compraste / dónde</code>"
    return base


# ─── Procesamiento de mensajes ─────────────────────────────────────────────────

def process_uid(client: IMAPClient, uid: int, check_age: bool = True) -> None:
    """Descarga y procesa un email por su UID.
    check_age=False en el loop de poll (ya filtrado por watermark).
    check_age=True en startup para no reprocesar emails viejos.
    """
    global pending_purchase
    try:
        data = client.fetch([uid], ["RFC822"])
        raw  = data[uid][b"RFC822"]
        msg  = email.message_from_bytes(raw)

        sender  = decode_header_str(msg.get("From", ""))
        subject = decode_header_str(msg.get("Subject", ""))
        date    = msg.get("Date", "")

        log.info(f"UID {uid} — De: {sender!r} | Asunto: {subject!r}")

        if not is_banco_chile_alert(sender):
            log.info(f"UID {uid} — remitente no es Banco de Chile, ignorado.")
            return

        if check_age and not is_recent(date):
            log.info(f"UID {uid} — email demasiado antiguo, ignorado.")
            return

        body = get_body(msg)
        info = extract_alert_info(subject, body)
        log.info(f"UID {uid} — alerta detectada: monto={info['monto']!r} comercio={info['comercio']!r}")

        with pending_lock:
            pending_purchase = {
                "fecha":    format_date_es(date),
                "monto":    info["monto"],
                "comercio": info["comercio"],
            }

        text = build_telegram_message(subject, sender, date, info)
        send_telegram(text)

    except Exception as exc:
        log.error(f"Error procesando UID {uid}: {exc}")


# ─── Loop principal con IMAP IDLE ─────────────────────────────────────────────

def monitor() -> None:
    if not GMAIL_USER or not GMAIL_PASSWORD:
        log.error("Faltan credenciales Gmail (GMAIL_USER, GMAIL_PASSWORD).")
        raise SystemExit(1)

    if not GOOGLE_CREDENTIALS_JSON or not GOOGLE_SHEET_ID:
        log.error("Faltan variables GOOGLE_CREDENTIALS_JSON o GOOGLE_SHEET_ID.")
        raise SystemExit(1)

    # Arrancar hilo de polling de Telegram (escucha respuestas del usuario)
    t = threading.Thread(target=telegram_polling, daemon=True)
    t.start()

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
                startup_uids = client.search(["SINCE", imap_since()])
                if startup_uids:
                    log.info(f"Revisando {len(startup_uids)} email(s) de hoy al arrancar …")
                    for uid in startup_uids:
                        process_uid(client, uid, check_age=True)

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
                    log.info(f"idle_check: {'evento recibido' if responses else 'timeout 60s'}")

                    client.idle_done()

                    # Buscar siempre, con o sin evento IDLE
                    all_current = client.search(["SINCE", imap_since()])
                    new_uids = [uid for uid in all_current if uid > watermark]
                    log.info(f"Poll: {len(all_current)} email(s) hoy, {len(new_uids)} nuevo(s) (watermark={watermark}).")
                    for uid in new_uids:
                        process_uid(client, uid, check_age=False)
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
