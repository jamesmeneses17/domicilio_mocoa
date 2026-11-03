"""Reglas de negocio y herramientas del agente.

Todo lo que toca datos (precios, totales, estados, asignación) se calcula aquí
con SQL/Python. El LLM solo decide qué herramienta llamar y redacta la respuesta.
"""
import re
import requests
from datetime import datetime, time, timedelta
from decimal import Decimal

from . import config
from .db import js, q, tx

ESTADOS_CANCELABLES = ("borrador", "programado", "pendiente_restaurante", "aceptado")
ESTADOS_MODIFICABLES = ("borrador", "programado", "pendiente_restaurante")


class _Abortar(Exception):
    """Fuerza rollback de una transacción devolviendo un resultado al llamador."""

    def __init__(self, resultado):
        self.resultado = resultado


# ---------------------------------------------------------------- bitácora
def evento(sesion_id, herramienta, entrada=None, salida=None, ms=None, pedido_id=None):
    """Registra un paso del proceso (se muestra en el panel de trazabilidad)."""
    q(
        "INSERT INTO acciones_agente (sesion_id, pedido_id, herramienta, entrada, salida, ms) "
        "VALUES (%s,%s,%s,%s,%s,%s)",
        (sesion_id, pedido_id, herramienta, js(entrada), js(salida), ms),
    )


def registrar_error(sesion_id, componente, detalle):
    q("INSERT INTO errores (sesion_id, componente, detalle) VALUES (%s,%s,%s)",
      (sesion_id, componente, str(detalle)[:1000]))
    evento(sesion_id, f"error:{componente}", salida={"detalle": str(detalle)[:300]})


def guardar_mensaje(sesion_id, rol, contenido, canal="texto", pedido_id=None,
                    ms=None, audio=None):
    fila = q(
        "INSERT INTO mensajes (sesion_id, rol, contenido, canal, pedido_id, ms_respuesta, audio_archivo) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id",
        (sesion_id, rol, contenido, canal, pedido_id, ms, audio), uno=True)
    return fila["id"]


# ---------------------------------------------------------------- utilidades
def _abierto(r, t: time) -> bool:
    if not r["abierto"]:
        return False
    ap, ci = r["hora_apertura"], r["hora_cierre"]
    return ap <= t < ci if ap < ci else (t >= ap or t < ci)


def _horario(r):
    return f"{r['hora_apertura']:%H:%M}-{r['hora_cierre']:%H:%M}"


def _cliente(sesion_id):
    return q("SELECT * FROM clientes WHERE sesion_id=%s", (sesion_id,), uno=True)


def _items_pedido(pedido_id):
    return q("SELECT pi.producto_id, p.nombre, pi.cantidad, pi.precio_unitario, pi.observacion "
             "FROM pedido_items pi JOIN productos p ON p.id=pi.producto_id "
             "WHERE pi.pedido_id=%s ORDER BY pi.id", (pedido_id,))


def _zona_por_barrio(barrio):
    if not barrio:
        return None
    b = str(barrio).strip()
    z = q("SELECT * FROM zonas WHERE lower(nombre)=lower(%s)", (b,), uno=True)
    return z or q("SELECT * FROM zonas WHERE nombre ILIKE %s ORDER BY id LIMIT 1",
                  (f"%{b}%",), uno=True)


def _recalcular(cur, pedido_id):
    cur.execute("SELECT COALESCE(SUM(cantidad*precio_unitario),0) AS s FROM pedido_items WHERE pedido_id=%s",
                (pedido_id,))
    sub = cur.fetchone()["s"]
    cur.execute("UPDATE pedidos SET subtotal=%s, total=%s+costo_domicilio, actualizado_en=now() "
                "WHERE id=%s RETURNING subtotal, costo_domicilio, total", (sub, sub, pedido_id))
    return cur.fetchone()


# ---------------------------------------------------------------- herramientas
PIDE_ALCOHOL = re.compile(r"cerveza|michelada|alcohol|poker|aguila|águila|corona|club colombia|redd|bud ")
ETIQUETAS = ("liviano", "contundente", "economico", "dulce", "refrescante", "para_el_frio", "picante",
             "sin_picante", "vegetariano", "saludable", "desayuno", "para_compartir")


