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
ADMIN_PHONE = os.environ.get("ADMIN_PHONE", "51994011725")

TIEMPO_ESPERA = 10

conversaciones = {}
buffer_mensajes = {}
buffer_lock = threading.Lock()

MENU_PRINCIPAL = (
    "Como podemos ayudarle?\n\n"
    "*1* - Reportar un problema nuevo\n"
    "*2* - Consultar un ticket existente\n\n"
    "Responda con *1* o *2*"
)

MENU_ADMIN = (
    "Panel de Administrador de *IT Support and Services SAC*\n\n"
    "Comandos disponibles:\n\n"
    "*R[#] [mensaje]* - Responder a un cliente\n"
    "   Ejemplo: R24 Ya estamos revisando su caso\n\n"
    "*E[#] [estado]* - Cambiar estado de ticket\n"
    "   Ejemplo: E24 En proceso\n"
    "   Estados: Pendiente, En proceso, Resuelto\n\n"
    "*V[#]* - Ver detalles de un ticket\n"
    "   Ejemplo: V24\n\n"
    "*T* - Ver todos los tickets pendientes\n\n"
    "*ayuda* - Ver este menu de nuevo"
)


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


def buscar_tickets_cliente(telefono):
    sheet = obtener_o_crear_hoja()
    if not sheet:
        return []
    try:
        todas_las_filas = sheet.get_all_values()
        tickets = []
        for i, fila in enumerate(todas_las_filas):
            if i == 0:
                continue
            if len(fila) >= 5 and fila[2] == telefono:
                tickets.append({
                    "numero": fila[0],
                    "fecha": fila[1],
                    "descripcion": fila[3][:80] + ("..." if len(fila[3]) > 80 else ""),
                    "estado": fila[4],
                    "fila": i + 1
                })
        return tickets
    except Exception as e:
        print(f"Error buscando tickets: {e}")
        return []


def buscar_tickets_pendientes():
    """Busca todos los tickets pendientes o en proceso."""
    sheet = obtener_o_crear_hoja()
    if not sheet:
        return []
    try:
        todas_las_filas = sheet.get_all_values()
        tickets = []
        for i, fila in enumerate(todas_las_filas):
            if i == 0:
                continue
            if len(fila) >= 5 and fila[4] in ["Pendiente", "En proceso"]:
                tickets.append({
                    "numero": fila[0],
                    "fecha": fila[1],
                    "telefono": fila[2],
                    "descripcion": fila[3][:80] + ("..." if len(fila[3]) > 80 else ""),
                    "estado": fila[4],
                })
        return tickets
    except Exception as e:
        print(f"Error buscando tickets pendientes: {e}")
        return []


def obtener_ticket(numero_ticket):
    sheet = obtener_o_crear_hoja()
    if not sheet:
        return None
    try:
        todas_las_filas = sheet.get_all_values()
        for i, fila in enumerate(todas_las_filas):
            if i == 0:
                continue
            if len(fila) >= 5 and fila[0] == str(numero_ticket):
                return {
                    "numero": fila[0],
                    "fecha": fila[1],
                    "telefono": fila[2],
                    "descripcion": fila[3],
                    "estado": fila[4],
                    "imagenes": fila[5] if len(fila) > 5 else "",
                    "fila": i + 1
                }
        return None
    except Exception as e:
        print(f"Error obteniendo ticket: {e}")
        return None


def cambiar_estado_ticket(numero_ticket, nuevo_estado):
    """Cambia el estado de un ticket."""
    sheet = obtener_o_crear_hoja()
    if not sheet:
        return False
    try:
        todas_las_filas = sheet.get_all_values()
        for i, fila in enumerate(todas_las_filas):
            if i == 0:
                continue
            if len(fila) >= 5 and fila[0] == str(numero_ticket):
                fila_num = i + 1
                sheet.update_cell(fila_num, 5, nuevo_estado)
                print(f"Estado del ticket #{numero_ticket} cambiado a: {nuevo_estado}")
                return True
        return False
    except Exception as e:
        print(f"Error cambiando estado: {e}")
        return False


