"""Agente en modo GUIADO, pensado para LLM pequeños (p. ej. qwen2.5:1.5b en CPU).

Reparto de trabajo:
  * El LLM interpreta el mensaje (intención + datos) con salida JSON obligatoria y redacta las
    respuestas abiertas (preguntas frecuentes, políticas).
  * El código elige la herramienta, valida y arma los mensajes que llevan dinero o estados,
    para que ningún total o estado dependa de lo que "invente" el modelo.
Cada herramienta usada queda en la bitácora igual que en el modo de herramientas.
"""
import difflib
import json
import random
import re
import time
import unicodedata
from datetime import timedelta

import requests

from . import config, logica
from .agente import FUNCIONES, LOCK_LLM, _ejecutar_herramienta
from .db import dumps, q

BORRADORES = {}  # sesion_id -> borrador del pedido en construcción

ESQUEMA = {"type": "object", "properties": {
    "intencion": {"type": "string",
                  "enum": ["pedir", "menu", "estado", "cancelar", "pregunta", "reclamo", "saludo", "otro"]},
    "items": {"type": "array", "items": {"type": "object", "properties": {
        "producto": {"type": "string"}, "cantidad": {"type": "integer"},
        "observacion": {"type": "string"}}, "required": ["producto", "cantidad"]}},
    "barrio": {"type": "string"}, "direccion": {"type": "string"},
    "metodo_pago": {"type": "string"}, "pedido_id": {"type": "integer"}},
    "required": ["intencion", "items", "barrio", "direccion", "metodo_pago", "pedido_id"]}

ESTADO_LEGIBLE = {
    "borrador": "esperando tu decisión", "programado": "programado, se enviará al restaurante a tiempo",
    "pendiente_restaurante": "esperando que el restaurante lo acepte",
    "aceptado": "aceptado por el restaurante, falta asignar domiciliario",
    "rechazado": "rechazado por el restaurante", "asignado": "asignado a un domiciliario que va por él",
    "en_camino": "en camino hacia ti", "entregado": "entregado", "cancelado": "cancelado"}


# ------------------------------------------------------------------ utilidades
def norm(s):
    s = unicodedata.normalize("NFD", str(s or "").lower())
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return re.sub(r"[^a-z0-9 ]+", " ", s).strip()


def dinero(n):
    return "$" + f"{float(n):,.0f}".replace(",", ".")


def _nuevo():
    return {"items": [], "barrio": None, "direccion": None, "pago": None, "programado_para": None,
            "preguntando": None, "esperando_confirmacion": False, "esperando_hora": False, "falta": None}


def _llm(sesion_id, system, user, formato=None, max_tokens=200, etiqueta="llm"):
    cuerpo = {"model": config.LLM_MODEL, "stream": False, "keep_alive": "30m",
              "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
              "options": {"temperature": 0, "num_ctx": 2048, "num_predict": max_tokens}}
    if formato:
        cuerpo["format"] = formato
    t0 = time.time()
    with LOCK_LLM:
        r = requests.post(f"{config.OLLAMA_URL}/api/chat", json=cuerpo, timeout=600)
    r.raise_for_status()
    ms = int((time.time() - t0) * 1000)
    return r.json()["message"]["content"].strip(), ms


def usar(sesion_id, nombre, **args):
    """Ejecuta una herramienta y la deja en la bitácora."""
    ctx = {"sesion_id": sesion_id}
    t0 = time.time()
    try:
        res = _ejecutar_herramienta(ctx, nombre, args)
    except Exception as e:  # noqa: BLE001
        logica.registrar_error(sesion_id, "herramienta", f"{nombre}: {e}")
        res = {"ok": False, "error": "Fallo interno de la herramienta."}
    if nombre != "escalar_a_humano":  # esa herramienta ya deja su propio registro
        logica.evento(sesion_id, nombre, args, res, int((time.time() - t0) * 1000),
                      pedido_id=res.get("pedido_id") if isinstance(res, dict) else None)
    return res


# ------------------------------------------------------------------ interpretación (LLM)
def _prompt_extraccion(sesion_id, d, ultima_pregunta):
    rests = q("SELECT nombre FROM restaurantes WHERE activo ORDER BY id")
    zonas = [z["nombre"] for z in q("SELECT nombre FROM zonas ORDER BY id")]
    return f"""Eres un extractor de datos para un servicio de domicilios. Lee el último mensaje del cliente y \
responde SOLO con JSON.
Restaurantes: {', '.join(r['nombre'] for r in rests)}.
Barrios con cobertura: {', '.join(zonas)}.
Reglas:
- intencion: "pedir" si quiere comida o da datos de su pedido (productos, barrio, dirección, pago); \
"menu" si pregunta qué hay o precios; "estado" si pregunta por su pedido; "cancelar"; \
"pregunta" si tiene una duda (pagos, horarios, políticas, recomendaciones); \
"reclamo" si se queja o dice que se demora; "saludo"; "otro" si no tiene que ver con domicilios.
- items: nombre del plato o bebida tal como lo dijo el cliente (ej. "pizza hawaiana", "jugo de lulo") y cantidad entera. Ejemplo: "dos corrientazos, uno de pollo y uno de carne \
y una limonada" => [{{"producto":"Corrientazo de pollo","cantidad":1}},{{"producto":"Corrientazo de carne",\
"cantidad":1}},{{"producto":"Limonada","cantidad":1}}]
- barrio: solo si nombra un barrio o lugar (ej. "esquina del colegio"); si no, "".
- direccion: calle, carrera o número de casa; si no hay, "".
- metodo_pago: "efectivo", "transferencia" o "".
- pedido_id: número de pedido mencionado o 0.
Lo último que preguntó el asistente: "{ultima_pregunta[:160]}"."""