def buscar_menu(ctx, restaurante_id=None, texto=None, temperatura=None, etiqueta=None, categoria=None, limite=30):
    """Lista productos disponibles con precio, atributos y si el restaurante está abierto ahora.

    Para recomendar según el contexto se filtra por temperatura (caliente|fria), etiqueta o categoría."""
    rid = int(restaurante_id) if restaurante_id not in (None, "") else None
    patron = f"%{texto}%" if texto else None
    temp = temperatura if temperatura in ("caliente", "fria") else None
    etq = etiqueta if etiqueta in ETIQUETAS else None
    cat = f"%{categoria}%" if categoria else None
    sql = ("SELECT p.id AS producto_id, p.nombre AS producto, p.descripcion, p.precio, p.categoria, "
           "p.temperatura, p.etiquetas, r.id AS restaurante_id, r.nombre AS restaurante, r.abierto, "
           "r.hora_apertura, r.hora_cierre "
           "FROM productos p JOIN restaurantes r ON r.id=p.restaurante_id "
           "WHERE p.disponible AND r.activo AND (%s::int IS NULL OR r.id=%s) "
           "AND (%s::text IS NULL OR p.nombre ILIKE %s OR p.descripcion ILIKE %s OR r.nombre ILIKE %s "
           "     OR p.categoria ILIKE %s) "
           "AND (%s::text IS NULL OR p.temperatura=%s) AND (%s::text IS NULL OR %s = ANY(p.etiquetas)) "
           "AND (%s::text IS NULL OR p.categoria ILIKE %s) "
           "ORDER BY r.id, p.id")
    filas = q(sql, (rid, rid, patron, patron, patron, patron, patron, temp, temp, etq, etq, cat, cat))
    nota = None
    if not filas and (texto or temp or etq or cat):
        filas = q(sql, (rid, rid, None, None, None, None, None, None, None, None, None, None, None))
        nota = "Nada coincide exactamente con el filtro; se muestra el menú completo."
    t = config.ahora().time()
    out = [{"producto_id": f["producto_id"], "producto": f["producto"],
            "descripcion": f["descripcion"], "precio": f["precio"], "categoria": f["categoria"],
            "temperatura": f["temperatura"], "etiquetas": f["etiquetas"],
            "restaurante_id": f["restaurante_id"], "restaurante": f["restaurante"],
            "abierto_ahora": _abierto(f, t), "horario": _horario(f)} for f in filas]
    # Las bebidas alcohólicas no se ofrecen salvo que el cliente las pida por su nombre.
    if not PIDE_ALCOHOL.search(f"{texto or ''} {etiqueta or ''}".lower()):
        out = [x for x in out if "alcohol" not in (x["etiquetas"] or [])]
    out.sort(key=lambda x: not x["abierto_ahora"])  # primero los restaurantes abiertos (orden estable)
    try:
        lim = max(1, min(int(limite), 700))
    except (TypeError, ValueError):
        lim = 30
    if len(out) > lim:
        nota = (nota or "") + f" Hay {len(out)} productos que coinciden; se muestran {lim}. Afina con restaurante_id, "                               "temperatura, etiqueta, categoria o texto."
    return {"productos": out[:lim], "total": len(out), "nota": nota}


def listar_restaurantes(ctx, texto=None):
    """Restaurantes disponibles con su tipo de comida, horario y si están abiertos ahora."""
    patron = f"%{texto}%" if texto else None
    filas = q("SELECT id, nombre, tipo_comida, telefono, hora_apertura, hora_cierre, abierto FROM restaurantes "
              "WHERE activo AND (%s::text IS NULL OR nombre ILIKE %s OR tipo_comida ILIKE %s) ORDER BY id",
              (patron, patron, patron))
    t = config.ahora().time()
    return {"restaurantes": [{"restaurante_id": f["id"], "nombre": f["nombre"], "tipo_de_comida": f["tipo_comida"],
                              "abierto_ahora": _abierto(f, t), "horario": _horario(f), "telefono": f["telefono"]}
                             for f in filas]}


def verificar_restaurante(ctx, restaurante_id):
    """Dice si un restaurante está abierto ahora y cuál es su horario."""
    try:
        r = q("SELECT * FROM restaurantes WHERE id=%s", (int(restaurante_id),), uno=True)
    except (TypeError, ValueError):
        return {"ok": False, "error": "restaurante_id inválido. Usa buscar_menu."}
    if not r:
        return {"ok": False, "error": "Restaurante no encontrado."}
    ahora = config.ahora()
    return {"ok": True, "restaurante": r["nombre"], "abierto_ahora": _abierto(r, ahora.time()),
            "horario": _horario(r), "hora_actual": f"{ahora:%H:%M}"}


