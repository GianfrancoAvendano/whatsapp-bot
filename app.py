import os
import json
import threading
import base64
from datetime import datetime, timedelta
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
PERU_UTC_OFFSET = -5
HORA_RESUMEN = 7

conversaciones = {}
buffer_mensajes = {}
buffer_lock = threading.Lock()
hotel_cache = {}

SALUDOS = [
    "hola", "hello", "hi", "hey", "buenas", "buenos dias", "buenas tardes",
    "buenas noches", "buen dia", "ola", "holi", "que tal", "como estas",
    "buenas buenas", "saludos", "buena", "bnas", "buen dia", "wenas",
    "1", "2", "menu"
]

MENU_PRINCIPAL = (
    "📋 *¿Como podemos ayudarle?*\n"
    "━━━━━━━━━━━━━━━━━━━━\n\n"
    "  *1* 🛠️ Reportar un problema nuevo\n"
    "  *2* 🔍 Consultar un ticket existente\n\n"
    "━━━━━━━━━━━━━━━━━━━━\n"
    "Responda con *1* o *2*"
)

MENU_ADMIN = (
    "⚙️ *PANEL DE ADMINISTRADOR*\n"
    "*IT Support and Services SAC*\n"
    "━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
    "📨 *RESPONDER*\n"
    "  *R[#] [mensaje]*\n"
    "  _Ej: R24 Ya estamos revisando su caso_\n\n"
    "📊 *CAMBIAR ESTADO*\n"
    "  *E[#] [estado]*\n"
    "  _Ej: E24 En proceso_\n"
    "  _Estados: Pendiente · En proceso · Resuelto_\n\n"
    "🎯 *ASIGNAR PRIORIDAD*\n"
    "  *P[#] [prioridad]*\n"
    "  _Ej: P24 Alta_\n"
    "  _Prioridades: Alta · Media · Baja_\n\n"
    "🔎 *VER TICKET*\n"
    "  *V[#]* — _Ej: V24_\n\n"
    "📋 *LISTADOS*\n"
    "  *T* — Ver tickets pendientes\n"
    "  *H [hotel]* — Filtrar por hotel\n"
    "  *resumen* — Resumen completo\n\n"
    "━━━━━━━━━━━━━━━━━━━━━━━━━\n"
    "Escriba *ayuda* para ver este menu"
)


def hora_peru():
    return datetime.utcnow() + timedelta(hours=PERU_UTC_OFFSET)


def es_saludo(texto):
    texto_limpio = texto.lower().strip()
    if texto_limpio in SALUDOS:
        return True
    palabras = texto_limpio.split()
    if len(palabras) <= 2:
        for saludo in SALUDOS:
            if texto_limpio.startswith(saludo):
                resto = texto_limpio[len(saludo):].strip().strip(",").strip()
                if len(resto) < 10:
                    return True
    return False


def es_descripcion_problema(texto):
    texto_limpio = texto.lower().strip()
    if len(texto_limpio) > 20:
        return True
    palabras_problema = [
        "no funciona", "no sirve", "error", "problema", "falla", "ayuda",
        "no puedo", "no anda", "se trabo", "se colgo", "pantalla azul",
        "no prende", "no enciende", "lento", "virus", "hackeado",
        "no conecta", "sin internet", "no imprime", "se apago"
    ]
    for palabra in palabras_problema:
        if palabra in texto_limpio:
            return True
    return False


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
        encabezado = sheet.row_values(1)
        if len(encabezado) < 8 or "Hotel" not in encabezado:
            print("Migrando hoja al formato nuevo con Hotel y Prioridad...")
            migrar_hoja(sheet)
        return sheet
    except gspread.SpreadsheetNotFound:
        try:
            spreadsheet = client.create(GOOGLE_SHEET_NAME)
            sheet = spreadsheet.sheet1
            sheet.append_row(["#", "Fecha y Hora", "Telefono del Cliente", "Hotel", "Descripcion del Problema", "Estado", "Prioridad", "Imagenes"])
            return sheet
        except Exception as e:
            print(f"Error creando la hoja: {e}")
            return None


def migrar_hoja(sheet):
    try:
        todas_las_filas = sheet.get_all_values()
        if not todas_las_filas:
            sheet.append_row(["#", "Fecha y Hora", "Telefono del Cliente", "Hotel", "Descripcion del Problema", "Estado", "Prioridad", "Imagenes"])
            return
        nuevas_filas = []
        for i, fila in enumerate(todas_las_filas):
            if i == 0:
                nuevas_filas.append(["#", "Fecha y Hora", "Telefono del Cliente", "Hotel", "Descripcion del Problema", "Estado", "Prioridad", "Imagenes"])
                continue
            while len(fila) < 6:
                fila.append("")
            nueva_fila = [
                fila[0], fila[1], fila[2],
                "Sin especificar",
                fila[3], fila[4],
                "Sin asignar",
                fila[5],
            ]
            nuevas_filas.append(nueva_fila)
        sheet.clear()
        for fila in nuevas_filas:
            sheet.append_row(fila)
        print(f"Migracion completada: {len(nuevas_filas) - 1} tickets migrados")
    except Exception as e:
        print(f"Error en migracion: {e}")
        import traceback
        traceback.print_exc()


