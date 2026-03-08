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

# Formato: "Nombre1:51999999999,Nombre2:51888888888"
ASSISTANTS_RAW = os.environ.get("ASSISTANTS", "Ronald:51993708881")
ASSISTANTS = {}
for entry in ASSISTANTS_RAW.split(","):
    entry = entry.strip()
    if ":" in entry:
        nombre, numero = entry.split(":", 1)
        ASSISTANTS[numero.strip()] = nombre.strip()

def es_asistente(telefono):
    return telefono in ASSISTANTS

def nombre_asistente(telefono):
    return ASSISTANTS.get(telefono, telefono)

TIEMPO_RECORDATORIO = 60  # 1 minuto para recordar al cliente que presione Listo
PERU_UTC_OFFSET = -5
HORA_RESUMEN = 7

conversaciones = {}
buffer_mensajes = {}
admin_estado = {}  # {"accion": "respondiendo", "ticket": "24"}
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
    "👤 *ASIGNAR A ASISTENTE*\n"
    "  *A[#]* — _Ej: A24_\n\n"
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

MENU_ASISTENTE = (
    "⚙️ *PANEL DE ASISTENTE*\n"
    "*IT Support and Services SAC*\n"
    "━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
    "📨 *PROPONER RESPUESTA*\n"
    "  *R[#] [mensaje]*\n"
    "  _Se envia al admin para revision_\n\n"
    "📊 *CAMBIAR ESTADO*\n"
    "  *E[#] [estado]*\n"
    "  _Estados: Pendiente · En proceso · Resuelto_\n\n"
    "🔎 *VER TICKET*\n"
    "  *V[#]* — _Ej: V24_\n\n"
    "📋 *MIS TICKETS*\n"
    "  *T* — Ver tickets asignados\n\n"
    "━━━━━━━━━━━━━━━━━━━━━━━━━\n"
    "Escriba *ayuda* para ver este menu"
)


def asignado_texto(asignado):
    if asignado in ASSISTANTS:
        return f"👤 {ASSISTANTS[asignado]}"
    elif asignado and asignado != "Sin asignar":
        return f"👤 {formatear_telefono(asignado)}"
    return ""


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


def enviar_prompt_nuevo_reporte(telefono):
    """Envia el prompt de nuevo reporte con botones Listo/Cancelar integrados."""
    enviar_botones(
        telefono,
        "🛠️ *Nuevo reporte*\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "Describanos el problema o consulta.\n\n"
        "📝 Puede enviar texto\n"
        "📸 Puede enviar imagenes/screenshots\n\n"
        "Cuando haya terminado, presione *Listo*.",
        [
            {"id": "btn_listo_nuevo", "title": "✅ Listo, enviar"},
            {"id": "btn_cancelar", "title": "🚫 Cancelar"},
        ],
        texto_footer="Puede seguir enviando mensajes e imagenes"
    )
    set_estado(telefono, "acumulando_nuevo")


def enviar_prompt_agregar_info(telefono):
    """Envia el prompt de agregar info con botones Listo/Cancelar integrados."""
    enviar_botones(
        telefono,
        "✏️ *Agregar informacion*\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "Envie la informacion adicional.\n\n"
        "📝 Puede enviar texto\n"
        "📸 Puede enviar imagenes/screenshots\n\n"
        "Cuando haya terminado, presione *Listo*.",
        [
            {"id": "btn_listo_info", "title": "✅ Listo, enviar"},
            {"id": "btn_cancelar", "title": "🚫 Cancelar"},
        ],
        texto_footer="Puede seguir enviando mensajes e imagenes"
    )
    set_estado(telefono, "acumulando_info")


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


