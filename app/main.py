import os
import threading
import time
from contextlib import asynccontextmanager

import requests
from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import agente, config, gemini, guiado, logica, voz
from .db import q

ESTATICOS = os.path.join(os.path.dirname(__file__), "static")


def _tareas_fondo():
    # Pre-carga del LLM y de la voz para que la primera respuesta no espere la carga de modelos.
    if config.PROVEEDOR == "ollama":  # con Gemini el modelo local solo se carga si hace falta el respaldo
        try:
            requests.post(f"{config.OLLAMA_URL}/api/generate",
                          json={"model": config.LLM_MODEL, "prompt": "", "keep_alive": "30m"}, timeout=300)
        except Exception:  # noqa: BLE001
            pass
    voz.precalentar()
    while True:  # pedidos programados -> restaurante
        try:
            logica.promover_programados()
        except Exception:  # noqa: BLE001
            pass
        time.sleep(30)


@asynccontextmanager
async def lifespan(app):
    threading.Thread(target=_tareas_fondo, daemon=True).start()
    yield


app = FastAPI(title="Agente despachador de domicilios", lifespan=lifespan)


# ------------------------------------------------------------------ turnos del agente
def turno_cliente(sesion_id, hablar):
    """Un turno de conversación del cliente: agente -> mensaje -> (opcional) audio."""
    with agente.trabajando():
        texto, ms = agente.ejecutar(sesion_id)
    audio = None
    if hablar:
        t0 = time.time()
        try:
            audio = voz.sintetizar(texto)
            logica.evento(sesion_id, "tts", {"caracteres": len(texto)}, {"archivo": audio},
                          int((time.time() - t0) * 1000))
        except Exception as e:  # noqa: BLE001
            logica.registrar_error(sesion_id, "tts", e)
    logica.guardar_mensaje(sesion_id, "agente", texto, "voz" if hablar else "texto", ms=ms, audio=audio)
    logica.evento(sesion_id, "respuesta_agente", None, {"texto": texto}, ms)


def turno_sistema(sesion_id, aviso, pedido_id, accion=None, producto_id=None):
    """Un evento de la plataforma (restaurante aceptó, falta producto...) entra al mismo agente."""
    logica.guardar_mensaje(sesion_id, "sistema", aviso, pedido_id=pedido_id)
    logica.evento(sesion_id, "evento_sistema", {"aviso": aviso}, pedido_id=pedido_id)
    t0 = time.time()
    usa_guiado = config.PROVEEDOR == "ollama" and config.MODO == "guiado"
    with agente.trabajando():
        if usa_guiado:
            texto = guiado.evento_sistema(sesion_id, pedido_id, accion, producto_id)
            ms = int((time.time() - t0) * 1000)
        else:
            try:
                texto, ms = agente.ejecutar(sesion_id, despacho=True)
            except gemini.NoDisponible:  # ninguna clave sirvió: se atiende con reglas y modelo local
                usa_guiado = True
                if not config.GEMINI_FALLBACK_OLLAMA:
                    logica.escalar_a_humano({"sesion_id": sesion_id}, f"Gemini no disponible (pedido {pedido_id})")
                    texto = "Hay una novedad con tu pedido; un despachador humano te contactará."
                else:
                    gemini.marcar_respaldo()
                    logica.evento(sesion_id, "respaldo_ollama", {"motivo": "Gemini no disponible"})
                    texto = guiado.evento_sistema(sesion_id, pedido_id, accion, producto_id)
                ms = int((time.time() - t0) * 1000)
    if pedido_id and not usa_guiado:  # respaldo determinista si el modelo no asignó domiciliario
        r = logica.asignacion_de_respaldo(pedido_id)
        if r:
            logica.evento(sesion_id, "asignacion_de_respaldo", {"pedido_id": pedido_id}, r, pedido_id=pedido_id)
            if "asignado" not in texto.lower():
                texto += f" Tu pedido {pedido_id} fue asignado a {r['domiciliario']}."
    logica.guardar_mensaje(sesion_id, "agente", texto, ms=ms, pedido_id=pedido_id)
    logica.evento(sesion_id, "respuesta_agente", None, {"texto": texto}, ms, pedido_id)


# ------------------------------------------------------------------ cliente
class SesionIn(BaseModel):
    sesion_id: str
    nombre: str