def agregar_info_a_ticket(numero_ticket, nueva_info, nuevas_imagenes=None):
    sheet = obtener_o_crear_hoja()
    if not sheet:
        return False
    try:
        todas_las_filas = sheet.get_all_values()
        for i, fila in enumerate(todas_las_filas):
            if i == 0:
                continue
            if len(fila) >= 5 and fila[0] == str(numero_ticket):
                fila_num = i + 1
                ahora = datetime.now().strftime("%Y-%m-%d %H:%M")
                descripcion_actual = fila[3]
                descripcion_nueva = f"{descripcion_actual}\n\n--- Actualizacion ({ahora}) ---\n{nueva_info}"
                sheet.update_cell(fila_num, 4, descripcion_nueva)
                if nuevas_imagenes:
                    imagenes_actuales = fila[5] if len(fila) > 5 else ""
                    nuevos_links = "\n".join(nuevas_imagenes)
                    if imagenes_actuales:
                        imagenes_nueva = f"{imagenes_actuales}\n{nuevos_links}"
                    else:
                        imagenes_nueva = nuevos_links
                    sheet.update_cell(fila_num, 6, imagenes_nueva)
                print(f"Info agregada al ticket #{numero_ticket}")
                return True
        return False
    except Exception as e:
        print(f"Error agregando info al ticket: {e}")
        return False


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


def notificar_admin(mensaje):
    """Envia una notificacion al administrador."""
    enviar_mensaje(ADMIN_PHONE, mensaje)


def formatear_telefono(telefono):
    """Formatea el telefono para que sea clickeable en WhatsApp."""
    return f"+{telefono}"


def get_estado(telefono):
    if telefono not in conversaciones or conversaciones[telefono] is None:
        return None
    return conversaciones[telefono].get("estado")


def set_estado(telefono, estado, ticket_actual=None):
    if conversaciones.get(telefono) is None:
        conversaciones[telefono] = {}
    conversaciones[telefono]["estado"] = estado
    if ticket_actual is not None:
        conversaciones[telefono]["ticket_actual"] = ticket_actual


def procesar_nuevo_ticket(telefono):
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
        # Mensaje al cliente
        enviar_mensaje(
            telefono,
            f"Muchas gracias por contactarnos!\n\n"
            f"Su ticket *#{numero_ticket}* ha sido registrado exitosamente.\n\n"
            f"Resumen:\n{resumen}{img_texto}\n\n"
            f"Un miembro de nuestro equipo se pondra en contacto con usted a la brevedad posible.\n\n"
            f"Gracias por confiar en *IT Support and Services SAC*!"
        )
        # Notificacion al admin
        img_admin = ""
        if imagenes:
            img_admin = f"\n📎 {len(imagenes)} imagen(es):\n" + "\n".join(imagenes)
        notificar_admin(
            f"🆕 *NUEVO TICKET #{numero_ticket}*\n\n"
            f"📞 Cliente: {formatear_telefono(telefono)}\n"
            f"🕐 {datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n"
            f"📝 {resumen}{img_admin}\n\n"
            f"---\n"
            f"Responder: *R{numero_ticket} [tu mensaje]*\n"
            f"Estado: *E{numero_ticket} En proceso*"
        )
    else:
        enviar_mensaje(
            telefono,
            "Hemos recibido su mensaje. Nuestro equipo se pondra en contacto con usted pronto.\n\n"
            "Gracias por contactar a *IT Support and Services SAC*!"
        )
    set_estado(telefono, "listo")


def procesar_info_adicional(telefono):
    with buffer_lock:
        if telefono not in buffer_mensajes:
            return
        mensajes = buffer_mensajes[telefono]["mensajes"]
        imagenes = buffer_mensajes[telefono].get("imagenes", [])
        del buffer_mensajes[telefono]
    ticket_actual = conversaciones.get(telefono, {}).get("ticket_actual")
    if not ticket_actual:
        enviar_mensaje(telefono, "Hubo un error. Escriba cualquier mensaje para volver al menu principal.")
        set_estado(telefono, "listo")
        return
    nueva_info = "\n".join(mensajes) if mensajes else "(Solo imagenes adicionales)"
    exito = agregar_info_a_ticket(ticket_actual, nueva_info, imagenes if imagenes else None)
    if exito:
        img_texto = ""
        if imagenes:
            img_texto = f"\n{len(imagenes)} imagen(es) adjunta(s)"
        # Mensaje al cliente
        enviar_mensaje(
            telefono,
            f"Informacion agregada exitosamente al ticket *#{ticket_actual}*.{img_texto}\n\n"
            f"Gracias por confiar en *IT Support and Services SAC*!"
        )
        # Notificacion al admin
        img_admin = ""
        if imagenes:
            img_admin = f"\n📎 {len(imagenes)} imagen(es):\n" + "\n".join(imagenes)
        notificar_admin(
            f"📎 *ACTUALIZACION TICKET #{ticket_actual}*\n\n"
            f"📞 Cliente: {formatear_telefono(telefono)}\n\n"
            f"📝 {nueva_info[:200]}{img_admin}\n\n"
            f"---\n"
            f"Responder: *R{ticket_actual} [tu mensaje]*"
        )
    else:
        enviar_mensaje(
            telefono,
            f"No se pudo agregar la informacion al ticket #{ticket_actual}. Por favor intente de nuevo."
        )
    set_estado(telefono, "listo")