def interpretar(sesion_id, d, texto):
    ult = q("SELECT contenido FROM mensajes WHERE sesion_id=%s AND rol='agente' ORDER BY id DESC LIMIT 1",
            (sesion_id,), uno=True)
    try:
        crudo, ms = _llm(sesion_id, _prompt_extraccion(sesion_id, d, ult["contenido"] if ult else ""),
                         texto, formato=ESQUEMA, max_tokens=220)
        ex = json.loads(crudo)
    except Exception as e:  # noqa: BLE001
        logica.registrar_error(sesion_id, "llm", f"interpretación: {e}")
        return None
    logica.evento(sesion_id, "llm_interpreta", {"mensaje": texto}, ex, ms)
    return ex


# ------------------------------------------------------------------ reconocimiento por reglas
AFIRMA = re.compile(r"\b(si|sii+|confirmo|confirmado|dale|listo|ok|okay|claro|correcto|de una|perfecto|"
                    r"adelante|hagale|va|bueno)\b")
NIEGA = re.compile(r"^\s*(no|nop|nel|mejor no)\b")


def _afirma(t):
    n = norm(t)
    return bool(AFIRMA.search(n)) and not NIEGA.match(n)


def _niega(t):
    return bool(NIEGA.match(norm(t)))


PREGUNTA = re.compile(r"^\W*(como|cual|cuales|cuanto|cuanta|cuantos|que|quien|donde|cuando|puedo|se puede|"
                      r"aceptan|hay|tienen|atienden|reciben|hasta que)\b")
MENU = re.compile(r"\b(menu|carta|que tienen|que hay|que venden|que ofrecen|precios)\b")


RECO = re.compile(r"(recomiend|sugier|antoj|ganas de|que me (sirve|das|ofreces)|"
                  r"algo (caliente|liviano|ligero|economico|barato|dulce|frio|fresco|rico)|sin picante)")
SALUD = re.compile(r"\b(acne|diabet|gastritis|colesterol|presion|enferm|dolor|alergi|gripa|dieta|"
                   r"bajar de peso|curar|medicin)")


_RECOMENDADOS = {}  # sesion_id -> ids de productos ya recomendados (para no repetir)


def _contexto(texto):
    """Qué busca el cliente según lo que dice: temperatura, etiquetas y categoría."""
    n = norm(texto)
    ctx = {"temperatura": None, "etiquetas": [], "categoria": None, "motivo": ""}
    if re.search(r"(algo|bebida|jugo|postre|comida) (bien |muy )?(frio|fria|helado|helada|fresco|fresca)", n)             or re.search(r"(calor|caluros|sed|sofoc|refresc|insolacion)", n):
        ctx["temperatura"], ctx["etiquetas"], ctx["motivo"] = "fria", ["refrescante"], "Para el calor"
    elif re.search(r"(frio|llov|lluvi|fresquito|neblina|madrugada|caliente|calientico|calentar)", n):
        ctx["temperatura"], ctx["etiquetas"], ctx["motivo"] = "caliente", ["para_el_frio"], "Para el frío"
    extra = {"liviano": "liviano|ligero|saludable|dieta|sano|acne", "economico": "economic|barato|poca plata|ahorrar",
             "dulce": "dulce|postre|golosina", "sin_picante": "sin picante|no picante",
             "picante": "picante|ají|aji", "contundente": "hambre|contundente|llen|abundante",
             "desayuno": "desayuno|manana|madrug", "vegetariano": "vegetarian|sin carne"}
    for et, pat in extra.items():
        if re.search(pat, n) and not (et == "picante" and "sin picante" in n):
            ctx["etiquetas"].append(et)
    for cat, pat in {"sopa": "sopa|caldo", "bebida": "bebida|tomar|jugo|beber", "postre": "postre|helado|dulce",
                     "snack": "snack|pasaboca|empanada", "plato fuerte": "almuerzo|plato fuerte|comida fuerte"}.items():
        if re.search(pat, n):
            ctx["categoria"] = cat
            break
    return ctx


_ULTIMO_CTX = {}  # sesion_id -> último contexto de recomendación
OTRA = re.compile(r"(otra opcion|otras opciones|algo distinto|algo diferente|que mas)")


def _base(nombre):
    """'Jugo de lulo (16 oz)' -> 'jugo de lulo': para no recomendar dos tamaños del mismo producto."""
    return re.sub(r"\s*\(.*?\)", "", nombre).strip().lower()


def _texto_restaurantes(sesion_id, filtro=None):
    """Lista de restaurantes (tipo de comida y si están abiertos) para que el cliente elija."""
    r = usar(sesion_id, "listar_restaurantes", **({"texto": filtro} if filtro else {}))
    lineas = [f"• {x['nombre']} — {x['tipo_de_comida'] or 'comida variada'}"
              f"{'' if x['abierto_ahora'] else ' (cerrado ahora)'}" for x in r["restaurantes"]]
    if not lineas:
        return "No encontré restaurantes con ese nombre o tipo de comida."
    return ("Estos son los restaurantes disponibles:\n" + "\n".join(lineas) +
            "\n¿De cuál quieres ver el menú, o qué se te antoja (por ejemplo «algo caliente» o «una pizza»)?")