def calcular_tarifa(ctx, barrio):
    z = _zona_por_barrio(barrio)
    if not z:
        return {"cobertura": False,
                "mensaje": "No se encontró un barrio con ese nombre. Si es un apodo o referencia, "
                           "usa consultar_conocimiento con tipo 'lugar'; si de verdad no hay "
                           "cobertura, informa al cliente."}
    return {"cobertura": True, "zona": z["nombre"], "tarifa_domicilio": z["tarifa_domicilio"]}


def consultar_conocimiento(ctx, pregunta, tipo=None):
    """Búsqueda semántica en pgvector (políticas, preguntas frecuentes, lugares, menú)."""
    t0 = config.ahora()
    r = requests.post(f"{config.OLLAMA_URL}/api/embed",
                      json={"model": config.EMBED_MODEL, "input": pregunta}, timeout=120)
    r.raise_for_status()
    vec = str(r.json()["embeddings"][0])
    tipo = tipo if tipo in ("politica", "faq", "menu", "lugar") else None
    filas = q("SELECT tipo, titulo, contenido, 1 - (embedding <=> %s::vector) AS similitud "
              "FROM conocimiento WHERE (%s::text IS NULL OR tipo=%s) "
              "ORDER BY embedding <=> %s::vector LIMIT 3", (vec, tipo, tipo, vec))
    mejor = float(filas[0]["similitud"]) if filas else 0.0
    res = {"resultados": [{"tipo": f["tipo"], "titulo": f["titulo"], "contenido": f["contenido"],
                           "similitud": round(float(f["similitud"]), 3)} for f in filas],
           "suficiente": mejor >= config.UMBRAL_SIMILITUD}
    if not res["suficiente"]:
        res["mensaje"] = "Similitud baja: no hay información confiable. No inventes; ofrece escalar_a_humano."
    return res


def _normalizar_items(restaurante_id, items):
    """Valida productos contra la BD y devuelve (items_validos, error)."""
    validos = []
    for it in items or []:
        if not isinstance(it, dict):
            return None, "Formato de items inválido."
        pid, nombre = it.get("producto_id"), it.get("producto") or it.get("nombre")
        try:
            cant = int(it.get("cantidad", 1))
        except (TypeError, ValueError):
            return None, "Cantidad inválida."
        if cant < 1 or cant > 20:
            return None, "La cantidad debe estar entre 1 y 20."
        prod = None
        if pid not in (None, ""):
            try:
                prod = q("SELECT * FROM productos WHERE id=%s AND restaurante_id=%s", (int(pid), restaurante_id), uno=True)
            except (TypeError, ValueError):
                prod = None
        if prod is None and nombre:
            prod = q("SELECT * FROM productos WHERE restaurante_id=%s AND nombre ILIKE %s ORDER BY id LIMIT 1",
                     (restaurante_id, f"%{nombre}%"), uno=True)
        if prod is None:
            return None, f"El producto {pid or nombre} no existe en ese restaurante. Usa buscar_menu."
        if not prod["disponible"]:
            return None, f"{prod['nombre']} no está disponible."
        validos.append({"producto_id": prod["id"], "nombre": prod["nombre"], "cantidad": cant,
                        "precio_unitario": prod["precio"], "observacion": it.get("observacion")})
    if not validos:
        return None, "El pedido no tiene productos."
    return validos, None


