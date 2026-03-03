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
    "  *resumen* — Resumen por hotel y prioridad\n\n"
    "━━━━━━━━━━━━━━━━━━━━━━━━━\n"
    "Escriba *ayuda* para ver este menu"
)

MSG_DESPEDIDA = (
    "👋 *¡Gracias por contactarnos!*\n"
    "━━━━━━━━━━━━━━━━━━━━\n\n"
    "Si necesita ayuda nuevamente, no dude en escribirnos.\n\n"
    "━━━━━━━━━━━━━━━━━━━━\n"
    "*IT Support and Services SAC* 🤝"
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


# ============================================
# WHATSAPP MESSAGE FUNCTIONS
# ============================================

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


def enviar_botones(telefono, texto_cuerpo, botones, texto_header=None, texto_footer=None):
    url = f"https://graph.facebook.com/v22.0/{PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json",
    }
    interactive = {
        "type": "button",
        "body": {"text": texto_cuerpo},
        "action": {
            "buttons": [
                {"type": "reply", "reply": {"id": b["id"], "title": b["title"][:20]}}
                for b in botones[:3]
            ]
        }
    }
    if texto_header:
        interactive["header"] = {"type": "text", "text": texto_header}
    if texto_footer:
        interactive["footer"] = {"text": texto_footer}
    data = {
        "messaging_product": "whatsapp",
        "to": telefono,
        "type": "interactive",
        "interactive": interactive,
    }
    try:
        response = http_requests.post(url, headers=headers, json=data)
        response.raise_for_status()
        print(f"Botones enviados a {telefono}")
    except http_requests.exceptions.RequestException as e:
        print(f"Error enviando botones a {telefono}: {e}")
        if hasattr(e, "response") and e.response is not None:
            print(f"Respuesta: {e.response.text}")


def enviar_lista(telefono, texto_cuerpo, boton_texto, secciones, texto_header=None, texto_footer=None):
    url = f"https://graph.facebook.com/v22.0/{PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json",
    }
    interactive = {
        "type": "list",
        "body": {"text": texto_cuerpo},
        "action": {
            "button": boton_texto[:20],
            "sections": secciones
        }
    }
    if texto_header:
        interactive["header"] = {"type": "text", "text": texto_header}
    if texto_footer:
        interactive["footer"] = {"text": texto_footer}
    data = {
        "messaging_product": "whatsapp",
        "to": telefono,
        "type": "interactive",
        "interactive": interactive,
    }
    try:
        response = http_requests.post(url, headers=headers, json=data)
        response.raise_for_status()
        print(f"Lista enviada a {telefono}")
    except http_requests.exceptions.RequestException as e:
        print(f"Error enviando lista a {telefono}: {e}")
        if hasattr(e, "response") and e.response is not None:
            print(f"Respuesta: {e.response.text}")


def enviar_menu_principal(telefono):
    enviar_botones(
        telefono,
        "📋 *¿Como podemos ayudarle?*",
        [
            {"id": "menu_nuevo", "title": "🛠️ Nuevo reporte"},
            {"id": "menu_consultar", "title": "🔍 Consultar ticket"},
        ],
        texto_header="IT Support and Services SAC",
        texto_footer="Seleccione una opcion"
    )


def enviar_despedida(telefono):
    enviar_mensaje(telefono, MSG_DESPEDIDA)
    set_estado(telefono, "listo")


def enviar_botones_post_accion(telefono):
    """Botones que aparecen despues de crear/actualizar ticket."""
    enviar_botones(
        telefono,
        "¿Desea realizar otra consulta?",
        [
            {"id": "menu_nuevo", "title": "🛠️ Nuevo reporte"},
            {"id": "menu_consultar", "title": "🔍 Consultar ticket"},
            {"id": "finalizar", "title": "✅ Eso es todo"},
        ],
        texto_footer="Seleccione una opcion"
    )


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


PRIORIDAD_ORDEN = {"Alta": 0, "Media": 1, "Baja": 2, "Sin asignar": 3}
ESTADO_ORDEN = {"Pendiente": 0, "En proceso": 1, "Resuelto": 2}


def ordenar_tickets(tickets):
    """Ordena: Pendientes primero, luego En proceso. Dentro de cada grupo, por prioridad descendente."""
    return sorted(tickets, key=lambda t: (
        ESTADO_ORDEN.get(t.get("estado", "Pendiente"), 9),
        PRIORIDAD_ORDEN.get(t.get("prioridad", "Sin asignar"), 9)
    ))