def enviar_botones_post_respuesta(telefono, numero_ticket):
    """Botones que aparecen despues de que el admin responde al cliente."""
    # Guardar ticket actual para que pueda agregar info
    if telefono not in conversaciones:
        conversaciones[telefono] = {}
    conversaciones[telefono]["ticket_actual"] = str(numero_ticket)
    set_estado(telefono, "viendo_ticket")
    enviar_botones(
        telefono,
        f"📋 Ticket *#{numero_ticket}*\n¿Desea agregar mas informacion o realizar otra accion?",
        [
            {"id": "vticket_agregar", "title": "✏️ Agregar info"},
            {"id": "vticket_menu", "title": "📋 Menu principal"},
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
        todos_tickets = []
        for data in hoteles.values():
            todos_tickets.extend(data["tickets"])
        if not todos_tickets:
            enviar_mensaje(ADMIN_PHONE, "✅ *No hay tickets abiertos.* ¡Todo al dia!")
            return
        tickets_ordenados = ordenar_tickets(todos_tickets)
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
                asig = asignado_texto(t.get("asignado", ""))
                asig_line = f"\n     {asig}" if asig else ""
                mensaje += (
                    f"  {e_emoji}{p_emoji} *#{t['numero']}* · {t['estado']} · {t.get('prioridad', 'Sin asignar')}\n"
                    f"     📞 {formatear_telefono(t['telefono'])}\n"
                    f"     📝 {t['descripcion']}{asig_line}\n\n"
                )
        enviar_mensaje(ADMIN_PHONE, mensaje)
        enviar_lista_seleccion_ticket(tickets_ordenados)
    else:
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
            asig = asignado_texto(t.get("asignado", ""))
            asig_line = f"\n  {asig}" if asig else ""
            mensaje += (
                f"{e_emoji}{p_emoji} *Ticket #{t['numero']}* · {t.get('prioridad', 'Sin asignar')}\n"
                f"  📞 {formatear_telefono(t['telefono'])}\n"
                f"  🕐 {t['fecha']}\n"
                f"  📝 {t['descripcion']}{asig_line}\n\n"
            )
        enviar_mensaje(ADMIN_PHONE, mensaje)
        enviar_lista_seleccion_ticket(tickets_ordenados)


def enviar_lista_seleccion_ticket(tickets):
    """Muestra lista interactiva para seleccionar un ticket y ver sus opciones."""
    rows = []
    for t in tickets[:10]:
        e = estado_emoji(t["estado"])
        p = prioridad_emoji(t.get("prioridad", "Sin asignar"))
        rows.append({
            "id": f"adm_ver_{t['numero']}",
            "title": f"{e}{p} Ticket #{t['numero']}"[:24],
            "description": f"{t.get('hotel', '')} · {t['descripcion'][:40]}"[:72]
        })
    if rows:
        enviar_lista(
            ADMIN_PHONE,
            "Seleccione un ticket para ver opciones:",
            "📋 Seleccionar ticket",
            [{"title": "Tickets abiertos", "rows": rows}]
        )


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


ENCABEZADO_HOJA = ["#", "Fecha y Hora", "Telefono del Cliente", "Hotel", "Descripcion del Problema", "Estado", "Prioridad", "Imagenes", "Asignado a", "Fecha Resuelto", "Tiempo Resolucion"]


def obtener_o_crear_hoja():
    client = conectar_google_sheets()
    if not client:
        return None
    try:
        sheet = client.open(GOOGLE_SHEET_NAME).sheet1
        encabezado = sheet.row_values(1)
        if len(encabezado) < 11 or "Asignado a" not in encabezado:
            migrar_hoja(sheet)
        return sheet
    except gspread.SpreadsheetNotFound:
        try:
            spreadsheet = client.create(GOOGLE_SHEET_NAME)
            sheet = spreadsheet.sheet1
            sheet.append_row(ENCABEZADO_HOJA)
            return sheet
        except Exception as e:
            print(f"Error creando la hoja: {e}")
            return None


def migrar_hoja(sheet):
    try:
        todas_las_filas = sheet.get_all_values()
        if not todas_las_filas:
            sheet.append_row(ENCABEZADO_HOJA)
            return
        encabezado_actual = todas_las_filas[0]
        tiene_hotel = "Hotel" in encabezado_actual
        tiene_asignado = "Asignado a" in encabezado_actual
        if tiene_hotel and tiene_asignado:
            return  # Ya migrado
        nuevas_filas = [ENCABEZADO_HOJA]
        for i, fila in enumerate(todas_las_filas):
            if i == 0:
                continue
            if not tiene_hotel:
                # Migrar desde formato sin Hotel
                while len(fila) < 6:
                    fila.append("")
                nueva_fila = [fila[0], fila[1], fila[2], "Sin especificar", fila[3], fila[4], "Sin asignar", fila[5], "Sin asignar", "", ""]
            else:
                # Ya tiene Hotel pero no tiene Asignado
                while len(fila) < 8:
                    fila.append("")
                nueva_fila = list(fila[:8]) + ["Sin asignar", "", ""]
            nuevas_filas.append(nueva_fila)
        sheet.clear()
        for fila in nuevas_filas:
            sheet.append_row(fila)
        print(f"Migracion completada: {len(nuevas_filas) - 1} tickets migrados")
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
                    "asignado": fila[8] if len(fila) > 8 else "Sin asignar",
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
                        "asignado": fila[8] if len(fila) > 8 else "Sin asignar",
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
                    "asignado": fila[8] if len(fila) > 8 else "Sin asignar",
                    "fecha_resuelto": fila[9] if len(fila) > 9 else "",
                    "tiempo_resolucion": fila[10] if len(fila) > 10 else "",
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
                fila_num = i + 1
                sheet.update_cell(fila_num, 6, nuevo_estado)
                if nuevo_estado == "Resuelto":
                    ahora = hora_peru()
                    fecha_resuelto = ahora.strftime("%Y-%m-%d %H:%M:%S")
                    sheet.update_cell(fila_num, 10, fecha_resuelto)
                    # Calcular tiempo de resolucion
                    try:
                        fecha_creacion = datetime.strptime(fila[1], "%Y-%m-%d %H:%M:%S")
                        diff = ahora - fecha_creacion
                        dias = diff.days
                        horas, resto = divmod(diff.seconds, 3600)
                        minutos = resto // 60
                        if dias > 0:
                            tiempo_str = f"{dias}d {horas}h {minutos}m"
                        elif horas > 0:
                            tiempo_str = f"{horas}h {minutos}m"
                        else:
                            tiempo_str = f"{minutos}m"
                        sheet.update_cell(fila_num, 11, tiempo_str)
                    except:
                        sheet.update_cell(fila_num, 11, "N/A")
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
        sheet.append_row([numero_ticket, ahora, telefono, hotel, descripcion, "Pendiente", "Sin asignar", links_imagenes, "Sin asignar", "", ""])
        print(f"Ticket #{numero_ticket} guardado: {telefono} - Hotel: {hotel}")
        return numero_ticket
    except Exception as e:
        print(f"Error guardando ticket: {e}")
        return 0


def asignar_ticket(numero_ticket, telefono_asignado):
    sheet = obtener_o_crear_hoja()
    if not sheet:
        return False
    try:
        todas_las_filas = sheet.get_all_values()
        for i, fila in enumerate(todas_las_filas):
            if i == 0:
                continue
            if len(fila) >= 6 and fila[0] == str(numero_ticket):
                sheet.update_cell(i + 1, 9, telefono_asignado)
                return True
        return False
    except:
        return False


def buscar_tickets_asistente(telefono_asistente):
    """Busca tickets pendientes/en proceso asignados a un asistente."""
    sheet = obtener_o_crear_hoja()
    if not sheet:
        return []
    try:
        todas_las_filas = sheet.get_all_values()
        tickets = []
        for i, fila in enumerate(todas_las_filas):
            if i == 0:
                continue
            if len(fila) >= 9 and fila[5] in ["Pendiente", "En proceso"] and fila[8] == telefono_asistente:
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
            # Pressed Listo without sending anything
            enviar_mensaje(telefono, "⚠️ No ha descrito ningun problema todavia.\n\nPor favor envie texto o imagenes describiendo su consulta y luego presione *Listo*.")
            enviar_boton_listo(telefono, "nuevo")
            return
        tiene_contenido = bool(buffer_mensajes[telefono]["mensajes"]) or bool(buffer_mensajes[telefono]["imagenes"])
        if not tiene_contenido:
            enviar_mensaje(telefono, "⚠️ No ha descrito ningun problema todavia.\n\nPor favor envie texto o imagenes describiendo su consulta y luego presione *Listo*.")
            enviar_boton_listo(telefono, "nuevo")
            return
        if buffer_mensajes[telefono].get("timer"):
            buffer_mensajes[telefono]["timer"].cancel()
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
            f"📊 Total pendientes: *{len(pendientes)}*"
        )
        enviar_botones_accion_admin(numero_ticket)
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
            enviar_mensaje(telefono, "⚠️ No ha enviado informacion adicional todavia.\n\nEnvie texto o imagenes y luego presione *Listo*.")
            enviar_boton_listo(telefono, "info")
            return
        tiene_contenido = bool(buffer_mensajes[telefono]["mensajes"]) or bool(buffer_mensajes[telefono]["imagenes"])
        if not tiene_contenido:
            enviar_mensaje(telefono, "⚠️ No ha enviado informacion adicional todavia.\n\nEnvie texto o imagenes y luego presione *Listo*.")
            enviar_boton_listo(telefono, "info")
            return
        if buffer_mensajes[telefono].get("timer"):
            buffer_mensajes[telefono]["timer"].cancel()
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
            f"📝 *Info nueva:*\n{nueva_info[:200]}{img_admin}"
        )
        enviar_botones_accion_admin(ticket_actual)
    else:
        enviar_mensaje(telefono, f"❌ No se pudo agregar la informacion al ticket #{ticket_actual}.")
    set_estado(telefono, "menu")


