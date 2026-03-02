"""
WhatsApp Chatbot para IT Support and Services SAC
==================================================
Bot que saluda, registra tickets de soporte y los guarda en Google Sheets.
"""

import os
import json
from datetime import datetime
from flask import Flask, request, jsonify
import requests
import gspread
from google.oauth2.service_account import Credentials

app = Flask(__name__)

# ============================================================
# CONFIGURACIÓN
# ============================================================
VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN", "mi_token_secreto_123")
WHATSAPP_TOKEN = os.environ.get("WHATSAPP_TOKEN", "")
PHONE_NUMBER_ID = os.environ.get("PHONE_NUMBER_ID", "")
GOOGLE_CREDENTIALS_JSON = os.environ.get("GOOGLE_CREDENTIALS_JSON", "")
GOOGLE_SHEET_NAME = os.environ.get("GOOGLE_SHEET_NAME", "Tickets IT Support")

# Diccionario para rastrear en qué paso está cada conversación
conversaciones = {}


def conectar_google_sheets():
    """Conecta con Google Sheets usando las credenciales del service account."""
    try:
        creds_dict = json.loads(GOOGLE_CREDENTIALS_JSON)
        scopes = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        ]
        creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
        client = gspread.authorize(creds)
        return client
    except Exception as e:
        print(f"❌ Error conectando a Google Sheets: {e}")
        return None


def obtener_o_crear_hoja():
    """Obtiene la hoja de Google Sheets, o la crea si no existe."""
    client = conectar_google_sheets()
    if not client:
        return None

    try:
        sheet = client.open(GOOGLE_SHEET_NAME).sheet1
        return sheet
    except gspread.SpreadsheetNotFound:
        try:
            spreadsheet = client.create(GOOGLE_SHEET_NAME)
            sheet = spreadsheet.sheet1
            sheet.append_row(["#", "Fecha y Hora", "Teléfono del Cliente", "Descripción del Problema", "Estado"])
            sheet.format("A1:E1", {"textFormat": {"bold": True}})
            print(f"✅ Hoja '{GOOGLE_SHEET_NAME}' creada exitosamente.")
            return sheet
        except Exception as e:
            print(f"❌ Error creando la hoja: {e}")
            return None


def guardar_ticket(telefono, descripcion):
    """Guarda un nuevo ticket en Google Sheets."""
    sheet = obtener_o_crear_hoja()
    if not sheet:
        print("❌ No se pudo conectar a Google Sheets para guardar el ticket.")
        return 0

    try:
        todas_las_filas = sheet.get_all_values()
        numero_ticket = len(todas_las_filas)

        ahora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        nueva_fila = [numero_ticket, ahora, telefono, descripcion, "Pendiente"]
        sheet.append_row(nueva_fila)

        print(f"📝 Ticket #{numero_ticket} guardado: {telefono} - {descripcion[:50]}...")
        return numero_ticket
    except Exception as e:
        print(f"❌ Error guardando ticket: {e}")
        return 0


def enviar_mensaje(telefono, mensaje):
    """Envía un mensaje de WhatsApp usando la API de Meta."""
    url = f"https://graph.facebook.com/v22.0/{PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json",
    }
    data = {
        "messaging_product": "whatsapp",
        "to": telefono,
        "type": "text",
        "text": {"body": mensaje},
    }

    try:
        response = requests.post(url, headers=headers, json=data)
        response.raise_for_status()
        print(f"✅ Mensaje enviado a {telefono}")
    except requests.exceptions.RequestException as e:
        print(f"❌ Error enviando mensaje a {telefono}: {e}")
        if hasattr(e, "response") and e.response is not None:
            print(f"   Respuesta: {e.response.text}")


# ============================================================
# RUTAS DE LA APLICACIÓN
# ============================================================

@app.route("/", methods=["GET"])
def home():
    return "🤖 Bot de IT Support and Services SAC está activo!", 200


@app.route("/webhook", methods=["GET"])
def verificar_webhook():
    """Meta envía un GET para verificar el webhook."""
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")

    if mode == "subscribe" and token == VERIFY_TOKEN:
        print("✅ Webhook verificado exitosamente!")
        return challenge, 200
    else:
        print("❌ Verificación de webhook fallida.")
        return "Error de verificación", 403


@app.route("/webhook", methods=["POST"])
def recibir_mensaje():
    """Procesa mensajes entrantes de WhatsApp."""
    body = request.get_json()

    try:
        entry = body.get("entry", [{}])[0]
        changes = entry.get("changes", [{}])[0]
        value = changes.get("value", {})
        messages = value.get("messages", [])

        if not messages:
            return jsonify({"status": "ok"}), 200

        mensaje = messages[0]
        telefono = mensaje.get("from", "")
        tipo_mensaje = mensaje.get("type", "")

        if tipo_mensaje != "text":
            enviar_mensaje(
                telefono,
                "¡Hola! 👋 Bienvenido/a a *IT Support and Services SAC*.\n\n"
                "Por el momento solo podemos recibir mensajes de texto. "
                "Por favor, escríbenos tu consulta. 😊"
            )
            return jsonify({"status": "ok"}), 200

        texto = mensaje.get("text", {}).get("body", "").strip()

        # ========================================
        # FLUJO DE CONVERSACIÓN
        # ========================================

        if telefono not in conversaciones or conversaciones[telefono] is None:
            conversaciones[telefono] = "esperando_problema"

            enviar_mensaje(
                telefono,
                "¡Hola! 👋 Bienvenido/a a *IT Support and Services SAC*.\n\n"
                "Somos su aliado en soporte técnico y servicios de TI.\n\n"
                "Por favor, descríbanos el problema o consulta que tiene "
                "y con gusto le ayudaremos. 💻🔧"
            )

        elif conversaciones[telefono] == "esperando_problema":
            numero_ticket = guardar_ticket(telefono, texto)

            if numero_ticket > 0:
                enviar_mensaje(
                    telefono,
                    f"✅ ¡Muchas gracias por contactarnos!\n\n"
                    f"Su ticket *#{numero_ticket}* ha sido registrado exitosamente.\n\n"
                    f"📋 *Resumen:*\n{texto[:200]}{'...' if len(texto) > 200 else ''}\n\n"
                    f"Un miembro de nuestro equipo se pondrá en contacto con usted "
                    f"a la brevedad posible.\n\n"
                    f"¡Gracias por confiar en *IT Support and Services SAC*! 🙏"
                )
            else:
                enviar_mensaje(
                    telefono,
                    "Hemos recibido su mensaje. Nuestro equipo se pondrá en contacto "
                    "con usted pronto.\n\n¡Gracias por contactar a *IT Support and Services SAC*! 🙏"
                )

            conversaciones[telefono] = None

    except Exception as e:
        print(f"❌ Error procesando mensaje: {e}")
        import traceback
        traceback.print_exc()

    return jsonify({"status": "ok"}), 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