def _restaurante_mencionado(texto):
    """Restaurante al que se refiere el cliente: por su nombre (peso 3) o por su tipo de comida (peso 1).

    Devuelve None si no hay uno claro (p. ej. «pollo» está en varios), y entonces se muestra la lista."""
    t = norm(texto)
    palabras = {w[:5] for w in t.split() if len(w) >= 4}   # raíz de 5 letras: «pizza» ~ «pizzas»
    ignorar = {"restaurante", "nombre", "confirmar", "menu", "mocoa", "comida", "comidas", "personales", "medianas",
               "rellenos", "variada", "platos", "especiales", "bebidas", "ver", "quiero"}
    mejor, mejor_pt, empate = None, 0, False
    for r in q("SELECT id, nombre, tipo_comida FROM restaurantes WHERE activo ORDER BY id"):
        n = norm(r["nombre"])
        pt = 10 if n in t else 0
        pt += 3 * sum(1 for w in n.split() if len(w) >= 4 and w not in ignorar and w[:5] in palabras)
        pt += sum(1 for w in norm(r["tipo_comida"]).replace("/", " ").split()
                  if len(w) >= 4 and w not in ignorar and w[:5] in palabras)
        if pt > mejor_pt:
            mejor, mejor_pt, empate = r, pt, False
        elif pt == mejor_pt and pt > 0:
            empate = True
    return None if empate or mejor_pt < 1 else mejor


def _menu_de(sesion_id, rest):
    """Carta resumida de un restaurante (sin adicionales; los tamaños se agrupan)."""
    m = usar(sesion_id, "buscar_menu", restaurante_id=rest["id"], limite=300)
    prods = [p for p in m["productos"] if p["categoria"] != "adicional"]
    por_base = {}
    for p in prods:
        por_base.setdefault(_base(p["producto"]), []).append(p)
    lineas = []
    for lista in por_base.values():
        precios = sorted({float(x["precio"]) for x in lista})
        rango = dinero(precios[0]) if len(precios) == 1 else f"{dinero(precios[0])} a {dinero(precios[-1])}"
        lineas.append(f"• {_base(lista[0]['producto']).capitalize()} — {rango}")
    top = lineas[:14]
    resto = f"\n…y {len(lineas) - 14} opciones más; dime qué se te antoja." if len(lineas) > 14 else ""
    return f"Menú de {prods[0]['restaurante'] if prods else rest['nombre']}:\n" + "\n".join(top) + resto


def _recomendar(sesion_id, texto, salud):
    """Recomienda platos según el contexto: etiquetas del menú + búsqueda semántica en pgvector.

    Puntúa, prefiere restaurantes abiertos y evita repetir lo ya recomendado en la conversación."""
    ctx = _contexto(texto)
    if not (ctx["temperatura"] or ctx["etiquetas"] or ctx["categoria"]) and _ULTIMO_CTX.get(sesion_id):
        ctx = _ULTIMO_CTX[sesion_id]   # «otra opción»: se mantiene lo que buscaba (frío, calor, liviano...)
    _ULTIMO_CTX[sesion_id] = ctx
    k = usar(sesion_id, "consultar_conocimiento", pregunta=texto, tipo="menu")
    semanticos = {norm(r["titulo"]) for r in k.get("resultados", [])}
    m = usar(sesion_id, "buscar_menu", limite=700)
    productos = [p for p in m["productos"] if p.get("categoria") != "adicional"]
    vistos = _RECOMENDADOS.setdefault(sesion_id, set())

    def puntaje(p):
        pt = 0.0
        if ctx["temperatura"] and p.get("temperatura") == ctx["temperatura"]:
            pt += 3
        pt += 2 * sum(1 for e in ctx["etiquetas"] if e in (p.get("etiquetas") or []))
        if ctx["categoria"] and ctx["categoria"] in (p.get("categoria") or ""):
            pt += 2
        if norm(p["producto"]) in semanticos:
            pt += 1.5
        return pt

    random.shuffle(productos)  # desempata al azar para que no salga siempre lo mismo
    pts = {p["producto_id"]: puntaje(p) for p in productos}
    candidatos = [p for p in productos if pts[p["producto_id"]] > 0] or productos
    abiertos = [p for p in candidatos if p["abierto_ahora"]]
    candidatos = abiertos or candidatos
    nuevos = [p for p in candidatos if _base(p["producto"]) not in vistos]
    candidatos = nuevos if len(nuevos) >= 2 else candidatos
    candidatos.sort(key=lambda p: -pts[p["producto_id"]])
    elegidos, rests, bases = [], set(), set()
    for p in candidatos:  # variedad: un producto por restaurante, sin repetir el mismo en otro tamaño
        if len(elegidos) < 3 and p["restaurante_id"] not in rests and _base(p["producto"]) not in bases:
            elegidos.append(p)
            rests.add(p["restaurante_id"])
            bases.add(_base(p["producto"]))
    for p in candidatos:
        if len(elegidos) < 3 and p not in elegidos and _base(p["producto"]) not in bases:
            elegidos.append(p)
            bases.add(_base(p["producto"]))
    vistos.update(_base(p["producto"]) for p in elegidos)
    nota = ("No puedo dar consejos médicos ni tratar enfermedades, pero tomo nota de que prefieres algo "
            "liviano. ") if salud else ""
    intro = (ctx["motivo"] + " te recomiendo") if ctx["motivo"] else "Te recomiendo"
    lista = "; ".join(f"{p['producto']} ({dinero(p['precio'])}, {p['restaurante']}"
                      f"{'' if p['abierto_ahora'] else ', cerrado ahora'})" for p in elegidos)
    return f"{nota}{intro}: {lista}. ¿Te armo el pedido con alguno?"