def enviar_boton_listo(telefono, tipo_proceso):
    """Envia el boton de Listo para que el cliente confirme que termino."""
    if tipo_proceso == "nuevo":
        enviar_botones(
            telefono,
            "📝 Cuando haya terminado de describir su problema, presione el boton.",
            [
                {"id": "btn_listo_nuevo", "title": "✅ Listo, enviar"},
                {"id": "btn_cancelar", "title": "🚫 Cancelar"},
            ],
            texto_footer="Puede seguir enviando mensajes e imagenes"
        )
    else:
        enviar_botones(
            telefono,
            "📝 Cuando haya terminado de agregar informacion, presione el boton.",
            [
                {"id": "btn_listo_info", "title": "✅ Listo, enviar"},
                {"id": "btn_cancelar", "title": "🚫 Cancelar"},
            ],
            texto_footer="Puede seguir enviando mensajes e imagenes"
        )


def recordar_boton_listo(telefono):
    """Recordatorio despues de 1 minuto de inactividad."""
    with buffer_lock:
        if telefono not in buffer_mensajes:
            return
        tipo = buffer_mensajes[telefono].get("tipo", "nuevo")
        # Verificar que no se haya enviado ya un recordatorio
        if buffer_mensajes[telefono].get("recordatorio_enviado"):
            return
        buffer_mensajes[telefono]["recordatorio_enviado"] = True
    if tipo == "nuevo":
        enviar_botones(
            telefono,
            "⏳ ¿Ya termino de describir su problema?\n\nSi ya agrego toda la informacion, presione *Listo* para crear su ticket.",
            [
                {"id": "btn_listo_nuevo", "title": "✅ Listo, enviar"},
                {"id": "btn_cancelar", "title": "🚫 Cancelar"},
            ],
            texto_footer="O siga enviando mas detalles"
        )
    else:
        enviar_botones(
            telefono,
            "⏳ ¿Ya termino de agregar informacion?\n\nSi ya agrego todo, presione *Listo* para actualizar su ticket.",
            [
                {"id": "btn_listo_info", "title": "✅ Listo, enviar"},
                {"id": "btn_cancelar", "title": "🚫 Cancelar"},
            ],
            texto_footer="O siga enviando mas detalles"
        )


def agregar_al_buffer(telefono, tipo_proceso, texto=None, imagen_link=None):
    with buffer_lock:
        if telefono not in buffer_mensajes:
            buffer_mensajes[telefono] = {"mensajes": [], "imagenes": [], "timer": None, "tipo": tipo_proceso, "recordatorio_enviado": False}
        if buffer_mensajes[telefono]["timer"]:
            buffer_mensajes[telefono]["timer"].cancel()
        # Reset recordatorio flag cuando llega nuevo mensaje
        buffer_mensajes[telefono]["recordatorio_enviado"] = False
        if texto:
            buffer_mensajes[telefono]["mensajes"].append(texto)
        if imagen_link:
            buffer_mensajes[telefono]["imagenes"].append(imagen_link)
        # Timer para recordatorio (no para crear ticket)
        buffer_mensajes[telefono]["timer"] = threading.Timer(TIEMPO_RECORDATORIO, recordar_boton_listo, args=[telefono])
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
# SHARED ADMIN/ASSISTANT FUNCTIONS
# ============================================