def buscar_hotel_cliente(telefono):
    if telefono in hotel_cache:
        return hotel_cache[telefono]
    sheet = obtener_o_crear_hoja()
    if not sheet:
        return None
    try:
        todas_las_filas = sheet.get_all_values()
        for fila in reversed(todas_las_filas):
            if len(fila) >= 4 and fila[2] == telefono and fila[3] and fila[3] != "Sin especificar":
                hotel_cache[telefono] = fila[3]
                return fila[3]
        return None
    except Exception as e:
        print(f"Error buscando hotel: {e}")
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
            if len(fila) >= 6 and fila[2] == telefono:
                tickets.append({
                    "numero": fila[0],
                    "fecha": fila[1],
                    "hotel": fila[3] if len(fila) > 3 else "",
                    "descripcion": fila[4][:80] + ("..." if len(fila[4]) > 80 else "") if len(fila) > 4 else "",
                    "estado": fila[5] if len(fila) > 5 else "Pendiente",
                    "prioridad": fila[6] if len(fila) > 6 else "Sin asignar",
                    "fila": i + 1
                })
        return tickets
    except Exception as e:
        print(f"Error buscando tickets: {e}")
        return []


def buscar_tickets_pendientes():
    sheet = obtener_o_crear_hoja()
    if not sheet:
        return []
    try:
        todas_las_filas = sheet.get_all_values()
        tickets = []
        for i, fila in enumerate(todas_las_filas):
            if i == 0:
                continue
            if len(fila) >= 6 and fila[5] in ["Pendiente", "En proceso"]:
                tickets.append({
                    "numero": fila[0],
                    "fecha": fila[1],
                    "telefono": fila[2],
                    "hotel": fila[3] if len(fila) > 3 else "",
                    "descripcion": fila[4][:80] + ("..." if len(fila[4]) > 80 else "") if len(fila) > 4 else "",
                    "estado": fila[5],
                    "prioridad": fila[6] if len(fila) > 6 else "Sin asignar",
                })
        return tickets
    except Exception as e:
        print(f"Error buscando tickets pendientes: {e}")
        return []


def buscar_tickets_por_hotel(hotel_buscar):
    sheet = obtener_o_crear_hoja()
    if not sheet:
        return []
    try:
        todas_las_filas = sheet.get_all_values()
        tickets = []
        hotel_lower = hotel_buscar.lower()
        for i, fila in enumerate(todas_las_filas):
            if i == 0:
                continue
            if len(fila) >= 6 and fila[5] in ["Pendiente", "En proceso"]:
                hotel_fila = fila[3].lower() if len(fila) > 3 else ""
                if hotel_lower in hotel_fila:
                    tickets.append({
                        "numero": fila[0],
                        "fecha": fila[1],
                        "telefono": fila[2],
                        "hotel": fila[3] if len(fila) > 3 else "",
                        "descripcion": fila[4][:80] + ("..." if len(fila[4]) > 80 else "") if len(fila) > 4 else "",
                        "estado": fila[5],
                        "prioridad": fila[6] if len(fila) > 6 else "Sin asignar",
                    })
        return tickets
    except Exception as e:
        print(f"Error buscando tickets por hotel: {e}")
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
            if len(fila) >= 6 and fila[0] == str(numero_ticket):
                return {
                    "numero": fila[0],
                    "fecha": fila[1],
                    "telefono": fila[2],
                    "hotel": fila[3] if len(fila) > 3 else "",
                    "descripcion": fila[4] if len(fila) > 4 else "",
                    "estado": fila[5] if len(fila) > 5 else "Pendiente",
                    "prioridad": fila[6] if len(fila) > 6 else "Sin asignar",
                    "imagenes": fila[7] if len(fila) > 7 else "",
                    "fila": i + 1
                }
        return None
    except Exception as e:
        print(f"Error obteniendo ticket: {e}")
        return None


def cambiar_estado_ticket(numero_ticket, nuevo_estado):
    sheet = obtener_o_crear_hoja()
    if not sheet:
        return False
    try:
        todas_las_filas = sheet.get_all_values()
        for i, fila in enumerate(todas_las_filas):
            if i == 0:
                continue
            if len(fila) >= 6 and fila[0] == str(numero_ticket):
                sheet.update_cell(i + 1, 6, nuevo_estado)
                print(f"Estado del ticket #{numero_ticket} cambiado a: {nuevo_estado}")
                return True
        return False
    except Exception as e:
        print(f"Error cambiando estado: {e}")
        return False


def cambiar_prioridad_ticket(numero_ticket, nueva_prioridad):
    sheet = obtener_o_crear_hoja()
    if not sheet:
        return False
    try:
        todas_las_filas = sheet.get_all_values()
        for i, fila in enumerate(todas_las_filas):
            if i == 0:
                continue
            if len(fila) >= 6 and fila[0] == str(numero_ticket):
                sheet.update_cell(i + 1, 7, nueva_prioridad)
                print(f"Prioridad del ticket #{numero_ticket} cambiada a: {nueva_prioridad}")
                return True
        return False
    except Exception as e:
        print(f"Error cambiando prioridad: {e}")
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
            if len(fila) >= 6 and fila[0] == str(numero_ticket):
                fila_num = i + 1
                ahora = hora_peru().strftime("%Y-%m-%d %H:%M")
                descripcion_actual = fila[4] if len(fila) > 4 else ""
                descripcion_nueva = f"{descripcion_actual}\n\n--- Actualizacion ({ahora}) ---\n{nueva_info}"
                sheet.update_cell(fila_num, 5, descripcion_nueva)
                if nuevas_imagenes:
                    imagenes_actuales = fila[7] if len(fila) > 7 else ""
                    nuevos_links = "\n".join(nuevas_imagenes)
                    if imagenes_actuales:
                        imagenes_nueva = f"{imagenes_actuales}\n{nuevos_links}"
                    else:
                        imagenes_nueva = nuevos_links
                    sheet.update_cell(fila_num, 8, imagenes_nueva)
                print(f"Info agregada al ticket #{numero_ticket}")
                return True
        return False
    except Exception as e:
        print(f"Error agregando info al ticket: {e}")
        return False