def agregar_al_buffer(telefono, tipo_proceso, texto=None, imagen_link=None):
    with buffer_lock:
        if telefono not in buffer_mensajes:
            buffer_mensajes[telefono] = {"mensajes": [], "imagenes": [], "timer": None, "tipo": tipo_proceso}
        if buffer_mensajes[telefono]["timer"]:
            buffer_mensajes[telefono]["timer"].cancel()
        if texto:
            buffer_mensajes[telefono]["mensajes"].append(texto)
        if imagen_link:
            buffer_mensajes[telefono]["imagenes"].append(imagen_link)
        if tipo_proceso == "nuevo":
            callback = procesar_nuevo_ticket
        else:
            callback = procesar_info_adicional
        buffer_mensajes[telefono]["timer"] = threading.Timer(TIEMPO_ESPERA, callback, args=[telefono])
        buffer_mensajes[telefono]["timer"].start()


def procesar_imagen(telefono, mensaje, tipo_proceso):
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
                agregar_al_buffer(telefono, tipo_proceso, texto=caption if caption else None, imagen_link=link)
            else:
                agregar_al_buffer(telefono, tipo_proceso, texto=caption if caption else "(Imagen - error al subir)")
        else:
            agregar_al_buffer(telefono, tipo_proceso, texto="(Imagen - error al descargar)")


# ============================================
# FUNCIONES ADMIN
# ============================================