def enviar_detalle_ticket_admin(destinatario, ticket):
    """Muestra detalle de ticket para admin o asistente."""
    e_emoji = estado_emoji(ticket["estado"])
    p_emoji = prioridad_emoji(ticket.get("prioridad", "Sin asignar"))
    asignado = ticket.get("asignado", "Sin asignar")
    detalle = (
        f"{e_emoji} *TICKET #{ticket['numero']}*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🏨 *Hotel:* {ticket.get('hotel', 'Sin especificar')}\n"
        f"📞 *Cliente:* {formatear_telefono(ticket['telefono'])}\n"
        f"🕐 *Fecha:* {ticket['fecha']}\n"
        f"📊 *Estado:* {ticket['estado']}\n"
        f"🎯 *Prioridad:* {p_emoji} {ticket.get('prioridad', 'Sin asignar')}\n"
    )
    if asignado and asignado != "Sin asignar":
        detalle += f"👤 *Asignado:* {asignado_texto(asignado)}\n"
    if ticket.get("tiempo_resolucion"):
        detalle += f"⏱️ *Tiempo resolucion:* {ticket['tiempo_resolucion']}\n"
    detalle += (
        f"\n─────────────────────\n"
        f"📝 *Descripcion:*\n{ticket['descripcion']}\n"
    )
    if ticket.get("imagenes"):
        detalle += f"\n📎 *Imagenes:*\n{ticket['imagenes']}\n"
    enviar_mensaje(destinatario, detalle)
    # Mostrar botones de accion para admin
    if destinatario == ADMIN_PHONE:
        enviar_botones_accion_admin(ticket["numero"])
    else:
        # Asistentes ven texto con comandos
        enviar_mensaje(
            destinatario,
            f"⚡ *Acciones:*\n"
            f"  *R{ticket['numero']}* [mensaje] → Proponer respuesta\n"
            f"  *E{ticket['numero']}* [estado] → Cambiar estado\n"
            f"  *V{ticket['numero']}* → Ver detalle"
        )


def enviar_botones_accion_admin(numero_ticket):
    """Muestra botones de accion para un ticket al admin."""
    enviar_botones(
        ADMIN_PHONE,
        f"⚡ *Acciones — Ticket #{numero_ticket}*",
        [
            {"id": f"adm_resp_{numero_ticket}", "title": "📨 Responder"},
            {"id": f"adm_estado_{numero_ticket}", "title": "📊 Estado"},
            {"id": f"adm_mas_{numero_ticket}", "title": "⚙️ Mas opciones"},
        ],
        texto_footer="Seleccione una accion"
    )


def enviar_lista_estado_admin(numero_ticket):
    """Muestra lista de estados para cambiar un ticket."""
    enviar_lista(
        ADMIN_PHONE,
        f"📊 *Cambiar estado — Ticket #{numero_ticket}*\n\nSeleccione el nuevo estado:",
        "📊 Elegir estado",
        [{
            "title": "Estados",
            "rows": [
                {"id": f"adm_e_{numero_ticket}_pendiente", "title": "🟡 Pendiente"},
                {"id": f"adm_e_{numero_ticket}_enproceso", "title": "🔵 En proceso"},
                {"id": f"adm_e_{numero_ticket}_resuelto", "title": "🟢 Resuelto"},
            ]
        }]
    )


def enviar_lista_mas_opciones_admin(numero_ticket):
    """Muestra lista con prioridad y asignacion."""
    rows = [
        {"id": f"adm_p_{numero_ticket}_alta", "title": "🔴 Prioridad Alta"},
        {"id": f"adm_p_{numero_ticket}_media", "title": "🟠 Prioridad Media"},
        {"id": f"adm_p_{numero_ticket}_baja", "title": "🟢 Prioridad Baja"},
    ]
    if len(ASSISTANTS) == 1:
        tel = list(ASSISTANTS.keys())[0]
        nombre = ASSISTANTS[tel]
        rows.append({"id": f"asignar_{numero_ticket}_{tel}", "title": f"👤 Asignar a {nombre}"[:24]})
    elif len(ASSISTANTS) > 1:
        rows.append({"id": f"adm_asignar_{numero_ticket}", "title": "👤 Asignar a..."})
    enviar_lista(
        ADMIN_PHONE,
        f"⚙️ *Opciones — Ticket #{numero_ticket}*\n\nSeleccione una accion:",
        "⚙️ Ver opciones",
        [{"title": "Prioridad y asignacion", "rows": rows}]
    )


# ============================================
# ADMIN COMMANDS
# ============================================

