import os
import json
import threading
import base64
from datetime import datetime
from flask import Flask, request, jsonify
import requests as http_requests
import gspread
from google.oauth2.service_account import Credentials
from google.auth.transport.requests import Request as GoogleAuthRequest

app = Flask(__name__)

VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN", "mi_token_secreto_123")
WHATSAPP_TOKEN = os.environ.get("WHATSAPP_TOKEN", "")
PHONE_NUMBER_ID = os.environ.get("PHONE_NUMBER_ID", "")
GOOGLE_CREDENTIALS_JSON = os.environ.get("GOOGLE_CREDENTIALS_JSON", "")
GOOGLE_SHEET_NAME = os.environ.get("GOOGLE_SHEET_NAME", "Tickets IT Support")
IMGBB_API_KEY = os.environ.get("IMGBB_API_KEY", "")

TIEMPO_ESPERA = 10

conversaciones = {}
buffer_mensajes = {}
buffer_lock = threading.Lock()


def get_google_creds():
    creds_dict = json.loads(GOOGLE_CREDENTIALS_JSON)
    scopes = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    return creds


def conectar_google_sheets():
    try:
        creds = get_google_creds()
        client = gspread.authorize(creds)
        return client
    except Exception as e:
        print(f"Error conectando a Google Sheets: {e}")
        return None


def subir_imagen_a_imgbb(image_data, filename):
    try:
        image_base64 = base64.b64encode(image_data).decode("utf-8")
        url = "https://api.imgbb.com/1/upload"
        payload = {
            "key": IMGBB_API_KEY,
            "image": image_base64,
            "name": filename,
        }
        resp = http_requests.post(url, data=payload)
        print(f"Respuesta de imgbb: {resp.status_code}")
        resp.raise_for_status()
        result = resp.json()
        if result.get("success"):
            link = result["data"]["url"]
            print(f"Imagen subida a imgbb: {link}")
            return link
        else:
            print(f"Error en imgbb: {result}")
            return None
    except Exception as e:
        print(f"Error subiendo imagen a imgbb: {e}")
        import traceback
        traceback.print_exc()
        return None


def descargar_media_whatsapp(media_id):
    try:
        url = f"https://graph.facebook.com/v22.0/{media_id}"
        headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}"}
        response = http_requests.get(url, headers=headers)
        response.raise_for_status()
        media_url = response.json().get("url")
        if not media_url:
            return None
        media_response = http_requests.get(media_url, headers=headers)
        media_response.raise_for_status()
        return media_response.content
    except Exception as e:
        print(f"Error descargando media: {e}")
        return None


def obtener_o_crear_hoja():
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
            sheet.append_row(["#", "Fecha y Hora", "Telefono del Cliente", "Descripcion del Problema", "Estado", "Imagenes"])
            return sheet
        except Exception as e:
            print(f"Error creando la hoja: {e}")
            return None


def guardar_ticket(telefono, descripcion, imagenes=None):
    sheet = obtener_o_crear_hoja()
    if not sheet:
        return 0
    try:
        todas_las_filas = sheet.get_all_values()
        numero_ticket = len(todas_las_filas)
        ahora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        links_imagenes = ""
        if imagenes:
            links_imagenes = "\n".join(imagenes)
        nueva_fila = [numero_ticket, ahora, telefono, descripcion, "Pendiente", links_imagenes]
        sheet.append_row(nueva_fila)
        print(f"Ticket #{numero_ticket} guardado: {telefono}")
        return numero_ticket
    except Exception as e:
        print(f"Error guardando ticket: {e}")
        return 0


def enviar_mensaje(telefono, mensaje):
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
        response = http_requests.post(url, headers=headers, json=data)
        response.raise_for_status()
        print(f"Mensaje enviado a {telefono}")
    except http_requests.exceptions.RequestException as e:
        print(f"Error enviando mensaje a {telefono}: {e}")
        if hasattr(e, "response") and e.response is not None:
            print(f"Respuesta: {e.response.text}")