def _cotizar(restaurante_id, items, barrio, programado_para=None):
    try:
        rid = int(restaurante_id)
    except (TypeError, ValueError):
        return {"ok": False, "error": "restaurante_id inválido. Usa buscar_menu."}
    r = q("SELECT * FROM restaurantes WHERE id=%s AND activo", (rid,), uno=True)
    if not r:
        return {"ok": False, "error": "Restaurante no encontrado."}
    zona = _zona_por_barrio(barrio)
    if not zona:
        return {"ok": False, "error": "Sin cobertura en ese barrio o barrio no reconocido. "
                                      "Prueba consultar_conocimiento (tipo 'lugar') o informa que no hay servicio."}
    validos, err = _normalizar_items(rid, items)
    if err:
        return {"ok": False, "error": err}

    programado = None
    if programado_para:
        try:
            programado = datetime.fromisoformat(str(programado_para))
            if programado.tzinfo is None:
                programado = programado.replace(tzinfo=config.TZ)
        except ValueError:
            return {"ok": False, "error": "programado_para debe tener formato YYYY-MM-DDTHH:MM."}
        if programado <= config.ahora():
            return {"ok": False, "error": "La hora programada ya pasó."}
        if not _abierto(r, programado.timetz().replace(tzinfo=None)):
            return {"ok": False, "error": f"{r['nombre']} está cerrado a esa hora (horario {_horario(r)})."}
    elif not _abierto(r, config.ahora().time()):
        return {"ok": False, "restaurante_cerrado": True,
                "error": f"{r['nombre']} está cerrado ahora (horario {_horario(r)}). "
                         "Ofrece programar el pedido para una hora en que esté abierto."}

    subtotal = sum(Decimal(i["precio_unitario"]) * i["cantidad"] for i in validos)
    tarifa = Decimal(zona["tarifa_domicilio"])
    return {"ok": True, "restaurante": r["nombre"], "restaurante_id": rid, "zona": zona["nombre"],
            "zona_id": zona["id"], "items": validos, "subtotal": subtotal, "costo_domicilio": tarifa,
            "total": subtotal + tarifa, "programado": programado}


def cotizar_pedido(ctx, restaurante_id, items, barrio, programado_para=None):
    """Calcula subtotal, domicilio y total SIN crear el pedido (para pedir confirmación)."""
    c = _cotizar(restaurante_id, items, barrio, programado_para)
    if not c["ok"]:
        return c
    return {"ok": True, "restaurante": c["restaurante"], "zona": c["zona"],
            "items": [{"producto": i["nombre"], "cantidad": i["cantidad"],
                       "precio_unitario": i["precio_unitario"]} for i in c["items"]],
            "subtotal": c["subtotal"], "costo_domicilio": c["costo_domicilio"], "total": c["total"]}


def crear_pedido(ctx, restaurante_id, items, direccion, barrio, metodo_pago, programado_para=None):
    """Crea el pedido (solo tras confirmación explícita del cliente) y lo envía al restaurante."""
    cli = _cliente(ctx["sesion_id"])
    if not cli:
        return {"ok": False, "error": "Cliente no registrado."}
    if not direccion or not str(direccion).strip():
        return {"ok": False, "error": "Falta la dirección de entrega."}
    c = _cotizar(restaurante_id, items, barrio, programado_para)
    if not c["ok"]:
        return c

    # Evita duplicados si el modelo llama dos veces seguidas a la herramienta.
    dup = q("SELECT id, estado, total FROM pedidos WHERE cliente_id=%s AND restaurante_id=%s AND total=%s "
            "AND estado IN ('pendiente_restaurante','programado') AND creado_en > now() - interval '2 minutes' "
            "ORDER BY id DESC LIMIT 1", (cli["id"], c["restaurante_id"], c["total"]), uno=True)
    if dup:
        return {"ok": True, "pedido_id": dup["id"], "estado": dup["estado"], "total": dup["total"],
                "nota": "El pedido ya estaba creado; no se duplicó."}

    estado = "programado" if c["programado"] else "pendiente_restaurante"
    with tx() as cur:
        cur.execute(
            "INSERT INTO pedidos (cliente_id, restaurante_id, estado, direccion_entrega, zona_id, metodo_pago, "
            "subtotal, costo_domicilio, total, programado_para) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
            (cli["id"], c["restaurante_id"], estado, str(direccion).strip(), c["zona_id"], metodo_pago,
             c["subtotal"], c["costo_domicilio"], c["total"], c["programado"]))
        pid = cur.fetchone()["id"]
        for i in c["items"]:
            cur.execute("INSERT INTO pedido_items (pedido_id, producto_id, cantidad, precio_unitario, observacion) "
                        "VALUES (%s,%s,%s,%s,%s)",
                        (pid, i["producto_id"], i["cantidad"], i["precio_unitario"], i["observacion"]))
    evento(ctx["sesion_id"], "notificar_restaurante",
           {"pedido_id": pid, "restaurante": c["restaurante"]}, {"estado": estado}, pedido_id=pid)
    return {"ok": True, "pedido_id": pid, "estado": estado, "restaurante": c["restaurante"],
            "subtotal": c["subtotal"], "costo_domicilio": c["costo_domicilio"], "total": c["total"]}