def obtener_hoteles_activos():
    """Obtiene la lista de hoteles con tickets pendientes/en proceso."""
    tickets = buscar_tickets_pendientes()
    hoteles = {}
    for t in tickets:
        h = t.get("hotel", "Sin especificar")
        if h not in hoteles:
            hoteles[h] = {"pendientes": 0, "en_proceso": 0, "tickets": []}
        if t["estado"] == "Pendiente":
            hoteles[h]["pendientes"] += 1
        else:
            hoteles[h]["en_proceso"] += 1
        hoteles[h]["tickets"].append(t)
    return hoteles


def enviar_resumen_interactivo():
    """Muestra lista de hoteles para que el admin elija cual ver."""
    hoteles = obtener_hoteles_activos()
    if not hoteles:
        enviar_mensaje(ADMIN_PHONE, "✅ *No hay tickets abiertos.* ¡Todo al dia!")
        return
    # Preparar filas para la lista
    rows = []
    rows.append({
        "id": "resumen_todos",
        "title": "📊 Todos los hoteles",
        "description": f"{sum(h['pendientes'] + h['en_proceso'] for h in hoteles.values())} tickets abiertos"
    })
    for nombre, data in hoteles.items():
        total = data["pendientes"] + data["en_proceso"]
        desc = f"🟡 {data['pendientes']} pend. · 🔵 {data['en_proceso']} en proc."
        rows.append({
            "id": f"resumen_{nombre[:50]}",
            "title": nombre[:24],
            "description": desc[:72]
        })
    # Max 10 rows en lista
    enviar_lista(
        ADMIN_PHONE,
        f"📊 *RESUMEN DE TICKETS*\n"
        f"*{hora_peru().strftime('%d/%m/%Y · %H:%M')}*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📋 *{sum(h['pendientes'] + h['en_proceso'] for h in hoteles.values())} ticket(s) abierto(s)*\n"
        f"en *{len(hoteles)} hotel(es)*\n\n"
        f"Seleccione un hotel para ver sus tickets ordenados por prioridad.",
        "🏨 Elegir hotel",
        [{"title": "Hoteles", "rows": rows[:10]}],
        texto_footer="Ordenados por prioridad"
    )


def enviar_resumen_hotel(nombre_hotel):
    """Muestra tickets de un hotel ordenados por estado y prioridad."""
    hoteles = obtener_hoteles_activos()
    if nombre_hotel == "todos":
        # Mostrar todos los hoteles
        todos_tickets = []
        for data in hoteles.values():
            todos_tickets.extend(data["tickets"])
        if not todos_tickets:
            enviar_mensaje(ADMIN_PHONE, "✅ *No hay tickets abiertos.* ¡Todo al dia!")
            return
        tickets_ordenados = ordenar_tickets(todos_tickets)
        # Agrupar por hotel manteniendo el orden
        hoteles_ordenados = {}
        for t in tickets_ordenados:
            h = t.get("hotel", "Sin especificar")
            if h not in hoteles_ordenados:
                hoteles_ordenados[h] = []
            hoteles_ordenados[h].append(t)
        ahora = hora_peru()
        mensaje = (
            f"📊 *RESUMEN COMPLETO*\n"
            f"*{ahora.strftime('%d/%m/%Y · %H:%M')}*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"📋 *{len(todos_tickets)} ticket(s) abierto(s)*\n\n"
        )
        for hotel, tks in hoteles_ordenados.items():
            pendientes = sum(1 for t in tks if t["estado"] == "Pendiente")
            en_proc = sum(1 for t in tks if t["estado"] == "En proceso")
            mensaje += (
                f"🏨 *{hotel}* — 🟡 {pendientes} · 🔵 {en_proc}\n"
                f"─────────────────────\n"
            )
            for t in tks:
                e_emoji = estado_emoji(t["estado"])
                p_emoji = prioridad_emoji(t.get("prioridad", "Sin asignar"))
                mensaje += (
                    f"  {e_emoji}{p_emoji} *#{t['numero']}* · {t['estado']} · {t.get('prioridad', 'Sin asignar')}\n"
                    f"     📞 {formatear_telefono(t['telefono'])}\n"
                    f"     📝 {t['descripcion']}\n\n"
                )
        enviar_mensaje(ADMIN_PHONE, mensaje)
    else:
        # Buscar hotel especifico
        hotel_encontrado = None
        tickets_hotel = []
        for nombre, data in hoteles.items():
            if nombre.lower() == nombre_hotel.lower() or nombre_hotel.lower() in nombre.lower():
                hotel_encontrado = nombre
                tickets_hotel = data["tickets"]
                break
        if not tickets_hotel:
            enviar_mensaje(ADMIN_PHONE, f"✅ *No hay tickets pendientes* para: _{nombre_hotel}_")
            return
        tickets_ordenados = ordenar_tickets(tickets_hotel)
        pendientes = sum(1 for t in tickets_ordenados if t["estado"] == "Pendiente")
        en_proc = sum(1 for t in tickets_ordenados if t["estado"] == "En proceso")
        ahora = hora_peru()
        mensaje = (
            f"🏨 *{hotel_encontrado}*\n"
            f"*{ahora.strftime('%d/%m/%Y · %H:%M')}*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"📋 *{len(tickets_ordenados)} ticket(s) abierto(s)*\n"
            f"  🟡 Pendientes: *{pendientes}*\n"
            f"  🔵 En proceso: *{en_proc}*\n\n"
            f"─────────────────────\n\n"
        )
        for t in tickets_ordenados:
            e_emoji = estado_emoji(t["estado"])
            p_emoji = prioridad_emoji(t.get("prioridad", "Sin asignar"))
            mensaje += (
                f"{e_emoji}{p_emoji} *Ticket #{t['numero']}* · {t.get('prioridad', 'Sin asignar')}\n"
                f"  📞 {formatear_telefono(t['telefono'])}\n"
                f"  🕐 {t['fecha']}\n"
                f"  📝 {t['descripcion']}\n\n"
            )
        mensaje += (
            f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"⚡ Responder: *R[#] [mensaje]*\n"
            f"⚡ Estado: *E[#] [estado]*\n"
            f"⚡ Prioridad: *P[#] [prioridad]*"
        )
        enviar_mensaje(ADMIN_PHONE, mensaje)


