import json
import os
import asyncio
import time
import re
import random
import string
import threading
import http.server
import socketserver
from datetime import datetime, timedelta
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton, Message, CallbackQuery
from pyrogram.enums import ParseMode
import logging
from dotenv import load_dotenv

import db as database

# Configuración de logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

load_dotenv()

# Configuraciones básicas desde .env
API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
BOT_USERNAME = os.getenv("BOT_USERNAME", "KudoTVBot")
ADMIN_IDS_STR = os.getenv("ADMIN_IDS", "[]")
ADMIN_IDS = json.loads(ADMIN_IDS_STR) if isinstance(ADMIN_IDS_STR, str) else ADMIN_IDS_STR
GRUPO_ESTRENOS = os.getenv("GRUPO_ESTRENOS", "https://t.me/+placeholder")
GRUPO_ESTRENOS_ID = int(os.getenv("GRUPO_ESTRENOS_ID", "-100"))

app = Client("pelis_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# Variable global para el tiempo de inicio del bot
start_time = datetime.now()
# Estados para el proceso de pago
estados_pago = {}
# Estados para el proceso de pedidos
estados_pedido = {}

# Función para calcular el tiempo activo
def calcular_tiempo_activo():
    delta = datetime.now() - start_time
    dias = delta.days
    horas, remainder = divmod(delta.seconds, 3600)
    minutos, _ = divmod(remainder, 60)
    return f"{dias}d {horas}h {minutos}m"

# Archivos de datos
CANAL_PRIVADO_ID = int(os.getenv("CANAL_PRIVADO_ID", "-100"))
CANAL_DESTINO_ID = int(os.getenv("CANAL_DESTINO_ID", "-100"))
       
# Base de datos SQLite (reemplaza MongoDB para pruebas)
db = database.DB("kudo_tv.db")
codigos_regalo_col = db.codigos_regalo
codigos_col = db.codigos
usuarios_col = db.usuarios
peliculas_col = db.peliculas
pedidos_col = db.pedidos

# Definición de planes (con límites de contenido aumentados)
PLANES = {
    "Free": {
        "limite_pedido": 1,  # Búsquedas limitadas
        "limite_contenido": 5,
        "limite_maximo": 5,
        "precio_diamantes": 0
    },
    "Pro": {
        "limite_pedido": 5,  # Búsquedas limitadas
        "limite_contenido": 15,  # Disminuido de 50 a 15
        "limite_maximo": 15,
        "precio_diamantes": 100
    },
    "Plus": {
        "limite_pedido": 10,  # Búsquedas limitadas
        "limite_contenido": 30,  # Disminuido de 80 a 30
        "limite_maximo": 30,
        "precio_diamantes": 300
    },
    "Ultra": {
        "limite_pedido": 9999,
        "limite_contenido": 9999,
        "limite_maximo": 9999,
        "precio_diamantes": 500
    }
}

# Inicializar campos para usuarios existentes
usuarios_col.update_many(
    {"estado": {"$exists": False}},
    {"$set": {"estado": None}}
)

usuarios_col.update_many(
    {"limite_maximo": {"$exists": False}},
    {"$set": {"limite_maximo": 5}}
)

# Función para expulsar usuarios del grupo de estrenos cuando su plan expire
async def expulsar_usuario_grupo(user_id):
    try:
        # Intentar expulsar al usuario del grupo
        await app.ban_chat_member(GRUPO_ESTRENOS_ID, int(user_id))
        logger.info(f"Usuario {user_id} expulsado del grupo de estrenos por plan vencido")
        
        # Intentar notificar al usuario
        try:
            await app.send_message(
                int(user_id),
                "❌ Tu acceso al grupo de estrenos ha sido revocado debido a que tu plan Ultra ha expirado."
            )
        except:
            pass
            
    except Exception as e:
        logger.error(f"Error al expulsar usuario {user_id} del grupo: {e}")

# Tarea en segundo plano para resetear límites diarios y verificar expiración
async def reset_limits_and_check_expiration():
    while True:
        await asyncio.sleep(24 * 3600)  # Esperar 24 horas
        try:
            # Primero verificar planes vencidos y revertirlos a Free
            ahora = datetime.now()
            usuarios_vencidos = usuarios_col.find({
                "plan": {"$in": ["Pro", "Plus", "Ultra"]},
                "expira": {"$lt": ahora}
            })
            
            for usuario in usuarios_vencidos:
                usuarios_col.update_one(
                    {"_id": usuario["_id"]},
                    {"$set": {
                        "plan": "Free",
                        "expira": None,
                        "limite_pedido": 0,
                        "limite_contenido": 0,
                        "limite_maximo": PLANES["Free"]["limite_maximo"]
                    }}
                )
                
                # Si el usuario tenía plan Ultra, expulsarlo del grupo de estrenos
                if usuario.get("plan") == "Ultra":
                    await expulsar_usuario_grupo(usuario["user_id"])
            
            # Luego resetear límites diarios para todos los usuarios según su plan actual
            usuarios = usuarios_col.find({})
            
            for usuario in usuarios:
                plan = usuario.get("plan", "Free")
                limites_plan = PLANES.get(plan, PLANES["Free"])
                
                usuarios_col.update_one(
                    {"_id": usuario["_id"]},
                    {"$set": {
                        "limite_pedido": 0,
                        "limite_contenido": 0,
                        "limite_maximo": limites_plan["limite_maximo"]
                    }}
                )
            
            print("✅ Límites diarios reseteados y planes vencidos verificados.")
        except Exception as e:
            print(f"❌ Error en la tarea periódica: {e}")

# Helpers
def generar_id_aleatorio():
    return ''.join(random.choices(string.ascii_letters + string.digits, k=16))

def extraer_titulo_limpio(caption):
    if caption:
        titulo = caption.split("\n")[0].strip()
        titulo = re.sub(r"📺\s*Serie:\s*|🎬\s*Película:\s*", "", titulo)
        return titulo
    return "Título no especificado"

def normalizar_texto(texto):
    texto = texto.lower()
    texto = re.sub(r"[^\w\s]", "", texto)
    texto = texto.replace(".", " ")
    return texto

@app.on_message(filters.command("start") & filters.private)
async def start_command(client, message: Message):
    try:
        user = message.from_user
        if not user:
            return await message.reply("❌ No se pudo identificar al usuario.")

        user_id = str(user.id)
        random_id_param = None
        referido_por = None

        # Manejo de parámetros de inicio
        if len(message.command) > 1:
            param = message.command[1]
            
            # Verificar si es un código de referido (case-insensitive)
            if re.match(r'^ref_', param, re.IGNORECASE):
                referido_por = param[4:].strip()
                # Validar que no sea auto-referido y que el referido exista
                if referido_por != user_id and referido_por.isdigit():
                    if not usuarios_col.find_one({"user_id": referido_por}):
                        referido_por = None
                else:
                    referido_por = None
            else:
                # Si no es referido, tratar como ID de contenido
                random_id_param = param

        user_data = usuarios_col.find_one({"user_id": user_id})

        if not user_data:
            new_user = {
                "user_id": user_id,
                "nombre": user.first_name,
                "saldo": 0.00,
                "plan": "Free",
                "expira": None,
                "limite_pedido": 0,
                "limite_contenido": 0,
                "limite_maximo": PLANES["Free"]["limite_maximo"],
                "fecha_union": datetime.now(),
                "ultimo_acceso": datetime.now(),
                "referidos": 0,
                "referido_por": referido_por
            }
            usuarios_col.insert_one(new_user)
            user_data = new_user

            # Recompensar al referidor SOLO si es un nuevo usuario
            if referido_por:
                recompensa = 5  # Diamantes por referido
                usuarios_col.update_one(
                    {"user_id": referido_por},
                    {"$inc": {"saldo": recompensa, "referidos": 1}}
                )
                try:
                    await client.send_message(
                        chat_id=int(referido_por),
                        text=f"🎉 ¡Has ganado {recompensa} diamantes por invitar a un nuevo usuario!"
                    )
                except:
                    pass

        # Verificar si el plan ha expirado (mejorado)
        if user_data.get("plan") in ["Pro", "Plus", "Ultra"] and user_data.get("expira") and user_data["expira"] < datetime.now():
            usuarios_col.update_one(
                {"user_id": user_id},
                {"$set": {
                    "plan": "Free",
                    "expira": None,
                    "limite_pedido": 0,
                    "limite_contenido": 0,
                    "limite_maximo": PLANES["Free"]["limite_maximo"]
                }}
            )
            user_data["plan"] = "Free"

        if random_id_param:
            contenido = peliculas_col.find_one({"random_id": random_id_param})

            if not contenido:
                return await message.reply("❌ El contenido solicitado no existe o fue eliminado.")

            # Verificar límites según el plan
            plan_actual = user_data.get("plan", "Free")
            limites_plan = PLANES.get(plan_actual, PLANES["Free"])
            
            if plan_actual != "Ultra":
                if user_data.get("limite_contenido", 0) >= user_data.get("limite_maximo", limites_plan["limite_maximo"]):
                    return await message.reply("🚫 Has alcanzado tu límite diario de contenido. Actualiza tu plan con /planes")

            try:
                if contenido["tipo"] == "película":
                    await client.copy_message(
                        chat_id=message.chat.id,
                        from_chat_id=CANAL_PRIVADO_ID,
                        message_id=contenido["id"],
                        protect_content=(user_data["plan"] != "Ultra")
                    )

                    for parte_id in contenido.get("partes", []):
                        await client.copy_message(
                            chat_id=message.chat.id,
                            from_chat_id=CANAL_PRIVADO_ID,
                            message_id=parte_id,
                            protect_content=(user_data["plan"] != "Ultra")
                        )

                    usuarios_col.update_one(
                        {"user_id": user_id},
                        {"$inc": {"limite_contenido": 1}}
                    )

                elif contenido["tipo"] == "serie":
                    markup = []
                    for idx, episodio in enumerate(contenido["partes"], start=1):
                        btn_text = f"📺 Episodio {idx}"
                        if "título" in episodio:
                            btn_text = f"{idx}. {episodio['título'][:20]}"

                        markup.append([InlineKeyboardButton(
                            btn_text,
                            callback_data=f"ep_{contenido['random_id']}_{episodio['id']}"
                        )])

                    markup.append([
                        InlineKeyboardButton("🎬 Enviar Todos", callback_data=f"send_all_{contenido['random_id']}")
                    ])

                    await message.reply(
                        f"**{contenido['título']}**\nSelecciona un episodio:",
                        reply_markup=InlineKeyboardMarkup(markup)
                    )
            except Exception as e:
                logger.error(f"Error al enviar contenido: {str(e)}")
                return await message.reply("❌ Error al enviar el contenido. Por favor, inténtalo más tarde.")

            return

        # Obtener límites según el plan actual
        plan_actual = user_data.get("plan", "Free")
        limites_plan = PLANES.get(plan_actual, PLANES["Free"])
        limite_pedido_actual = user_data.get("limite_pedido", 0)
        limite_contenido_actual = user_data.get("limite_contenido", 0)
        
        # Mostrar límites según el plan
        if plan_actual == "Ultra":
            texto_limites = "∞"
        else:
            texto_limites = f"{limite_pedido_actual}/{limites_plan['limite_pedido']} | {limite_contenido_actual}/{limites_plan['limite_contenido']}"

        welcome_msg = f"""🎬 **¡Bienvenido{' de vuelta' if user_data else ''} {user.first_name}!** 🍿

▸ 📌 Plan: **{user_data.get('plan', 'Free')}**
▸ 💎 Saldo: **{user_data.get('saldo', 0.00):.2f} diamantes**
▸ 🔄 Límites diarios:
   → Búsquedas/Contenidos: `{texto_limites}`"""

        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("📺 Canal Oficial", url="https://t.me/kudotv")],
            [
                InlineKeyboardButton("💎 Planes", callback_data="planes"),
                InlineKeyboardButton("👤 Perfil", callback_data="perfil")
            ],
            [
                InlineKeyboardButton("ℹ️ Info", callback_data="info"),
                InlineKeyboardButton("🆘 Ayuda", callback_data="ayuda")
            ]
        ])

        await message.reply(
            welcome_msg,
            reply_markup=keyboard,
            disable_web_page_preview=True
        )

        usuarios_col.update_one(
            {"user_id": user_id},
            {"$set": {"ultimo_acceso": datetime.now()}}
        )

    except Exception as e:
        logger.error(f"Error en start_command: {str(e)}")
        await message.reply("❌ Ocurrió un error. Por favor, inténtalo más tarde.")