def estado_pedido(ctx, pedido_id=None):
    cli = _cliente(ctx["sesion_id"])
    if not cli:
        return {"ok": False, "error": "Cliente no registrado."}
    if pedido_id in (None, ""):
        filas = q("SELECT id FROM pedidos WHERE cliente_id=%s ORDER BY id DESC LIMIT 3", (cli["id"],))
    else:
        try:
            filas = q("SELECT id FROM pedidos WHERE id=%s AND cliente_id=%s", (int(pedido_id), cli["id"]))
        except (TypeError, ValueError):
            return {"ok": False, "error": "pedido_id inválido."}
    if not filas:
        return {"ok": False, "error": "No hay pedidos para consultar."}
    out = []
    for f in filas:
        p = q("SELECT p.*, r.nombre AS restaurante, d.nombre AS domiciliario, "
              "EXTRACT(EPOCH FROM (now()-p.creado_en))/60 AS minutos "
              "FROM pedidos p JOIN restaurantes r ON r.id=p.restaurante_id "
              "LEFT JOIN domiciliarios d ON d.id=p.domiciliario_id WHERE p.id=%s", (f["id"],), uno=True)
        out.append({"pedido_id": p["id"], "estado": p["estado"], "restaurante": p["restaurante"],
                    "domiciliario": p["domiciliario"], "total": p["total"],
                    "minutos_desde_creacion": round(float(p["minutos"])),
                    "programado_para": p["programado_para"], "notas": p["notas"],
                    "items": [{"producto": i["nombre"], "cantidad": i["cantidad"]} for i in _items_pedido(p["id"])]})
    return {"ok": True, "pedidos": out}


def modificar_o_cancelar_pedido(ctx, pedido_id, accion, producto_id=None, cantidad=1):
    """accion: cancelar | quitar_producto | agregar_producto. Aplica las reglas por estado."""
    cli = _cliente(ctx["sesion_id"])
    try:
        pid = int(pedido_id)
    except (TypeError, ValueError):
        return {"ok": False, "error": "pedido_id inválido."}
    p = q("SELECT * FROM pedidos WHERE id=%s AND cliente_id=%s", (pid, cli["id"] if cli else -1), uno=True)
    if not p:
        return {"ok": False, "error": "No encontré ese pedido en tu cuenta."}

    if accion == "cancelar":
        if p["estado"] not in ESTADOS_CANCELABLES:
            return {"ok": False, "estado": p["estado"],
                    "motivo": "Según la política, un pedido ya asignado, en camino o entregado no se puede cancelar."}
        q("UPDATE pedidos SET estado='cancelado', actualizado_en=now() WHERE id=%s", (pid,))
        return {"ok": True, "pedido_id": pid, "estado": "cancelado"}

    if accion in ("quitar_producto", "agregar_producto"):
        if p["estado"] not in ESTADOS_MODIFICABLES:
            return {"ok": False, "estado": p["estado"],
                    "motivo": "El pedido ya no se puede modificar en este estado."}
        try:
            prod_id = int(producto_id)
            cant = int(cantidad or 1)
        except (TypeError, ValueError):
            return {"ok": False, "error": "producto_id inválido."}
        try:
            return _modificar_items(ctx, p, pid, accion, prod_id, cant)
        except _Abortar as e:
            return e.resultado
    return {"ok": False, "error": "accion debe ser cancelar, quitar_producto o agregar_producto."}