def completar_asignacion(numero_ticket, tel_asistente, ticket=None):
    """Ejecuta la asignacion de un ticket a un asistente."""
    if not ticket:
        ticket = obtener_ticket(numero_ticket)
    if not ticket:
        enviar_mensaje(ADMIN_PHONE, f"❌ No se encontro el ticket *#{numero_ticket}*")
        return
    exito = asignar_ticket(numero_ticket, tel_asistente)
    if exito:
        nombre = nombre_asistente(tel_asistente)
        enviar_mensaje(
            ADMIN_PHONE,
            f"👤 *Ticket #{numero_ticket} asignado a {nombre}*\n"
            f"  🏨 {ticket.get('hotel', '')} · 📞 {formatear_telefono(ticket['telefono'])}"
        )
        p_emoji = prioridad_emoji(ticket.get("prioridad", "Sin asignar"))
        e_emoji = estado_emoji(ticket["estado"])
        enviar_mensaje(
            tel_asistente,
            f"📋 *TICKET #{numero_ticket} ASIGNADO*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"🏨 *Hotel:* {ticket.get('hotel', 'Sin especificar')}\n"
            f"📞 *Cliente:* {formatear_telefono(ticket['telefono'])}\n"
            f"🕐 *Fecha:* {ticket['fecha']}\n"
            f"📊 *Estado:* {e_emoji} {ticket['estado']}\n"
            f"🎯 *Prioridad:* {p_emoji} {ticket.get('prioridad', 'Sin asignar')}\n\n"
            f"─────────────────────\n"
            f"📝 *Descripcion:*\n{ticket['descripcion'][:300]}\n\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"⚡ *Acciones:*\n"
            f"  *R{numero_ticket}* [mensaje] → Responder\n"
            f"  *E{numero_ticket}* [estado] → Cambiar estado\n"
            f"  *V{numero_ticket}* → Ver detalle"
        )
    else:
        enviar_mensaje(ADMIN_PHONE, f"❌ Error asignando ticket *#{numero_ticket}*")


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
                asig = asignado_texto(t.get("asignado", ""))
                asig_line = f"\n     {asig}" if asig else ""
                lista += (
                    f"  {e_emoji}{p_emoji} *#{t['numero']}* · {t['estado']}\n"
                    f"     📞 {formatear_telefono(t['telefono'])}\n"
                    f"     🕐 {t['fecha']}\n"
                    f"     📝 {t['descripcion']}{asig_line}\n\n"
                )
        enviar_mensaje(ADMIN_PHONE, lista)
        enviar_lista_seleccion_ticket(tickets)
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
                enviar_detalle_ticket_admin(ADMIN_PHONE, ticket)
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
                # Mostrar botones al cliente para que pueda continuar
                enviar_botones_post_respuesta(ticket["telefono"], ticket["numero"])
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

    if texto_lower.startswith("a"):
        try:
            numero = texto[1:].strip()
            if not numero:
                enviar_mensaje(ADMIN_PHONE, "❌ Formato: *A24*")
                return
            ticket = obtener_ticket(numero)
            if not ticket:
                enviar_mensaje(ADMIN_PHONE, f"❌ No se encontro el ticket *#{numero}*")
                return
            if len(ASSISTANTS) == 1:
                # Solo un asistente → asignar directo
                tel_asistente = list(ASSISTANTS.keys())[0]
                completar_asignacion(numero, tel_asistente, ticket)
            else:
                # Multiples asistentes → mostrar lista
                rows = []
                for tel, nombre in ASSISTANTS.items():
                    rows.append({
                        "id": f"asignar_{numero}_{tel}",
                        "title": nombre[:24],
                        "description": f"Tel: +{tel}"[:72]
                    })
                enviar_lista(
                    ADMIN_PHONE,
                    f"👤 *Asignar Ticket #{numero}*\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n\n"
                    f"🏨 {ticket.get('hotel', '')}\n"
                    f"📝 {ticket['descripcion'][:100]}\n\n"
                    f"Seleccione a quien asignar:",
                    "👤 Elegir asistente",
                    [{"title": "Asistentes", "rows": rows[:10]}]
                )
        except:
            enviar_mensaje(ADMIN_PHONE, "❌ Formato: *A24*")
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
                    msg_estado = f"{e_emoji} *Estado actualizado*\n  📊 Ticket *#{numero}* → *{nuevo_estado}*"
                    if nuevo_estado == "Resuelto":
                        # Re-read ticket to get resolution time
                        ticket_actualizado = obtener_ticket(numero)
                        if ticket_actualizado and ticket_actualizado.get("tiempo_resolucion"):
                            msg_estado += f"\n  ⏱️ Tiempo de resolucion: *{ticket_actualizado['tiempo_resolucion']}*"
                    enviar_mensaje(ADMIN_PHONE, msg_estado)
                    # Notificar al asistente si el ticket esta asignado
                    asignado = ticket.get("asignado", "")
                    if es_asistente(asignado):
                        enviar_mensaje(asignado, msg_estado)
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
# ASSISTANT COMMANDS
# ============================================