@app.on_message(filters.command("search"))
async def buscar_contenido(client, message: Message):
    try:
        user_id = str(message.from_user.id)
        user_data = usuarios_col.find_one({"user_id": user_id})
        
        if not user_data:
            return await message.reply("❌ Primero debes iniciar con /start")
            
        # Búsquedas ilimitadas para todos los planes - eliminada verificación de límites
            
        args = message.text.split(None, 1)
        if len(args) < 2:
            return await message.reply("Uso: /search <nombre>")

        termino = args[1]
        termino_normalizado = normalizar_texto(termino)

        resultados = []
        try:
            pipeline = [
                {
                    "$addFields": {
                        "titulo_normalizado": {
                            "$toLower": "$título"
                        }
                    }
                },
                {
                    "$match": {
                        "titulo_normalizado": {
                            "$regex": f".*{re.escape(termino_normalizado)}.*",
                            "$options": "i"
                        }
                    }
                }
            ]
            documentos = peliculas_col.aggregate(pipeline)
            
            for doc in documentos:
                resultados.append({
                    "título": doc["título"],
                    "id": doc["random_id"],
                    "tipo": doc["tipo"]
                })

        except Exception as e:
            logger.error(f"Error en MongoDB: {e}")
            return await message.reply("❌ Error al buscar en la base de datos.")

        if not resultados:
            return await message.reply("❌ No se encontraron resultados.")

        # Búsquedas ilimitadas - eliminado incremento de contador

        texto = "**Resultados encontrados:**\n"
        botones = []
        for i, resultado in enumerate(resultados, start=1):
            texto += f"{i}. {resultado['título']} ({resultado['tipo'].capitalize()})\n"
            botones.append([InlineKeyboardButton(str(i), callback_data=f"get_{resultado['id']}")])

        await message.reply(texto, reply_markup=InlineKeyboardMarkup(botones))

    except Exception as e:
        logger.error(f"Error en buscar_contenido: {str(e)}")
        await message.reply("❌ Ocurrió un error al buscar. Por favor, inténtalo más tarde.")

@app.on_message(filters.command("index") & filters.user(ADMIN_IDS))
async def indexar_dinamicamente(client, message: Message):
    try:
        args = message.text.split()
        if len(args) < 4:
            return await message.reply("Uso: /index <id_inicio> <id_final> <yes/no>")

        inicio = int(args[1])
        fin = int(args[2])
        enviar_portadas = args[3].lower() == "yes"

        contenidos_indexados = []
        total_mensajes = fin - inicio + 1
        errores = 0

        def generar_id_aleatorio():
            return ''.join(random.choices(string.ascii_letters + string.digits, k=16))

        def extraer_titulo_limpio(caption):
            if caption:
                titulo = caption.split("\n")[0].strip()
                titulo = re.sub(r"📺\s*Serie:\s*|🎬\s*Película:\s*", "", titulo)
                return titulo
            return "Título no especificado"

        progreso_msg = await message.reply("Iniciando indexación...")

        indexados = 0
        for msg_id in range(inicio, fin + 1):
            try:
                msg = await client.get_messages(CANAL_PRIVADO_ID, msg_id)

                if msg.photo:
                    titulo_base = extraer_titulo_limpio(msg.caption)
                    temporada = re.search(r"(?i)temporada[:\s]*(\d+)", msg.caption or "")
                    
                    contenido_actual = {
                        "id": msg.id,
                        "random_id": generar_id_aleatorio(),
                        "título": f"{titulo_base} Temporada {temporada.group(1)}" if temporada else titulo_base,
                        "tipo": "serie" if "serie" in (msg.caption or "").lower() else "película",
                        "partes": []
                    }
                    contenidos_indexados.append(contenido_actual)

                elif msg.video and contenidos_indexados:
                    ultimo_contenido = contenidos_indexados[-1]
                    if ultimo_contenido["tipo"] == "serie":
                        ultimo_contenido["partes"].append({
                            "título": f"Episodio {len(ultimo_contenido['partes']) + 1}",
                            "id": msg.id
                        })
                    else:
                        ultimo_contenido["partes"].append(msg.id)

                indexados += 1
                porcentaje = (indexados / total_mensajes) * 100
                bloques = int(porcentaje / 10)
                barra = "█" * bloques + "░" * (10 - bloques)
                progreso = f"[👾] Progreso: {porcentaje:.1f}%\n{barra}\n✅ Reenviados: {indexados}/{total_mensajes}\n⚠️ Errores: {errores}"
                await progreso_msg.edit_text(progreso)

            except Exception as e:
                errores += 1
                logger.error(f"Error al procesar mensaje {msg_id}: {e}")

        if contenidos_indexados:
            peliculas_col.insert_many(contenidos_indexados)

        await progreso_msg.edit_text("✅ Indexación completada.")

        if enviar_portadas:
            for contenido in contenidos_indexados:
                try:
                    enlace_bot = f"https://t.me/{BOT_USERNAME}?start={contenido['random_id']}"
                    botones = [[InlineKeyboardButton("Ver aquí", url=enlace_bot)]]
                    await client.copy_message(
                        chat_id=CANAL_DESTINO_ID,
                        from_chat_id=CANAL_PRIVADO_ID,
                        message_id=contenido["id"],
                        reply_markup=InlineKeyboardMarkup(botones)
                    )
                    await asyncio.sleep(60)
                except Exception as e:
                    logger.error(f"Error al enviar portada: {e}")

    except Exception as e:
        logger.error(f"Error durante la indexación: {e}")
        await message.reply(f"❌ Error durante la indexación: {e}")