def procesar_comando_admin(texto_original):
    """Procesa los comandos del administrador."""
    texto = texto_original.strip()
    texto_lower = texto.lower()

    # Comando: ayuda
    if texto_lower == "ayuda" or texto_lower == "help" or texto_lower == "menu":
        enviar_mensaje(ADMIN_PHONE, MENU_ADMIN)
        return

    # Comando: T - ver tickets pendientes
    if texto_lower == "t":
        tickets = buscar_tickets_pendientes()
        if not tickets:
            enviar_mensaje(ADMIN_PHONE, "✅ No hay tickets pendientes!")
            return
        lista = f"📋 *Tickets abiertos ({len(tickets)}):*\n\n"
        for t in tickets:
            estado_emoji = "🟡" if t["estado"] == "Pendiente" else "🔵"
            lista += (
                f"{estado_emoji} *Ticket #{t['numero']}* - {t['estado']}\n"
                f"   📞 {formatear_telefono(t['telefono'])}\n"
                f"   🕐 {t['fecha']}\n"
                f"   📝 {t['descripcion']}\n\n"
            )
        enviar_mensaje(ADMIN_PHONE, lista)
        return

    # Comando: V[numero] - ver ticket
    if texto_lower.startswith("v"):
        try:
            numero = texto[1:].strip()
            ticket = obtener_ticket(numero)
            if ticket:
                estado_emoji = "🟡" if ticket["estado"] == "Pendiente" else ("🔵" if ticket["estado"] == "En proceso" else "🟢")
                detalle = (
                    f"{estado_emoji} *Ticket #{ticket['numero']}*\n\n"
                    f"📞 Cliente: {formatear_telefono(ticket['telefono'])}\n"
                    f"🕐 Fecha: {ticket['fecha']}\n"
                    f"📊 Estado: {ticket['estado']}\n\n"
                    f"📝 *Descripcion:*\n{ticket['descripcion']}\n"
                )
                if ticket["imagenes"]:
                    detalle += f"\n📎 *Imagenes:*\n{ticket['imagenes']}\n"
                detalle += (
                    f"\n---\n"
                    f"Responder: *R{ticket['numero']} [mensaje]*\n"
                    f"Cambiar estado: *E{ticket['numero']} [estado]*"
                )
                enviar_mensaje(ADMIN_PHONE, detalle)
            else:
                enviar_mensaje(ADMIN_PHONE, f"❌ No se encontro el ticket #{numero}")
        except Exception:
            enviar_mensaje(ADMIN_PHONE, "❌ Formato incorrecto. Use: *V24*")
        return

    # Comando: R[numero] [mensaje] - responder a cliente
    if texto_lower.startswith("r"):
        try:
            # Separar numero de ticket del mensaje
            resto = texto[1:].strip()
            partes = resto.split(" ", 1)
            if len(partes) < 2:
                enviar_mensaje(ADMIN_PHONE, "❌ Formato: *R24 Tu mensaje aqui*")
                return
            numero = partes[0].strip()
            mensaje_respuesta = partes[1].strip()
            ticket = obtener_ticket(numero)
            if ticket:
                # Enviar respuesta al cliente
                enviar_mensaje(
                    ticket["telefono"],
                    f"📩 *Respuesta de IT Support and Services SAC*\n"
                    f"_(Ticket #{ticket['numero']})_\n\n"
                    f"{mensaje_respuesta}"
                )
                # Guardar respuesta en el ticket
                ahora = datetime.now().strftime("%Y-%m-%d %H:%M")
                agregar_info_a_ticket(
                    numero,
                    f"[RESPUESTA ADMIN] {mensaje_respuesta}"
                )
                # Confirmar al admin
                enviar_mensaje(ADMIN_PHONE, f"✅ Respuesta enviada al cliente del ticket #{numero}")
            else:
                enviar_mensaje(ADMIN_PHONE, f"❌ No se encontro el ticket #{numero}")
        except Exception as e:
            print(f"Error en comando R: {e}")
            enviar_mensaje(ADMIN_PHONE, "❌ Formato: *R24 Tu mensaje aqui*")
        return

    # Comando: E[numero] [estado] - cambiar estado
    if texto_lower.startswith("e"):
        try:
            resto = texto[1:].strip()
            partes = resto.split(" ", 1)
            if len(partes) < 2:
                enviar_mensaje(ADMIN_PHONE, "❌ Formato: *E24 En proceso*\nEstados: Pendiente, En proceso, Resuelto")
                return
            numero = partes[0].strip()
            nuevo_estado = partes[1].strip()
            # Normalizar estado
            estado_lower = nuevo_estado.lower()
            if estado_lower in ["pendiente"]:
                nuevo_estado = "Pendiente"
            elif estado_lower in ["en proceso", "en progreso", "proceso", "progreso"]:
                nuevo_estado = "En proceso"
            elif estado_lower in ["resuelto", "cerrado", "completado", "listo"]:
                nuevo_estado = "Resuelto"
            else:
                enviar_mensaje(ADMIN_PHONE, f"❌ Estado no valido: {nuevo_estado}\nUse: Pendiente, En proceso, o Resuelto")
                return
            ticket = obtener_ticket(numero)
            if ticket:
                exito = cambiar_estado_ticket(numero, nuevo_estado)
                if exito:
                    estado_emoji = "🟡" if nuevo_estado == "Pendiente" else ("🔵" if nuevo_estado == "En proceso" else "🟢")
                    enviar_mensaje(ADMIN_PHONE, f"{estado_emoji} Ticket #{numero} actualizado a: *{nuevo_estado}*")
                    # Notificar al cliente del cambio de estado
                    enviar_mensaje(
                        ticket["telefono"],
                        f"📋 *Actualizacion de su ticket #{numero}*\n\n"
                        f"Su ticket ha sido actualizado a: *{nuevo_estado}*\n\n"
                        f"Gracias por su paciencia.\n"
                        f"*IT Support and Services SAC*"
                    )
                else:
                    enviar_mensaje(ADMIN_PHONE, f"❌ Error actualizando ticket #{numero}")
            else:
                enviar_mensaje(ADMIN_PHONE, f"❌ No se encontro el ticket #{numero}")
        except Exception as e:
            print(f"Error en comando E: {e}")
            enviar_mensaje(ADMIN_PHONE, "❌ Formato: *E24 En proceso*")
        return

    # Si no es ningun comando reconocido
    enviar_mensaje(
        ADMIN_PHONE,
        f"No reconozco ese comando.\n\n{MENU_ADMIN}"
    )


