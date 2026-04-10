# Monitor Banco de Chile

Bot que monitorea alertas de compra del Banco de Chile via IMAP y las notifica en Telegram, con registro automático en Google Sheets.

## Flujo

1. Llega un email de alerta del Banco de Chile a Gmail
2. El bot lo detecta y envía una notificación a Telegram con monto, fecha y comercio
3. El usuario responde con el formato `qué compraste / dónde` (ej: `ropa / Falabella`)
4. El bot guarda el gasto en Google Sheets

## Variables de entorno

| Variable | Descripción |
|---|---|
| `GMAIL_USER` | Tu dirección de Gmail |
| `GMAIL_PASSWORD` | Contraseña de aplicación de Google (16 caracteres) |
| `TELEGRAM_TOKEN` | Token del bot de Telegram |
| `TELEGRAM_CHAT_ID` | ID del chat donde se envían las notificaciones |
| `GOOGLE_SHEET_ID` | ID del Google Sheet donde se registran los gastos |
| `GOOGLE_CREDENTIALS_JSON` | Contenido del JSON de la cuenta de servicio de Google (en una sola línea) |

## Configuración

### Gmail

1. Activar IMAP: Configuración → Ver toda la configuración → Reenvío e IMAP
2. Activar verificación en 2 pasos en tu cuenta Google
3. Generar una contraseña de aplicación: myaccount.google.com → Seguridad → Contraseñas de aplicaciones

### Telegram

1. Crear un bot con [@BotFather](https://t.me/BotFather) y copiar el token
2. Obtener tu `CHAT_ID` iniciando conversación con el bot y consultando `https://api.telegram.org/bot<TOKEN>/getUpdates`

### Google Sheets

1. En [Google Cloud Console](https://console.cloud.google.com):
   - Activar **Google Sheets API** y **Google Drive API**
   - Crear una cuenta de servicio y descargar el JSON de credenciales
2. Crear un Google Sheet y compartirlo con el `client_email` del JSON (rol: Editor)
3. Copiar el ID del Sheet desde la URL: `https://docs.google.com/spreadsheets/d/**ID**/edit`
4. Convertir el JSON a una sola línea para la variable de entorno:
   ```bash
   python -c "import json,sys; print(json.dumps(json.load(open('credenciales.json'))))"
   ```

## Instalación local

```bash
pip install -r requirements.txt
export GMAIL_USER="tu@gmail.com"
export GMAIL_PASSWORD="xxxx xxxx xxxx xxxx"
export TELEGRAM_TOKEN="..."
export TELEGRAM_CHAT_ID="..."
export GOOGLE_SHEET_ID="..."
export GOOGLE_CREDENTIALS_JSON='{"type":"service_account",...}'
python gmail_banco_chile_monitor.py
```

## Deploy en Railway

El proyecto incluye un `Procfile` configurado para correr como worker:

```
worker: python gmail_banco_chile_monitor.py
```

Agrega todas las variables de entorno en el panel de Railway y despliega.

## Google Sheet generado

| Fecha | Monto | Comercio (banco) | ¿Qué compraste? | ¿Dónde? |
|---|---|---|---|---|
| Jueves 10 de abril | 15.990 | RIPLEY | ropa | Ripley |