@app.on_message(filters.command("reenviarportadas") & filters.user(ADMIN_IDS))
async def reenviar_portadas(client, message: Message):
    try:
        args = message.text.split()
        if len(args) < 3:
            return await message.reply("Uso: /reenviarportadas <id_inicio> <id_final>")

        inicio = int(args[1])
        fin = int(args[2])
        total = fin - inicio + 1
        reenviadas = 0
        errores = 0

        progreso_msg = await message.reply("🔄 Iniciando reenvío de portadas...")

        def generar_barra_progreso(enviados, total, errores, id_actual):
            porcentaje = (enviados / total) * 100
            barra = "█" * int(porcentaje // 10) + "░" * (10 - int(porcentaje // 10))
            return f"[👾] Progreso: {porcentaje:.1f}%\n{barra}\n✅ Enviadas: {enviados}/{total}\n⚠️ Errores: {errores}\n🔄 ID actual: {id_actual}"

        for msg_id in range(inicio, fin + 1):
            try:
                msg = await client.get_messages(CANAL_PRIVADO_ID, msg_id)
                
                if msg.photo or msg.video:
                    contenido = peliculas_col.find_one({"id": msg.id})
                    
                    if contenido:
                        enlace_bot = f"https://t.me/{BOT_USERNAME}?start={contenido['random_id']}"
                        botones = [[InlineKeyboardButton("🎬 Ver Aquí", url=enlace_bot)]]

                        await client.copy_message(
                            chat_id=CANAL_DESTINO_ID,
                            from_chat_id=CANAL_PRIVADO_ID,
                            message_id=msg.id,
                            reply_markup=InlineKeyboardMarkup(botones)
                        )
                        reenviadas += 1
                    else:
                        logger.warning(f"⚠️ ID {msg.id} no encontrado en MongoDB")
                        continue

                    await progreso_msg.edit_text(generar_barra_progreso(reenviadas, total, errores, msg_id))
                    await asyncio.sleep(5)

            except Exception as e:
                errores += 1
                logger.error(f"Error en ID {msg_id}: {str(e)}")

        await progreso_msg.edit_text(f"✅ Reenvío completado.\nEnviadas: {reenviadas}\nErrores: {errores}")

    except Exception as e:
        logger.error(f"Error crítico: {str(e)}")
        await message.reply(f"❌ Error crítico: {str(e)}")

@app.on_message(filters.command("stats") & filters.user(ADMIN_IDS))
async def mostrar_estadisticas(client, message: Message):
    try:
        total_usuarios = usuarios_col.count_documents({})
        
        siete_dias_atras = datetime.now() - timedelta(days=7)
        usuarios_activos = usuarios_col.count_documents({
            "ultimo_acceso": {"$gte": siete_dias_atras}
        })
        
        pro = usuarios_col.count_documents({"plan": "Pro"})
        plus = usuarios_col.count_documents({"plan": "Plus"})
        ultra = usuarios_col.count_documents({"plan": "Ultra"})
        
        total_indexado = peliculas_col.count_documents({})
        total_descargas = sum(len(p.get("partes", [])) for p in peliculas_col.find())
        
        # Obtener estadísticas de pedidos
        total_pedidos = pedidos_col.count_documents({})
        pedidos_pendientes = pedidos_col.count_documents({"estado": "pendiente"})
        pedidos_completados = pedidos_col.count_documents({"estado": "completado"})
        
        size_mb = 45.7
        fecha_actual = datetime.now().strftime("%Y-%m-%Y %H:%M:%S")

        texto = f"""📊 **Estadísticas del Bot**

👥 **Usuarios:**
├ Total: {total_usuarios}
├ Activos (7 días): {usuarios_activos}
├ Plan Pro: {pro}
├ Plan Plus: {plus}
└ Plan Ultra: {ultra}

🎬 **Contenido:**
├ Total indexado: {total_indexado}
├ Total descargas: {total_descargas}
└ Tamaño DB: {size_mb} MB

📋 **Pedidos:**
├ Total: {total_pedidos}
├ Pendientes: {pedidos_pendientes}
└ Completados: {pedidos_completados}

📅 **Fecha:** {fecha_actual}"""

        botones = [[InlineKeyboardButton("🔄 Actualizar", callback_data="actualizar_stats")]]
        await message.reply(texto, reply_markup=InlineKeyboardMarkup(botones))

    except Exception as e:
        logger.error(f"Error al obtener estadísticas: {e}")
        await message.reply(f"❌ Error al obtener estadísticas:\n`{e}`")

@app.on_callback_query(filters.regex("actualizar_stats"))
async def actualizar_estadisticas(client, callback_query: CallbackQuery):
    try:
        total_usuarios = usuarios_col.count_documents({})
        siete_dias_atras = datetime.now() - timedelta(days=7)
        usuarios_activos = usuarios_col.count_documents({"ultimo_acceso": {"$gte": siete_dias_atras}})
        pro = usuarios_col.count_documents({"plan": "Pro"})
        plus = usuarios_col.count_documents({"plan": "Plus"})
        ultra = usuarios_col.count_documents({"plan": "Ultra"})
        total_indexado = peliculas_col.count_documents({})
        total_descargas = sum(len(p.get("partes", [])) for p in peliculas_col.find())
        
        # Obtener estadísticas de pedidos
        total_pedidos = pedidos_col.count_documents({})
        pedidos_pendientes = pedidos_col.count_documents({"estado": "pendiente"})
        pedidos_completados = pedidos_col.count_documents({"estado": "completado"})
        
        fecha_actual = datetime.now().strftime("%Y-%m-%Y %H:%M:%S")

        texto = f"""📊 **Estadísticas Actualizadas**

👥 **Usuarios:**
├ Total: {total_usuarios}
├ Activos (7 días): {usuarios_activos}
├ Plan Pro: {pro}
├ Plan Plus: {plus}
└ Plan Ultra: {ultra}

🎬 **Contenido:**
├ Total indexado: {total_indexado}
├ Total descargas: {total_descargas}
└ Tamaño DB: 45.7 MB

📋 **Pedidos:**
├ Total: {total_pedidos}
├ Pendientes: {pedidos_pendientes}
└ Completados: {pedidos_completados}

📅 **Fecha:** {fecha_actual}"""

        botones = [[InlineKeyboardButton("🔄 Actualizar", callback_data="actualizar_stats")]]
        await callback_query.message.edit_text(texto, reply_markup=InlineKeyboardMarkup(botones))

    except Exception as e:
        logger.error(f"Error al actualizar estadísticas: {e}")
        await callback_query.message.edit_text(f"❌ Error al actualizar: {e}")

@app.on_message(filters.command("setplan") & filters.user(ADMIN_IDS))
async def set_plan(client, message: Message):
    try:
        args = message.text.split()
        if len(args) != 3:
            return await message.reply("Uso: /setplan <user_id> <plan>")

        user_id = args[1]
        nuevo_plan = args[2].capitalize()

        if nuevo_plan not in ["Free", "Pro", "Plus", "Ultra"]:
            return await message.reply("❌ Plan inválido. Opciones: Free, Pro, Plus, Ultra")

        # Establecer expiración según el plan
        if nuevo_plan != "Free":
            expiracion = datetime.now() + timedelta(days=30)
        else:
            expiracion = None

        result = usuarios_col.update_one(
            {"user_id": user_id},
            {"$set": {
                "plan": nuevo_plan,
                "expira": expiracion,
                "limite_pedido": 0,
                "limite_contenido": 0
            }},
            upsert=False
        )

        if result.modified_count > 0:
            await message.reply(f"✅ Plan actualizado a {nuevo_plan} para {user_id}")
        else:
            await message.reply("❌ Usuario no encontrado")

    except Exception as e:
        logger.error(f"Error en set_plan: {e}")
        await message.reply(f"Error: {e}")
        
@app.on_message(filters.command("recargar") & filters.user(ADMIN_IDS))
async def recargar_diamantes(client, message: Message):
    try:
        args = message.text.split()
        if len(args) < 3:
            return await message.reply("Uso: /recargar <user_id> <cantidad>")

        user_id = args[1]
        cantidad = float(args[2])

        result = usuarios_col.update_one(
            {"user_id": user_id},
            {"$inc": {"saldo": cantidad}},
            upsert=False
        )

        if result.modified_count > 0:
            await message.reply(f"✅ Recargados {cantidad:.2f} diamantes a {user_id}")
        else:
            await message.reply("❌ Usuario no encontrado")

    except ValueError:
        await message.reply("❌ La cantidad debe ser un número")
    except Exception as e:
        logger.error(f"Error en recargar_diamantes: {e}")
        await message.reply(f"Error: {e}")
 
@app.on_callback_query(filters.regex("mensaje_principal"))
async def mensaje_principal(client, callback_query: CallbackQuery):
    try:
        user_id = str(callback_query.from_user.id)
        user_data = usuarios_col.find_one({"user_id": user_id})

        if not user_data:
            texto = f"""🎬 ¡Bienvenido a KudoTV! 🍿

🔹 Usa /start para registrarte y acceder al catálogo."""
            botones = [
                [InlineKeyboardButton("📺 Canal Oficial", url="https://t.me/kudotv")],
                [InlineKeyboardButton("👥 Grupo", url="https://t.me/+OJtfsw5-zANmNzQx")]
            ]
        else:
            # Verificar si el plan ha expirado
            if user_data.get("plan") in ["Pro", "Plus", "Ultra"] and user_data.get("expira") and user_data["expira"] < datetime.now():
                usuarios_col.update_one(
                    {"user_id": user_id},
                    {"$set": {
                        "plan": "Free",
                        "expira": None,
                        "limite_pedido": 0,
                        "limite_contenido": 0
                    }}
                )
                user_data["plan"] = "Free"

            nombre = user_data.get("nombre", "Usuario")
            plan = user_data.get("plan", "Free")
            saldo = user_data.get("saldo", 0.00)
            
            texto = f"""✨ **Hola {nombre}** 👋

▸ 📌 Plan: **{plan}**
▸ 💎 Saldo: **{saldo:.2f} diamantes**
▸ 🔄 Límites diarios:
   → Búsquedas: `∞`
   → Contenidos: `{user_data.get('limite_contenido', 0)}/{'∞' if plan == 'Ultra' else PLANES[plan]['limite_contenido']}`"""

            botones = [
                [InlineKeyboardButton("📺 Canal Oficial", url="https://t.me/kudotv")],
                [
                    InlineKeyboardButton("💎 Planes", callback_data="planes"),
                    InlineKeyboardButton("👤 Perfil", callback_data="perfil")
                ],
                [
                    InlineKeyboardButton("ℹ️ Info", callback_data="info"),
                    InlineKeyboardButton("🆘 Ayuda", callback_data="ayuda")
                ]
            ]

        await callback_query.message.edit(
            texto,
            reply_markup=InlineKeyboardMarkup(botones),
            disable_web_page_preview=True
        )
        await callback_query.answer()

    except Exception as e:
        logger.error(f"Error en mensaje_principal: {str(e)}")
        await callback_query.answer("❌ Error al cargar el mensaje principal.")

@app.on_callback_query(filters.regex("planes"))
async def planes_callback(client, callback_query: CallbackQuery):
    try:
        user_id = str(callback_query.from_user.id)
        
        usuario = usuarios_col.find_one({"user_id": user_id})
        
        if not usuario:
            return await callback_query.answer("❌ Primero debes registrarte con /start", show_alert=True)
        
        saldo = usuario.get("saldo", 0.00)
        plan_actual = usuario.get("plan", "Free")

        texto = f"""🎁 **Planes de Suscripción | {plan_actual}**

💎 Saldo disponible: {saldo:.2f} diamantes

▰▰▰▰▰▰▰▰▰▰▰▰▰
✨ **Plan FREE (Gratis)**
→ Búsquedas ilimitadas
→ {PLANES['Free']['limite_contenido']} contenidos cada 24 horas
→ Sin reenvío/guardado
→ Soporte básico

✨ **Plan PRO** 100💎
→ Búsquedas ilimitadas
→ {PLANES['Pro']['limite_contenido']} contenidos cada 24 horas
→ Sin reenvío/guardado
→ Soporte estándar

✨ **Plan PLUS** 300💎
→ Búsquedas ilimitadas
→ {PLANES['Plus']['limite_contenido']} contenidos cada 24 horas
→ Sin reenvío/guardado
→ Soporte prioritario

✨ **Plan ULTRA** 500💎
→ Búsquedas ilimitadas
→ Contenidos ilimitados
→ Reenvío/guardado completo
→ Soporte VIP
→ Acceso exclusivo al grupo de estrenos
▰▰▰▰▰▰▰▰▰▰▰▰▰

💡 **También disponible en:**
📱 Saldo móvil • 💳 Tarjeta CUP • 💰 USDT"""

        botones = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("PRO 🛒", callback_data="plan_pro"),
                InlineKeyboardButton("PLUS 🛒", callback_data="plan_plus"),
                InlineKeyboardButton("ULTRA 🛒", callback_data="plan_ultra")
            ],
            [
                InlineKeyboardButton("💸 Recargar Saldo", callback_data="recargar_menu"),
                InlineKeyboardButton("📊 Mi Perfil", callback_data="perfil")
            ],
            [InlineKeyboardButton("🔙 Volver al Inicio", callback_data="mensaje_principal")]
        ])

        await callback_query.message.edit_text(texto, reply_markup=botones)
        await callback_query.answer()

    except Exception as e:
        error_msg = f"❌ Error al cargar los planes: {str(e)}"
        await callback_query.message.reply(error_msg)
        await callback_query.answer()