# ============================================
# GOOGLE SHEETS
# ============================================

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
        payload = {"key": IMGBB_API_KEY, "image": image_base64, "name": filename}
        resp = http_requests.post(url, data=payload)
        resp.raise_for_status()
        result = resp.json()
        if result.get("success"):
            return result["data"]["url"]
        return None
    except Exception as e:
        print(f"Error subiendo imagen a imgbb: {e}")
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
            nuevas_filas.append([fila[0], fila[1], fila[2], "Sin especificar", fila[3], fila[4], "Sin asignar", fila[5]])
        sheet.clear()
        for fila in nuevas_filas:
            sheet.append_row(fila)
    except Exception as e:
        print(f"Error en migracion: {e}")


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
    except:
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
                    "numero": fila[0], "fecha": fila[1],
                    "hotel": fila[3] if len(fila) > 3 else "",
                    "descripcion": fila[4][:80] + ("..." if len(fila[4]) > 80 else "") if len(fila) > 4 else "",
                    "estado": fila[5] if len(fila) > 5 else "Pendiente",
                    "prioridad": fila[6] if len(fila) > 6 else "Sin asignar",
                    "fila": i + 1
                })
        return tickets
    except:
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
                    "numero": fila[0], "fecha": fila[1], "telefono": fila[2],
                    "hotel": fila[3] if len(fila) > 3 else "",
                    "descripcion": fila[4][:80] + ("..." if len(fila[4]) > 80 else "") if len(fila) > 4 else "",
                    "estado": fila[5],
                    "prioridad": fila[6] if len(fila) > 6 else "Sin asignar",
                })
        return tickets
    except:
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
                        "numero": fila[0], "fecha": fila[1], "telefono": fila[2],
                        "hotel": fila[3] if len(fila) > 3 else "",
                        "descripcion": fila[4][:80] + ("..." if len(fila[4]) > 80 else "") if len(fila) > 4 else "",
                        "estado": fila[5],
                        "prioridad": fila[6] if len(fila) > 6 else "Sin asignar",
                    })
        return tickets
    except:
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
                    "numero": fila[0], "fecha": fila[1], "telefono": fila[2],
                    "hotel": fila[3] if len(fila) > 3 else "",
                    "descripcion": fila[4] if len(fila) > 4 else "",
                    "estado": fila[5] if len(fila) > 5 else "Pendiente",
                    "prioridad": fila[6] if len(fila) > 6 else "Sin asignar",
                    "imagenes": fila[7] if len(fila) > 7 else "",
                    "fila": i + 1
                }
        return None
    except:
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
                return True
        return False
    except:
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
                return True
        return False
    except:
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
                sheet.update_cell(fila_num, 5, f"{descripcion_actual}\n\n--- Actualizacion ({ahora}) ---\n{nueva_info}")
                if nuevas_imagenes:
                    imagenes_actuales = fila[7] if len(fila) > 7 else ""
                    nuevos_links = "\n".join(nuevas_imagenes)
                    sheet.update_cell(fila_num, 8, f"{imagenes_actuales}\n{nuevos_links}" if imagenes_actuales else nuevos_links)
                return True
        return False
    except:
        return False


