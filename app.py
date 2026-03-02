"""
WhatsApp Chatbot para IT Support and Services SAC
==================================================
Bot que saluda, registra tickets de soporte y los guarda en Google Sheets.
Acumula mensajes múltiples en un solo ticket.
"""

import os
import json
import time
import threading
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

# Tiempo de espera (segundos) para acumular mensajes antes de crear el ticket
TIEMPO_ESPERA = 10

# Estado de conversaciones
conversaciones = {}

# Buffer de mensajes: {telefono: {"mensajes": [...], "timer": Timer}}
buffer_mensajes = {}
buffer_lock = threading.Lock()


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


def procesar_ticket(telefono):
    """
    Se ejecuta después de TIEMPO_ESPERA segundos.
    Junta todos los mensajes acumulados y crea un solo ticket.
    """
    with buffer_lock:
        if telefono not in buffer_mensajes:
            return
        mensajes = buffer_mensajes[telefono]["mensajes"]
        del buffer_mensajes[telefono]

    descripcion_completa = "\n".join(mensajes)
    numero_ticket = guardar_ticket(telefono, descripcion_completa)

    if numero_ticket > 0:
        resumen = descripcion_completa[:200]
        if len(descripcion_completa) > 200:
            resumen += "..."

        enviar_mensaje(
            telefono,
            f"✅ ¡Muchas gracias por contactarnos!\n\n"
            f"Su ticket *#{numero_ticket}* ha sido registrado exitosamente.\n\n"
            f"📋 *Resumen:*\n{resumen}\n\n"
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


# ============================================================
# RUTAS DE LA APLICACIÓN
# ============================================================

@app.route("/", methods=["GET"])
def home():
    return "🤖 Bot de IT Support and Services SAC está activo!", 200


@app.route("/privacy", methods=["GET"])
def privacy():
    return """
    <html><head><title>Política de Privacidad - IT Support and Services SAC</title></head>
    <body style="font-family:Arial;max-width:800px;margin:40px auto;padding:20px;">
    <h1>Política de Privacidad</h1>
    <p>IT Support and Services SAC recopila únicamente el número de teléfono y la descripción
    del problema proporcionada por el usuario para fines de soporte técnico.</p>
    <p>No compartimos esta información con terceros. Los datos se almacenan de forma segura
    y se utilizan exclusivamente para brindar el servicio solicitado.</p>
    <p>Contacto: IT Support and Services SAC</p>
    </body></html>
    """, 200


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
            return jsonify({"status": "ok"})