@app.on_callback_query(filters.regex("plan_"))
async def opciones_pago(client, callback_query: CallbackQuery):
    plan = callback_query.data.split("_")[1]

    costos_diamantes = {"pro": 100, "plus": 300, "ultra": 500}
    costos_cup = {"pro": 100, "plus": 300, "ultra": 500}
    costos_usdt = {"pro": 1, "plus": 1.5, "ultra": 2}
    
    texto = f"""**▧ Pago del Plan {plan.capitalize()} ▧**

💰 **Precios:**
- 💎 Diamantes: {costos_diamantes[plan]}
- 📱 Saldo móvil: {costos_cup[plan]} CUP
- 💳 Tarjeta CUP: {costos_cup[plan]} CUP
- 💰 USDT: {costos_usdt[plan]} USDT

Elige el método de pago:
"""
    botones = [
        [InlineKeyboardButton("💎 Saldo Bot", callback_data=f"comprar|{plan}|saldo_bot")],
        [InlineKeyboardButton("📱 Saldo Móvil", callback_data=f"comprar|{plan}|saldo_movil")],
        [InlineKeyboardButton("💳 Tarjeta CUP", callback_data=f"comprar|{plan}|tarjeta_cup")],
        [InlineKeyboardButton("💰 USDT BEP20", callback_data=f"comprar|{plan}|usdt_bep20")],
        [InlineKeyboardButton("💰 USDT TRC20", callback_data=f"comprar|{plan}|usdt_trc20")],
        [InlineKeyboardButton("🔒 TRX (Próximamente)", callback_data="trx_soon")],
        [InlineKeyboardButton("🔙 Volver", callback_data="planes")]
    ]
    await callback_query.message.edit_text(texto, reply_markup=InlineKeyboardMarkup(botones))
    await callback_query.answer()

@app.on_callback_query(filters.regex("trx_soon"))
async def trx_proximamente(client, callback_query: CallbackQuery):
    await callback_query.answer("🔒 TRX estará disponible próximamente", show_alert=True)

@app.on_callback_query(filters.regex("^saldo_movil_"))
async def pago_saldo(client, callback_query: CallbackQuery):
    plan_id = callback_query.data.split("_")[2]
    plan_info = PLANES.get(f"plan_{plan_id}")

    if plan_info:
        texto = f"""**Pago con Saldo ETECSA**

**Precio:** 400 SALDO

**Detalles de pago:**
Telef: 50664186
 Ya (incluye 20% adicional)

⚠️ Después de realizar el pago, mandar captura del pago a @Emanuel14APK para activar tu plan."""
        botones = [[InlineKeyboardButton("Volver", callback_data="planes")]]
        await callback_query.message.edit(
            text=texto, 
            reply_markup=InlineKeyboardMarkup(botones)
        )
    else:
        await callback_query.answer("El plan no existe.", show_alert=True)

@app.on_callback_query(filters.regex("view_"))
async def mostrar_contenido(client, callback_query: CallbackQuery):
    random_id = callback_query.data.split("_")[1]

    try:
        contenido = peliculas_col.find_one({"random_id": random_id})
        if not contenido:
            return await callback_query.message.reply("❌ No se encontró el contenido.")

        if contenido["tipo"] == "serie":
            texto_episodios = f"**{contenido['título']}**\nSelecciona un episodio:"
            botones = []
            for i, episodio in enumerate(contenido.get("partes", []), start=1):
                botones.append([InlineKeyboardButton(f"Capítulo {i}", callback_data=f"episodio_{episodio['id']}")])
            botones.append([InlineKeyboardButton("Enviar todos", callback_data=f"send_all_{contenido['id']}")])

            await client.send_message(
                chat_id=callback_query.message.chat.id,
                text=texto_episodios,
                reply_markup=InlineKeyboardMarkup(botones)
            )
        elif contenido["tipo"] == "película":
            try:
                await client.copy_message(
                    chat_id=callback_query.message.chat.id,
                    from_chat_id=CANAL_PRIVADO_ID,
                    message_id=contenido["id"]
                )
                for parte in contenido.get("partes", []):
                    await client.copy_message(
                        chat_id=callback_query.message.chat.id,
                        from_chat_id=CANAL_PRIVADO_ID,
                        message_id=parte
                    )
            except Exception as e:
                return await callback_query.message.reply(f"❌ Error al enviar el contenido: {e}")

    except Exception as e:
        logger.error(f"Error en mostrar_contenido: {e}")
        return await callback_query.message.reply("❌ Error al procesar el contenido.")

@app.on_callback_query(filters.regex("show_"))
async def mostrar_episodios(client, callback_query: CallbackQuery):
    titulo = callback_query.data.split("_", 1)[1]

    contenido = peliculas_col.find_one({"título": titulo, "tipo": "serie"})
    if not contenido:
        return await callback_query.message.reply("No se encontraron episodios para esta serie.")

    texto = f"**Episodios de {titulo}:**\n"
    botones = []

    for episodio in contenido["partes"]:
        texto += f"• {episodio['título']}\n"
        botones.append([InlineKeyboardButton(episodio["título"], callback_data=f"get_{episodio['id']}")])

    botones.append([InlineKeyboardButton("Enviar todo", callback_data=f"send_all_{titulo}")])
    botones.append([InlineKeyboardButton("Volver", callback_data="planes")])

    await callback_query.message.edit(texto, reply_markup=InlineKeyboardMarkup(botones))
    
@app.on_callback_query(filters.regex("send_all_"))
async def enviar_todos_episodios(client, callback_query: CallbackQuery):
    try:
        random_id = callback_query.data.split("send_all_", 1)[1]
        user_id = str(callback_query.from_user.id)

        usuario = usuarios_col.find_one({"user_id": user_id})
        if not usuario:
            return await callback_query.answer("❌ Debes registrarte primero con /start", show_alert=True)

        if usuario.get("plan") != "Ultra":
            if usuario.get("limite_contenido", 0) >= usuario.get("limite_maximo", 5):
                return await callback_query.answer("❌ Límite diario alcanzado", show_alert=True)

        serie = peliculas_col.find_one({
            "random_id": random_id,
            "tipo": "serie"
        })

        if not serie:
            return await callback_query.answer("❌ Serie no encontrada", show_alert=True)

        protect_content = usuario.get("plan") != "Ultra"
        enviados = 0
        errores = 0

        progress_msg = await callback_query.message.reply("⏳ Iniciando envío...")

        for episodio in serie.get("partes", []):
            try:
                await client.copy_message(
                    chat_id=callback_query.message.chat.id,
                    from_chat_id=CANAL_PRIVADO_ID,
                    message_id=episodio["id"],
                    protect_content=protect_content
                )
                enviados += 1
                
                if enviados % 5 == 0:
                    await progress_msg.edit_text(
                        f"📤 Progreso: {enviados}/{len(serie['partes'])} episodios enviados\n"
                        f"⚠️ Errores: {errores}"
                )
                    
                await asyncio.sleep(0.5)

            except Exception as e:
                errores += 1
                logger.error(f"Error enviando episodio {episodio.get('id')}: {str(e)}")

        nuevos_datos = {
            "$inc": {
                "limite_contenido": enviados,
                "total_descargas": enviados
            },
            "$set": {"ultimo_acceso": datetime.now()}
        }
        usuarios_col.update_one({"user_id": user_id}, nuevos_datos)

        peliculas_col.update_one(
            {"random_id": random_id},
            {"$inc": {"veces_enviado": enviados}}
        )

        await progress_msg.delete()
        resultado = f"""✅ Envío completado
→ Episodios enviados: {enviados}
→ Errores: {errores}
→ Nuevo límite: {usuario.get('limite_contenido', 0) + enviados}/{usuario.get('limite_maximo', 5)}"""

        if errores > 0:
            resultado += "\n\n⚠️ Algunos episodios no pudieron enviarse. Contacta a soporte."

        await callback_query.message.reply(resultado)
        await callback_query.answer()

    except Exception as e:
        logger.error(f"Error en enviar_todos_episodios: {str(e)}")
        await callback_query.answer("❌ Error crítico al procesar la solicitud", show_alert=True)
        await callback_query.message.reply(f"⚠️ Error grave: {str(e)[:200]}")
   
@app.on_callback_query(filters.regex("get_"))
async def procesar_seleccion(client, callback_query: CallbackQuery):
    try:
        user_id = str(callback_query.from_user.id)
        random_id = callback_query.data.split("_", 1)[1]

        usuario = usuarios_col.find_one({"user_id": user_id})
        if not usuario:
            return await callback_query.answer("🔒 Debes iniciar con /start primero", show_alert=True)

        if usuario["plan"] != "Ultra" and usuario.get("limite_contenido", 0) >= usuario.get("limite_maximo", 5):
            return await callback_query.answer("❌ Límite diario alcanzado", show_alert=True)

        contenido = peliculas_col.find_one({"random_id": random_id})
        if not contenido:
            return await callback_query.answer("❌ El contenido ya no está disponible", show_alert=True)

        protect_content = usuario["plan"] != "Ultra"

        await client.copy_message(
            chat_id=callback_query.message.chat.id,
            from_chat_id=CANAL_PRIVADO_ID,
            message_id=contenido["id"],
            protect_content=protect_content
        )

        if contenido["tipo"] == "serie":
            markup = []
            for idx, episodio in enumerate(contenido["partes"], start=1):
                btn_text = f"📺 Episodio {idx}"
                if "título" in episodio:
                    btn_text = f"{idx}. {episodio['título'][:20]}"
                
                markup.append([
                    InlineKeyboardButton(
                        btn_text,
                        callback_data=f"ep_{contenido['random_id']}_{episodio['id']}"
                    )
                ])

            markup.append([
                InlineKeyboardButton("🎬 Enviar Todos", callback_data=f"send_all_{contenido['random_id']}")
            ])

            await callback_query.message.reply(
                f"**{contenido['título']}**\nSelecciona un episodio:",
                reply_markup=InlineKeyboardMarkup(markup)
            )

        elif contenido["tipo"] == "película":
            for parte_id in contenido.get("partes", []):
                await client.copy_message(
                    chat_id=callback_query.message.chat.id,
                    from_chat_id=CANAL_PRIVADO_ID,
                    message_id=parte_id,
                    protect_content=protect_content
                )

            usuarios_col.update_one(
                {"user_id": user_id},
                {"$inc": {"limite_contenido": 1}}
            )

        usuarios_col.update_one(
            {"user_id": user_id},
            {"$set": {"ultimo_acceso": datetime.now()}}
        )

        await callback_query.answer("✅ Contenido enviado correctamente")

    except Exception as e:
        logger.error(f"Error en procesar_seleccion: {str(e)}")
        await callback_query.answer("⚠️ Ocurrió un error, intenta nuevamente")