# ============================================
# RUTAS
# ============================================

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

        texto = ""
        if tipo_mensaje == "text":
            texto = mensaje.get("text", {}).get("body", "").strip()

        # ============================================
        # ADMIN: Si el mensaje es del admin, procesar como comando
        # ============================================
        if telefono == ADMIN_PHONE:
            if tipo_mensaje == "text":
                procesar_comando_admin(texto)
            else:
                enviar_mensaje(ADMIN_PHONE, "Los comandos de admin solo funcionan con texto.\n\n" + MENU_ADMIN)
            return jsonify({"status": "ok"}), 200

        # ============================================
        # CLIENTE: Flujo normal del cliente
        # ============================================
        texto_lower = texto.lower()
        estado = get_estado(telefono)

        # Primera vez o conversacion reseteada
        if estado is None:
            enviar_mensaje(
                telefono,
                f"Hola! Bienvenido/a a *IT Support and Services SAC*.\n\n"
                f"Somos su aliado en soporte tecnico y servicios de TI.\n\n"
                f"{MENU_PRINCIPAL}"
            )
            set_estado(telefono, "menu")
            return jsonify({"status": "ok"}), 200

        # Estado "listo" - cliente escribe de nuevo despues de una accion
        if estado == "listo":
            enviar_mensaje(telefono, MENU_PRINCIPAL)
            set_estado(telefono, "menu")
            return jsonify({"status": "ok"}), 200

        # Menu principal
        if estado == "menu":
            if texto_lower == "1":
                enviar_mensaje(
                    telefono,
                    "Por favor, describanos el problema o consulta que tiene.\n\n"
                    "Puede enviar texto y/o imagenes/screenshots.\n\n"
                    "_(Escriba *menu* en cualquier momento para volver al menu principal)_"
                )
                set_estado(telefono, "esperando_problema")
            elif texto_lower == "2":
                tickets = buscar_tickets_cliente(telefono)
                if not tickets:
                    enviar_mensaje(
                        telefono,
                        "No tiene tickets registrados todavia.\n\n" + MENU_PRINCIPAL
                    )
                else:
                    lista = "Sus tickets:\n\n"
                    for t in tickets:
                        estado_emoji = "🟡" if t["estado"] == "Pendiente" else ("🔵" if t["estado"] == "En proceso" else "🟢")
                        lista += f"{estado_emoji} *Ticket #{t['numero']}* - {t['estado']}\n"
                        lista += f"   {t['fecha']}\n"
                        lista += f"   {t['descripcion']}\n\n"
                    lista += "Escriba el *numero del ticket* que desea consultar.\n\n"
                    lista += "_(Escriba *menu* para volver al menu principal)_"
                    enviar_mensaje(telefono, lista)
                    set_estado(telefono, "listando_tickets")
            else:
                enviar_mensaje(telefono, "Por favor, responda con *1* o *2*.\n\n" + MENU_PRINCIPAL)
            return jsonify({"status": "ok"}), 200

        # Esperando descripcion del problema (nuevo ticket)
        if estado == "esperando_problema":
            if texto_lower == "menu":
                enviar_mensaje(telefono, MENU_PRINCIPAL)
                set_estado(telefono, "menu")
                return jsonify({"status": "ok"}), 200
            set_estado(telefono, "acumulando_nuevo")
            if tipo_mensaje == "text":
                agregar_al_buffer(telefono, "nuevo", texto=texto)
            elif tipo_mensaje == "image":
                procesar_imagen(telefono, mensaje, "nuevo")
            else:
                enviar_mensaje(telefono, "Por el momento solo podemos recibir texto e imagenes.")
            return jsonify({"status": "ok"}), 200

        # Acumulando mensajes para nuevo ticket
        if estado == "acumulando_nuevo":
            if texto_lower == "menu":
                with buffer_lock:
                    if telefono in buffer_mensajes:
                        if buffer_mensajes[telefono]["timer"]:
                            buffer_mensajes[telefono]["timer"].cancel()
                        del buffer_mensajes[telefono]
                enviar_mensaje(telefono, "Ticket cancelado.\n\n" + MENU_PRINCIPAL)
                set_estado(telefono, "menu")
                return jsonify({"status": "ok"}), 200
            if tipo_mensaje == "text":
                agregar_al_buffer(telefono, "nuevo", texto=texto)
            elif tipo_mensaje == "image":
                procesar_imagen(telefono, mensaje, "nuevo")
            else:
                enviar_mensaje(telefono, "Por el momento solo podemos recibir texto e imagenes.")
            return jsonify({"status": "ok"}), 200

        # Listando tickets, esperando seleccion
        if estado == "listando_tickets":
            if texto_lower == "menu":
                enviar_mensaje(telefono, MENU_PRINCIPAL)
                set_estado(telefono, "menu")
                return jsonify({"status": "ok"}), 200
            try:
                numero_ticket = texto_lower.replace("#", "").strip()
                ticket = obtener_ticket(numero_ticket)
                if ticket and ticket["telefono"] == telefono:
                    estado_emoji = "🟡" if ticket["estado"] == "Pendiente" else ("🔵" if ticket["estado"] == "En proceso" else "🟢")
                    detalle = (
                        f"{estado_emoji} *Ticket #{ticket['numero']}*\n\n"
                        f"*Fecha:* {ticket['fecha']}\n"
                        f"*Estado:* {ticket['estado']}\n\n"
                        f"*Descripcion:*\n{ticket['descripcion']}\n"
                    )
                    if ticket["imagenes"]:
                        detalle += f"\n*Imagenes:*\n{ticket['imagenes']}\n"
                    detalle += (
                        f"\n---\n"
                        f"*1* - Agregar informacion a este ticket\n"
                        f"*2* - Volver al menu principal\n\n"
                        f"Responda con *1* o *2*"
                    )
                    enviar_mensaje(telefono, detalle)
                    set_estado(telefono, "viendo_ticket", ticket_actual=numero_ticket)
                else:
                    enviar_mensaje(
                        telefono,
                        "No se encontro ese ticket o no le pertenece. Por favor escriba un numero de ticket valido.\n\n"
                        "_(Escriba *menu* para volver al menu principal)_"
                    )
            except Exception:
                enviar_mensaje(
                    telefono,
                    "Por favor escriba solo el numero del ticket (ejemplo: *5*).\n\n"
                    "_(Escriba *menu* para volver al menu principal)_"
                )
            return jsonify({"status": "ok"}), 200

        # Viendo un ticket, esperando accion
        if estado == "viendo_ticket":
            if texto_lower == "1":
                enviar_mensaje(
                    telefono,
                    "Envie la informacion adicional que desea agregar al ticket.\n\n"
                    "Puede enviar texto y/o imagenes/screenshots.\n\n"
                    "_(Escriba *menu* para cancelar y volver al menu principal)_"
                )
                set_estado(telefono, "esperando_info")
            elif texto_lower == "2" or texto_lower == "menu":
                enviar_mensaje(telefono, MENU_PRINCIPAL)
                set_estado(telefono, "menu")
            else:
                enviar_mensaje(telefono, "Por favor, responda con *1* para agregar informacion o *2* para volver al menu.")
            return jsonify({"status": "ok"}), 200

        # Esperando info adicional para ticket existente
        if estado == "esperando_info":
            if texto_lower == "menu":
                enviar_mensaje(telefono, MENU_PRINCIPAL)
                set_estado(telefono, "menu")
                return jsonify({"status": "ok"}), 200
            set_estado(telefono, "acumulando_info")
            if tipo_mensaje == "text":
                agregar_al_buffer(telefono, "info", texto=texto)
            elif tipo_mensaje == "image":
                procesar_imagen(telefono, mensaje, "info")
            else:
                enviar_mensaje(telefono, "Por el momento solo podemos recibir texto e imagenes.")
            return jsonify({"status": "ok"}), 200

        # Acumulando info adicional
        if estado == "acumulando_info":
            if texto_lower == "menu":
                with buffer_lock:
                    if telefono in buffer_mensajes:
                        if buffer_mensajes[telefono]["timer"]:
                            buffer_mensajes[telefono]["timer"].cancel()
                        del buffer_mensajes[telefono]
                enviar_mensaje(telefono, "Actualizacion cancelada.\n\n" + MENU_PRINCIPAL)
                set_estado(telefono, "menu")
                return jsonify({"status": "ok"}), 200
            if tipo_mensaje == "text":
                agregar_al_buffer(telefono, "info", texto=texto)
            elif tipo_mensaje == "image":
                procesar_imagen(telefono, mensaje, "info")
            else:
                enviar_mensaje(telefono, "Por el momento solo podemos recibir texto e imagenes.")
            return jsonify({"status": "ok"}), 200

        # Estado desconocido - resetear
        enviar_mensaje(telefono, MENU_PRINCIPAL)
        set_estado(telefono, "menu")

    except Exception as e:
        print(f"Error procesando mensaje: {e}")
        import traceback
        traceback.print_exc()
    return jsonify({"status": "ok"}), 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