def guardar_ticket(telefono, hotel, descripcion, imagenes=None):
    sheet = obtener_o_crear_hoja()
    if not sheet:
        return 0
    try:
        todas_las_filas = sheet.get_all_values()
        numero_ticket = len(todas_las_filas)
        ahora = hora_peru().strftime("%Y-%m-%d %H:%M:%S")
        links_imagenes = ""
        if imagenes:
            links_imagenes = "\n".join(imagenes)
        nueva_fila = [numero_ticket, ahora, telefono, hotel, descripcion, "Pendiente", "Sin asignar", links_imagenes]
        sheet.append_row(nueva_fila)
        print(f"Ticket #{numero_ticket} guardado: {telefono} - Hotel: {hotel}")
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
    enviar_mensaje(ADMIN_PHONE, mensaje)


def formatear_telefono(telefono):
    return f"+{telefono}"


def estado_emoji(estado):
    if estado == "Pendiente":
        return "🟡"
    elif estado == "En proceso":
        return "🔵"
    elif estado == "Resuelto":
        return "🟢"
    return "⚪"


def prioridad_emoji(prioridad):
    if prioridad == "Alta":
        return "🔴"
    elif prioridad == "Media":
        return "🟠"
    elif prioridad == "Baja":
        return "🟢"
    return "⚪"


def get_estado(telefono):
    if telefono not in conversaciones or conversaciones[telefono] is None:
        return None
    return conversaciones[telefono].get("estado")


def get_hotel(telefono):
    if telefono in conversaciones and conversaciones[telefono]:
        hotel = conversaciones[telefono].get("hotel")
        if hotel:
            return hotel
    return hotel_cache.get(telefono)


def set_estado(telefono, estado, ticket_actual=None, hotel=None):
    if conversaciones.get(telefono) is None:
        conversaciones[telefono] = {}
    conversaciones[telefono]["estado"] = estado
    if ticket_actual is not None:
        conversaciones[telefono]["ticket_actual"] = ticket_actual
    if hotel is not None:
        conversaciones[telefono]["hotel"] = hotel
        hotel_cache[telefono] = hotel


