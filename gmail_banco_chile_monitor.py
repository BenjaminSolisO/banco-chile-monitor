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

# ─── Estado global (compras pendientes de clasificar) ──────────────────────────

pending_purchases = {}   # dict: message_id -> {fecha, monto, comercio, is_frequent, frequent_name}
last_purchase    = None   # dict: última compra guardada (para edits)
pending_lock      = threading.Lock()

# ─── Comercios frecuentes ──────────────────────────────────────────────────────

frequent_merchants = set()        # cargado desde Sheet "Frecuentes" (manual)
auto_frequent      = dict()       # {comercio: count} — detectados automáticamente (5+ en 30 días)
frequent_lock      = threading.Lock()

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


def get_frequent_sheet():
    """Retorna (o crea) la hoja 'Frecuentes' del spreadsheet."""
    scopes     = ["https://www.googleapis.com/auth/spreadsheets"]
    creds_dict = json.loads(GOOGLE_CREDENTIALS_JSON)
    creds      = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    gc         = gspread.authorize(creds)
    ss         = gc.open_by_key(GOOGLE_SHEET_ID)
    try:
        return ss.worksheet("Frecuentes")
    except gspread.exceptions.WorksheetNotFound:
        ws = ss.add_worksheet(title="Frecuentes", rows=100, cols=1)
        ws.append_row(["Comercio"])
        log.info("Hoja 'Frecuentes' creada.")
        return ws


def ensure_header(sheet) -> None:
    """Crea la fila de encabezado si la hoja está vacía."""
    if not sheet.row_values(1):
        sheet.append_row(SHEET_HEADER)


def load_frequent_merchants() -> None:
    """Carga la lista de comercios frecuentes desde el Sheet."""
    global frequent_merchants
    try:
        ws = get_frequent_sheet()
        values = ws.col_values(1)
        loaded = {v.strip().upper() for v in values[1:] if v.strip()}
        with frequent_lock:
            frequent_merchants = loaded
        log.info(f"Comercios frecuentes cargados: {len(frequent_merchants)} comercios.")
    except Exception as exc:
        log.error(f"Error cargando comercios frecuentes: {exc}")


def analyze_recent_purchases() -> None:
    """Analiza los últimos gastos (30 días) y detecta comercios que aparecen 5+ veces."""
    global auto_frequent
    try:
        sheet = get_sheet()
        all_rows = sheet.get_all_values()

        # Contar comercios en los últimos 30 días
        from datetime import timedelta
        cutoff_date = datetime.now() - timedelta(days=30)
        comercio_count = {}

        for row in all_rows[1:]:  # skip header
            if len(row) < 3 or not row[2]:
                continue

            comercio = row[2].strip().upper()

            # Intentar parsear la fecha (formato: "Lunes 15 de abril")
            try:
                fecha_str = row[0]
                # Extraer día y mes del formato español
                import re
                m = re.search(r'(\d+)\s+de\s+(\w+)', fecha_str)
                if m:
                    day = int(m.group(1))
                    month_name = m.group(2)
                    # Mapear mes español a número
                    months = {"enero": 1, "febrero": 2, "marzo": 3, "abril": 4,
                              "mayo": 5, "junio": 6, "julio": 7, "agosto": 8,
                              "septiembre": 9, "octubre": 10, "noviembre": 11, "diciembre": 12}
                    month = months.get(month_name.lower(), 0)
                    if month:
                        # Asumir año actual
                        fecha = datetime(datetime.now().year, month, day)
                        if fecha >= cutoff_date:
                            comercio_count[comercio] = comercio_count.get(comercio, 0) + 1
            except Exception:
                continue

        # Guardar solo comercios con 5+ apariciones
        detected = {c: count for c, count in comercio_count.items() if count >= 5}
        with frequent_lock:
            auto_frequent = detected

        log.info(f"Comercios automáticos detectados (5+ en 30 días): {detected}")
    except Exception as exc:
        log.error(f"Error analizando compras recientes: {exc}")


def find_frequent(comercio: str) -> tuple:
    """Busca si el comercio está en la lista de frecuentes (manual o automático).
    Retorna (is_frequent: bool, nombre_limpio: str)."""
    if not comercio or comercio == "N/D":
        return False, ""
    c = comercio.upper()

    with frequent_lock:
        # Buscar en lista manual
        for f in frequent_merchants:
            if f in c:
                return True, f

        # Buscar en comercios automáticos
        for auto_c in auto_frequent.keys():
            if auto_c in c:
                return True, auto_c

    return False, ""