def _modificar_items(ctx, p, pid, accion, prod_id, cant):
    if True:
        with tx() as cur:
            if accion == "quitar_producto":
                cur.execute("DELETE FROM pedido_items WHERE pedido_id=%s AND producto_id=%s", (pid, prod_id))
                if cur.rowcount == 0:
                    raise _Abortar({"ok": False, "error": "Ese producto no está en el pedido."})
            else:
                cur.execute("SELECT * FROM productos WHERE id=%s AND restaurante_id=%s AND disponible",
                            (prod_id, p["restaurante_id"]))
                prod = cur.fetchone()
                if not prod:
                    raise _Abortar({"ok": False, "error": "Producto no disponible en ese restaurante."})
                cur.execute("INSERT INTO pedido_items (pedido_id, producto_id, cantidad, precio_unitario) "
                            "VALUES (%s,%s,%s,%s)", (pid, prod_id, max(1, cant), prod["precio"]))
            cur.execute("SELECT COUNT(*) AS n FROM pedido_items WHERE pedido_id=%s", (pid,))
            if cur.fetchone()["n"] == 0:
                raise _Abortar({"ok": False, "error": "El pedido quedaría vacío; mejor cancélalo."})
            tot = _recalcular(cur, pid)
            # Si estaba esperando la decisión del cliente (falta de producto), se reenvía al restaurante.
            if p["estado"] == "borrador":
                cur.execute("UPDATE pedidos SET estado='pendiente_restaurante' WHERE id=%s", (pid,))
        evento(ctx["sesion_id"], "notificar_restaurante", {"pedido_id": pid, "motivo": "pedido modificado"},
               pedido_id=pid)
        return {"ok": True, "pedido_id": pid, "subtotal": tot["subtotal"],
                "costo_domicilio": tot["costo_domicilio"], "total": tot["total"],
                "estado": "pendiente_restaurante" if p["estado"] == "borrador" else p["estado"],
                "items": [{"producto": i["nombre"], "cantidad": i["cantidad"]} for i in _items_pedido(pid)]}


def escalar_a_humano(ctx, motivo):
    evento(ctx["sesion_id"], "escalar_a_humano", {"motivo": motivo},
           {"alerta": "Caso enviado al despachador humano"})
    return {"ok": True, "mensaje": "Un despachador humano revisará el caso y se comunicará con el cliente."}


def candidatos_domiciliario(ctx, pedido_id):
    p = q("SELECT zona_id FROM pedidos WHERE id=%s", (int(pedido_id),), uno=True)
    if not p:
        return {"ok": False, "error": "Pedido no encontrado."}
    filas = q("SELECT id AS domiciliario_id, nombre, pedidos_activos FROM domiciliarios "
              "WHERE estado <> 'fuera_de_servicio' AND pedidos_activos < 2 AND zona_id=%s "
              "ORDER BY pedidos_activos, id LIMIT 3", (p["zona_id"],))
    if not filas:
        return {"ok": True, "candidatos": [],
                "mensaje": "No hay domiciliarios disponibles en la zona; usa escalar_a_humano."}
    return {"ok": True, "candidatos": filas}


def asignar_domiciliario(ctx, pedido_id, domiciliario_id):
    try:
        pid, did = int(pedido_id), int(domiciliario_id)
    except (TypeError, ValueError):
        return {"ok": False, "error": "Ids inválidos."}
    with tx() as cur:
        cur.execute("SELECT * FROM pedidos WHERE id=%s FOR UPDATE", (pid,))
        p = cur.fetchone()
        if not p or p["estado"] != "aceptado":
            return {"ok": False, "error": "Solo se asigna domiciliario a pedidos en estado aceptado."}
        cur.execute("SELECT * FROM domiciliarios WHERE id=%s AND zona_id=%s AND estado<>'fuera_de_servicio' "
                    "AND pedidos_activos < 2 FOR UPDATE", (did, p["zona_id"]))
        d = cur.fetchone()
        if not d:
            return {"ok": False, "error": "Ese domiciliario no es un candidato válido para la zona."}
        cur.execute("UPDATE domiciliarios SET pedidos_activos=pedidos_activos+1, estado='ocupado' WHERE id=%s", (did,))
        cur.execute("UPDATE pedidos SET estado='asignado', domiciliario_id=%s, actualizado_en=now() WHERE id=%s",
                    (did, pid))
    return {"ok": True, "pedido_id": pid, "domiciliario": d["nombre"], "estado": "asignado"}