def procesar_ticket(telefono):
    with buffer_lock:
        if telefono not in buffer_mensajes:
            return
        mensajes = buffer_mensajes[telefono]["mensajes"]
        imagenes = buffer_mensajes[telefono].get("imagenes", [])
        del buffer_mensajes[telefono]
    descripcion_completa = "\n".join(mensajes) if mensajes else "(Solo imagenes)"
    numero_ticket = guardar_ticket(telefono, descripcion_completa, imagenes if imagenes else None)
    if numero_ticket > 0:
        resumen = descripcion_completa[:200]
        if len(descripcion_completa) > 200:
            resumen += "..."
        img_texto = ""
        if imagenes:
            img_texto = f"\n\n{len(imagenes)} imagen(es) adjunta(s)"
        enviar_mensaje(
            telefono,
            f"Muchas gracias por contactarnos!\n\nSu ticket *#{numero_ticket}* ha sido registrado exitosamente.\n\nResumen:\n{resumen}{img_texto}\n\nUn miembro de nuestro equipo se pondra en contacto con usted a la brevedad posible.\n\nGracias por confiar en *IT Support and Services SAC*!"
        )
    else:
        enviar_mensaje(
            telefono,
            "Hemos recibido su mensaje. Nuestro equipo se pondra en contacto con usted pronto.\n\nGracias por contactar a *IT Support and Services SAC*!"
        )
    conversaciones[telefono] = None


def agregar_al_buffer(telefono, texto=None, imagen_link=None):
    with buffer_lock:
        if telefono not in buffer_mensajes:
            buffer_mensajes[telefono] = {"mensajes": [], "imagenes": [], "timer": None}
        if buffer_mensajes[telefono]["timer"]:
            buffer_mensajes[telefono]["timer"].cancel()
        if texto:
            buffer_mensajes[telefono]["mensajes"].append(texto)
        if imagen_link:
            buffer_mensajes[telefono]["imagenes"].append(imagen_link)
        buffer_mensajes[telefono]["timer"] = threading.Timer(TIEMPO_ESPERA, procesar_ticket, args=[telefono])
        buffer_mensajes[telefono]["timer"].start()


@app.route("/", methods=["GET"])
def home():
    return "Bot de IT Support and Services SAC esta activo!", 200


@app.route("/privacy", methods=["GET"])
def privacy():
    return "<html><body><h1>Politica de Privacidad</h1><p>IT Support and Services SAC recopila unicamente el numero de telefono y la descripcion del problema proporcionada por el usuario para fines de soporte tecnico. No compartimos esta informacion con terceros.</p></body></html>", 200


@app.route("/webhook", methods=["GET"])
def verificar_webhook():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    if mode == "subscribe" and token == VERIFY_TOKEN:
        print("Webhook verificado!")
        return challenge, 200
    else:
        return "Error de verificacion", 403


@app.route("/webhook", methods=["POST"])
def recibir_mensaje():
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
        if telefono not in conversaciones or conversaciones[telefono] is None:
            conversaciones[telefono] = "esperando_problema"
            enviar_mensaje(telefono, "Hola! Bienvenido/a a *IT Support and Services SAC*.\n\nSomos su aliado en soporte tecnico y servicios de TI.\n\nPor favor, describanos el problema o consulta que tiene y con gusto le ayudaremos.\n\nPuede enviar texto y/o imagenes/screenshots.")
            return jsonify({"status": "ok"}), 200
        if conversaciones[telefono] in ["esperando_problema", "acumulando_mensajes"]:
            conversaciones[telefono] = "acumulando_mensajes"
            if tipo_mensaje == "text":
                texto = mensaje.get("text", {}).get("body", "").strip()
                agregar_al_buffer(telefono, texto=texto)
            elif tipo_mensaje == "image":
                image_info = mensaje.get("image", {})
                media_id = image_info.get("id")
                caption = image_info.get("caption", "")
                if media_id:
                    image_data = descargar_media_whatsapp(media_id)
                    if image_data:
                        ahora = datetime.now().strftime("%Y%m%d_%H%M%S")
                        filename = f"ticket_{telefono}_{ahora}"
                        link = subir_imagen_a_imgbb(image_data, filename)
                        if link:
                            agregar_al_buffer(telefono, texto=caption if caption else None, imagen_link=link)
                        else:
                            agregar_al_buffer(telefono, texto=caption if caption else "(Imagen - error al subir)")
                    else:
                        agregar_al_buffer(telefono, texto="(Imagen - error al descargar)")
            else:
                enviar_mensaje(telefono, "Por el momento solo podemos recibir texto e imagenes. Por favor, describanos su problema.")
    except Exception as e:
        print(f"Error procesando mensaje: {e}")
        import traceback
        traceback.print_exc()
    return jsonify({"status": "ok"}), 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