def clean_monto(raw: str):
    """Convierte un monto en formato chileno ('1.400,50' o '1.400') a número (float o int).
    Retorna el número, o el string original si no se puede convertir."""
    if not raw or raw == "N/D":
        return raw
    try:
        s = raw.strip().lstrip("$").strip()
        if "," in s:
            s = s.replace(".", "").replace(",", ".")
            return float(s)
        else:
            s = s.replace(".", "").replace(",", ".")
            return int(s) if s.isdigit() else float(s)
    except (ValueError, AttributeError):
        return raw


def save_to_sheets(purchase: dict, que: str, donde: str) -> bool:
    try:
        sheet = get_sheet()
        ensure_header(sheet)
        monto_num = clean_monto(purchase["monto"])
        sheet.append_row([
            purchase["fecha"],
            monto_num,
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
    log.info(f"update_in_sheets INICIADA")
    try:
        log.info(f"update_in_sheets: Conectando a Sheet...")
        sheet = get_sheet()
        log.info(f"update_in_sheets: Obteniendo todas las filas...")
        all_rows = sheet.get_all_values()

        log.info(f"update_in_sheets: Buscando: fecha='{purchase['fecha']}' monto='{purchase['monto']}' comercio='{purchase['comercio']}'")
        log.info(f"update_in_sheets: Total de filas en Sheet: {len(all_rows)}")

        for idx, row in enumerate(all_rows[1:], start=2):
            log.debug(f"update_in_sheets: Fila {idx}: {row[0:3] if len(row) >= 3 else row}")
            if (len(row) >= 3 and
                row[0] == purchase["fecha"] and
                str(clean_monto(row[1])) == str(clean_monto(purchase["monto"])) and
                row[2] == purchase["comercio"]):
                log.info(f"update_in_sheets: ¡ENCONTRADA! Fila {idx}. Actualizando columnas 4 y 5...")
                sheet.update_cell(idx, 4, que)
                sheet.update_cell(idx, 5, donde)
                log.info(f"update_in_sheets: ✅ Gasto actualizado en Google Sheets (fila {idx}).")
                return True

        log.warning(f"update_in_sheets: ❌ No se encontró el registro anterior para actualizar.")
        log.warning(f"update_in_sheets: Primeras 3 filas del Sheet:")
        for idx, row in enumerate(all_rows[:3], start=1):
            log.warning(f"  Fila {idx}: {row}")
        return False
    except Exception as exc:
        log.error(f"update_in_sheets: ❌ Error: {exc}", exc_info=True)
        return False


# ─── Telegram ─────────────────────────────────────────────────────────────────

def send_telegram(text: str):
    """Envía un mensaje HTML a Telegram. Retorna el message_id si fue exitoso, None si falló."""
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
    }
    try:
        resp = requests.post(f"{TELEGRAM_API_URL}/sendMessage", json=payload, timeout=10)
        resp.raise_for_status()
        log.info("Notificacion Telegram enviada.")
        return resp.json().get("result", {}).get("message_id")
    except requests.RequestException as exc:
        log.error(f"Error enviando a Telegram: {exc}")
        return None


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


def parse_notification_text(raw: str) -> dict:
    """Extrae fecha, monto y comercio del texto de una notificación enviada por el bot.
    Retorna None si no se puede parsear."""
    try:
        fecha = monto = comercio = None
        for line in raw.split("\n"):
            line = line.strip()
            if line.startswith("Fecha:"):
                fecha = line[len("Fecha:"):].strip()
            elif line.startswith("Monto:"):
                monto = line[len("Monto:"):].strip().lstrip("$")
            elif line.startswith("Comercio:"):
                comercio = line[len("Comercio:"):].strip()
            elif line.startswith("Asunto:") and monto is None:
                monto = "N/D"

        if not fecha or not monto:
            return None

        comercio = comercio or "N/D"
        is_freq, freq_name = find_frequent(comercio)

        return {
            "fecha":         fecha,
            "monto":         monto,
            "comercio":      comercio,
            "is_frequent":   is_freq,
            "frequent_name": freq_name,
        }
    except Exception:
        return None


def handle_reply(text: str, reply_to_msg_id: int = None, is_edit: bool = False,
                reply_to_text: str = None) -> None:
    global pending_purchases, last_purchase

    with pending_lock:
        if is_edit:
            source = last_purchase
            target_msg_id = None
        elif reply_to_msg_id and reply_to_msg_id in pending_purchases:
            target_msg_id = reply_to_msg_id
            source = pending_purchases[target_msg_id]
        elif pending_purchases:
            target_msg_id = max(pending_purchases.keys())
            source = pending_purchases[target_msg_id]
        else:
            source = None
            target_msg_id = None

        log.debug(f"handle_reply: is_edit={is_edit}, source is None={source is None}")
        if source:
            log.debug(f"  source = fecha:{source.get('fecha')} monto:{source.get('monto')} comercio:{source.get('comercio')}")

        if source is None and reply_to_text and not is_edit:
            parsed = parse_notification_text(reply_to_text)
            if parsed:
                log.info(f"handle_reply: fallback — compra reconstruida desde reply: {parsed}")
                source = parsed
                target_msg_id = None  # no hay msg_id real, no se puede restaurar

        if source is None:
            log.info(f"handle_reply: No hay compra para procesar (is_edit={is_edit})")
            if not is_edit:
                send_telegram("No hay compras pendientes para registrar. ¿Reinició el bot?")
            return

        # ── Formato según tipo de compra ────────────────────────────────────────
        if source.get("is_frequent"):
            que = text.strip()
            donde = source.get("frequent_name", "")
            log.info(f"handle_reply: Comercio frecuente detectado: {que} / {donde}")
        else:
            if "/" not in text:
                send_telegram(
                    "Formato incorrecto. Responde así:\n"
                    "<code>qué compraste / dónde</code>\n"
                    "Ej: <code>ropa / Falabella</code>"
                )
                return
            parts = text.split("/", 1)
            que = parts[0].strip()
            donde = parts[1].strip()

        if not is_edit and target_msg_id is not None:
            pending_purchases.pop(target_msg_id, None)

    # ── Guardar o actualizar ──────────────────────────────────────────────────
    if is_edit:
        ok = update_in_sheets(source, que, donde)
        if ok:
            send_telegram(
                f"Actualizado en Google Sheets.\n"
                f"<b>{que}</b> en <b>{donde}</b>"
            )
        else:
            send_telegram("Error al actualizar. Intenta de nuevo.")
    else:
        ok = save_to_sheets(source, que, donde)
        if ok:
            last_purchase = source
            send_telegram(
                f"Guardado en Google Sheets.\n"
                f"<b>{que}</b> en <b>{donde}</b> — <b>${source['monto']}</b>"
            )
        else:
            send_telegram("Error al guardar en Google Sheets. Intenta responder de nuevo.")
            if target_msg_id is not None:
                with pending_lock:
                    pending_purchases[target_msg_id] = source


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
                    reply_to = msg.get("reply_to_message", {})
                    reply_to_msg_id = reply_to.get("message_id") if reply_to else None
                    reply_to_text = reply_to.get("text") if reply_to else None
                    try:
                        handle_reply(text, reply_to_msg_id=reply_to_msg_id, reply_to_text=reply_to_text)
                    except Exception as exc:
                        log.error(f"Error en handle_reply: {exc}", exc_info=True)

            # Detectar mensajes editados
            edited_msg = upd.get("edited_message")
            if edited_msg:
                text    = edited_msg.get("text", "").strip()
                chat_id = str(edited_msg.get("chat", {}).get("id", ""))
                if chat_id == str(TELEGRAM_CHAT_ID) and text:
                    log.info(f"Mensaje EDITADO recibido: {text}")
                    try:
                        handle_reply(text, is_edit=True)
                    except Exception as exc:
                        log.error(f"Error en handle_reply (edit): {exc}", exc_info=True)
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


def build_telegram_message(subject: str, sender: str, date: str, info: dict, frequent: bool = False) -> str:
    monto = info["monto"]
    comercio = info.get("comercio", "N/D")
    fecha = format_date_es(date)

    base = f"<b>Alerta Banco de Chile</b>\n<b>Fecha:</b> {fecha}\n"
    if monto != "N/D":
        base += f"<b>Monto:</b> ${monto}\n"
    else:
        base += f"<b>Asunto:</b> {subject}\n"
    if comercio != "N/D":
        base += f"<b>Comercio:</b> {comercio}\n"

    if frequent:
        base += "\nResponde: <code>qué compraste</code>"
    else:
        base += "\nResponde: <code>qué compraste / dónde</code>"
    return base


# ─── Procesamiento de mensajes ─────────────────────────────────────────────────

def process_uid(client: IMAPClient, uid: int, check_age: bool = True) -> None:
    """Descarga y procesa un email por su UID.
    check_age=False en el loop de poll (ya filtrado por watermark).
    check_age=True en startup para no reprocesar emails viejos.
    """
    global pending_purchases
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

        is_freq, freq_name = find_frequent(info["comercio"])

        text = build_telegram_message(subject, sender, date, info, frequent=is_freq)
        msg_id = send_telegram(text)
        if msg_id:
            with pending_lock:
                pending_purchases[msg_id] = {
                    "fecha":         format_date_es(date),
                    "monto":         info["monto"],
                    "comercio":      info["comercio"],
                    "is_frequent":   is_freq,
                    "frequent_name": freq_name,
                }

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
    load_frequent_merchants()
    analyze_recent_purchases()

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