def guardar_ticket(telefono, hotel, descripcion, imagenes=None):
    sheet = obtener_o_crear_hoja()
    if not sheet:
        return 0
    try:
        todas_las_filas = sheet.get_all_values()
        numero_ticket = len(todas_las_filas)
        ahora = hora_peru().strftime("%Y-%m-%d %H:%M:%S")
        links_imagenes = "\n".join(imagenes) if imagenes else ""
        sheet.append_row([numero_ticket, ahora, telefono, hotel, descripcion, "Pendiente", "Sin asignar", links_imagenes])
        print(f"Ticket #{numero_ticket} guardado: {telefono} - Hotel: {hotel}")
        return numero_ticket
    except Exception as e:
        print(f"Error guardando ticket: {e}")
        return 0


# ============================================
# CONVERSATION STATE
# ============================================

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


# ============================================
# BUFFER / TICKET PROCESSING
# ============================================

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
        resumen = descripcion_completa[:200] + ("..." if len(descripcion_completa) > 200 else "")
        img_texto = f"\n📎 _{len(imagenes)} imagen(es) adjunta(s)_" if imagenes else ""

        # Mensaje al cliente
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
        enviar_botones_post_accion(telefono)

        # Notificacion al admin
        img_admin = "\n📎 *Imagenes:*\n" + "\n".join(imagenes) if imagenes else ""
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
            "⚠️ Hemos recibido su mensaje. Nuestro equipo se pondra en contacto pronto.\n\n"
            "Gracias por contactar a *IT Support and Services SAC* 🤝"
        )
    set_estado(telefono, "menu")


def procesar_info_adicional(telefono):
    with buffer_lock:
        if telefono not in buffer_mensajes:
            return
        mensajes = buffer_mensajes[telefono]["mensajes"]
        imagenes = buffer_mensajes[telefono].get("imagenes", [])
        del buffer_mensajes[telefono]
    ticket_actual = conversaciones.get(telefono, {}).get("ticket_actual")
    if not ticket_actual:
        enviar_mensaje(telefono, "⚠️ Hubo un error.")
        enviar_menu_principal(telefono)
        set_estado(telefono, "menu")
        return
    nueva_info = "\n".join(mensajes) if mensajes else "(Solo imagenes adicionales)"
    exito = agregar_info_a_ticket(ticket_actual, nueva_info, imagenes if imagenes else None)
    if exito:
        img_texto = f"\n📎 _{len(imagenes)} imagen(es) adjunta(s)_" if imagenes else ""
        enviar_mensaje(
            telefono,
            f"✅ *Ticket #{ticket_actual} actualizado*\n"
            f"━━━━━━━━━━━━━━━━━━━━\n\n"
            f"Su informacion fue agregada exitosamente.{img_texto}\n\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"Gracias por confiar en *IT Support and Services SAC* 🤝"
        )
        enviar_botones_post_accion(telefono)

        # Notificacion al admin
        img_admin = "\n📎 *Imagenes:*\n" + "\n".join(imagenes) if imagenes else ""
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
        enviar_mensaje(telefono, f"❌ No se pudo agregar la informacion al ticket #{ticket_actual}.")
    set_estado(telefono, "menu")


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
        callback = procesar_nuevo_ticket if tipo_proceso == "nuevo" else procesar_info_adicional
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
    """Resumen automatico de las 7am - muestra todo ordenado por prioridad."""
    try:
        enviar_resumen_hotel("todos")
    except Exception as e:
        print(f"Error enviando resumen diario: {e}")