@app.on_callback_query(filters.regex(r"^ep_"))
async def manejar_episodio(client, callback_query: CallbackQuery):
    try:
        data = callback_query.data.split("_")
        random_id = data[1]
        episode_id = int(data[2])
        user_id = str(callback_query.from_user.id)

        usuario = usuarios_col.find_one({"user_id": user_id})
        protect_content = usuario.get("plan", "Free") != "Ultra"

        contenido = peliculas_col.find_one({
            "random_id": random_id,
            "partes.id": episode_id
        }, {"partes.$": 1})

        if not contenido or not contenido["partes"]:
            return await callback_query.answer("❌ Episodio no disponible")

        await client.copy_message(
            chat_id=callback_query.message.chat.id,
            from_chat_id=CANAL_PRIVADO_ID,
            message_id=contenido["partes"][0]["id"],
            protect_content=protect_content
        )

        usuarios_col.update_one(
            {"user_id": user_id},
            {"$inc": {"limite_contenido": 1}}
        )

        await callback_query.answer("🎬 Episodio enviado")

    except Exception as e:
        logger.error(f"Error en manejar_episodio: {e}")
        await callback_query.answer("❌ Error al enviar episodio")

@app.on_callback_query(filters.regex(r"^send_all_"))
async def enviar_todo_contenido(client, callback_query: CallbackQuery):
    try:
        user_id = str(callback_query.from_user.id)
        random_id = callback_query.data.split("_", 2)[2]
        
        usuario = usuarios_col.find_one({"user_id": user_id})
        if not usuario or usuario.get("plan") != "Ultra":
            return await callback_query.answer("🔒 Requiere plan Ultra", show_alert=True)

        contenido = peliculas_col.find_one({"random_id": random_id})
        if not contenido:
            return await callback_query.answer("❌ Contenido no encontrado")

        for item in [contenido["id"]] + [p["id"] if isinstance(p, dict) else p for p in contenido.get("partes", [])]:
            await client.copy_message(
                chat_id=callback_query.message.chat.id,
                from_chat_id=CANAL_PRIVADO_ID,
                message_id=item,
                protect_content=False
            )

        usuarios_col.update_one(
            {"user_id": user_id},
            {"$inc": {"limite_contenido": len(contenido.get("partes", [])) + 1}}
        )

        await callback_query.answer("📤 Todos los elementos enviados")

    except Exception as e:
        logger.error(f"Error en enviar_todo_contenido: {e}")
        await callback_query.answer(f"❌ Error: {str(e)[:50]}")

@app.on_callback_query(filters.regex("episodio_"))
async def enviar_episodio(client, callback_query: CallbackQuery):
    try:
        data_parts = callback_query.data.split("_")
        if len(data_parts) < 3:
            return await callback_query.answer("❌ Solicitud inválida", show_alert=True)
            
        contenido_id = data_parts[1]
        episodio_id = data_parts[2]

        serie = peliculas_col.find_one({
            "random_id": contenido_id,
            "tipo": "serie"
        })

        if not serie:
            return await callback_query.answer("❌ Serie no encontrada", show_alert=True)

        episodio = next((
            ep for ep in serie.get("partes", [])
            if str(ep.get("id")) == episodio_id
        ), None)

        if not episodio:
            return await callback_query.answer("❌ Episodio no disponible", show_alert=True)

        user_id = str(callback_query.from_user.id)
        usuario = usuarios_col.find_one({"user_id": user_id})
        
        if not usuario:
            return await callback_query.answer("❌ Debes registrarte primero", show_alert=True)

        plan = usuario.get("plan", "Free")
        allow_forwarding = plan == "Ultra"
        
        await client.copy_message(
            chat_id=callback_query.message.chat.id,
            from_chat_id=CANAL_PRIVADO_ID,
            message_id=episodio["id"],
            protect_content=not allow_forwarding
        )

        nuevos_limites = {
            "$inc": {
                "limite_contenido": 1,
                "limite_pedido": 1 if usuario.get("limite_pedido", 0) == 0 else 0
            }
        }
        usuarios_col.update_one({"user_id": user_id}, nuevos_limites)

        await callback_query.answer(f"🎬 {episodio.get('título', 'Episodio')} enviado", show_alert=False)

    except Exception as e:
        error_msg = f"""⚠️ Error al procesar la solicitud:
        
{str(e)}
Por favor intenta nuevamente o contacta soporte"""
        await callback_query.message.reply(error_msg)
        await callback_query.answer()

@app.on_callback_query(filters.regex("ayuda"))
async def ayuda_command(client, callback_query: CallbackQuery):
    texto = """**🆘 Comandos Disponibles:**

/start - Inicia el bot y te da la bienvenida.
/search <nombre> - Busca series o películas en tu colección.
/index <id_inicio> <id_final> <yes/no> - Indexa contenido desde el canal privado (ADMIN).
/reenviarportadas <id_inicio> <id_final> - Reenvía portadas de contenido al canal público(ADMIN).
/setplan <user_id> <plan> - Cambia el plan de un usuario (admin).
/recargar >user_id> <cantidad> - Recarga diamantes a un usuario (admin).
/get_code >Codigo de regalo> - Obten la cantidad de diamantes que tenga el codigo e implementalo para mejorar tu plan.
/gen_code - Genera el codigo de regalo para los usuarios aleatoriamente (admin).
/pedidos - Realiza el pedido que desee para que el admin lo complete, en cuanto se complete sera notificado.

**¡Utiliza estos comandos para interactuar con el bot!**"""
    
    await callback_query.message.edit(texto)

@app.on_callback_query(filters.regex("info"))
async def info_command(client, callback_query: CallbackQuery):
    try:
        total_usuarios = usuarios_col.count_documents({})
        total_indexado = peliculas_col.count_documents({})
        
        tiempo_activo = calcular_tiempo_activo()
        version = "v1.1.0"
        inicio_operaciones = datetime(2025, 5, 10).strftime("%d/%m/%Y")

        texto = f"""🔍 **Información del Sistema**

⏳ **Tiempo activo:** {tiempo_activo}
🛠 **Versión:** {version}
📅 **Inicio de operaciones:** {inicio_operaciones}

💻 **Tecnología:**
▸ Lenguaje: Python 3.13
▸ Database: MongoDB Atlas
▸ Framework: Pyrogram 2.0

📊 **Estadísticas:**
▸ Usuarios registrados: {total_usuarios}
▸ Contenido indexado: {total_indexado}

📢 **Canal oficial:** @kudotv
🆘 **Soporte técnico:** @Emanuel14APK"""

        botones = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 Actualizar", callback_data="info")]
        ])

        await callback_query.message.edit_text(texto, reply_markup=botones)
        await callback_query.answer()

    except Exception as e:
        error_msg = f"❌ Error al obtener información: {str(e)}"
        await callback_query.message.edit_text(error_msg)
        await callback_query.answer()

@app.on_callback_query(filters.regex("perfil"))
async def mostrar_perfil(client, callback_query: CallbackQuery):
    try:
        user_id = str(callback_query.from_user.id)
        usuario = usuarios_col.find_one({"user_id": user_id})
        
        if not usuario:
            return await callback_query.answer("❌ Primero usa /start para registrarte", show_alert=True)

        # Verificar si el plan ha expirado
        if usuario.get("plan") in ["Pro", "Plus", "Ultra"] and usuario.get("expira") and usuario["expira"] < datetime.now():
            usuarios_col.update_one(
                {"user_id": user_id},
                {"$set": {
                    "plan": "Free",
                    "expira": None,
                    "limite_pedido": 0,
                    "limite_contenido": 0
                }}
            )
            usuario["plan"] = "Free"

        expiracion = usuario.get("expira")
        dias_restantes = "N/A"
        if expiracion and isinstance(expiracion, datetime):
            dias_restantes = (expiracion - datetime.now()).days
            if dias_restantes < 0:
                dias_restantes = "Expirado"

        texto = f"""🌟 **Perfil de {usuario.get('nombre', 'Usuario')}**

▸ 💎 **Saldo:** {usuario.get('saldo', 0.00):.2f} diamantes
▸ 🆔 **ID:** `{user_id}`
▸ 📅 **Registro:** {usuario.get('fecha_union', 'N/A')}
▸ 💼 **Plan:** {usuario.get('plan', 'Free')}
▸ ⏳ **Expiración:** {expiracion.strftime('%d/%m/%Y') if expiracion else 'N/A'} ({dias_restantes} días)
        
▸ 🔄 **Límites diarios:**
   → Búsquedas: ∞
   → Contenidos: {usuario.get('limite_contenido', 0)}/{'∞' if usuario.get('plan') == 'Ultra' else PLANES[usuario.get('plan', 'Free')]['limite_contenido']}"""

        botones = [
            [
                InlineKeyboardButton("💎 Recargar", callback_data="recargar_menu"),
                InlineKeyboardButton("📈 Planes", callback_data="planes")
            ]
        ]
        
        # Solo mostrar botón de grupo de estrenos para usuarios Ultra
        if usuario.get("plan") == "Ultra":
            botones.append([InlineKeyboardButton("🎬 Grupo de Estrenos", url=GRUPO_ESTRENOS)])
            
        botones.append([InlineKeyboardButton("🔙 Volver", callback_data="mensaje_principal")])
        
        await callback_query.message.edit_text(
            texto, 
            reply_markup=InlineKeyboardMarkup(botones)
        )
        await callback_query.answer()

    except Exception as e:
        await callback_query.message.reply(f"❌ Error al cargar el perfil: {str(e)}")
        await callback_query.answer()

@app.on_callback_query(filters.regex("recargar_menu"))
async def recargar_menu(client, callback_query: CallbackQuery):
    try:
        user_id = str(callback_query.from_user.id)
        usuario = usuarios_col.find_one({"user_id": user_id})
        
        if not usuario:
            return await callback_query.answer("❌ Primero debes registrarte con /start", show_alert=True)

        texto = f"""💎 **Recargar Saldo**

▸ Saldo actual: {usuario.get('saldo', 0.00):.2f} diamantes

Elige el método de recarga:"""

        botones = InlineKeyboardMarkup([
            [InlineKeyboardButton("📱 Saldo Móvil", callback_data="recarga_saldo_movil")],
            [InlineKeyboardButton("💳 Tarjeta CUP", callback_data="recarga_tarjeta_cup")],
            [InlineKeyboardButton("💰 USDT", callback_data="recarga_usdt")],
            [InlineKeyboardButton("🎁 Código de Regalo", callback_data="recarga_codigo")],
            [InlineKeyboardButton("🔙 Volver", callback_data="mensaje_principal")]
        ])

        await callback_query.message.edit_text(texto, reply_markup=botones)
        await callback_query.answer()

    except Exception as e:
        logger.error(f"Error en recargar_menu: {e}")
        await callback_query.answer("❌ Error al cargar el menú de recarga")