def _hay_producto(t):
    return any(_mencionado(p["nombre"], t) for p in q("SELECT p.nombre FROM productos p JOIN restaurantes r ON r.id=p.restaurante_id WHERE r.activo AND p.disponible"))


def _pago(t):
    n = norm(t)
    if re.search(r"\befectivo\b|\bcash\b", n):
        return "efectivo"
    if re.search(r"transfer|nequi|daviplata|bancolombia", n):
        return "transferencia"
    return None


def _zona_en_texto(t):
    n = norm(t)
    for z in q("SELECT nombre FROM zonas ORDER BY length(nombre) DESC"):
        if re.search(rf"\b{re.escape(norm(z['nombre']))}\b", n):
            return z["nombre"]
    return None


def _hora_en_texto(t):
    n = norm(t)
    m = re.search(r"(\d{1,2})(?:[: ](\d{2}))?\s*(am|pm|de la manana|de la tarde|de la noche)?", n)
    if "mediodia" in n:
        h, mi = 12, 0
    elif m:
        h, mi = int(m.group(1)), int(m.group(2) or 0)
        suf = m.group(3) or ""
        if suf in ("pm", "de la tarde", "de la noche") and h < 12:
            h += 12
    else:
        return None
    if h > 23 or mi > 59:
        return None
    ahora = config.ahora()
    dia = ahora + timedelta(days=1) if "manana" in n else ahora
    dt = dia.replace(hour=h, minute=mi, second=0, microsecond=0)
    if dt <= ahora:
        dt += timedelta(days=1)
    return dt.strftime("%Y-%m-%dT%H:%M")


GENERICAS = {"pizza", "pizzas", "hamburguesa", "burger", "jugo", "jugos", "sandwich", "arepa", "cono", "perro", "soda",
             "arroz", "pollo", "carne", "cerdo", "salchipapa", "granizado", "ensalada", "combinacion", "frutas",
             "personal", "mediana", "combo", "unidad", "cuarto", "medio", "octavo", "base", "frappe", "bowl",
             "menu", "caliente", "calientes", "frio", "fria", "natural", "naturales", "especial", "sencillo",
             "sencilla", "tradicional", "infantil", "clasica"}


def _coincide(palabra, dichas):
    """¿Dijo el cliente esta palabra (o su singular/plural)?  'pizzas' ~ 'pizza', 'maracuyada' ~ 'maracuya'."""
    return any(palabra == u or (len(u) >= 4 and len(palabra) >= 4 and abs(len(u) - len(palabra)) <= 3
                                and (palabra.startswith(u) or u.startswith(palabra)))
               for u in dichas)


