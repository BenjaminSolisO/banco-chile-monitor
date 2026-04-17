# Monitor Banco de Chile

Bot que monitorea alertas de compra del Banco de Chile via IMAP IDLE y las notifica en Telegram, con registro automático en Google Sheets.

**Estado:** ✅ En producción en Railway

## 🎯 Cómo funciona

### Flujo general

1. **Compra realizada**: Banco de Chile envía email de alerta a tu Gmail
2. **Detección**: El bot escucha en tiempo real (IMAP IDLE) y detecta el email
3. **Extracción**: Saca automáticamente: monto, comercio, fecha
4. **Notificación**: Te envía un mensaje en Telegram con los datos
5. **Clasificación**: Tú respondes con lo que compraste y dónde
6. **Guardado**: El bot registra todo automáticamente en Google Sheets

### Ejemplo paso a paso

```
⏰ 19:57 — Realizas una compra en TOTTUS
↓
📧 Banco envía email: "Compra por $18.148 en TOTTUS CATEDRAL..."
↓
🤖 Bot detecta el email y envía a Telegram:
   "Alerta Banco de Chile
    Fecha: Lunes 15 de abril
    Monto: $18.148
    ¿Qué compraste?"
↓
✍️ Tú respondes: "fideos, leche y pan"  ← sin "/" porque TOTTUS es frecuente
↓
✅ Bot guarda en Google Sheets automáticamente con "TOTTUS" en ¿Dónde?
```

## 💡 Características principales

### Detección de comercios frecuentes

El bot reconoce comercios habituales de dos formas:

1. **Manual**: Los que agregas en la pestaña "Frecuentes" del Sheet
2. **Automático**: Comercios que repites 5+ veces en 30 días

**Resultado**: Para comercios frecuentes, el bot **solo pregunta "¿qué compraste?"** (sin dónde)

### Interacción con Telegram

**Comercio frecuente**:
```
Bot:  "¿Qué compraste?"
User: "fideos y leche"     ← sin "/" requerido
```

**Comercio nuevo**:
```
Bot:  "¿Qué compraste? / ¿Dónde?"
User: "ropa / Falabella"   ← con "/" requerido
```

### Ediciones en tiempo real
- ✏️ Edita tu respuesta en Telegram después de enviarla
- El bot detecta el edit y **actualiza automáticamente** la fila en Google Sheets
- Sin duplicados

### Técnicas
- ✅ **IMAP IDLE**: Escucha en tiempo real (sin polling)
- ✅ **Telegram long polling**: Detecta respuestas y ediciones
- ✅ **Regex inteligente**: Extrae monto y comercio automáticamente
- ✅ **Reconnect automático**: En caso de falla
- ✅ **Localización**: Fechas en español (Chile)
- ✅ **24/7**: Corre en Railway sin intervención

## 📊 Google Sheets — Estructura

### Pestaña "Sheet1" (datos de compras)

| Fecha | Monto | Comercio (banco) | ¿Qué compraste? | ¿Dónde? |
|---|---|---|---|---|
| Lunes 17 de abril | 45.990 | FALABELLA PORTAL | ropa | FALABELLA |
| Martes 18 de abril | 18.148 | TOTTUS CATEDRAL | fideos, leche | TOTTUS |

**Notas:**
- **Comercio (banco)**: Exacto del email del banco (puede incluir sucursal)
- **¿Dónde?**: 
  - Si es **frecuente** → nombre de tu lista (MAYÚSCULAS)
  - Si es **nuevo** → lo que escribas

### Pestaña "Frecuentes" (tu lista)

| Comercio |
|---|
| TOTTUS |
| JUMBO |
| UNIMARC |

Edita directamente en el Sheet. El bot reconoce automáticamente los comercios en esta lista.

## 📖 Guía de Uso

### Primer inicio
1. Configura credenciales (Gmail, Telegram, Google Sheets)
2. Inicia el bot
3. El bot crea automáticamente la pestaña "Frecuentes"

### Operación diaria

**Agregar un comercio a frecuentes:**
1. Abre tu Google Sheet
2. Ve a pestaña "Frecuentes"
3. Escribe en MAYÚSCULAS: `TOTTUS`, `JUMBO`, etc.
4. Listo — se reconoce automáticamente en próximos emails

**Editar una respuesta:**
1. En Telegram, edita el mensaje que respondiste
2. Cambia lo que compraste o dónde
3. El bot actualiza el Sheet automáticamente

**Detección automática:**
- El bot analiza tus últimos 30 días cada vez que inicia
- Si un comercio aparece 5+ veces → se detecta como frecuente automáticamente
- No necesitas hacer nada, sucede en background

### Verificar que funciona
Revisa los logs (local: terminal | Railway: Dashboard → Logs):
- `Conectando a imap.gmail.com...`
- `Comercios frecuentes cargados: X`
- `Notificacion Telegram enviada`
- `Gasto guardado en Google Sheets`

## 🔧 Configuración

### Gmail
1. **Activar IMAP**: Configuración → Reenvío e IMAP
2. **Activar 2FA**: Seguridad → Verificación en dos pasos
3. **Contraseña de aplicación**: myaccount.google.com → Contraseñas de aplicaciones → Correo + Windows

### Telegram
1. Busca [@BotFather](https://t.me/BotFather) en Telegram
2. `/newbot` → sigue instrucciones → copia TOKEN
3. Para CHAT_ID: `https://api.telegram.org/bot<TOKEN>/getUpdates` (envía mensaje al bot primero)

### Google Sheets
1. **Google Cloud Console** (https://console.cloud.google.com):
   - Activar Google Sheets API + Google Drive API
   - Crear cuenta de servicio → Descargar JSON
2. **Tu Sheet**: Compartir con `client_email` del JSON (Rol: Editor)
3. **JSON a una línea**: `python -c "import json; print(json.dumps(json.load(open('credenciales.json')), separators=(',', ':')))"

## 🚀 Instalación

### Local
```bash
git clone https://github.com/BenjaminSolisO/banco-chile-monitor.git
cd banco-chile-monitor
pip install -r requirements.txt
cp .env.example .env
# Edita .env con tus valores
python gmail_banco_chile_monitor.py
```

### Railway
1. Conecta repo a Railway
2. Agrega variables de entorno en Dashboard
3. Automático desde ahí

## 🛠️ Troubleshooting

| Problema | Causa | Solución |
|---|---|---|
| No recibe notificaciones | IMAP desactivado | Configuración Gmail → Activar IMAP |
| Token de Telegram incorrecto | Variable equivocada | BotFather → `/mybots` → API Token |
| Error al guardar en Sheets | Sheet no compartido | Compartir Sheet con `client_email` |
| Comercio no se detecta como frecuente | Mayúsculas diferentes | En "Frecuentes" escribe EN MAYÚSCULAS |
| El bot no se reinicia | Pestaña "Frecuentes" corrupta | Reinicia el bot (redeploy en Railway) |

## 📝 Licencia

MIT

---

**Desarrollado por**: Benjamín Solís  
**Última actualización**: 2026-04-17  
**Repo**: https://github.com/BenjaminSolisO/banco-chile-monitor