def procesar_comando_asistente(texto_original, telefono):
    texto = texto_original.strip()
    texto_lower = texto.lower()
    nombre = nombre_asistente(telefono)

    if texto_lower in ["ayuda", "help", "menu"]:
        enviar_mensaje(telefono, MENU_ASISTENTE)
        return

    if texto_lower == "t":
        tickets = buscar_tickets_asistente(telefono)
        if not tickets:
            enviar_mensaje(telefono, "✅ *No tienes tickets asignados.* ¡Todo al dia!")
            return
        tickets_ordenados = ordenar_tickets(tickets)
        lista = f"📋 *MIS TICKETS ASIGNADOS ({len(tickets)})*\n━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        for t in tickets_ordenados:
            e_emoji = estado_emoji(t["estado"])
            p_emoji = prioridad_emoji(t.get("prioridad", "Sin asignar"))
            lista += (
                f"{e_emoji}{p_emoji} *#{t['numero']}* · {t['estado']}\n"
                f"  🏨 {t.get('hotel', '')}\n"
                f"  📞 {formatear_telefono(t['telefono'])}\n"
                f"  📝 {t['descripcion']}\n\n"
            )
        enviar_mensaje(telefono, lista)
        return

    if texto_lower.startswith("v"):
        try:
            numero = texto[1:].strip()
            ticket = obtener_ticket(numero)
            if ticket:
                enviar_detalle_ticket_admin(telefono, ticket)
            else:
                enviar_mensaje(telefono, f"❌ No se encontro el ticket *#{numero}*")
        except:
            enviar_mensaje(telefono, "❌ Formato: *V24*")
        return

    if texto_lower.startswith("r"):
        try:
            resto = texto[1:].strip()
            partes = resto.split(" ", 1)
            if len(partes) < 2:
                enviar_mensaje(telefono, "❌ Formato: *R24 Tu mensaje aqui*")
                return
            numero = partes[0].strip()
            mensaje_respuesta = partes[1].strip()
            ticket = obtener_ticket(numero)
            if ticket:
                # Enviar al admin para revision, NO al cliente
                enviar_mensaje(
                    ADMIN_PHONE,
                    f"💬 *{nombre} propone respuesta — Ticket #{numero}*\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                    f"🏨 {ticket.get('hotel', '')} · 📞 {formatear_telefono(ticket['telefono'])}\n\n"
                    f"📝 *Mensaje propuesto:*\n{mensaje_respuesta}\n\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"Para enviar al cliente:\n"
                    f"  *R{numero} {mensaje_respuesta}*"
                )
                enviar_mensaje(telefono, f"📤 *Respuesta enviada al administrador para revision*\n  📨 Ticket *#{numero}* · 🏨 {ticket.get('hotel', '')}")
            else:
                enviar_mensaje(telefono, f"❌ No se encontro el ticket *#{numero}*")
        except:
            enviar_mensaje(telefono, "❌ Formato: *R24 Tu mensaje aqui*")
        return

    if texto_lower.startswith("e"):
        try:
            resto = texto[1:].strip()
            partes = resto.split(" ", 1)
            if len(partes) < 2:
                enviar_mensaje(telefono, "❌ Formato: *E24 En proceso*\n_Estados: Pendiente · En proceso · Resuelto_")
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
                enviar_mensaje(telefono, "❌ Estado no valido.\n_Use: Pendiente · En proceso · Resuelto_")
                return
            ticket = obtener_ticket(numero)
            if ticket:
                if cambiar_estado_ticket(numero, nuevo_estado):
                    e_emoji = estado_emoji(nuevo_estado)
                    msg_estado = f"{e_emoji} *Estado actualizado*\n  📊 Ticket *#{numero}* → *{nuevo_estado}*"
                    if nuevo_estado == "Resuelto":
                        ticket_actualizado = obtener_ticket(numero)
                        if ticket_actualizado and ticket_actualizado.get("tiempo_resolucion"):
                            msg_estado += f"\n  ⏱️ Tiempo de resolucion: *{ticket_actualizado['tiempo_resolucion']}*"
                    enviar_mensaje(telefono, msg_estado)
                    enviar_mensaje(ADMIN_PHONE, f"👤 *{nombre}* actualizó ticket:\n{msg_estado}")
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
                    enviar_mensaje(telefono, f"❌ Error actualizando ticket *#{numero}*")
            else:
                enviar_mensaje(telefono, f"❌ No se encontro el ticket *#{numero}*")
        except:
            enviar_mensaje(telefono, "❌ Formato: *E24 En proceso*")
        return

    enviar_mensaje(telefono, "❓ Comando no reconocido.\n\nEscriba *ayuda* para ver los comandos.")


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

        # ── FILTRO 1: Ignorar notificaciones de estado (entregado, leido, etc) ──
        if "statuses" in value:
            return jsonify({"status": "ok"}), 200

        # Ignorar si no hay campo messages
        if "messages" not in value:
            return jsonify({"status": "ok"}), 200

        messages = value.get("messages", [])
        if not messages:
            return jsonify({"status": "ok"}), 200
        mensaje = messages[0]
        telefono = mensaje.get("from", "")
        tipo_mensaje = mensaje.get("type", "")

        # ── FILTRO 2: Solo procesar tipos de mensaje validos ──
        if tipo_mensaje not in ["text", "image", "interactive"]:
            print(f"[IGNORADO] Tipo: {tipo_mensaje} de {telefono}")
            return jsonify({"status": "ok"}), 200

        # ── FILTRO 3: Ignorar mensajes sin contenido real ──
        if not telefono:
            return jsonify({"status": "ok"}), 200

        texto = ""
        texto_original = ""
        button_id = ""

        if tipo_mensaje == "text":
            texto_original = mensaje.get("text", {}).get("body", "").strip()
            texto = texto_original.lower()
            if not texto_original:
                return jsonify({"status": "ok"}), 200
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
            if not button_id:
                return jsonify({"status": "ok"}), 200

        print(f"[MENSAJE] Tipo: {tipo_mensaje} | De: {telefono} | Texto: {texto_original[:50] if texto_original else '(vacio)'}")

        # ============================================
        # ADMIN
        # ============================================
        if telefono == ADMIN_PHONE:
            # ── Admin en modo respuesta (esperando texto para enviar a cliente) ──
            if admin_estado.get("accion") == "respondiendo" and tipo_mensaje == "text":
                if texto == "cancelar":
                    admin_estado.clear()
                    enviar_mensaje(ADMIN_PHONE, "🚫 Respuesta cancelada.")
                    return jsonify({"status": "ok"}), 200
                ticket_num = admin_estado.get("ticket")
                admin_estado.clear()
                ticket = obtener_ticket(ticket_num)
                if ticket:
                    enviar_mensaje(
                        ticket["telefono"],
                        f"💬 *Respuesta — Ticket #{ticket['numero']}*\n"
                        f"━━━━━━━━━━━━━━━━━━━━\n\n"
                        f"{texto_original}\n\n"
                        f"━━━━━━━━━━━━━━━━━━━━\n"
                        f"*IT Support and Services SAC*"
                    )
                    enviar_botones_post_respuesta(ticket["telefono"], ticket["numero"])
                    agregar_info_a_ticket(ticket_num, f"[RESPUESTA ADMIN] {texto_original}")
                    enviar_mensaje(ADMIN_PHONE, f"✅ *Respuesta enviada*\n  📨 Ticket *#{ticket_num}* · 🏨 {ticket.get('hotel', '')}")
                else:
                    enviar_mensaje(ADMIN_PHONE, f"❌ No se encontro el ticket *#{ticket_num}*")
                return jsonify({"status": "ok"}), 200

            # ── Botones interactivos del admin ──
            if tipo_mensaje == "interactive" and button_id:
                # Si estaba en modo respuesta, cancelar
                if admin_estado.get("accion"):
                    admin_estado.clear()
                # Responder ticket
                if button_id.startswith("adm_resp_"):
                    ticket_num = button_id.replace("adm_resp_", "")
                    admin_estado["accion"] = "respondiendo"
                    admin_estado["ticket"] = ticket_num
                    enviar_mensaje(
                        ADMIN_PHONE,
                        f"📨 *Responder — Ticket #{ticket_num}*\n"
                        f"━━━━━━━━━━━━━━━━━━━━\n\n"
                        f"Escriba su respuesta ahora.\n"
                        f"El siguiente mensaje que envie se enviara al cliente.\n\n"
                        f"_Escriba *cancelar* para cancelar_"
                    )
                # Estado
                elif button_id.startswith("adm_estado_"):
                    ticket_num = button_id.replace("adm_estado_", "")
                    enviar_lista_estado_admin(ticket_num)
                elif button_id.startswith("adm_e_"):
                    # adm_e_[ticket]_[estado]
                    partes = button_id.replace("adm_e_", "").rsplit("_", 1)
                    if len(partes) == 2:
                        ticket_num, estado_key = partes
                        mapa_estado = {"pendiente": "Pendiente", "enproceso": "En proceso", "resuelto": "Resuelto"}
                        nuevo_estado = mapa_estado.get(estado_key)
                        if nuevo_estado:
                            ticket = obtener_ticket(ticket_num)
                            if ticket and cambiar_estado_ticket(ticket_num, nuevo_estado):
                                e_emoji = estado_emoji(nuevo_estado)
                                msg = f"{e_emoji} *Estado actualizado*\n  📊 Ticket *#{ticket_num}* → *{nuevo_estado}*"
                                if nuevo_estado == "Resuelto":
                                    t2 = obtener_ticket(ticket_num)
                                    if t2 and t2.get("tiempo_resolucion"):
                                        msg += f"\n  ⏱️ Tiempo: *{t2['tiempo_resolucion']}*"
                                enviar_mensaje(ADMIN_PHONE, msg)
                                asignado = ticket.get("asignado", "")
                                if es_asistente(asignado):
                                    enviar_mensaje(asignado, msg)
                                enviar_mensaje(
                                    ticket["telefono"],
                                    f"📋 *Actualizacion — Ticket #{ticket_num}*\n"
                                    f"━━━━━━━━━━━━━━━━━━━━\n\n"
                                    f"Su ticket ha sido actualizado a: *{nuevo_estado}*\n\n"
                                    f"Gracias por su paciencia 🙏\n\n"
                                    f"━━━━━━━━━━━━━━━━━━━━\n"
                                    f"*IT Support and Services SAC*"
                                )
                            else:
                                enviar_mensaje(ADMIN_PHONE, f"❌ Error con ticket *#{ticket_num}*")
                # Mas opciones
                elif button_id.startswith("adm_mas_"):
                    ticket_num = button_id.replace("adm_mas_", "")
                    enviar_lista_mas_opciones_admin(ticket_num)
                elif button_id.startswith("adm_asignar_"):
                    # Multi-asistente: mostrar lista
                    ticket_num = button_id.replace("adm_asignar_", "")
                    ticket = obtener_ticket(ticket_num)
                    if ticket:
                        rows = []
                        for tel, nombre in ASSISTANTS.items():
                            rows.append({
                                "id": f"asignar_{ticket_num}_{tel}",
                                "title": nombre[:24],
                                "description": f"Tel: +{tel}"[:72]
                            })
                        enviar_lista(
                            ADMIN_PHONE,
                            f"👤 *Asignar Ticket #{ticket_num}*\n\nSeleccione a quien asignar:",
                            "👤 Elegir asistente",
                            [{"title": "Asistentes", "rows": rows[:10]}]
                        )
                # Prioridad
                elif button_id.startswith("adm_p_"):
                    partes = button_id.replace("adm_p_", "").rsplit("_", 1)
                    if len(partes) == 2:
                        ticket_num, prio_key = partes
                        mapa_prio = {"alta": "Alta", "media": "Media", "baja": "Baja"}
                        nueva_prio = mapa_prio.get(prio_key)
                        if nueva_prio and cambiar_prioridad_ticket(ticket_num, nueva_prio):
                            p_emoji = prioridad_emoji(nueva_prio)
                            enviar_mensaje(ADMIN_PHONE, f"{p_emoji} *Prioridad actualizada*\n  🎯 Ticket *#{ticket_num}* → *{nueva_prio}*")
                        else:
                            enviar_mensaje(ADMIN_PHONE, f"❌ Error con ticket *#{ticket_num}*")
                # Asignar (existente)
                elif button_id.startswith("asignar_"):
                    partes = button_id.split("_", 2)
                    if len(partes) == 3:
                        completar_asignacion(partes[1], partes[2])
                    else:
                        enviar_mensaje(ADMIN_PHONE, "❌ Error en la asignacion.")
                # Resumen
                elif button_id == "resumen_todos":
                    enviar_resumen_hotel("todos")
                elif button_id.startswith("resumen_"):
                    enviar_resumen_hotel(button_id.replace("resumen_", ""))
                # Ver ticket desde lista
                elif button_id.startswith("adm_ver_"):
                    ticket_num = button_id.replace("adm_ver_", "")
                    ticket = obtener_ticket(ticket_num)
                    if ticket:
                        enviar_detalle_ticket_admin(ADMIN_PHONE, ticket)
                    else:
                        enviar_mensaje(ADMIN_PHONE, f"❌ No se encontro el ticket *#{ticket_num}*")
                else:
                    enviar_mensaje(ADMIN_PHONE, "⚠️ Opcion no reconocida.")
                return jsonify({"status": "ok"}), 200

            # ── Texto: comandos manuales (siguen funcionando) ──
            if tipo_mensaje == "text":
                procesar_comando_admin(texto_original)
                return jsonify({"status": "ok"}), 200

            # Imagen u otro tipo
            enviar_mensaje(ADMIN_PHONE, "⚠️ Use comandos de texto o botones.\n\nEscriba *ayuda* para ver los comandos.")
            return jsonify({"status": "ok"}), 200

        # ============================================
        # ASSISTANTS
        # ============================================
        if es_asistente(telefono):
            if tipo_mensaje == "text":
                procesar_comando_asistente(texto_original, telefono)
            else:
                enviar_mensaje(telefono, "⚠️ Los comandos solo funcionan con texto.\n\nEscriba *ayuda* para ver los comandos.")
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
                    enviar_botones(
                        telefono,
                        "👋 *Bienvenido/a a IT Support and Services SAC*\n"
                        "━━━━━━━━━━━━━━━━━━━━\n\n"
                        "📝 Estamos registrando su reporte.\n\n"
                        "Envie todos los detalles que necesite.\n"
                        "Cuando haya terminado, presione *Listo*.",
                        [
                            {"id": "btn_listo_nuevo", "title": "✅ Listo, enviar"},
                            {"id": "btn_cancelar", "title": "🚫 Cancelar"},
                        ],
                        texto_footer="Puede seguir enviando mensajes e imagenes"
                    )
                    set_estado(telefono, "acumulando_nuevo")
                    agregar_al_buffer(telefono, "nuevo", texto=texto_original)
            elif tipo_mensaje == "image":
                enviar_botones(
                    telefono,
                    "👋 *Bienvenido/a a IT Support and Services SAC*\n"
                    "━━━━━━━━━━━━━━━━━━━━\n\n"
                    "📝 Estamos registrando su reporte.\n\n"
                    "Envie todos los detalles que necesite.\n"
                    "Cuando haya terminado, presione *Listo*.",
                    [
                        {"id": "btn_listo_nuevo", "title": "✅ Listo, enviar"},
                        {"id": "btn_cancelar", "title": "🚫 Cancelar"},
                    ],
                    texto_footer="Puede seguir enviando mensajes e imagenes"
                )
                set_estado(telefono, "acumulando_nuevo")
                procesar_imagen(telefono, mensaje, "nuevo")
            elif tipo_mensaje == "interactive":
                if button_id == "menu_nuevo":
                    enviar_prompt_nuevo_reporte(telefono)
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
                enviar_prompt_nuevo_reporte(telefono)
            elif button_id == "menu_consultar" or texto == "2":
                mostrar_tickets_cliente(telefono)
            else:
                enviar_mensaje(telefono, "⚠️ Opcion no valida.")
                enviar_menu_principal(telefono)
            return jsonify({"status": "ok"}), 200

        # ── Acumulando mensajes para nuevo ticket ──
        if estado == "acumulando_nuevo":
            # Boton Listo → crear ticket
            if button_id == "btn_listo_nuevo":
                procesar_nuevo_ticket(telefono)
                return jsonify({"status": "ok"}), 200
            # Cancelar
            if texto == "menu" or button_id == "btn_cancelar":
                with buffer_lock:
                    if telefono in buffer_mensajes:
                        if buffer_mensajes[telefono]["timer"]:
                            buffer_mensajes[telefono]["timer"].cancel()
                        del buffer_mensajes[telefono]
                enviar_mensaje(telefono, "🚫 Ticket cancelado.")
                enviar_menu_principal(telefono)
                set_estado(telefono, "menu")
                return jsonify({"status": "ok"}), 200
            # Seguir acumulando
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
                enviar_prompt_agregar_info(telefono)
            elif button_id == "vticket_menu" or texto == "2" or texto == "menu":
                enviar_menu_principal(telefono)
                set_estado(telefono, "menu")
            else:
                enviar_mensaje(telefono, "⚠️ Seleccione una opcion valida.")
            return jsonify({"status": "ok"}), 200

        # ── Acumulando info adicional ──
        if estado == "acumulando_info":
            # Boton Listo → actualizar ticket
            if button_id == "btn_listo_info":
                procesar_info_adicional(telefono)
                return jsonify({"status": "ok"}), 200
            # Cancelar
            if texto == "menu" or button_id == "btn_cancelar":
                with buffer_lock:
                    if telefono in buffer_mensajes:
                        if buffer_mensajes[telefono]["timer"]:
                            buffer_mensajes[telefono]["timer"].cancel()
                        del buffer_mensajes[telefono]
                enviar_mensaje(telefono, "🚫 Actualizacion cancelada.")
                enviar_menu_principal(telefono)
                set_estado(telefono, "menu")
                return jsonify({"status": "ok"}), 200
            # Seguir acumulando
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