def _coincidencias(nombre_producto, texto):
    """(distintivas dichas, total de distintivas, ¿dijo la primera?, todas las palabras del nombre que dijo).

    Las palabras genéricas («pizza», «jugo», «caliente»...) no distinguen un producto de otro. Si TODAS las
    palabras del nombre son genéricas («Perro caliente Sencillo», «Jugo Hit») se exige que el cliente diga al
    menos dos tercios de ellas."""
    dichas = set(norm(texto).split())
    todas = [w for w in norm(nombre_producto).split() if len(w) >= 3]
    n_todas = sum(1 for w in todas if _coincide(w, dichas))
    claves = [w for w in todas if len(w) >= 4 and w not in GENERICAS]
    if not claves:   # nombre solo con palabras genéricas
        necesarias = max(1, -(-2 * len(todas) // 3))   # techo de 2/3
        return (len(todas), len(todas), True, n_todas) if n_todas >= necesarias else (0, len(todas), False, n_todas)
    esta = [_coincide(w, dichas) for w in claves]
    return sum(esta), len(claves), esta[0], n_todas


def _mencionado(nombre_producto, texto):
    """¿El cliente realmente nombró este producto? (los LLM pequeños rellenan 'items' con inventos).

    Basta la primera palabra distintiva o la mitad de ellas: «frío» solo no alcanza para «Milo frío o caliente»."""
    n, total, primera, _ = _coincidencias(nombre_producto, texto)
    return total > 0 and (primera or n >= total / 2)


def resolver_producto(texto):
    """Asocia un nombre dicho por el cliente con un producto disponible (o None)."""
    objetivo = norm(texto)
    todos = q("SELECT p.id, p.restaurante_id, p.nombre, p.precio, p.categoria FROM productos p "
              "JOIN restaurantes r ON r.id=p.restaurante_id WHERE p.disponible AND r.activo ORDER BY p.id")
    # Primero solo productos que comparten una palabra distintiva con lo dicho («cerveza» no debe dar «Cereza»);
    # los adicionales (toppings, adiciones) solo si el cliente los pide.
    quiere_adicional = bool(re.search(r"(adicion|topping|extra|agregale|agrega)", objetivo))
    candidatos = [p for p in todos if _mencionado(p["nombre"], objetivo)
                  and (quiere_adicional or p["categoria"] != "adicional")]
    if candidatos:   # gana: más palabras distintivas, luego más palabras del nombre, luego el más barato (el tamaño menor)
        def clave(p):
            n, total, _, n_todas = _coincidencias(p["nombre"], objetivo)
            return (-n, -n_todas, -(n / total if total else 0), p["precio"],
                    -difflib.SequenceMatcher(None, objetivo, norm(p["nombre"])).ratio())
        return sorted(candidatos, key=clave)[0]
    mejor, mejor_s = None, 0.0
    for p in todos:   # sin coincidencia por palabras: solo un parecido muy alto (errores de ortografía)
        s = difflib.SequenceMatcher(None, objetivo, norm(p["nombre"])).ratio()
        if s > mejor_s:
            mejor, mejor_s = p, s
    return mejor if mejor_s >= 0.8 else None


# ------------------------------------------------------------------ pasos del pedido
def _aplicar(sesion_id, d, ex, texto):
    """Mezcla en el borrador lo que dijo el cliente. Devuelve un mensaje si hay que interrumpir."""
    aviso = None
    # productos
    nuevos = []
    for it in (ex or {}).get("items") or []:
        prod = resolver_producto(it.get("producto", ""))
        if not prod:
            aviso = f"No encontré «{it.get('producto')}» en el menú."
            continue
        if not _mencionado(prod["nombre"], texto):
            continue  # el modelo lo inventó: el cliente no lo dijo
        nuevos.append({"producto_id": prod["id"], "restaurante_id": prod["restaurante_id"],
                       "nombre": prod["nombre"], "precio": prod["precio"],
                       "cantidad": max(1, int(it.get("cantidad") or 1)),
                       "observacion": (it.get("observacion") or None)})
    if nuevos:
        if d["items"] and re.search(r"\b(agrega|agregame|anade|anademe|tambien|ademas|otra|otro)\b", norm(texto)):
            for n in nuevos:
                ya = next((i for i in d["items"] if i["producto_id"] == n["producto_id"]), None)
                if ya:
                    ya["cantidad"] += n["cantidad"]
                else:
                    d["items"].append(n)
        else:
            d["items"] = nuevos
    # barrio: primero lo que se nombra en el texto; luego lo que extrajo el modelo / apodos (pgvector)
    zona = _zona_en_texto(texto)
    candidato = (ex or {}).get("barrio") or ""
    if candidato and norm(candidato) not in norm(texto):
        candidato = ""  # el modelo lo inventó: el cliente no lo dijo
    if not candidato and d["preguntando"] == "barrio":
        candidato = texto
    if not zona and candidato:
        zona = _zona_en_texto(candidato)
        if not zona:
            k = usar(sesion_id, "consultar_conocimiento", pregunta=candidato, tipo="lugar")
            if k.get("suficiente"):
                zona = _zona_en_texto(k["resultados"][0]["contenido"])
            if not zona:
                aviso = (f"No encontré cobertura en «{candidato}». Atendemos Centro, Pablo VI y Condagua.")
    if zona:
        d["barrio"] = zona
    # dirección
    dire = ((ex or {}).get("direccion") or "").strip()
    if not dire and d["preguntando"] == "direccion" and not _zona_en_texto(texto) == texto.strip():
        dire = texto.strip()[:150]
    if dire:
        d["direccion"] = dire
    # pago
    pago = _pago(texto) or (ex or {}).get("metodo_pago")
    if pago in ("efectivo", "transferencia"):
        d["pago"] = pago
    return aviso


def _resumen_items(items):
    return "\n".join(f"• {i['cantidad']} × {i['nombre']} ({dinero(i['precio'])} c/u)"
                     + (f" — {i['observacion']}" if i.get("observacion") else "") for i in items)


def _avanzar(sesion_id, d):
    """Pide el dato que falta o cotiza y pide confirmación."""
    d["esperando_confirmacion"] = d["esperando_hora"] = False
    if not d["items"]:
        d["preguntando"] = "items"
        return "¿Qué te gustaría pedir? " + _texto_restaurantes(sesion_id)
    rest_ids = {i["restaurante_id"] for i in d["items"]}
    if len(rest_ids) > 1:
        d["items"] = [i for i in d["items"] if i["restaurante_id"] == d["items"][0]["restaurante_id"]]
        d["preguntando"] = None
        return ("Cada pedido se hace a un solo restaurante, así que dejé solo los productos de "
                f"{q('SELECT nombre FROM restaurantes WHERE id=%s', (d['items'][0]['restaurante_id'],), uno=True)['nombre']}. "
                "¿Seguimos con eso?")
    if not d["barrio"]:
        d["preguntando"] = "barrio"
        return "¿En qué barrio te lo entregamos? (Centro, Pablo VI o Condagua)"
    if not d["direccion"]:
        d["preguntando"] = "direccion"
        return "¿Cuál es la dirección de entrega?"
    if not d["pago"]:
        d["preguntando"] = "pago"
        return "¿Pagas en efectivo o por transferencia?"
    cot = usar(sesion_id, "cotizar_pedido", restaurante_id=d["items"][0]["restaurante_id"],
               items=[{"producto_id": i["producto_id"], "cantidad": i["cantidad"],
                       "observacion": i["observacion"]} for i in d["items"]],
               barrio=d["barrio"], programado_para=d["programado_para"])
    if not cot.get("ok"):
        if cot.get("restaurante_cerrado") or "cerrado" in cot.get("error", ""):
            d["esperando_hora"] = True
            d["programado_para"] = None
            return (cot["error"].split(" Ofrece")[0] + " ¿Quieres programarlo? "
                    "Dime la hora (por ejemplo: «mañana a las 12»).")
        d["preguntando"] = None
        return cot.get("error", "No pude cotizar el pedido.")
    d["preguntando"], d["esperando_confirmacion"] = None, True
    cuando = f"\nProgramado para: {d['programado_para'].replace('T', ' ')}" if d["programado_para"] else ""
    return (f"Resumen de tu pedido en {cot['restaurante']}:\n"
            + "\n".join(f"• {i['cantidad']} × {i['producto']} ({dinero(i['precio_unitario'])} c/u)"
                        for i in cot["items"])
            + f"\nSubtotal: {dinero(cot['subtotal'])}\nDomicilio ({cot['zona']}): {dinero(cot['costo_domicilio'])}"
              f"\nTotal: {dinero(cot['total'])}\nDirección: {d['direccion']} · Pago: {d['pago']}{cuando}"
              "\n¿Confirmas el pedido?")


def _confirmar(sesion_id, d):
    r = usar(sesion_id, "crear_pedido", restaurante_id=d["items"][0]["restaurante_id"],
             items=[{"producto_id": i["producto_id"], "cantidad": i["cantidad"], "observacion": i["observacion"]}
                    for i in d["items"]],
             direccion=d["direccion"], barrio=d["barrio"], metodo_pago=d["pago"],
             programado_para=d["programado_para"])
    if not r.get("ok"):
        d["esperando_confirmacion"] = False
        return r.get("error", "No pude crear el pedido.")
    BORRADORES[sesion_id] = _nuevo()
    if r["estado"] == "programado":
        return (f"¡Listo! Tu pedido {r['pedido_id']} quedó programado ({dinero(r['total'])}). "
                "Lo enviaremos al restaurante unos 40 minutos antes de la hora.")
    return (f"¡Listo! Tu pedido {r['pedido_id']} fue enviado a {r['restaurante']} (total {dinero(r['total'])}). "
            "Te aviso apenas lo acepten.")


def _pedido_objetivo(sesion_id, ex, texto, solo_activos=True):
    m = re.search(r"pedido\s*(?:n[o°º.]*\s*)?#?(\d+)", norm(texto))
    pid = int(m.group(1)) if m else (ex or {}).get("pedido_id") or 0
    if pid:
        return pid
    extra = "AND p.estado NOT IN ('entregado','cancelado','rechazado')" if solo_activos else ""
    f = q(f"SELECT p.id FROM pedidos p JOIN clientes c ON c.id=p.cliente_id WHERE c.sesion_id=%s {extra} "
          "ORDER BY p.id DESC LIMIT 1", (sesion_id,), uno=True)
    return f["id"] if f else None


def _texto_estado(p):
    base = f"Tu pedido {p['pedido_id']} en {p['restaurante']} está {ESTADO_LEGIBLE.get(p['estado'], p['estado'])}"
    if p.get("domiciliario") and p["estado"] in ("asignado", "en_camino"):
        base += f" (domiciliario: {p['domiciliario']})"
    return base + f". Total: {dinero(p['total'])}."


# ------------------------------------------------------------------ turno del cliente
def _responder_pregunta(sesion_id, texto):
    k = usar(sesion_id, "consultar_conocimiento", pregunta=texto)
    if k.get("resultados") and k["resultados"][0]["similitud"] < 0.3:
        return ("Con gusto, pero solo puedo ayudarte con domicilios: pedidos, menús, estados, "
                "pagos y cancelaciones. 🙂")
    if not k.get("suficiente"):
        usar(sesion_id, "escalar_a_humano", motivo=f"Sin información confiable para: {texto[:120]}")
        return ("No tengo esa información con seguridad, así que pasé tu consulta a un despachador "
                "humano que te responderá pronto.")
    base = "\n".join(f"- {r['contenido']}" for r in k["resultados"][:2])
    try:
        t, ms = _llm(sesion_id,
                     "Eres el asistente de un servicio de domicilios en Mocoa. Responde en español, amable, "
                     "máximo 2 frases, usando SOLO esta información. Si no alcanza, dilo.\n" + base,
                     texto, max_tokens=110)
        logica.evento(sesion_id, "llm_redacta", {"fuente": [r["titulo"] for r in k["resultados"][:2]]},
                      {"texto": t}, ms)
        return t
    except Exception as e:  # noqa: BLE001
        logica.registrar_error(sesion_id, "llm", e)
        return k["resultados"][0]["contenido"]


def ejecutar(sesion_id, despacho=False):
    t_total = time.time()
    cli = q("SELECT nombre FROM clientes WHERE sesion_id=%s", (sesion_id,), uno=True)
    nombre = cli["nombre"] if cli else "cliente"
    ultimo = q("SELECT contenido FROM mensajes WHERE sesion_id=%s AND rol='usuario' ORDER BY id DESC LIMIT 1",
               (sesion_id,), uno=True)
    texto = ultimo["contenido"] if ultimo else ""
    d = BORRADORES.setdefault(sesion_id, _nuevo())
    msg = _turno(sesion_id, d, nombre, texto)
    return msg, int((time.time() - t_total) * 1000)


def _turno(sesion_id, d, nombre, texto):
    # --- respuesta a una falta de producto avisada por el restaurante
    if d["falta"]:
        f = d["falta"]
        if re.search(r"cancel", norm(texto)):
            d["falta"] = None
            r = usar(sesion_id, "modificar_o_cancelar_pedido", pedido_id=f["pedido_id"], accion="cancelar")
            return f"Listo, cancelé el pedido {f['pedido_id']}." if r.get("ok") else r.get("motivo", "No pude cancelarlo.")
        ex = None
        if not (_afirma(texto) or re.search(r"\b(sin|quita|quitalo|continu|sigue|asi|no importa)\b", norm(texto))):
            ex = interpretar(sesion_id, d, texto)
        nuevos = [resolver_producto(i["producto"]) for i in (ex or {}).get("items", [])]
        nuevos = [(p, i) for p, i in zip(nuevos, (ex or {}).get("items", [])) if p and p["restaurante_id"] == f["restaurante_id"]]
        if not nuevos and ex is not None:
            return "¿Prefieres continuar sin ese producto o cambiarlo por otro? Dime cuál."
        d["falta"] = None
        r = usar(sesion_id, "modificar_o_cancelar_pedido", pedido_id=f["pedido_id"], accion="quitar_producto",
                 producto_id=f["producto_id"])
        for p, i in nuevos:
            r = usar(sesion_id, "modificar_o_cancelar_pedido", pedido_id=f["pedido_id"],
                     accion="agregar_producto", producto_id=p["id"], cantidad=max(1, int(i.get("cantidad") or 1)))
        if not r.get("ok"):
            return r.get("error") or r.get("motivo") or "No pude modificar el pedido."
        return (f"Listo, actualicé tu pedido {f['pedido_id']}. Nuevo total: {dinero(r['total'])}. "
                "Ya se lo reenvié al restaurante.")

    # --- confirmación del resumen
    if d["esperando_confirmacion"]:
        if _afirma(texto):
            return _confirmar(sesion_id, d)
        if _niega(texto):
            d["esperando_confirmacion"] = False
            return "Sin problema. ¿Qué quieres cambiar: productos, dirección o forma de pago?"

    # --- hora para pedido programado
    if d["esperando_hora"]:
        if _niega(texto):
            BORRADORES[sesion_id] = _nuevo()
            return "Listo, no programé nada. Cuando quieras, me escribes de nuevo."
        hora = _hora_en_texto(texto)
        if hora:
            d["programado_para"] = hora
            return _avanzar(sesion_id, d)
        return "Dime la hora en que lo quieres (por ejemplo «mañana a las 12»)."

    ex = interpretar(sesion_id, d, texto)
    if ex is None:
        return "Perdón, no te entendí bien. ¿Me lo puedes repetir de otra forma?"
    intencion = ex["intencion"]
    n = norm(texto)
    # reglas que corrigen la clasificación de un LLM pequeño
    if not _hay_producto(texto):
        if MENU.search(n):
            intencion = "menu"
        elif (PREGUNTA.match(n) or "?" in texto) and intencion in ("pedir", "otro", "pregunta", "saludo", "menu"):
            intencion = "pregunta"
    # si estaba pidiendo un dato del pedido, lo que llegue se interpreta como ese dato
    if d["preguntando"] and intencion in ("otro", "saludo", "menu"):
        intencion = "pedir"

    if not _hay_producto(texto) and not d["preguntando"] and (
            RECO.search(n) or SALUD.search(n) or (sesion_id in _ULTIMO_CTX and OTRA.search(n))):
        return _recomendar(sesion_id, texto, bool(SALUD.search(n)))
    if intencion == "saludo":
        return f"¡Hola {nombre}! Soy el despachador virtual de {config.NOMBRE_EMPRESA}. ¿Qué te gustaría pedir hoy?"
    if intencion == "otro":
        return "Con gusto, pero solo puedo ayudarte con domicilios: pedidos, menús, estados y cancelaciones. 🙂"
    if intencion == "menu":
        rest = _restaurante_mencionado(texto)
        return _menu_de(sesion_id, rest) if rest else _texto_restaurantes(sesion_id)
    if intencion == "pregunta":
        resp = _responder_pregunta(sesion_id, texto)
        if d["items"] and (d["preguntando"] or d["esperando_confirmacion"]):
            resp += "\n\nSigamos con tu pedido:\n" + _avanzar(sesion_id, d)
        return resp

    if intencion == "estado":
        pid = _pedido_objetivo(sesion_id, ex, texto, solo_activos=False)
        if not pid:
            return "Todavía no tienes pedidos conmigo. ¿Quieres hacer uno?"
        r = usar(sesion_id, "estado_pedido", pedido_id=pid)
        return _texto_estado(r["pedidos"][0]) if r.get("ok") else r.get("error", "No encontré ese pedido.")

    if intencion == "reclamo" or re.search(r"\b(lleva|llevo|demora|demoro|tarda|tardando|tarde)\b", n):
        pid = _pedido_objetivo(sesion_id, ex, texto)
        if not pid:
            usar(sesion_id, "escalar_a_humano", motivo=f"Reclamo sin pedido: {texto[:120]}")
            return "Lamento el inconveniente. Pasé tu caso a un despachador humano para que te contacte."
        e = usar(sesion_id, "estado_pedido", pedido_id=pid)
        k = usar(sesion_id, "consultar_conocimiento", pregunta="qué pasa si el pedido se retrasa", tipo="politica")
        p = e["pedidos"][0] if e.get("ok") else None
        dicho = re.search(r"(\d{2,3})\s*min", n)
        minutos = max(p["minutos_desde_creacion"] if p else 0, int(dicho.group(1)) if dicho else 0)
        usar(sesion_id, "escalar_a_humano", motivo=f"Reclamo por demora (~{minutos} min) en pedido {pid}")
        pol = " Según nuestra política, un retraso de más de 30 minutos se escala a un despachador."             if k.get("suficiente") else ""
        return (f"Lamento la demora. {_texto_estado(p) if p else ''}{pol} "
                "Ya pasé tu caso a un despachador humano para que lo revise.")

    if intencion == "cancelar":
        pid = _pedido_objetivo(sesion_id, ex, texto)
        if not pid:
            if d["items"]:
                BORRADORES[sesion_id] = _nuevo()
                return "Listo, descarté el pedido que estábamos armando."
            return "No tienes pedidos activos para cancelar."
        r = usar(sesion_id, "modificar_o_cancelar_pedido", pedido_id=pid, accion="cancelar")
        if r.get("ok"):
            return f"Listo, cancelé tu pedido {pid}."
        k = usar(sesion_id, "consultar_conocimiento", pregunta="política de cancelación de pedidos", tipo="politica")
        pol = f" {k['resultados'][0]['contenido']}" if k.get("suficiente") else ""
        return f"No puedo cancelar el pedido {pid}: está {ESTADO_LEGIBLE.get(r.get('estado'), r.get('estado'))}.{pol}"

    # --- pedir / completar datos
    aviso = _aplicar(sesion_id, d, ex, texto)
    siguiente = _avanzar(sesion_id, d)
    return f"{aviso}\n{siguiente}" if aviso else siguiente


# ------------------------------------------------------------------ eventos de la plataforma
def evento_sistema(sesion_id, pedido_id, accion, producto_id=None):
    """El restaurante actuó sobre un pedido: el agente decide y avisa al cliente."""
    p = q("SELECT p.*, r.nombre AS restaurante FROM pedidos p JOIN restaurantes r ON r.id=p.restaurante_id "
          "WHERE p.id=%s", (pedido_id,), uno=True)
    if accion == "aceptar":
        c = usar(sesion_id, "candidatos_domiciliario", pedido_id=pedido_id)
        if c.get("candidatos"):
            mejor = c["candidatos"][0]
            a = usar(sesion_id, "asignar_domiciliario", pedido_id=pedido_id,
                     domiciliario_id=mejor["domiciliario_id"])
            if a.get("ok"):
                return (f"¡Buenas noticias! {p['restaurante']} aceptó tu pedido {pedido_id} y lo asigné a "
                        f"{a['domiciliario']}, que lo recogerá enseguida.")
        usar(sesion_id, "escalar_a_humano", motivo=f"Sin domiciliarios para el pedido {pedido_id}")
        return (f"{p['restaurante']} aceptó tu pedido {pedido_id}, pero ahora no hay domiciliarios disponibles "
                "en tu zona. Ya avisé a un despachador humano para resolverlo.")
    if accion == "rechazar":
        return (f"{p['restaurante']} no pudo aceptar tu pedido {pedido_id}. Lamento el inconveniente. "
                "¿Quieres que armemos otro pedido con otras opciones?")
    if accion == "falta":
        prod = q("SELECT * FROM productos WHERE id=%s", (producto_id,), uno=True)
        item = q("SELECT cantidad, precio_unitario FROM pedido_items WHERE pedido_id=%s AND producto_id=%s",
                 (pedido_id, producto_id), uno=True)
        sin = float(p["total"]) - float(item["cantidad"] * item["precio_unitario"]) if item else float(p["total"])
        alt = usar(sesion_id, "buscar_menu", restaurante_id=p["restaurante_id"])
        enpedido = {i["producto_id"] for i in logica._items_pedido(pedido_id)}
        opciones = [x for x in alt["productos"] if x["producto_id"] not in enpedido
                    and x["categoria"] != "adicional"][:3]
        BORRADORES.setdefault(sesion_id, _nuevo())["falta"] = {
            "pedido_id": pedido_id, "producto_id": producto_id, "restaurante_id": p["restaurante_id"]}
        extra = (" O puedo cambiarlo por: " + ", ".join(f"{o['producto']} ({dinero(o['precio'])})" for o in opciones)
                 + ".") if opciones else ""
        return (f"Al pedido {pedido_id} le falta {prod['nombre']}. Puedo continuar sin ese producto "
                f"(nuevo total: {dinero(sin)}).{extra} ¿Qué prefieres?")
    return "Hay una novedad con tu pedido."