def programar_resumen_diario():
    ahora = hora_peru()
    proxima = ahora.replace(hour=HORA_RESUMEN, minute=0, second=0, microsecond=0)
    if ahora >= proxima:
        proxima += timedelta(days=1)
    segundos_hasta = (proxima - ahora).total_seconds()
    print(f"Proximo resumen en {segundos_hasta/3600:.1f}h ({proxima.strftime('%Y-%m-%d %H:%M')} Peru)")
    timer = threading.Timer(segundos_hasta, ejecutar_y_reprogramar)
    timer.daemon = True
    timer.start()


def ejecutar_y_reprogramar():
    enviar_resumen_diario()
    programar_resumen_diario()


# ============================================
# ADMIN COMMANDS
# ============================================

def procesar_comando_admin(texto_original):
    texto = texto_original.strip()
    texto_lower = texto.lower()

    if texto_lower in ["ayuda", "help", "menu"]:
        enviar_mensaje(ADMIN_PHONE, MENU_ADMIN)
        return

    if texto_lower == "resumen":
        enviar_resumen_interactivo()
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
        lista = f"📋 *TICKETS ABIERTOS ({len(tickets)})*\n━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        for hotel, tks in hoteles.items():
            lista += f"🏨 *{hotel}*\n─────────────────────\n"
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
        lista = f"🏨 *TICKETS — {hotel_buscar.upper()}* ({len(tickets)})\n━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
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
        except:
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
                enviar_mensaje(ADMIN_PHONE, f"✅ *Respuesta enviada*\n  📨 Ticket *#{numero}* · 🏨 {ticket.get('hotel', '')}")
            else:
                enviar_mensaje(ADMIN_PHONE, f"❌ No se encontro el ticket *#{numero}*")
        except:
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
            nueva_prioridad = partes[1].strip().lower()
            if nueva_prioridad in ["alta", "urgente", "critica"]:
                nueva_prioridad = "Alta"
            elif nueva_prioridad in ["media", "normal"]:
                nueva_prioridad = "Media"
            elif nueva_prioridad in ["baja", "menor"]:
                nueva_prioridad = "Baja"
            else:
                enviar_mensaje(ADMIN_PHONE, "❌ Prioridad no valida.\n_Use: Alta · Media · Baja_")
                return
            ticket = obtener_ticket(numero)
            if ticket:
                if cambiar_prioridad_ticket(numero, nueva_prioridad):
                    p_emoji = prioridad_emoji(nueva_prioridad)
                    enviar_mensaje(ADMIN_PHONE, f"{p_emoji} *Prioridad actualizada*\n  🎯 Ticket *#{numero}* → *{nueva_prioridad}*")
                else:
                    enviar_mensaje(ADMIN_PHONE, f"❌ Error actualizando ticket *#{numero}*")
            else:
                enviar_mensaje(ADMIN_PHONE, f"❌ No se encontro el ticket *#{numero}*")
        except:
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
            nuevo_estado = partes[1].strip().lower()
            if nuevo_estado in ["pendiente"]:
                nuevo_estado = "Pendiente"
            elif nuevo_estado in ["en proceso", "en progreso", "proceso", "progreso"]:
                nuevo_estado = "En proceso"
            elif nuevo_estado in ["resuelto", "cerrado", "completado", "listo"]:
                nuevo_estado = "Resuelto"
            else:
                enviar_mensaje(ADMIN_PHONE, "❌ Estado no valido.\n_Use: Pendiente · En proceso · Resuelto_")
                return
            ticket = obtener_ticket(numero)
            if ticket:
                if cambiar_estado_ticket(numero, nuevo_estado):
                    e_emoji = estado_emoji(nuevo_estado)
                    enviar_mensaje(ADMIN_PHONE, f"{e_emoji} *Estado actualizado*\n  📊 Ticket *#{numero}* → *{nuevo_estado}*")
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
        except:
            enviar_mensaje(ADMIN_PHONE, "❌ Formato: *E24 En proceso*")
        return

    enviar_mensaje(ADMIN_PHONE, "❓ Comando no reconocido.\n\nEscriba *ayuda* para ver los comandos.")


# ============================================
# HELPER DISPLAY FUNCTIONS
# ============================================