class ChatIn(BaseModel):
    sesion_id: str
    texto: str
    hablar: bool = False  # True: el agente responde también con audio (TTS)


@app.post("/api/sesion")
def sesion(d: SesionIn):
    nombre = d.nombre.strip()[:60] or "Cliente"
    q("INSERT INTO clientes (sesion_id, nombre) VALUES (%s,%s) "
      "ON CONFLICT (sesion_id) DO UPDATE SET nombre=EXCLUDED.nombre", (d.sesion_id, nombre))
    return {"ok": True, "nombre": nombre}


def _verificar(sesion_id):
    if not q("SELECT 1 FROM clientes WHERE sesion_id=%s", (sesion_id,), uno=True):
        raise HTTPException(400, "Primero escribe tu nombre para iniciar la sesión.")


@app.post("/api/chat")
def chat(d: ChatIn, fondo: BackgroundTasks):
    _verificar(d.sesion_id)
    texto = d.texto.strip()[:1000]
    if not texto:
        raise HTTPException(400, "Mensaje vacío.")
    logica.guardar_mensaje(d.sesion_id, "usuario", texto)
    logica.evento(d.sesion_id, "mensaje_usuario", {"canal": "texto"}, {"texto": texto})
    fondo.add_task(turno_cliente, d.sesion_id, d.hablar)
    return {"ok": True}


@app.post("/api/voz")
async def mensaje_de_voz(fondo: BackgroundTasks, sesion_id: str = Form(...), audio: UploadFile = File(...)):
    _verificar(sesion_id)
    datos = await audio.read()
    ext = os.path.splitext(audio.filename or "")[1] or ".webm"
    t0 = time.time()
    try:
        texto = voz.transcribir(datos, ext)
    except Exception as e:  # noqa: BLE001
        logica.registrar_error(sesion_id, "stt", e)
        raise HTTPException(500, "No pude transcribir el audio.")
    ms = int((time.time() - t0) * 1000)
    logica.evento(sesion_id, "stt", {"bytes": len(datos)}, {"texto": texto}, ms)
    if not texto:
        raise HTTPException(422, "No se entendió el audio; intenta de nuevo.")
    logica.guardar_mensaje(sesion_id, "usuario", texto, "voz")
    logica.evento(sesion_id, "mensaje_usuario", {"canal": "voz"}, {"texto": texto})
    fondo.add_task(turno_cliente, sesion_id, True)
    return {"ok": True, "texto": texto}


@app.get("/api/mensajes")
def mensajes(sesion_id: str, desde: int = 0):
    filas = q("SELECT id, rol, contenido, canal, audio_archivo, ms_respuesta, creado_en FROM mensajes "
              "WHERE sesion_id=%s AND id>%s AND rol<>'sistema' ORDER BY id", (sesion_id, desde))
    return {"mensajes": [{**f, "audio": f"/api/audio/{f['audio_archivo']}" if f["audio_archivo"] else None}
                         for f in filas],
            "pensando": agente.pensando()}


@app.get("/api/audio/{archivo}")
def audio(archivo: str):
    ruta = os.path.join(config.AUDIO_DIR, os.path.basename(archivo))
    if not os.path.isfile(ruta):
        raise HTTPException(404)
    return FileResponse(ruta, media_type="audio/wav")


# ------------------------------------------------------------------ restaurante y domiciliario
class AccionIn(BaseModel):
    producto_id: int | None = None