def procesar_nuevo_ticket(telefono):
    with buffer_lock:
        if telefono not in buffer_mensajes:
            return
        mensajes = buffer_mensajes[telefono]["mensajes"]
        imagenes = buffer_mensajes[telefono].get("imagenes", [])
        del buffer_mensajes[telefono]
    descripcion_completa = "\n".join(mensajes) if mensajes else "(Solo imagenes)"
    hotel = get_hotel(telefono) or "Sin especificar"
    numero_ticket = guardar_ticket(telefono, hotel, descripcion_completa, imagenes if imagenes else None)
    if numero_ticket > 0:
        resumen = descripcion_completa[:200]
        if len(descripcion_completa) > 200:
            resumen += "..."
        img_texto = ""
        if imagenes:
            img_texto = f"\n📎 _{len(imagenes)} imagen(es) adjunta(s)_"

        # ── Mensaje al cliente ──
        enviar_mensaje(
            telefono,
            f"✅ *Ticket #{numero_ticket} registrado*\n"
            f"━━━━━━━━━━━━━━━━━━━━\n\n"
            f"🏨 *Hotel:* {hotel}\n"
            f"📝 *Resumen:*\n{resumen}{img_texto}\n\n"
            f"Un miembro de nuestro equipo se pondra en contacto con usted a la brevedad posible.\n\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"Gracias por confiar en *IT Support and Services SAC* 🤝"
        )

        # ── Notificacion al admin ──
        img_admin = ""
        if imagenes:
            img_admin = "\n📎 *Imagenes:*\n" + "\n".join(imagenes)
        pendientes = buscar_tickets_pendientes()
        notificar_admin(
            f"🆕 *NUEVO TICKET #{numero_ticket}*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"🏨 {hotel}\n"
            f"📞 {formatear_telefono(telefono)}\n"
            f"🕐 {hora_peru().strftime('%d/%m/%Y %H:%M')}\n\n"
            f"📝 *Descripcion:*\n{resumen}{img_admin}\n\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📊 Total pendientes: *{len(pendientes)}*\n\n"
            f"⚡ *Acciones rapidas:*\n"
            f"  *R{numero_ticket}* [mensaje] → Responder\n"
            f"  *E{numero_ticket}* En proceso → Estado\n"
            f"  *P{numero_ticket}* Alta → Prioridad"
        )
    else:
        enviar_mensaje(
            telefono,
            "⚠️ Hemos recibido su mensaje. Nuestro equipo se pondra en contacto con usted pronto.\n\n"
            "Gracias por contactar a *IT Support and Services SAC* 🤝"
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
        enviar_mensaje(telefono, "⚠️ Hubo un error. Escriba cualquier mensaje para volver al menu principal.")
        set_estado(telefono, "listo")
        return
    nueva_info = "\n".join(mensajes) if mensajes else "(Solo imagenes adicionales)"
    exito = agregar_info_a_ticket(ticket_actual, nueva_info, imagenes if imagenes else None)
    if exito:
        img_texto = ""
        if imagenes:
            img_texto = f"\n📎 _{len(imagenes)} imagen(es) adjunta(s)_"

        # ── Mensaje al cliente ──
        enviar_mensaje(
            telefono,
            f"✅ *Ticket #{ticket_actual} actualizado*\n"
            f"━━━━━━━━━━━━━━━━━━━━\n\n"
            f"Su informacion fue agregada exitosamente.{img_texto}\n\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"Gracias por confiar en *IT Support and Services SAC* 🤝"
        )

        # ── Notificacion al admin ──
        img_admin = ""
        if imagenes:
            img_admin = "\n📎 *Imagenes:*\n" + "\n".join(imagenes)
        hotel = get_hotel(telefono) or "Sin especificar"
        notificar_admin(
            f"📎 *ACTUALIZACION — Ticket #{ticket_actual}*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"🏨 {hotel}\n"
            f"📞 {formatear_telefono(telefono)}\n\n"
            f"📝 *Info nueva:*\n{nueva_info[:200]}{img_admin}\n\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"⚡ Responder: *R{ticket_actual}* [mensaje]"
        )
    else:
        enviar_mensaje(
            telefono,
            f"❌ No se pudo agregar la informacion al ticket #{ticket_actual}. Por favor intente de nuevo."
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
            ahora = hora_peru().strftime("%Y%m%d_%H%M%S")
            filename = f"ticket_{telefono}_{ahora}"
            link = subir_imagen_a_imgbb(image_data, filename)
            if link:
                agregar_al_buffer(telefono, tipo_proceso, texto=caption if caption else None, imagen_link=link)
            else:
                agregar_al_buffer(telefono, tipo_proceso, texto=caption if caption else "(Imagen - error al subir)")
        else:
            agregar_al_buffer(telefono, tipo_proceso, texto="(Imagen - error al descargar)")


# ============================================
# RESUMEN DIARIO
# ============================================

def enviar_resumen_diario():
    try:
        ahora = hora_peru()
        tickets = buscar_tickets_pendientes()
        pendientes = [t for t in tickets if t["estado"] == "Pendiente"]
        en_proceso = [t for t in tickets if t["estado"] == "En proceso"]

        mensaje = (
            f"📊 *RESUMEN DE TICKETS*\n"
            f"*{ahora.strftime('%d/%m/%Y · %H:%M')}*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        )

        if not tickets:
            mensaje += "✅ *No hay tickets abiertos.*\n¡Todo al dia!"
        else:
            mensaje += (
                f"📋 *{len(tickets)} ticket(s) abierto(s)*\n\n"
                f"  🟡 Pendientes: *{len(pendientes)}*\n"
                f"  🔵 En proceso: *{len(en_proceso)}*\n\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            )

            hoteles = {}
            for t in tickets:
                h = t.get("hotel", "Sin especificar")
                if h not in hoteles:
                    hoteles[h] = []
                hoteles[h].append(t)

            for hotel, tks in hoteles.items():
                mensaje += f"🏨 *{hotel}* — {len(tks)} ticket(s)\n"
                mensaje += f"─────────────────────\n"
                for t in tks:
                    e_emoji = estado_emoji(t["estado"])
                    p_emoji = prioridad_emoji(t.get("prioridad", "Sin asignar"))
                    mensaje += (
                        f"  {e_emoji}{p_emoji} *#{t['numero']}* · {t['estado']}\n"
                        f"     📞 {formatear_telefono(t['telefono'])}\n"
                        f"     📝 {t['descripcion']}\n\n"
                    )

        notificar_admin(mensaje)
        print(f"Resumen enviado: {ahora.strftime('%Y-%m-%d %H:%M')}")
    except Exception as e:
        print(f"Error enviando resumen diario: {e}")
        import traceback
        traceback.print_exc()


def programar_resumen_diario():
    ahora = hora_peru()
    proxima = ahora.replace(hour=HORA_RESUMEN, minute=0, second=0, microsecond=0)
    if ahora >= proxima:
        proxima += timedelta(days=1)
    segundos_hasta = (proxima - ahora).total_seconds()
    print(f"Proximo resumen diario en {segundos_hasta/3600:.1f} horas ({proxima.strftime('%Y-%m-%d %H:%M')} hora Peru)")
    timer = threading.Timer(segundos_hasta, ejecutar_y_reprogramar)
    timer.daemon = True
    timer.start()


def ejecutar_y_reprogramar():
    enviar_resumen_diario()
    programar_resumen_diario()


# ============================================
# FUNCIONES ADMIN
# ============================================

def procesar_comando_admin(texto_original):
    texto = texto_original.strip()
    texto_lower = texto.lower()

    if texto_lower in ["ayuda", "help", "menu"]:
        enviar_mensaje(ADMIN_PHONE, MENU_ADMIN)
        return

    if texto_lower == "resumen":
        enviar_resumen_diario()
        return

    if texto_lower == "t":
        tickets = buscar_tickets_pendientes()
        if not tickets:
            enviar_mensaje(ADMIN_PHONE, "✅ *No hay tickets pendientes.* ¡Todo al dia!")
            return
        hoteles = {}
        for t in tickets:
            h = t.get("hotel", "Sin especificar")
            if h not in hoteles:
                hoteles[h] = []
            hoteles[h].append(t)
        lista = (
            f"📋 *TICKETS ABIERTOS ({len(tickets)})*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        )
        for hotel, tks in hoteles.items():
            lista += f"🏨 *{hotel}*\n"
            lista += f"─────────────────────\n"
            for t in tks:
                e_emoji = estado_emoji(t["estado"])
                p_emoji = prioridad_emoji(t.get("prioridad", "Sin asignar"))
                lista += (
                    f"  {e_emoji}{p_emoji} *#{t['numero']}* · {t['estado']}\n"
                    f"     📞 {formatear_telefono(t['telefono'])}\n"
                    f"     🕐 {t['fecha']}\n"
                    f"     📝 {t['descripcion']}\n\n"
                )
        enviar_mensaje(ADMIN_PHONE, lista)
        return

    if texto_lower.startswith("h "):
        hotel_buscar = texto[2:].strip()
        if not hotel_buscar:
            enviar_mensaje(ADMIN_PHONE, "❌ Formato: *H Hilton*")
            return
        tickets = buscar_tickets_por_hotel(hotel_buscar)
        if not tickets:
            enviar_mensaje(ADMIN_PHONE, f"✅ *No hay tickets pendientes* para: _{hotel_buscar}_")
            return
        lista = (
            f"🏨 *TICKETS — {hotel_buscar.upper()}* ({len(tickets)})\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        )
        for t in tickets:
            e_emoji = estado_emoji(t["estado"])
            p_emoji = prioridad_emoji(t.get("prioridad", "Sin asignar"))
            lista += (
                f"{e_emoji}{p_emoji} *#{t['numero']}* · {t['estado']}\n"
                f"  🏨 {t['hotel']}\n"
                f"  📞 {formatear_telefono(t['telefono'])}\n"
                f"  🕐 {t['fecha']}\n"
                f"  📝 {t['descripcion']}\n\n"
            )
        enviar_mensaje(ADMIN_PHONE, lista)
        return

    if texto_lower.startswith("v"):
        try:
            numero = texto[1:].strip()
            ticket = obtener_ticket(numero)
            if ticket:
                e_emoji = estado_emoji(ticket["estado"])
                p_emoji = prioridad_emoji(ticket.get("prioridad", "Sin asignar"))
                detalle = (
                    f"{e_emoji} *TICKET #{ticket['numero']}*\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                    f"🏨 *Hotel:* {ticket.get('hotel', 'Sin especificar')}\n"
                    f"📞 *Cliente:* {formatear_telefono(ticket['telefono'])}\n"
                    f"🕐 *Fecha:* {ticket['fecha']}\n"
                    f"📊 *Estado:* {ticket['estado']}\n"
                    f"🎯 *Prioridad:* {p_emoji} {ticket.get('prioridad', 'Sin asignar')}\n\n"
                    f"─────────────────────\n"
                    f"📝 *Descripcion:*\n{ticket['descripcion']}\n"
                )
                if ticket.get("imagenes"):
                    detalle += f"\n📎 *Imagenes:*\n{ticket['imagenes']}\n"
                detalle += (
                    f"\n━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"⚡ *Acciones rapidas:*\n"
                    f"  *R{ticket['numero']}* [mensaje] → Responder\n"
                    f"  *E{ticket['numero']}* [estado] → Cambiar estado\n"
                    f"  *P{ticket['numero']}* [prioridad] → Prioridad"
                )
                enviar_mensaje(ADMIN_PHONE, detalle)
            else:
                enviar_mensaje(ADMIN_PHONE, f"❌ No se encontro el ticket *#{numero}*")
        except Exception:
            enviar_mensaje(ADMIN_PHONE, "❌ Formato incorrecto. Use: *V24*")
        return

    if texto_lower.startswith("r"):
        try:
            resto = texto[1:].strip()
            partes = resto.split(" ", 1)
            if len(partes) < 2:
                enviar_mensaje(ADMIN_PHONE, "❌ Formato: *R24 Tu mensaje aqui*")
                return
            numero = partes[0].strip()
            mensaje_respuesta = partes[1].strip()
            ticket = obtener_ticket(numero)
            if ticket:
                enviar_mensaje(
                    ticket["telefono"],
                    f"💬 *Respuesta — Ticket #{ticket['numero']}*\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n\n"
                    f"{mensaje_respuesta}\n\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"*IT Support and Services SAC*"
                )
                agregar_info_a_ticket(numero, f"[RESPUESTA ADMIN] {mensaje_respuesta}")
                enviar_mensaje(
                    ADMIN_PHONE,
                    f"✅ *Respuesta enviada*\n"
                    f"  📨 Ticket *#{numero}* · 🏨 {ticket.get('hotel', '')}"
                )
            else:
                enviar_mensaje(ADMIN_PHONE, f"❌ No se encontro el ticket *#{numero}*")
        except Exception as e:
            print(f"Error en comando R: {e}")
            enviar_mensaje(ADMIN_PHONE, "❌ Formato: *R24 Tu mensaje aqui*")
        return

    if texto_lower.startswith("p"):
        try:
            resto = texto[1:].strip()
            partes = resto.split(" ", 1)
            if len(partes) < 2:
                enviar_mensaje(ADMIN_PHONE, "❌ Formato: *P24 Alta*\n_Prioridades: Alta · Media · Baja_")
                return
            numero = partes[0].strip()
            nueva_prioridad = partes[1].strip()
            prioridad_lower = nueva_prioridad.lower()
            if prioridad_lower in ["alta", "urgente", "critica"]:
                nueva_prioridad = "Alta"
            elif prioridad_lower in ["media", "normal"]:
                nueva_prioridad = "Media"
            elif prioridad_lower in ["baja", "menor"]:
                nueva_prioridad = "Baja"
            else:
                enviar_mensaje(ADMIN_PHONE, f"❌ Prioridad no valida: _{nueva_prioridad}_\n_Use: Alta · Media · Baja_")
                return
            ticket = obtener_ticket(numero)
            if ticket:
                exito = cambiar_prioridad_ticket(numero, nueva_prioridad)
                if exito:
                    p_emoji = prioridad_emoji(nueva_prioridad)
                    enviar_mensaje(
                        ADMIN_PHONE,
                        f"{p_emoji} *Prioridad actualizada*\n"
                        f"  🎯 Ticket *#{numero}* → *{nueva_prioridad}*"
                    )
                else:
                    enviar_mensaje(ADMIN_PHONE, f"❌ Error actualizando ticket *#{numero}*")
            else:
                enviar_mensaje(ADMIN_PHONE, f"❌ No se encontro el ticket *#{numero}*")
        except Exception as e:
            print(f"Error en comando P: {e}")
            enviar_mensaje(ADMIN_PHONE, "❌ Formato: *P24 Alta*")
        return

    if texto_lower.startswith("e"):
        try:
            resto = texto[1:].strip()
            partes = resto.split(" ", 1)
            if len(partes) < 2:
                enviar_mensaje(ADMIN_PHONE, "❌ Formato: *E24 En proceso*\n_Estados: Pendiente · En proceso · Resuelto_")
                return
            numero = partes[0].strip()
            nuevo_estado = partes[1].strip()
            estado_lower = nuevo_estado.lower()
            if estado_lower in ["pendiente"]:
                nuevo_estado = "Pendiente"
            elif estado_lower in ["en proceso", "en progreso", "proceso", "progreso"]:
                nuevo_estado = "En proceso"
            elif estado_lower in ["resuelto", "cerrado", "completado", "listo"]:
                nuevo_estado = "Resuelto"
            else:
                enviar_mensaje(ADMIN_PHONE, f"❌ Estado no valido: _{nuevo_estado}_\n_Use: Pendiente · En proceso · Resuelto_")
                return
            ticket = obtener_ticket(numero)
            if ticket:
                exito = cambiar_estado_ticket(numero, nuevo_estado)
                if exito:
                    e_emoji = estado_emoji(nuevo_estado)
                    enviar_mensaje(
                        ADMIN_PHONE,
                        f"{e_emoji} *Estado actualizado*\n"
                        f"  📊 Ticket *#{numero}* → *{nuevo_estado}*"
                    )
                    enviar_mensaje(
                        ticket["telefono"],
                        f"📋 *Actualizacion — Ticket #{numero}*\n"
                        f"━━━━━━━━━━━━━━━━━━━━\n\n"
                        f"Su ticket ha sido actualizado a: *{nuevo_estado}*\n\n"
                        f"Gracias por su paciencia 🙏\n\n"
                        f"━━━━━━━━━━━━━━━━━━━━\n"
                        f"*IT Support and Services SAC*"
                    )
                else:
                    enviar_mensaje(ADMIN_PHONE, f"❌ Error actualizando ticket *#{numero}*")
            else:
                enviar_mensaje(ADMIN_PHONE, f"❌ No se encontro el ticket *#{numero}*")
        except Exception as e:
            print(f"Error en comando E: {e}")
            enviar_mensaje(ADMIN_PHONE, "❌ Formato: *E24 En proceso*")
        return

    enviar_mensaje(
        ADMIN_PHONE,
        f"❓ Comando no reconocido.\n\nEscriba *ayuda* para ver los comandos disponibles."
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
        texto_original = ""
        if tipo_mensaje == "text":
            texto_original = mensaje.get("text", {}).get("body", "").strip()
            texto = texto_original.lower()

        # ============================================
        # ADMIN
        # ============================================
        if telefono == ADMIN_PHONE:
            if tipo_mensaje == "text":
                procesar_comando_admin(texto_original)
            else:
                enviar_mensaje(
                    ADMIN_PHONE,
                    "⚠️ Los comandos de admin solo funcionan con texto.\n\nEscriba *ayuda* para ver los comandos."
                )
            return jsonify({"status": "ok"}), 200

        # ============================================
        # CLIENTE
        # ============================================
        estado = get_estado(telefono)
        hotel_conocido = get_hotel(telefono)

        if not hotel_conocido:
            hotel_conocido = buscar_hotel_cliente(telefono)
            if hotel_conocido:
                if conversaciones.get(telefono) is None:
                    conversaciones[telefono] = {}
                conversaciones[telefono]["hotel"] = hotel_conocido
                hotel_cache[telefono] = hotel_conocido

        # Esperando nombre del hotel (cliente nuevo)
        if estado == "esperando_hotel":
            if tipo_mensaje == "text" and texto_original:
                hotel = texto_original.strip()
                set_estado(telefono, "menu", hotel=hotel)
                enviar_mensaje(
                    telefono,
                    f"✅ Registrado como contacto de *{hotel}*\n\n{MENU_PRINCIPAL}"
                )
            else:
                enviar_mensaje(
                    telefono,
                    "✏️ Por favor, escriba el nombre del hotel desde donde nos contacta."
                )
            return jsonify({"status": "ok"}), 200

        # Primera vez o conversacion reseteada
        if estado is None or estado == "listo":
            if not hotel_conocido:
                enviar_mensaje(
                    telefono,
                    f"👋 *Bienvenido/a a IT Support and Services SAC*\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n\n"
                    f"Somos su aliado en soporte tecnico y servicios de TI.\n\n"
                    f"Para poder atenderle mejor:\n"
                    f"🏨 *¿De que hotel nos esta contactando?*"
                )
                set_estado(telefono, "esperando_hotel")
                return jsonify({"status": "ok"}), 200

            if tipo_mensaje == "text":
                if es_saludo(texto) and not es_descripcion_problema(texto):
                    if estado is None:
                        enviar_mensaje(
                            telefono,
                            f"👋 *Bienvenido/a a IT Support and Services SAC*\n"
                            f"━━━━━━━━━━━━━━━━━━━━\n\n"
                            f"Somos su aliado en soporte tecnico y servicios de TI.\n\n"
                            f"{MENU_PRINCIPAL}"
                        )
                    else:
                        enviar_mensaje(telefono, MENU_PRINCIPAL)
                    set_estado(telefono, "menu")
                else:
                    enviar_mensaje(
                        telefono,
                        f"👋 *Bienvenido/a a IT Support and Services SAC*\n"
                        f"━━━━━━━━━━━━━━━━━━━━\n\n"
                        f"📝 Estamos registrando su reporte.\n\n"
                        f"Si desea agregar mas detalles o imagenes, envielos ahora.\n"
                        f"Su ticket se creara en unos segundos ⏳"
                    )
                    set_estado(telefono, "acumulando_nuevo")
                    agregar_al_buffer(telefono, "nuevo", texto=texto_original)
            elif tipo_mensaje == "image":
                enviar_mensaje(
                    telefono,
                    f"👋 *Bienvenido/a a IT Support and Services SAC*\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n\n"
                    f"📝 Estamos registrando su reporte.\n\n"
                    f"Si desea agregar mas detalles o imagenes, envielos ahora.\n"
                    f"Su ticket se creara en unos segundos ⏳"
                )
                set_estado(telefono, "acumulando_nuevo")
                procesar_imagen(telefono, mensaje, "nuevo")
            else:
                enviar_mensaje(
                    telefono,
                    f"👋 *Bienvenido/a a IT Support and Services SAC*\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n\n"
                    f"{MENU_PRINCIPAL}"
                )
                set_estado(telefono, "menu")
            return jsonify({"status": "ok"}), 200

        # Menu principal
        if estado == "menu":
            if texto == "1":
                enviar_mensaje(
                    telefono,
                    "🛠️ *Nuevo reporte*\n"
                    "━━━━━━━━━━━━━━━━━━━━\n\n"
                    "Describanos el problema o consulta que tiene.\n\n"
                    "📝 Puede enviar texto\n"
                    "📸 Puede enviar imagenes/screenshots\n\n"
                    "━━━━━━━━━━━━━━━━━━━━\n"
                    "_Escriba *menu* para volver al menu principal_"
                )
                set_estado(telefono, "esperando_problema")
            elif texto == "2":
                tickets = buscar_tickets_cliente(telefono)
                if not tickets:
                    enviar_mensaje(
                        telefono,
                        "📭 No tiene tickets registrados todavia.\n\n" + MENU_PRINCIPAL
                    )
                else:
                    lista = (
                        "🔍 *Sus tickets*\n"
                        "━━━━━━━━━━━━━━━━━━━━\n\n"
                    )
                    for t in tickets:
                        e_emoji = estado_emoji(t["estado"])
                        lista += (
                            f"{e_emoji} *Ticket #{t['numero']}* · {t['estado']}\n"
                            f"    🕐 {t['fecha']}\n"
                            f"    📝 {t['descripcion']}\n\n"
                        )
                    lista += (
                        "━━━━━━━━━━━━━━━━━━━━\n"
                        "Escriba el *numero* del ticket que desea consultar.\n"
                        "_Escriba *menu* para volver_"
                    )
                    enviar_mensaje(telefono, lista)
                    set_estado(telefono, "listando_tickets")
            else:
                enviar_mensaje(
                    telefono,
                    "⚠️ Opcion no valida.\n\n" + MENU_PRINCIPAL
                )
            return jsonify({"status": "ok"}), 200

        # Esperando descripcion del problema
        if estado == "esperando_problema":
            if texto == "menu":
                enviar_mensaje(telefono, MENU_PRINCIPAL)
                set_estado(telefono, "menu")
                return jsonify({"status": "ok"}), 200
            set_estado(telefono, "acumulando_nuevo")
            if tipo_mensaje == "text":
                agregar_al_buffer(telefono, "nuevo", texto=texto_original)
            elif tipo_mensaje == "image":
                procesar_imagen(telefono, mensaje, "nuevo")
            else:
                enviar_mensaje(telefono, "⚠️ Por el momento solo podemos recibir texto e imagenes.")
            return jsonify({"status": "ok"}), 200

        # Acumulando mensajes para nuevo ticket
        if estado == "acumulando_nuevo":
            if texto == "menu":
                with buffer_lock:
                    if telefono in buffer_mensajes:
                        if buffer_mensajes[telefono]["timer"]:
                            buffer_mensajes[telefono]["timer"].cancel()
                        del buffer_mensajes[telefono]
                enviar_mensaje(
                    telefono,
                    "🚫 Ticket cancelado.\n\n" + MENU_PRINCIPAL
                )
                set_estado(telefono, "menu")
                return jsonify({"status": "ok"}), 200
            if tipo_mensaje == "text":
                agregar_al_buffer(telefono, "nuevo", texto=texto_original)
            elif tipo_mensaje == "image":
                procesar_imagen(telefono, mensaje, "nuevo")
            else:
                enviar_mensaje(telefono, "⚠️ Por el momento solo podemos recibir texto e imagenes.")
            return jsonify({"status": "ok"}), 200

        # Listando tickets
        if estado == "listando_tickets":
            if texto == "menu":
                enviar_mensaje(telefono, MENU_PRINCIPAL)
                set_estado(telefono, "menu")
                return jsonify({"status": "ok"}), 200
            try:
                numero_ticket = texto.replace("#", "").strip()
                ticket = obtener_ticket(numero_ticket)
                if ticket and ticket["telefono"] == telefono:
                    e_emoji = estado_emoji(ticket["estado"])
                    detalle = (
                        f"{e_emoji} *Ticket #{ticket['numero']}*\n"
                        f"━━━━━━━━━━━━━━━━━━━━\n\n"
                        f"🕐 *Fecha:* {ticket['fecha']}\n"
                        f"📊 *Estado:* {ticket['estado']}\n\n"
                        f"─────────────────────\n"
                        f"📝 *Descripcion:*\n{ticket['descripcion']}\n"
                    )
                    if ticket.get("imagenes"):
                        detalle += f"\n📎 *Imagenes:*\n{ticket['imagenes']}\n"
                    detalle += (
                        f"\n━━━━━━━━━━━━━━━━━━━━\n"
                        f"*1* ✏️ Agregar informacion\n"
                        f"*2* ↩️ Volver al menu\n\n"
                        f"Responda con *1* o *2*"
                    )
                    enviar_mensaje(telefono, detalle)
                    set_estado(telefono, "viendo_ticket", ticket_actual=numero_ticket)
                else:
                    enviar_mensaje(
                        telefono,
                        "❌ No se encontro ese ticket o no le pertenece.\n\n"
                        "_Escriba *menu* para volver al menu principal_"
                    )
            except Exception:
                enviar_mensaje(
                    telefono,
                    "⚠️ Escriba solo el numero del ticket.\n_Ejemplo: *5*_\n\n"
                    "_Escriba *menu* para volver_"
                )
            return jsonify({"status": "ok"}), 200

        # Viendo un ticket
        if estado == "viendo_ticket":
            if texto == "1":
                enviar_mensaje(
                    telefono,
                    "✏️ *Agregar informacion*\n"
                    "━━━━━━━━━━━━━━━━━━━━\n\n"
                    "Envie la informacion adicional.\n\n"
                    "📝 Puede enviar texto\n"
                    "📸 Puede enviar imagenes/screenshots\n\n"
                    "━━━━━━━━━━━━━━━━━━━━\n"
                    "_Escriba *menu* para cancelar_"
                )
                set_estado(telefono, "esperando_info")
            elif texto == "2" or texto == "menu":
                enviar_mensaje(telefono, MENU_PRINCIPAL)
                set_estado(telefono, "menu")
            else:
                enviar_mensaje(
                    telefono,
                    "⚠️ Responda con *1* para agregar informacion o *2* para volver."
                )
            return jsonify({"status": "ok"}), 200

        # Esperando info adicional
        if estado == "esperando_info":
            if texto == "menu":
                enviar_mensaje(telefono, MENU_PRINCIPAL)
                set_estado(telefono, "menu")
                return jsonify({"status": "ok"}), 200
            set_estado(telefono, "acumulando_info")
            if tipo_mensaje == "text":
                agregar_al_buffer(telefono, "info", texto=texto_original)
            elif tipo_mensaje == "image":
                procesar_imagen(telefono, mensaje, "info")
            else:
                enviar_mensaje(telefono, "⚠️ Por el momento solo podemos recibir texto e imagenes.")
            return jsonify({"status": "ok"}), 200

        # Acumulando info adicional
        if estado == "acumulando_info":
            if texto == "menu":
                with buffer_lock:
                    if telefono in buffer_mensajes:
                        if buffer_mensajes[telefono]["timer"]:
                            buffer_mensajes[telefono]["timer"].cancel()
                        del buffer_mensajes[telefono]
                enviar_mensaje(
                    telefono,
                    "🚫 Actualizacion cancelada.\n\n" + MENU_PRINCIPAL
                )
                set_estado(telefono, "menu")
                return jsonify({"status": "ok"}), 200
            if tipo_mensaje == "text":
                agregar_al_buffer(telefono, "info", texto=texto_original)
            elif tipo_mensaje == "image":
                procesar_imagen(telefono, mensaje, "info")
            else:
                enviar_mensaje(telefono, "⚠️ Por el momento solo podemos recibir texto e imagenes.")
            return jsonify({"status": "ok"}), 200

        # Estado desconocido
        enviar_mensaje(telefono, MENU_PRINCIPAL)
        set_estado(telefono, "menu")

    except Exception as e:
        print(f"Error procesando mensaje: {e}")
        import traceback
        traceback.print_exc()
    return jsonify({"status": "ok"}), 200


programar_resumen_diario()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