# ------------------------------------------------- acciones de restaurante y domiciliario
def accion_restaurante(pedido_id, accion, producto_id=None):
    """Devuelve (sesion_id, aviso [SISTEMA] para el agente) o lanza ValueError."""
    p = q("SELECT p.*, c.sesion_id, r.nombre AS restaurante FROM pedidos p "
          "JOIN clientes c ON c.id=p.cliente_id JOIN restaurantes r ON r.id=p.restaurante_id WHERE p.id=%s",
          (pedido_id,), uno=True)
    if not p:
        raise ValueError("Pedido no encontrado.")
    if p["estado"] != "pendiente_restaurante":
        raise ValueError(f"El pedido está en estado {p['estado']}; ya no admite esta acción.")
    if accion == "aceptar":
        q("UPDATE pedidos SET estado='aceptado', actualizado_en=now() WHERE id=%s", (pedido_id,))
        return p["sesion_id"], (f"[SISTEMA] El restaurante {p['restaurante']} aceptó el pedido {pedido_id}. "
                                "Asigna un domiciliario (candidatos_domiciliario y asignar_domiciliario) "
                                "y avisa al cliente.")
    if accion == "rechazar":
        q("UPDATE pedidos SET estado='rechazado', actualizado_en=now() WHERE id=%s", (pedido_id,))
        return p["sesion_id"], (f"[SISTEMA] El restaurante {p['restaurante']} rechazó el pedido {pedido_id}. "
                                "Informa al cliente y propón alternativas buscando en el menú.")
    if accion == "falta":
        prod = q("SELECT nombre FROM productos pr JOIN pedido_items pi ON pi.producto_id=pr.id "
                 "WHERE pi.pedido_id=%s AND pr.id=%s", (pedido_id, producto_id), uno=True)
        if not prod:
            raise ValueError("Indica qué producto del pedido falta.")
        q("UPDATE productos SET disponible=false WHERE id=%s", (producto_id,))
        q("UPDATE pedidos SET estado='borrador', notas=%s, actualizado_en=now() WHERE id=%s",
          (f"Falta: {prod['nombre']}", pedido_id))
        return p["sesion_id"], (
            f"[SISTEMA] Al pedido {pedido_id} le falta {prod['nombre']} (ya no está disponible). "
            "Informa al cliente, ofrécele continuar sin ese producto o cambiarlo por otro del menú "
            "(buscar_menu). Cuando el cliente decida, aplica el cambio con modificar_o_cancelar_pedido "
            "(producto_id numérico de buscar_menu), que recalcula el total y reenvía el pedido al restaurante.")
    raise ValueError("Acción desconocida.")


def accion_domiciliario(pedido_id, accion):
    """Devuelve (sesion_id, mensaje para el cliente)."""
    p = q("SELECT p.*, c.sesion_id, d.nombre AS domiciliario FROM pedidos p "
          "JOIN clientes c ON c.id=p.cliente_id LEFT JOIN domiciliarios d ON d.id=p.domiciliario_id "
          "WHERE p.id=%s", (pedido_id,), uno=True)
    if not p:
        raise ValueError("Pedido no encontrado.")
    if accion == "recogido":
        if p["estado"] != "asignado":
            raise ValueError(f"El pedido está en estado {p['estado']}.")
        q("UPDATE pedidos SET estado='en_camino', actualizado_en=now() WHERE id=%s", (pedido_id,))
        return p["sesion_id"], f"Tu pedido {pedido_id} ya fue recogido y {p['domiciliario']} va en camino. 🛵"
    if accion == "entregado":
        if p["estado"] != "en_camino":
            raise ValueError(f"El pedido está en estado {p['estado']}.")
        with tx() as cur:
            cur.execute("UPDATE pedidos SET estado='entregado', actualizado_en=now() WHERE id=%s", (pedido_id,))
            cur.execute("UPDATE domiciliarios SET pedidos_activos=GREATEST(pedidos_activos-1,0), "
                        "estado=CASE WHEN pedidos_activos-1 <= 0 THEN 'libre' ELSE 'ocupado' END WHERE id=%s",
                        (p["domiciliario_id"],))
        return p["sesion_id"], f"Tu pedido {pedido_id} fue entregado. ¡Buen provecho!"
    raise ValueError("Acción desconocida.")


def asignacion_de_respaldo(pedido_id):
    """Si el agente no asignó domiciliario a un pedido aceptado, se asigna al mejor candidato."""
    p = q("SELECT estado FROM pedidos WHERE id=%s", (pedido_id,), uno=True)
    if not p or p["estado"] != "aceptado":
        return None
    cands = candidatos_domiciliario({}, pedido_id).get("candidatos") or []
    if not cands:
        return None
    r = asignar_domiciliario({}, pedido_id, cands[0]["domiciliario_id"])
    return r if r.get("ok") else None


def promover_programados():
    """Pedidos programados cuya hora está a menos de 40 minutos pasan al restaurante."""
    filas = q("UPDATE pedidos SET estado='pendiente_restaurante', actualizado_en=now() "
              "WHERE estado='programado' AND programado_para <= now() + interval '40 minutes' RETURNING id")
    for f in filas:
        evento(None, "programado_enviado_al_restaurante", {"pedido_id": f["id"]}, pedido_id=f["id"])
    return len(filas)