@app.post("/api/restaurante/{pedido_id}/{accion}")
def restaurante(pedido_id: int, accion: str, d: AccionIn, fondo: BackgroundTasks):
    try:
        sesion_id, aviso = logica.accion_restaurante(pedido_id, accion, d.producto_id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    logica.evento(sesion_id, f"restaurante_{accion}", {"pedido_id": pedido_id, "producto_id": d.producto_id},
                  pedido_id=pedido_id)
    fondo.add_task(turno_sistema, sesion_id, aviso, pedido_id, accion, d.producto_id)
    return {"ok": True}


@app.post("/api/domiciliario/{pedido_id}/{accion}")
def domiciliario(pedido_id: int, accion: str):
    try:
        sesion_id, msg = logica.accion_domiciliario(pedido_id, accion)
    except ValueError as e:
        raise HTTPException(400, str(e))
    logica.evento(sesion_id, f"domiciliario_{accion}", {"pedido_id": pedido_id}, pedido_id=pedido_id)
    logica.guardar_mensaje(sesion_id, "agente", msg, pedido_id=pedido_id)
    return {"ok": True}


# ------------------------------------------------------------------ tablero y trazabilidad
@app.get("/api/estado")
def estado():
    pedidos = q("SELECT p.id, c.nombre AS cliente, c.sesion_id, r.nombre AS restaurante, "
                "d.nombre AS domiciliario, p.estado, p.total, p.subtotal, p.costo_domicilio, p.metodo_pago, "
                "p.notas, p.direccion_entrega, p.programado_para, p.creado_en, p.actualizado_en "
                "FROM pedidos p JOIN clientes c ON c.id=p.cliente_id JOIN restaurantes r ON r.id=p.restaurante_id "
                "LEFT JOIN domiciliarios d ON d.id=p.domiciliario_id ORDER BY p.id DESC LIMIT 60")
    for p in pedidos:
        p["items"] = [{"producto_id": i["producto_id"], "nombre": i["nombre"], "cantidad": i["cantidad"],
                       "observacion": i["observacion"]} for i in logica._items_pedido(p["id"])]
    doms = q("SELECT d.nombre, d.estado, d.pedidos_activos, z.nombre AS zona FROM domiciliarios d "
             "LEFT JOIN zonas z ON z.id=d.zona_id ORDER BY d.id")
    return {"pedidos": pedidos, "domiciliarios": doms, "pensando": agente.pensando(),
            "modelo": config.descripcion_modelo(), "proveedor": config.PROVEEDOR,
            "gemini": gemini.estado_claves() if config.PROVEEDOR == "gemini" else None}


@app.get("/api/llm")
def llm():
    """Qué modelo atiende y cómo están las claves (solo conteos: nunca las claves)."""
    info = {"proveedor": config.PROVEEDOR, "modelo": config.descripcion_modelo(),
            "modo": config.MODO if config.PROVEEDOR == "ollama" else "herramientas",
            "envia_datos_a_google": config.PROVEEDOR == "gemini"}
    if config.PROVEEDOR == "gemini":
        info["claves"] = gemini.estado_claves()
        info["respaldo_local"] = config.GEMINI_FALLBACK_OLLAMA
    return info


@app.get("/api/eventos")
def eventos(desde: int = 0, sesion_id: str | None = None):
    """Bitácora del agente. Con sesion_id solo trae los pasos de esa conversación."""
    if desde == 0:
        filas = q("SELECT * FROM (SELECT id, sesion_id, pedido_id, herramienta, entrada, salida, ms, creado_en "
                  "FROM acciones_agente WHERE (%s::text IS NULL OR sesion_id=%s) ORDER BY id DESC LIMIT 120) t "
                  "ORDER BY id", (sesion_id, sesion_id))
    else:
        filas = q("SELECT id, sesion_id, pedido_id, herramienta, entrada, salida, ms, creado_en "
                  "FROM acciones_agente WHERE id>%s AND (%s::text IS NULL OR sesion_id=%s) ORDER BY id LIMIT 300",
                  (desde, sesion_id, sesion_id))
    return {"eventos": filas}


# ------------------------------------------------------------------ banco de conversaciones
@app.get("/api/conversaciones")
def conversaciones():
    """Todas las conversaciones con clientes, la más reciente primero."""
    filas = q("""
        SELECT c.sesion_id, c.nombre,
               (SELECT count(*) FROM mensajes m WHERE m.sesion_id=c.sesion_id AND m.rol<>'sistema') AS mensajes,
               (SELECT m.contenido FROM mensajes m WHERE m.sesion_id=c.sesion_id AND m.rol<>'sistema'
                  ORDER BY m.id DESC LIMIT 1) AS ultimo,
               (SELECT m.rol FROM mensajes m WHERE m.sesion_id=c.sesion_id AND m.rol<>'sistema'
                  ORDER BY m.id DESC LIMIT 1) AS ultimo_rol,
               (SELECT max(m.creado_en) FROM mensajes m WHERE m.sesion_id=c.sesion_id) AS ultima_fecha,
               (SELECT p.estado FROM pedidos p WHERE p.cliente_id=c.id ORDER BY p.id DESC LIMIT 1) AS estado_pedido,
               (SELECT count(*) FROM pedidos p WHERE p.cliente_id=c.id) AS pedidos,
               (SELECT COALESCE(sum(p.total) FILTER (WHERE p.estado='entregado'),0)
                  FROM pedidos p WHERE p.cliente_id=c.id) AS vendido,
               EXISTS (SELECT 1 FROM acciones_agente a WHERE a.sesion_id=c.sesion_id
                         AND a.herramienta='escalar_a_humano') AS escalada
        FROM clientes c
        ORDER BY ultima_fecha DESC NULLS LAST, c.id DESC LIMIT 200""")
    return {"conversaciones": filas}


@app.delete("/api/conversaciones/{sesion_id}")
def borrar_conversacion(sesion_id: str):
    """Elimina una conversación y sus datos, salvo que tenga un pedido en curso."""
    cli = q("SELECT id FROM clientes WHERE sesion_id=%s", (sesion_id,), uno=True)
    if not cli:
        raise HTTPException(404, "Conversación no encontrada.")
    activo = q("SELECT 1 FROM pedidos WHERE cliente_id=%s AND estado IN ('pendiente_restaurante','aceptado',"
               "'asignado','en_camino','programado') LIMIT 1", (cli["id"],), uno=True)
    if activo:
        raise HTTPException(409, "Tiene un pedido en curso; espera a que termine o se cancele.")
    q("DELETE FROM mensajes WHERE sesion_id=%s", (sesion_id,))
    q("DELETE FROM acciones_agente WHERE sesion_id=%s", (sesion_id,))
    q("DELETE FROM errores WHERE sesion_id=%s", (sesion_id,))
    q("DELETE FROM pedidos WHERE cliente_id=%s", (cli["id"],))  # pedido_items se borra en cascada
    q("DELETE FROM clientes WHERE id=%s", (cli["id"],))
    BORRADORES_LIMPIAR(sesion_id)
    return {"ok": True}


def BORRADORES_LIMPIAR(sesion_id):
    guiado.BORRADORES.pop(sesion_id, None)
    guiado._RECOMENDADOS.pop(sesion_id, None)
    guiado._ULTIMO_CTX.pop(sesion_id, None)


# ------------------------------------------------------------------ voz de salida (elegir voz, velocidad, tono)
class VozIn(BaseModel):
    voz: str | None = None
    velocidad: float | None = None
    tono: float | None = None
    volumen: float | None = None


@app.get("/api/voces")
def voces():
    return {"voces": voz.listar_voces(), "actual": voz.leer_config(), "limites": voz.LIMITES}


@app.post("/api/voces/config")
def voces_guardar(d: VozIn):
    return {"actual": voz.guardar_config(d.model_dump())}


@app.post("/api/voces/probar")
def voces_probar(d: VozIn):
    """Genera una frase de prueba con los ajustes recibidos (sin guardarlos)."""
    try:
        archivo = voz.sintetizar(voz.FRASE_PRUEBA, d.model_dump())
    except Exception as e:  # noqa: BLE001
        logica.registrar_error(None, "tts", e)
        raise HTTPException(500, "No pude generar la voz de prueba.")
    return {"audio": f"/api/audio/{archivo}"}


@app.get("/api/metricas")
def metricas():
    """Resumen para la tabla de evaluación del informe."""
    return {
        "respuestas": q("SELECT count(*) AS n, round(avg(ms_respuesta)) AS ms_promedio, "
                        "max(ms_respuesta) AS ms_max FROM mensajes WHERE rol='agente' AND ms_respuesta IS NOT NULL",
                        uno=True),
        "herramientas": q("SELECT herramienta, count(*) AS n, round(avg(ms)) AS ms_promedio FROM acciones_agente "
                          "WHERE herramienta NOT LIKE 'error:%%' GROUP BY herramienta ORDER BY n DESC"),
        "errores": q("SELECT componente, count(*) AS n FROM errores GROUP BY componente"),
    }


@app.post("/api/demo/reiniciar")
def reiniciar():
    q("TRUNCATE pedido_items, pedidos, mensajes, acciones_agente, errores, clientes RESTART IDENTITY CASCADE")
    guiado.BORRADORES.clear()
    guiado._RECOMENDADOS.clear()
    guiado._ULTIMO_CTX.clear()
    q("UPDATE productos SET disponible=true")
    q("UPDATE domiciliarios SET estado='libre', pedidos_activos=0")
    return {"ok": True}


app.mount("/", StaticFiles(directory=ESTATICOS, html=True), name="static")