@app.on_callback_query(filters.regex("^comprar|"))
async def comprar_plan(client, callback_query: CallbackQuery):
    try:
        user_id = str(callback_query.from_user.id)
        data = callback_query.data.split("|")

        if len(data) != 3:
            return await callback_query.answer("❌ Solicitud inválida", show_alert=True)

        plan = data[1].lower()
        metodo_pago = data[2].lower()

        valid_plans = ["pro", "plus", "ultra"]
        valid_methods = ["saldo_bot", "saldo_movil", "tarjeta_cup", "usdt_bep20", "usdt_trc20"]

        if plan not in valid_plans or metodo_pago not in valid_methods:
            return await callback_query.answer("❌ Método de pago no válido", show_alert=True)

        usuario = usuarios_col.find_one({"user_id": user_id})
        if not usuario:
            return await callback_query.answer("❌ Primero inicia con /start", show_alert=True)

        plan_nombre = plan.capitalize()
        precio_diamantes = PLANES[plan_nombre]["precio_diamantes"]
        
        if metodo_pago == "saldo_bot":
            # Verificar saldo suficiente
            if usuario.get("saldo", 0) < precio_diamantes:
                return await callback_query.answer("❌ Saldo insuficiente", show_alert=True)
                
            # Procesar compra con saldo
            expiracion = datetime.now() + timedelta(days=30)
            
            # Actualizar usuario
            usuarios_col.update_one(
                {"user_id": user_id},
                {"$set": {
                    "plan": plan_nombre,
                    "expira": expiracion,
                    "limite_maximo": PLANES[plan_nombre]["limite_maximo"]
                },
                "$inc": {
                    "saldo": -precio_diamantes
                }}
            )
            
            # Si es plan Ultra, enviar enlace al grupo de estrenos
            if plan_nombre == "Ultra":
                try:
                    await callback_query.message.reply(
                        f"🎉 ¡Felicidades! Ahora tienes acceso al grupo exclusivo de estrenos:\n{GRUPO_ESTRENOS}",
                        disable_web_page_preview=True
                    )
                except:
                    pass
            
            # Notificar al usuario
            await callback_query.message.edit_text(
                f"✅ ¡Felicidades! Ahora tienes el plan {plan_nombre}\n"
                f"📅 Expira: {expiracion.strftime('%d/%m/%Y')}\n"
                f"💎 Saldo restante: {usuario.get('saldo', 0) - precio_diamantes:.2f} diamantes"
            )
            
            # Notificar al admin
            try:
                admin_id = 6438282268
                user_info = callback_query.from_user
                mensaje_admin = f"🛒 Compra con saldo\n\nUsuario: @{user_info.username}\nID: {user_id}\nPlan: {plan_nombre}\nPrecio: {precio_diamantes}💎"
                await client.send_message(admin_id, mensaje_admin)
            except:
                pass
                
        else:
            # Para otros métodos de pago, guardar el estado y pedir captura
            precios = {
                "saldo_movil": {"pro": 120, "plus": 240, "ultra": 360},
                "tarjeta_cup": {"pro": 100, "plus": 300, "ultra": 500},
                "usdt_bep20": {"pro": 1, "plus": 1.5, "ultra": 2},
                "usdt_trc20": {"pro": 1, "plus": 1.5, "ultra": 2}
            }
            
            # Guardar el estado de pago
            estados_pago[user_id] = {
                "plan": plan,
                "metodo_pago": metodo_pago,
                "cantidad": precios[metodo_pago][plan],
                "timestamp": datetime.now()
            }
            
            # Mensajes según el método de pago
            mensajes_pago = {
                "saldo_movil": f"""📱 **Pago con Saldo Móvil**

▸ Plan: {plan_nombre}
▸ Precio: {precios['saldo_movil'][plan]} CUP
▸ Número: 50664186
▸ Nombre: *Emanuel*

⚠️ **Pasos:**
1. Envía el saldo correspondiente
2. **Envía la captura de pantalla aquí mismo**""",
                
                "tarjeta_cup": f"""💳 **Pago con Tarjeta CUP**

▸ Plan: {plan_nombre}
▸ Precio: {precios['tarjeta_cup'][plan]} CUP
▸ Banco: BPA
▸ Tarjeta: **9205 1299 7949 1421**
▸ Movil a confirmar: **50664186**
▸ Titular: *Kudo TV Corp*

⚠️ **Pasos:**
1. Realiza la transferencia
2. **Envía el comprobante aquí mismo**""",
                
                "usdt_bep20": f"""💰 **Pago con USDT BEP20**

▸ Plan: {plan_nombre}
▸ Precio: {precios['usdt_bep20'][plan]} USDT
▸ Dirección Wallet: `0x53986a56dA6f75797c2d540fE419a487ee753418`  

⚠️ **Pasos:**
1. Envía la cantidad correspondiente en USDT BEP20
2. **Envía el comprobante aquí mismo**""",
                
                "usdt_trc20": f"""💰 **Pago con USDT TRC20**

▸ Plan: {plan_nombre}
▸ Precio: {precios['usdt_trc20'][plan]} USDT
▸ Dirección Wallet: `THbDfzEx7F4p58UNrGhPbBarVZGXP6U8o2`  

⚠️ **Pasos:**
1. Envía la cantidad correspondiente en USDT TRC20
2. **Envía el comprobante aquí mismo**"""
            }
            
            texto = mensajes_pago[metodo_pago]
            botones = InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Cancelar", callback_data="cancelar_pago")]
            ])
            
            await callback_query.message.edit_text(texto, reply_markup=botones, disable_web_page_preview=True)

        await callback_query.answer()

    except Exception as e:
        error_msg = f"""⚠️ **Error en la transacción**
        
{str(e)[:100]}
Contacta a @Emanuel14APK"""
        await callback_query.message.edit_text(error_msg)
        await callback_query.answer()


@app.on_callback_query(filters.regex("cancelar_pago"))
async def cancelar_pago(client, callback_query: CallbackQuery):
    user_id = str(callback_query.from_user.id)
    if user_id in estados_pago:
        del estados_pago[user_id]
    
    await callback_query.message.edit_text("❌ Pago cancelado.")
    await callback_query.answer()

@app.on_message(filters.photo & filters.private)
async def manejar_captura_pago(client: Client, message: Message):
    try:
        user_id = str(message.from_user.id)

        # Verificar si el usuario está en proceso de pago
        if user_id not in estados_pago:
            return await message.reply("❌ No tienes ningún proceso de pago pendiente.")

        pago_info = estados_pago[user_id]

        # Obtener información del usuario
        usuario = usuarios_col.find_one({"user_id": user_id})
        username = f"@{message.from_user.username}" if message.from_user.username else "No tiene username"
        nombre = message.from_user.first_name or "Sin nombre"

        # Diccionario de métodos de pago
        metodo_nombres = {
            "saldo_movil": "Saldo Móvil",
            "tarjeta_cup": "Tarjeta CUP",
            "usdt_bep20": "USDT BEP20",
            "usdt_trc20": "USDT TRC20"
        }

        metodo = pago_info.get("metodo_pago")
        plan = pago_info.get("plan", "Desconocido").capitalize()
        cantidad = pago_info.get("cantidad", "No especificada")
        moneda = "CUP" if metodo in ["saldo_movil", "tarjeta_cup"] else "USDT"

        # Validar método de pago
        metodo_legible = metodo_nombres.get(metodo, "Método desconocido")

        mensaje_admin = f"""🚨 **NUEVA SOLICITUD DE PAGO**

▸ 👤 Usuario: {nombre} ({username})
▸ 🔖 ID: `{user_id}`
▸ 💼 Plan: {plan}
▸ 💰 Método: {metodo_legible}
▸ 💎 Cantidad: {cantidad} {moneda}
▸ ⏰ Fecha: {datetime.now().strftime('%d/%m/%Y %H:%M')}

⚠️ **Verificar el pago y activar manualmente con:**
`/setplan {user_id} {plan}`
`/recargar {user_id} {cantidad}`"""


        # Enviar mensaje al admin
        await client.send_message(
            chat_id=6438282268,
            text=mensaje_admin,
            parse_mode=ParseMode.MARKDOWN
        )

        # Reenviar la foto al admin
        await message.forward(chat_id=6438282268)

        # Confirmar al usuario
        await message.reply("✅ Comprobante recibido. Tu pago será verificado en un plazo máximo de 24 horas. Te notificaremos cuando tu plan sea activado.")

        # Limpiar el estado de pago
        estados_pago.pop(user_id, None)

    except Exception as e:
        logger.error(f"Error al procesar captura de pago: {e}")
        await message.reply("❌ Ocurrió un error al procesar tu comprobante. Por favor, contacta a @Emanuel14APK.")
        
@app.on_message(filters.command("gen_code") & filters.user(ADMIN_IDS))
async def generar_codigo(client, message: Message):
    try:
        args = message.text.split()
        if len(args) < 2:
            return await message.reply("Uso: /gen_code <cantidad_diamantes>")

        cantidad = float(args[1])
        
        while True:
            codigo = ''.join(random.choices(string.ascii_uppercase + string.digits, k=8))
            if not codigos_col.find_one({"codigo": codigo}):
                break
        
        codigos_col.insert_one({
            "codigo": codigo,
            "recompensa": cantidad,
            "usado": False,
            "fecha_creacion": datetime.now(),
            "usuario_uso": None
        })
        
        await message.reply(f"✅ Código generado:\n`{codigo}`\nRecompensa: {cantidad}💎")

    except Exception as e:
        logger.error(f"Error en generar_codigo: {e}")
        await message.reply(f"❌ Error: {str(e)}")

@app.on_message(filters.command("get_code"))
async def canjear_codigo(client, message: Message):
    try:
        user_id = str(message.from_user.id)
        args = message.text.split()
        
        if len(args) < 2:
            return await message.reply("Uso: /get_code <codigo>")

        codigo = args[1].upper()
        documento = codigos_col.find_one({"codigo": codigo})

        if not documento:
            return await message.reply("❌ Código inválido")
            
        if documento["usado"]:
            return await message.reply("⚠️ Este código ya fue utilizado")

        usuarios_col.update_one(
            {"user_id": user_id},
            {"$inc": {"saldo": documento["recompensa"]}}
        )
        
        codigos_col.update_one(
            {"_id": documento["_id"]},
            {"$set": {"usado": True, "usuario_uso": user_id}}
        )

        admin_id = 1461573114
        user_info = message.from_user
        mensaje_admin = f"🚨 Código usado\n\nCódigo: {codigo}\nUsuario: @{user_info.username}\nID: {user_id}\nRecompensa: {documento['recompensa']}💎"
        
        await client.send_message(admin_id, mensaje_admin)
        await message.reply(f"🎉 ¡Recarga exitosa! Se han añadidos {documento['recompensa']}💎 a tu saldo")

    except Exception as e:
        logger.error(f"Error en canjear_codigo: {e}")
        await message.reply(f"❌ Error: {str(e)}")

# ==============================================================
# MEJORAS AL SISTEMA DE PEDIDOS
# ==============================================================