def mostrar_tickets_cliente(telefono):
    tickets = buscar_tickets_cliente(telefono)
    if not tickets:
        enviar_mensaje(telefono, "📭 No tiene tickets registrados todavia.")
        enviar_menu_principal(telefono)
        return
    if len(tickets) <= 10:
        rows = []
        for t in tickets:
            e_emoji = estado_emoji(t["estado"])
            title = f"{e_emoji} Ticket #{t['numero']}"
            desc = f"{t['estado']} · {t['descripcion'][:60]}"
            rows.append({"id": f"ticket_{t['numero']}", "title": title[:24], "description": desc[:72]})
        enviar_lista(
            telefono,
            f"🔍 *Sus tickets*\n━━━━━━━━━━━━━━━━━━━━\n\nTiene *{len(tickets)}* ticket(s) registrado(s).\n\nSeleccione un ticket para ver sus detalles.",
            "📋 Ver tickets",
            [{"title": "Sus tickets", "rows": rows}],
            texto_footer="Escriba 'menu' para volver"
        )
    else:
        lista = "🔍 *Sus tickets*\n━━━━━━━━━━━━━━━━━━━━\n\n"
        for t in tickets:
            e_emoji = estado_emoji(t["estado"])
            lista += f"{e_emoji} *Ticket #{t['numero']}* · {t['estado']}\n    🕐 {t['fecha']}\n    📝 {t['descripcion']}\n\n"
        lista += "━━━━━━━━━━━━━━━━━━━━\nEscriba el *numero* del ticket que desea consultar.\n_Escriba *menu* para volver_"
        enviar_mensaje(telefono, lista)
    set_estado(telefono, "listando_tickets")


def mostrar_detalle_ticket(telefono, ticket):
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
    enviar_mensaje(telefono, detalle)
    enviar_botones(
        telefono,
        "¿Que desea hacer?",
        [
            {"id": "vticket_agregar", "title": "✏️ Agregar info"},
            {"id": "vticket_menu", "title": "↩️ Volver al menu"},
            {"id": "finalizar", "title": "✅ Eso es todo"},
        ],
        texto_footer=f"Ticket #{ticket['numero']}"
    )
    set_estado(telefono, "viendo_ticket", ticket_actual=ticket["numero"])