@app.on_message(filters.command("pedidos"))
async def crear_pedido(client, message: Message):
    try:
        user = message.from_user
        if not user:
            return await message.reply("❌ No se pudo identificar al usuario.")

        user_id = str(user.id)
        texto_pedido = message.text.split(None, 1)
        if len(texto_pedido) < 2 or not texto_pedido[1].strip():
            return await message.reply("⚠️ Por favor escribe el pedido después del comando. Ejemplo:\n`/pedidos Quiero ver la película XYZ`")

        pedido_texto = texto_pedido[1].strip()
        
        # Guardar el estado temporal del pedido
        estados_pedido[user_id] = {
            "texto": pedido_texto,
            "timestamp": datetime.now()
        }
        
        # Pedir confirmación
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Confirmar", callback_data="confirmar_pedido")],
            [InlineKeyboardButton("❌ Cancelar", callback_data="cancelar_pedido")]
        ])
        
        await message.reply(
            f"📋 **Confirmar Pedido**\n\n"
            f"¿Estás seguro de que quieres enviar este pedido?\n\n"
            f"**Contenido solicitado:**\n{pedido_texto}",
            reply_markup=keyboard,
            parse_mode=ParseMode.MARKDOWN
        )

    except Exception as e:
        logger.error(f"Error en crear_pedido: {e}")
        await message.reply("❌ Ocurrió un error al procesar tu pedido. Intenta nuevamente.")

@app.on_callback_query(filters.regex("confirmar_pedido"))
async def confirmar_pedido(client, callback_query: CallbackQuery):
    try:
        user_id = str(callback_query.from_user.id)
        
        if user_id not in estados_pedido:
            return await callback_query.answer("❌ No hay pedido pendiente para confirmar", show_alert=True)
            
        pedido_info = estados_pedido[user_id]
        pedido_texto = pedido_info["texto"]
        pedido_id = ''.join(random.choices(string.ascii_letters + string.digits, k=16))

        # Obtener información del usuario
        usuario = usuarios_col.find_one({"user_id": user_id})
        if not usuario:
            return await callback_query.answer("❌ Primero debes registrarte con /start", show_alert=True)

        # Guardar pedido en la base de datos
        pedido_doc = {
            "pedido_id": pedido_id,
            "user_id": user_id,
            "username": callback_query.from_user.username,
            "first_name": callback_query.from_user.first_name,
            "texto": pedido_texto,
            "estado": "pendiente",
            "categoria": "general",
            "prioridad": "normal",
            "fecha_creacion": datetime.now(),
            "ultima_actualizacion": datetime.now()
        }

        pedidos_col.insert_one(pedido_doc)

        # Mensaje de confirmación al usuario
        await callback_query.message.edit_text(
            f"✅ **¡Pedido Registrado Exitosamente!**\n\n"
            f"📋 **ID de Pedido:** `{pedido_id}`\n"
            f"📝 **Solicitud:** {pedido_texto}\n\n"
            f"⏰ **Estado:** En espera de procesamiento\n"
            f"📬 **Notificación:** Serás avisado cuando se complete tu pedido",
            parse_mode=ParseMode.MARKDOWN
        )
        
        # Notificar a todos los administradores
        for admin_id in ADMIN_IDS:
            try:
                await client.send_message(
                    admin_id,
                    f"🆕 **NUEVO PEDIDO RECIBIDO**\n\n"
                    f"📋 **ID:** `{pedido_id}`\n"
                    f"👤 **Usuario:** @{callback_query.from_user.username or 'sin_usuario'} ({callback_query.from_user.first_name or 'Sin nombre'})\n"
                    f"🆔 **ID Usuario:** `{user_id}`\n"
                    f"📝 **Solicitud:** {pedido_texto}\n\n"
                    f"⏰ **Fecha:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                    f"🔰 **Prioridad:** Normal\n\n"
                    f"📊 **Para completar este pedido usa:**\n`/completepedido {pedido_id}`",
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("✅ Completar Pedido", callback_data=f"completar_pedido_{pedido_id}")]
                    ])
                )
            except Exception as e:
                logger.error(f"No se pudo notificar al admin {admin_id}: {e}")
        
        # Limpiar el estado temporal
        if user_id in estados_pedido:
            del estados_pedido[user_id]
            
        await callback_query.answer("✅ Pedido enviado correctamente")

    except Exception as e:
        logger.error(f"Error en confirmar_pedido: {e}")
        await callback_query.message.edit_text("❌ Ocurrió un error al registrar tu pedido. Intenta nuevamente.")

@app.on_callback_query(filters.regex("cancelar_pedido"))
async def cancelar_pedido(client, callback_query: CallbackQuery):
    user_id = str(callback_query.from_user.id)
    if user_id in estados_pedido:
        del estados_pedido[user_id]
    
    await callback_query.message.edit_text("❌ Pedido cancelado.")
    await callback_query.answer()

@app.on_message(filters.command("mispedidos"))
async def mis_pedidos(client, message: Message):
    try:
        user_id = str(message.from_user.id)
        
        # Obtener todos los pedidos del usuario
        pedidos = list(pedidos_col.find({"user_id": user_id}).sort("fecha_creacion", -1))
        
        if not pedidos:
            return await message.reply("📭 No has realizado ningún pedido aún.\n\nUsa `/pedidos [tu solicitud]` para hacer tu primer pedido.", parse_mode=ParseMode.MARKDOWN)
        
        texto = "📋 **Tus Pedidos**\n\n"
        
        for i, pedido in enumerate(pedidos, 1):
            estado_emoji = "✅" if pedido["estado"] == "completado" else "⏳"
            fecha = pedido["fecha_creacion"].strftime("%d/%m/%Y")
            
            texto += f"{i}. {estado_emoji} **ID:** `{pedido['pedido_id']}`\n"
            texto += f"   📅 **Fecha:** {fecha}\n"
            texto += f"   📝 **Solicitud:** {pedido['texto'][:50]}...\n"
            texto += f"   🚦 **Estado:** {pedido['estado'].capitalize()}\n\n"
        
        texto += "\n💡 **Nota:** Los pedidos completados pueden tardar hasta 24 horas en procesarse."
        
        # Dividir el mensaje si es demasiado largo
        if len(texto) > 4000:
            partes = [texto[i:i+4000] for i in range(0, len(texto), 4000)]
            for parte in partes:
                await message.reply(parte, parse_mode=ParseMode.MARKDOWN)
        else:
            await message.reply(texto, parse_mode=ParseMode.MARKDOWN)
            
    except Exception as e:
        logger.error(f"Error en mis_pedidos: {e}")
        await message.reply("❌ Ocurrió un error al obtener tus pedidos. Intenta nuevamente.")

@app.on_message(filters.command("completepedido") & filters.user(ADMIN_IDS))
async def completar_pedido(client, message: Message):
    try:
        args = message.text.split()
        if len(args) < 2:
            return await message.reply("ℹ️ Uso: `/completepedido <pedido_id> [nota_opcional]`", parse_mode=ParseMode.MARKDOWN)

        pedido_id = args[1]
        nota = " ".join(args[2:]) if len(args) > 2 else "Pedido completado por el administrador"
        
        pedido = pedidos_col.find_one({"pedido_id": pedido_id})

        if not pedido:
            return await message.reply("❌ Pedido no encontrado con ese ID.")

        if pedido["estado"] == "completado":
            return await message.reply("ℹ️ Este pedido ya fue marcado como completado anteriormente.")

        # Actualizar el pedido
        pedidos_col.update_one(
            {"pedido_id": pedido_id},
            {"$set": {
                "estado": "completado", 
                "fecha_completado": datetime.now(),
                "nota_completado": nota,
                "ultima_actualizacion": datetime.now()
            }}
        )

        # Mensaje de notificación para el usuario
        notif_text = f"""🎉 **¡Tu Pedido ha sido Completado!**

📋 **ID de Pedido:** `{pedido_id}`
📝 **Solicitud:** {pedido['texto']}
✅ **Estado:** Completado
📅 **Fecha de Completado:** {datetime.now().strftime('%d/%m/%Y %H:%M')}
💬 **Nota del administrador:** {nota}

¡Gracias por confiar en Kudo TV! 🎬"""

        try:
            await client.send_message(int(pedido["user_id"]), notif_text, parse_mode=ParseMode.MARKDOWN)
        except Exception as e:
            logger.error(f"No se pudo notificar al usuario {pedido['user_id']}: {e}")
            # Si no se puede notificar al usuario, informar al admin
            await message.reply(f"✅ Pedido completado pero no se pudo notificar al usuario: {e}")
            return

        await message.reply(f"✅ Pedido `{pedido_id}` marcado como completado y usuario notificado.")

    except Exception as e:
        logger.error(f"Error en completar_pedido: {e}")
        await message.reply("❌ Ocurrió un error al procesar el comando. Revisa el ID y vuelve a intentar.")

@app.on_callback_query(filters.regex(r"^completar_pedido_"))
async def completar_pedido_callback(client, callback_query: CallbackQuery):
    try:
        # Solo permitir a administradores
        if callback_query.from_user.id not in ADMIN_IDS:
            return await callback_query.answer("❌ No tienes permisos para realizar esta acción", show_alert=True)
            
        pedido_id = callback_query.data.split("_", 2)[2]
        pedido = pedidos_col.find_one({"pedido_id": pedido_id})

        if not pedido:
            return await callback_query.answer("❌ Pedido no encontrado", show_alert=True)

        if pedido["estado"] == "completado":
            return await callback_query.answer("ℹ️ Este pedido ya está completado", show_alert=True)

        # Actualizar el pedido
        pedidos_col.update_one(
            {"pedido_id": pedido_id},
            {"$set": {
                "estado": "completado", 
                "fecha_completado": datetime.now(),
                "nota_completado": "Completado desde botón",
                "ultima_actualizacion": datetime.now()
            }}
        )

        # Mensaje de notificación para el usuario
        notif_text = f"""🎉 **¡Tu Pedido ha sido Completado!**

📋 **ID de Pedido:** `{pedido_id}`
📝 **Solicitud:** {pedido['texto']}
✅ **Estado:** Completado
📅 **Fecha de Completado:** {datetime.now().strftime('%d/%m/%Y %H:%M')}

¡Gracias por confiar en Kudo TV! 🎬"""

        try:
            await client.send_message(int(pedido["user_id"]), notif_text, parse_mode=ParseMode.MARKDOWN)
        except Exception as e:
            logger.error(f"No se pudo notificar al usuario {pedido['user_id']}: {e}")

        # Actualizar mensaje del admin
        await callback_query.message.edit_text(
            f"✅ **PEDIDO COMPLETADO**\n\n"
            f"📋 **ID:** `{pedido_id}`\n"
            f"👤 **Usuario:** {pedido.get('first_name', 'N/A')} (@{pedido.get('username', 'N/A')})\n"
            f"📝 **Solicitud:** {pedido['texto']}\n"
            f"✅ **Estado:** Completado\n"
            f"📅 **Completado el:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            parse_mode=ParseMode.MARKDOWN
        )
        
        await callback_query.answer("✅ Pedido completado exitosamente")

    except Exception as e:
        logger.error(f"Error en completar_pedido_callback: {e}")
        await callback_query.answer("❌ Error al completar el pedido", show_alert=True)

@app.on_message(filters.command("ver_pedidos") & filters.user(ADMIN_IDS))
async def ver_pedidos_pendientes(client, message: Message):
    try:
        # Obtener filtros de búsqueda si los hay
        args = message.text.split()
        filtro_estado = "pendiente"
        filtro_usuario = None
        
        if len(args) > 1:
            if args[1].lower() in ["pendiente", "completado", "todos"]:
                filtro_estado = args[1].lower()
            else:
                filtro_usuario = args[1]
        
        # Construir query de búsqueda
        query = {}
        if filtro_estado != "todos":
            query["estado"] = filtro_estado
        if filtro_usuario:
            query["$or"] = [
                {"user_id": filtro_usuario},
                {"username": {"$regex": filtro_usuario, "$options": "i"}},
                {"first_name": {"$regex": filtro_usuario, "$options": "i"}}
            ]
        
        pedidos = list(pedidos_col.find(query).sort("fecha_creacion", -1).limit(50))
        
        if not pedidos:
            estado_text = "pendientes" if filtro_estado == "pendiente" else "completados" if filtro_estado == "completado" else ""
            return await message.reply(f"📭 No hay pedidos {estado_text} en este momento.")
        
        texto = f"📋 **Pedidos ({filtro_estado.capitalize()})**\n\n"
        if filtro_usuario:
            texto += f"🔍 Filtrado por usuario: `{filtro_usuario}`\n\n"
        
        for i, pedido in enumerate(pedidos, 1):
            estado_emoji = "✅" if pedido["estado"] == "completado" else "⏳"
            fecha = pedido["fecha_creacion"].strftime('%d/%m/%Y')
            user_info = f"@{pedido.get('username', 'sin_usuario')}" if pedido.get('username') else pedido.get('first_name', 'Usuario')
            
            texto += (
                f"{i}. {estado_emoji} **ID:** `{pedido.get('pedido_id')}`\n"
                f"   👤 **Usuario:** {user_info} (`{pedido.get('user_id')}`)\n"
                f"   📅 **Fecha:** {fecha}\n"
                f"   📝 **Pedido:** {pedido.get('texto')[:60]}...\n"
                f"   🚦 **Estado:** {pedido.get('estado').capitalize()}\n"
            )
            
            if pedido.get("estado") == "completado" and pedido.get("fecha_completado"):
                fecha_completado = pedido["fecha_completado"].strftime('%d/%m/%Y')
                texto += f"   ✅ **Completado:** {fecha_completado}\n"
                
            texto += f"   🛠 **Acción:** `/completepedido {pedido.get('pedido_id')}`\n"
            texto += "   ───────────────────\n"
            
        texto += f"\n📊 **Total encontrados:** {len(pedidos)}\n"
        
        # Añadir botones de acción rápida si hay pedidos pendientes
        botones = []
        if filtro_estado == "pendiente" and pedidos:
            botones.append([InlineKeyboardButton("🔄 Actualizar", callback_data="actualizar_pedidos")])
        
        # Dividir el mensaje si es demasiado largo
        if len(texto) > 4000:
            partes = [texto[i:i+4000] for i in range(0, len(texto), 4000)]
            for parte in partes:
                await message.reply(parte, parse_mode=ParseMode.MARKDOWN)
        else:
            await message.reply(texto, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(botones) if botones else None)
            
    except Exception as e:
        logger.error(f"Error en ver_pedidos_pendientes: {e}")
        await message.reply("❌ Ocurrió un error al obtener los pedidos. Verifica la sintaxis:\n`/ver_pedidos [pendiente|completado|todos] [usuario_opcional]`")

@app.on_callback_query(filters.regex("actualizar_pedidos"))
async def actualizar_pedidos_callback(client, callback_query: CallbackQuery):
    try:
        # Solo permitir a administradores
        if callback_query.from_user.id not in ADMIN_IDS:
            return await callback_query.answer("❌ No tienes permisos para esta acción", show_alert=True)
            
        # Obtener pedidos pendientes
        pedidos = list(pedidos_col.find({"estado": "pendiente"}).sort("fecha_creacion", -1).limit(50))
        
        if not pedidos:
            await callback_query.message.edit_text("✅ No hay pedidos pendientes en este momento.")
            return await callback_query.answer()
        
        texto = "📋 **Pedidos Pendientes**\n\n"
        
        for i, pedido in enumerate(pedidos, 1):
            fecha = pedido["fecha_creacion"].strftime('%d/%m/%Y')
            user_info = f"@{pedido.get('username', 'sin_usuario')}" if pedido.get('username') else pedido.get('first_name', 'Usuario')
            
            texto += (
                f"{i}. ⏳ **ID:** `{pedido.get('pedido_id')}`\n"
                f"   👤 **Usuario:** {user_info} (`{pedido.get('user_id')}`)\n"
                f"   📅 **Fecha:** {fecha}\n"
                f"   📝 **Pedido:** {pedido.get('texto')[:60]}...\n"
                f"   🛠 **Acción:** `/completepedido {pedido.get('pedido_id')}`\n"
                f"   ───────────────────\n"
            )
            
        texto += f"\n📊 **Total pendientes:** {len(pedidos)}\n"
        
        # Actualizar el mensaje
        if len(texto) > 4000:
            texto = texto[:4000] + "\n\n⚠️ *Se muestran solo los primeros 4000 caracteres*"
            
        await callback_query.message.edit_text(texto, parse_mode=ParseMode.MARKDOWN)
        await callback_query.answer("✅ Lista de pedidos actualizada")
        
    except Exception as e:
        logger.error(f"Error en actualizar_pedidos_callback: {e}")
        await callback_query.answer("❌ Error al actualizar la lista", show_alert=True)

@app.on_message(filters.command("estadisticas_pedidos") & filters.user(ADMIN_IDS))
async def estadisticas_pedidos(client, message: Message):
    try:
        # Obtener estadísticas
        total_pedidos = pedidos_col.count_documents({})
        pedidos_pendientes = pedidos_col.count_documents({"estado": "pendiente"})
        pedidos_completados = pedidos_col.count_documents({"estado": "completado"})
        
        # Obtener pedidos de los últimos 7 días
        siete_dias_atras = datetime.now() - timedelta(days=7)
        pedidos_ultima_semana = pedidos_col.count_documents({
            "fecha_creacion": {"$gte": siete_dias_atras}
        })
        
        pedidos_completados_semana = pedidos_col.count_documents({
            "estado": "completado",
            "fecha_completado": {"$gte": siete_dias_atras}
        })
        
        # Calcular ratio de completados
        ratio_completados = (pedidos_completados / total_pedidos * 100) if total_pedidos > 0 else 0
        
        texto = f"""📊 **Estadísticas de Pedidos**

📈 **Totales:**
├ 🎯 Total de pedidos: {total_pedidos}
├ ⏳ Pendientes: {pedidos_pendientes}
├ ✅ Completados: {pedidos_completados}
└ 📊 Ratio de completados: {ratio_completados:.1f}%

📅 **Últimos 7 días:**
├ 📥 Nuevos pedidos: {pedidos_ultima_semana}
├ ✅ Completados: {pedidos_completados_semana}
└ 🎯 Pendientes: {pedidos_pendientes}

⏰ **Actualizado:** {datetime.now().strftime('%d/%m/%Y %H:%M')}"""

        await message.reply(texto, parse_mode=ParseMode.MARKDOWN)
        
    except Exception as e:
        logger.error(f"Error en estadisticas_pedidos: {e}")
        await message.reply("❌ Ocurrió un error al obtener las estadísticas.")

# ==============================================================
# FIN DE MEJORAS AL SISTEMA DE PEDIDOS
# ==============================================================

@app.on_message(filters.command("invitar") & filters.private)
async def invitar_command(client: Client, message: Message):
    user_id = str(message.from_user.id)
    enlace_referido = f"https://t.me/{BOT_USERNAME}?start=ref_{user_id}"

    texto = f"""🎁 <b>¡Invita y gana diamantes!</b>

Comparte tu enlace único con amigos. Por cada registro válido, ganas <b>5 diamantes</b> automáticamente.

🔗 <b>Tu enlace de invitación:</b>
<code>{enlace_referido}</code>

📌 Puedes usarlo en grupos, redes sociales o enviarlo directamente."""

    botones = InlineKeyboardMarkup([
        [InlineKeyboardButton("📤 Compartir Enlace", url=f"https://t.me/share/url?url={enlace_referido}&text=Únete%20a%20MediaVerse%20para%20contenido%20exclusivo!")],
        [InlineKeyboardButton("👥 Ver Mis Referidos", callback_data="ver_referidos")]
    ])

    await message.reply(texto, reply_markup=botones, parse_mode=ParseMode.HTML)

@app.on_callback_query(filters.regex("ver_referidos"))
async def ver_referidos_callback(client, callback_query):
    user_id = str(callback_query.from_user.id)
    usuario = usuarios_col.find_one({"user_id": user_id})
    
    if not usuario:
        await callback_query.answer("❌ Primero debes registrarte con /start", show_alert=True)
        return

    cantidad = usuario.get("referidos", 0)
    saldo = usuario.get("saldo", 0.00)

    texto = f"""👥 **Tus Referidos**

▸ Total de usuarios invitados: `{cantidad}`
▸ Diamantes acumulados por referidos: `{5 * cantidad}` 💎
▸ Saldo total actual: `{saldo:.2f}` 💎

¡Sigue compartiendo para ganar más!"""

    await callback_query.message.edit_text(texto, parse_mode=ParseMode.MARKDOWN)
    await callback_query.answer()
    
# Función para iniciar tareas en segundo plano
async def start_background_tasks():
    asyncio.create_task(reset_limits_and_check_expiration())

# Health check HTTP server para Render (web service en free tier necesita un puerto)
def run_health_server():
    PORT = int(os.getenv("PORT", "8080"))

    class HealthHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"OK")

        def log_message(self, format, *args):
            pass

    with socketserver.TCPServer(("0.0.0.0", PORT), HealthHandler) as httpd:
        httpd.serve_forever()


threading.Thread(target=run_health_server, daemon=True).start()
print("Estoy online")

# Iniciar el bot con las tareas en segundo plano
if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    loop.create_task(start_background_tasks())
    app.run()