# ============================================
# ROUTES
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
        return challenge, 200
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
        button_id = ""

        if tipo_mensaje == "text":
            texto_original = mensaje.get("text", {}).get("body", "").strip()
            texto = texto_original.lower()
        elif tipo_mensaje == "interactive":
            interactive = mensaje.get("interactive", {})
            interactive_type = interactive.get("type", "")
            if interactive_type == "button_reply":
                button_id = interactive.get("button_reply", {}).get("id", "")
                texto_original = interactive.get("button_reply", {}).get("title", "")
                texto = button_id
            elif interactive_type == "list_reply":
                button_id = interactive.get("list_reply", {}).get("id", "")
                texto_original = interactive.get("list_reply", {}).get("title", "")
                texto = button_id

        # ============================================
        # ADMIN
        # ============================================
        if telefono == ADMIN_PHONE:
            if tipo_mensaje == "text":
                procesar_comando_admin(texto_original)
            elif tipo_mensaje == "interactive" and button_id:
                # Handle interactive replies from admin (resumen hotel selection)
                if button_id == "resumen_todos":
                    enviar_resumen_hotel("todos")
                elif button_id.startswith("resumen_"):
                    hotel_seleccionado = button_id.replace("resumen_", "")
                    enviar_resumen_hotel(hotel_seleccionado)
                else:
                    enviar_mensaje(ADMIN_PHONE, "⚠️ Use comandos de texto.\n\nEscriba *ayuda* para ver los comandos.")
            else:
                enviar_mensaje(ADMIN_PHONE, "⚠️ Los comandos solo funcionan con texto.\n\nEscriba *ayuda* para ver los comandos.")
            return jsonify({"status": "ok"}), 200

        # ============================================
        # CLIENT - Global finalizar check
        # ============================================
        if button_id == "finalizar":
            enviar_despedida(telefono)
            return jsonify({"status": "ok"}), 200

        # ============================================
        # CLIENT
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

        # ── Esperando nombre del hotel ──
        if estado == "esperando_hotel":
            if tipo_mensaje == "text" and texto_original:
                hotel = texto_original.strip()
                set_estado(telefono, "menu", hotel=hotel)
                enviar_mensaje(telefono, f"✅ Registrado como contacto de *{hotel}*")
                enviar_menu_principal(telefono)
            else:
                enviar_mensaje(telefono, "✏️ Por favor, escriba el nombre del hotel desde donde nos contacta.")
            return jsonify({"status": "ok"}), 200

        # ── Primera vez o estado listo ──
        if estado is None or estado == "listo":
            if not hotel_conocido:
                enviar_mensaje(
                    telefono,
                    "👋 *Bienvenido/a a IT Support and Services SAC*\n"
                    "━━━━━━━━━━━━━━━━━━━━\n\n"
                    "Somos su aliado en soporte tecnico y servicios de TI.\n\n"
                    "Para poder atenderle mejor:\n"
                    "🏨 *¿De que hotel nos esta contactando?*"
                )
                set_estado(telefono, "esperando_hotel")
                return jsonify({"status": "ok"}), 200

            if tipo_mensaje == "text":
                if es_saludo(texto) and not es_descripcion_problema(texto):
                    if estado is None:
                        enviar_mensaje(
                            telefono,
                            "👋 *Bienvenido/a a IT Support and Services SAC*\n"
                            "━━━━━━━━━━━━━━━━━━━━\n\n"
                            "Somos su aliado en soporte tecnico y servicios de TI."
                        )
                    enviar_menu_principal(telefono)
                    set_estado(telefono, "menu")
                else:
                    enviar_mensaje(
                        telefono,
                        "👋 *Bienvenido/a a IT Support and Services SAC*\n"
                        "━━━━━━━━━━━━━━━━━━━━\n\n"
                        "📝 Estamos registrando su reporte.\n\n"
                        "Si desea agregar mas detalles o imagenes, envielos ahora.\n"
                        "Su ticket se creara en unos segundos ⏳"
                    )
                    set_estado(telefono, "acumulando_nuevo")
                    agregar_al_buffer(telefono, "nuevo", texto=texto_original)
            elif tipo_mensaje == "image":
                enviar_mensaje(
                    telefono,
                    "👋 *Bienvenido/a a IT Support and Services SAC*\n"
                    "━━━━━━━━━━━━━━━━━━━━\n\n"
                    "📝 Estamos registrando su reporte.\n\n"
                    "Si desea agregar mas detalles o imagenes, envielos ahora.\n"
                    "Su ticket se creara en unos segundos ⏳"
                )
                set_estado(telefono, "acumulando_nuevo")
                procesar_imagen(telefono, mensaje, "nuevo")
            elif tipo_mensaje == "interactive":
                if button_id == "menu_nuevo":
                    enviar_mensaje(
                        telefono,
                        "🛠️ *Nuevo reporte*\n"
                        "━━━━━━━━━━━━━━━━━━━━\n\n"
                        "Describanos el problema o consulta.\n\n"
                        "📝 Puede enviar texto\n"
                        "📸 Puede enviar imagenes/screenshots\n\n"
                        "━━━━━━━━━━━━━━━━━━━━\n"
                        "_Escriba *menu* para volver_"
                    )
                    set_estado(telefono, "esperando_problema")
                elif button_id == "menu_consultar":
                    mostrar_tickets_cliente(telefono)
                else:
                    enviar_menu_principal(telefono)
                    set_estado(telefono, "menu")
            else:
                enviar_menu_principal(telefono)
                set_estado(telefono, "menu")
            return jsonify({"status": "ok"}), 200

        # ── Menu principal ──
        if estado == "menu":
            if button_id == "menu_nuevo" or texto == "1":
                enviar_mensaje(
                    telefono,
                    "🛠️ *Nuevo reporte*\n"
                    "━━━━━━━━━━━━━━━━━━━━\n\n"
                    "Describanos el problema o consulta.\n\n"
                    "📝 Puede enviar texto\n"
                    "📸 Puede enviar imagenes/screenshots\n\n"
                    "━━━━━━━━━━━━━━━━━━━━\n"
                    "_Escriba *menu* para volver_"
                )
                set_estado(telefono, "esperando_problema")
            elif button_id == "menu_consultar" or texto == "2":
                mostrar_tickets_cliente(telefono)
            else:
                enviar_mensaje(telefono, "⚠️ Opcion no valida.")
                enviar_menu_principal(telefono)
            return jsonify({"status": "ok"}), 200

        # ── Esperando descripcion del problema ──
        if estado == "esperando_problema":
            if texto == "menu":
                enviar_menu_principal(telefono)
                set_estado(telefono, "menu")
                return jsonify({"status": "ok"}), 200
            set_estado(telefono, "acumulando_nuevo")
            if tipo_mensaje == "text":
                agregar_al_buffer(telefono, "nuevo", texto=texto_original)
            elif tipo_mensaje == "image":
                procesar_imagen(telefono, mensaje, "nuevo")
            else:
                enviar_mensaje(telefono, "⚠️ Solo podemos recibir texto e imagenes.")
            return jsonify({"status": "ok"}), 200

        # ── Acumulando mensajes para nuevo ticket ──
        if estado == "acumulando_nuevo":
            if texto == "menu":
                with buffer_lock:
                    if telefono in buffer_mensajes:
                        if buffer_mensajes[telefono]["timer"]:
                            buffer_mensajes[telefono]["timer"].cancel()
                        del buffer_mensajes[telefono]
                enviar_mensaje(telefono, "🚫 Ticket cancelado.")
                enviar_menu_principal(telefono)
                set_estado(telefono, "menu")
                return jsonify({"status": "ok"}), 200
            if tipo_mensaje == "text":
                agregar_al_buffer(telefono, "nuevo", texto=texto_original)
            elif tipo_mensaje == "image":
                procesar_imagen(telefono, mensaje, "nuevo")
            else:
                enviar_mensaje(telefono, "⚠️ Solo podemos recibir texto e imagenes.")
            return jsonify({"status": "ok"}), 200

        # ── Listando tickets ──
        if estado == "listando_tickets":
            if texto == "menu" or button_id == "ticket_menu":
                enviar_menu_principal(telefono)
                set_estado(telefono, "menu")
                return jsonify({"status": "ok"}), 200
            numero_ticket = ""
            if button_id.startswith("ticket_"):
                numero_ticket = button_id.replace("ticket_", "")
            else:
                numero_ticket = texto.replace("#", "").strip()
            try:
                ticket = obtener_ticket(numero_ticket)
                if ticket and ticket["telefono"] == telefono:
                    mostrar_detalle_ticket(telefono, ticket)
                else:
                    enviar_mensaje(telefono, "❌ No se encontro ese ticket o no le pertenece.\n\n_Escriba *menu* para volver_")
            except:
                enviar_mensaje(telefono, "⚠️ Escriba solo el numero del ticket.\n_Ejemplo: *5*_\n\n_Escriba *menu* para volver_")
            return jsonify({"status": "ok"}), 200

        # ── Viendo un ticket ──
        if estado == "viendo_ticket":
            if button_id == "vticket_agregar" or texto == "1":
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
            elif button_id == "vticket_menu" or texto == "2" or texto == "menu":
                enviar_menu_principal(telefono)
                set_estado(telefono, "menu")
            else:
                enviar_mensaje(telefono, "⚠️ Seleccione una opcion valida.")
            return jsonify({"status": "ok"}), 200

        # ── Esperando info adicional ──
        if estado == "esperando_info":
            if texto == "menu":
                enviar_menu_principal(telefono)
                set_estado(telefono, "menu")
                return jsonify({"status": "ok"}), 200
            set_estado(telefono, "acumulando_info")
            if tipo_mensaje == "text":
                agregar_al_buffer(telefono, "info", texto=texto_original)
            elif tipo_mensaje == "image":
                procesar_imagen(telefono, mensaje, "info")
            else:
                enviar_mensaje(telefono, "⚠️ Solo podemos recibir texto e imagenes.")
            return jsonify({"status": "ok"}), 200

        # ── Acumulando info adicional ──
        if estado == "acumulando_info":
            if texto == "menu":
                with buffer_lock:
                    if telefono in buffer_mensajes:
                        if buffer_mensajes[telefono]["timer"]:
                            buffer_mensajes[telefono]["timer"].cancel()
                        del buffer_mensajes[telefono]
                enviar_mensaje(telefono, "🚫 Actualizacion cancelada.")
                enviar_menu_principal(telefono)
                set_estado(telefono, "menu")
                return jsonify({"status": "ok"}), 200
            if tipo_mensaje == "text":
                agregar_al_buffer(telefono, "info", texto=texto_original)
            elif tipo_mensaje == "image":
                procesar_imagen(telefono, mensaje, "info")
            else:
                enviar_mensaje(telefono, "⚠️ Solo podemos recibir texto e imagenes.")
            return jsonify({"status": "ok"}), 200

        # Estado desconocido
        enviar_menu_principal(telefono)
        set_estado(telefono, "menu")

    except Exception as e:
        print(f"Error procesando mensaje: {e}")
        import traceback
        traceback.print_exc()
    return jsonify({"status": "ok"}), 200


# ============================================
# START
# ============================================

programar_resumen_diario()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
