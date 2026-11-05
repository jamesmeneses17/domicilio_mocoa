"""Agente despachador: ciclo LLM + herramientas sobre Ollama, con trazabilidad."""
import contextvars
import inspect
import json
import re
import threading
import time

import requests

from . import config, logica
from .db import dumps, q

LOCK_LLM = threading.Lock()  # un solo modelo en CPU: se atiende de a una petición
_activos = 0
_lock_activos = threading.Lock()
_gen = {}        # sesion_id -> número del turno vigente (subirlo invalida el turno que esté corriendo)
_corriendo = {}  # sesion_id -> número del turno que está pensando ahora
_turno = contextvars.ContextVar("turno", default=None)  # (sesion_id, gen) del turno que ejecuta este hilo


class Cancelado(BaseException):  # BaseException: los "except Exception" de las herramientas no deben taparla
    """El cliente detuvo o reemplazó este turno: se abandona sin guardar respuesta."""


def nuevo_turno(sesion_id) -> int:
    """Abre un turno para la sesión e invalida el anterior (así un mensaje nuevo reemplaza al que estaba pensando)."""
    with _lock_activos:
        _gen[sesion_id] = _gen.get(sesion_id, 0) + 1
        _corriendo[sesion_id] = _gen[sesion_id]
        return _gen[sesion_id]


def detener(sesion_id) -> bool:
    """Detiene el turno en curso. Devuelve True si había uno pensando."""
    with _lock_activos:
        habia = sesion_id in _corriendo
        _gen[sesion_id] = _gen.get(sesion_id, 0) + 1
        _corriendo.pop(sesion_id, None)
        return habia


def vigente(sesion_id, gen) -> bool:
    return _gen.get(sesion_id, 0) == gen


def verificar():
    """Punto de control: se llama entre pasos (antes de cada llamada al modelo o herramienta)."""
    t = _turno.get()
    if t and not vigente(*t):
        raise Cancelado()


class trabajando:
    """Marca que hay un turno del agente en curso (para el indicador 'pensando' de la interfaz)."""

    def __init__(self, sesion_id=None, gen=None):
        self.sesion_id, self.gen = sesion_id, gen

    def __enter__(self):
        global _activos
        with _lock_activos:
            _activos += 1
        if self.sesion_id is not None:
            self.token = _turno.set((self.sesion_id, self.gen))

    def __exit__(self, *a):
        global _activos
        with _lock_activos:
            _activos -= 1
            if self.sesion_id is not None and _corriendo.get(self.sesion_id) == self.gen:
                _corriendo.pop(self.sesion_id, None)
        if self.sesion_id is not None:
            _turno.reset(self.token)


def pensando(sesion_id=None) -> bool:
    return sesion_id in _corriendo if sesion_id else _activos > 0


def sesiones_pensando() -> list:
    return list(_corriendo)


def _t(nombre, desc, props, req):
    return {"type": "function", "function": {
        "name": nombre, "description": desc,
        "parameters": {"type": "object", "properties": props, "required": req}}}


_ITEMS = {"type": "array", "description": "Productos del pedido",
          "items": {"type": "object", "properties": {
              "producto_id": {"type": "integer", "description": "id numérico devuelto por buscar_menu"},
              "cantidad": {"type": "integer"},
              "observacion": {"type": "string", "description": "ej. sin cebolla"}},
              "required": ["producto_id", "cantidad"]}}

HERRAMIENTAS_CLIENTE = [
    _t("buscar_menu",
       "Lista los productos DISPONIBLES con precio, id y si el restaurante está abierto ahora. "
       "Úsala siempre antes de hablar de productos, precios u horarios. Sin argumentos devuelve todo el menú. "
       "Para RECOMENDAR según el contexto filtra: frío, lluvia o mañana => temperatura='caliente' "
       "(o etiqueta 'para_el_frio'); calor o sed => temperatura='fria' (o etiqueta 'refrescante'); "
       "etiquetas: liviano, contundente, economico, dulce, picante, sin_picante, vegetariano, saludable, "
       "desayuno, para_compartir.",
       {"restaurante_id": {"type": "integer"},
        "texto": {"type": "string", "description": "palabra a buscar, ej. 'pollo' o 'hamburguesa'"},
        "temperatura": {"type": "string", "enum": ["caliente", "fria"]},
        "etiqueta": {"type": "string", "enum": ["liviano", "contundente", "economico", "dulce", "refrescante",
                                                "para_el_frio", "picante", "sin_picante", "vegetariano",
                                                "saludable", "desayuno", "para_compartir"]},
        "categoria": {"type": "string", "description": "sopa, bebida, postre, snack o plato fuerte"}}, []),
    _t("listar_restaurantes",
       "Lista los restaurantes disponibles con su tipo de comida, horario y si están abiertos ahora. Úsala cuando el "
       "cliente pregunte qué restaurantes o qué tipos de comida hay, o antes de recomendar un restaurante. "
       "Con 'texto' filtra por nombre o tipo (ej. 'pizza', 'china', 'jugos').",
       {"texto": {"type": "string", "description": "nombre o tipo de comida a buscar"}}, []),
    _t("verificar_restaurante",
       "Dice si un restaurante está abierto ahora y cuál es su horario. Úsala cuando el cliente pregunte "
       "si está abierto o antes de ofrecer un restaurante.",
       {"restaurante_id": {"type": "integer"}}, ["restaurante_id"]),
    _t("calcular_tarifa", "Devuelve la tarifa de domicilio de un barrio y si tiene cobertura.",
       {"barrio": {"type": "string"}}, ["barrio"]),
    _t("consultar_conocimiento",
       "Busca en la base de conocimiento por significado: políticas (cancelación, retrasos), preguntas "
       "frecuentes (pagos, horarios) y apodos de lugares (ej. 'esquina del colegio' => barrio). "
       "Si 'suficiente' es false no inventes la respuesta.",
       {"pregunta": {"type": "string"},
        "tipo": {"type": "string", "enum": ["politica", "faq", "menu", "lugar"]}}, ["pregunta"]),
    _t("cotizar_pedido",
       "Calcula subtotal, domicilio y total SIN crear el pedido. Úsala para mostrar el total al cliente "
       "y pedir confirmación. También valida que el restaurante esté abierto.",
       {"restaurante_id": {"type": "integer"}, "items": _ITEMS,
        "barrio": {"type": "string"},
        "programado_para": {"type": "string", "description": "solo si es programado: YYYY-MM-DDTHH:MM"}},
       ["restaurante_id", "items", "barrio"]),
    _t("crear_pedido",
       "Crea el pedido y lo envía al restaurante. Úsala SOLO después de que el cliente confirmó con claridad "
       "productos, dirección, forma de pago y total.",
       {"restaurante_id": {"type": "integer"}, "items": _ITEMS,
        "direccion": {"type": "string", "description": "dirección de entrega"},
        "barrio": {"type": "string"},
        "metodo_pago": {"type": "string", "enum": ["efectivo", "transferencia"]},
        "programado_para": {"type": "string", "description": "solo si es programado: YYYY-MM-DDTHH:MM"}},
       ["restaurante_id", "items", "direccion", "barrio", "metodo_pago"]),
    _t("estado_pedido",
       "Devuelve estado, restaurante, domiciliario, total y minutos transcurridos de un pedido. "
       "Sin pedido_id devuelve los últimos del cliente.",
       {"pedido_id": {"type": "integer"}}, []),
    _t("modificar_o_cancelar_pedido",
       "Cancela un pedido o quita/agrega un producto. Aplica las reglas de la política por estado; "
       "no prometas lo que devuelva ok=false.",
       {"pedido_id": {"type": "integer"},
        "accion": {"type": "string", "enum": ["cancelar", "quitar_producto", "agregar_producto"]},
        "producto_id": {"type": "integer"}, "cantidad": {"type": "integer"}}, ["pedido_id", "accion"]),
    _t("escalar_a_humano",
       "Pasa el caso a un despachador humano (reclamos, cliente molesto, herramienta que falla, "
       "sin información confiable o sin domiciliarios).",
       {"motivo": {"type": "string"}}, ["motivo"]),
]

HERRAMIENTAS_DESPACHO = [
    _t("candidatos_domiciliario",
       "Devuelve hasta 3 domiciliarios disponibles en la zona del pedido, ordenados por menos carga.",
       {"pedido_id": {"type": "integer"}}, ["pedido_id"]),
    _t("asignar_domiciliario",
       "Asigna un domiciliario (de candidatos_domiciliario) a un pedido aceptado.",
       {"pedido_id": {"type": "integer"}, "domiciliario_id": {"type": "integer"}},
       ["pedido_id", "domiciliario_id"]),
]

FUNCIONES = {f.__name__: f for f in (
    logica.buscar_menu, logica.listar_restaurantes, logica.verificar_restaurante, logica.calcular_tarifa, logica.consultar_conocimiento, logica.cotizar_pedido,
    logica.crear_pedido, logica.estado_pedido, logica.modificar_o_cancelar_pedido,
    logica.escalar_a_humano, logica.candidatos_domiciliario, logica.asignar_domiciliario)}


def _pedidos_del_cliente(sesion_id):
    """Resumen de los pedidos de esta conversación, para que el modelo distinga uno nuevo de uno ya cerrado."""
    filas = q("SELECT p.id, p.estado, p.total, p.metodo_pago, p.direccion_entrega, r.nombre AS restaurante "
              "FROM pedidos p JOIN clientes c ON c.id=p.cliente_id JOIN restaurantes r ON r.id=p.restaurante_id "
              "WHERE c.sesion_id=%s ORDER BY p.id DESC LIMIT 5", (sesion_id,))
    if not filas:
        return "Este cliente todavía no tiene pedidos."
    cerrados = ("entregado", "cancelado", "rechazado")
    return "\n".join(f"- Pedido {f['id']} en {f['restaurante']}: {f['estado']}"
                     f"{' (CERRADO, ya no se puede modificar)' if f['estado'] in cerrados else ' (en curso)'}; "
                     f"total {f['total']:,.0f}; pago {f['metodo_pago'] or 'sin definir'}; "
                     f"dirección {f['direccion_entrega']}" for f in filas)


def prompt_sistema(cliente, sesion_id=None):
    ahora = config.ahora()
    pedidos = _pedidos_del_cliente(sesion_id) if sesion_id else ""
    return f"""Eres el "Despachador virtual" de {config.NOMBRE_EMPRESA}, una empresa de domicilios en Mocoa. \
Hablas con clientes por chat, en español colombiano, con tono amable y breve (máximo 3 frases por mensaje).
Ahora son las {ahora:%H:%M} del {ahora:%Y-%m-%d} (hora de Colombia). El cliente se llama {cliente}.

Tu trabajo: tomar pedidos de comida, coordinarlos con el restaurante y el domiciliario, y responder sobre el \
estado de los pedidos.

Reglas:
1. Nunca inventes productos, precios, horarios ni estados. Siempre consulta con las herramientas.
2. Nunca calcules totales tú mismo: usa el valor que devuelven cotizar_pedido, crear_pedido o \
modificar_o_cancelar_pedido.
3. Antes de crear un pedido: busca el menú, cotiza y confirma con el cliente productos, observaciones, \
dirección, forma de pago (efectivo o transferencia) y total. Llama a crear_pedido solo cuando el cliente \
confirme con claridad.
4. Si falta un dato, pregunta solo ese dato, una pregunta a la vez.
5. Si el restaurante está cerrado o un producto no está disponible, dilo y propón alternativas o programar \
el pedido para una hora en que esté abierto.
6. Para cancelar o modificar un pedido usa modificar_o_cancelar_pedido; no prometas lo que la política no \
permite. Para entender un barrio o apodo de lugar usa consultar_conocimiento con tipo 'lugar'.
7. Si no encuentras la respuesta con las herramientas, dilo y usa escalar_a_humano. Escala también si el \
cliente reclama o está molesto, si una herramienta falla dos veces o si no hay domiciliarios.
8. Los mensajes que empiezan con [SISTEMA] son avisos de la plataforma, no del cliente. Actúa según ellos \
y redacta un mensaje claro para el cliente.
9. Si el cliente pide algo que no tiene que ver con domicilios, responde con amabilidad que solo puedes \
ayudar con pedidos.
10. Pide solo los datos necesarios para el pedido; no pidas documentos ni datos sensibles. No muestres ids \
internos al cliente, solo el número de pedido.
11. Entiende el contexto del cliente y úsalo para recomendar: filtra buscar_menu por temperatura o etiqueta \
según el clima o el antojo (frío o lluvia => caliente; calor => fria; "liviano", "económico", "dulce"...). \
Sugiere 2 o 3 opciones VARIADAS (de distintos restaurantes o categorías, p. ej. una sopa y una bebida) y no \
repitas las que ya recomendaste en la conversación. Recomienda solo productos que devuelvan las herramientas \
y prefiere los de restaurantes abiertos ahora.
12. Si el cliente menciona algo de salud (acné, dieta, una dolencia), trátalo únicamente como una preferencia \
(por ejemplo "prefiere comida liviana"). No des consejos médicos ni diagnósticos.
13. Los totales los calculan las herramientas; tú solo los comunicas tal cual.
14. Hay muchos restaurantes (comida china, pizzas, hamburguesas, arepas, jugos, parrilla...). Si el cliente \
pregunta qué hay o qué tipos de comida, usa listar_restaurantes; para ver una carta usa buscar_menu con \
restaurante_id (o con texto/filtros) y no listes más de 4 o 5 opciones a la vez. Cada pedido es de un solo \
restaurante. Muchos productos tienen varios tamaños: aclara cuál quiere (ej. 16 oz o 22 oz, personal o mediana).
15. Bebidas alcohólicas (cerveza, michelada): no las recomiendes ni las ofrezcas por tu cuenta; si el cliente \
las pide, confirma que es mayor de edad antes de crear el pedido.
16. Un pedido ENTREGADO, CANCELADO o RECHAZADO está cerrado. Si el cliente pide algo después, es un pedido NUEVO: \
no reutilices productos, restaurante ni totales del anterior; vuelve a buscar el menú, cotiza y pide confirmación \
otra vez. Puedes preguntar si usa la misma dirección y forma de pago de la vez anterior, pero no los des por hechos.
17. Los ids (producto_id, restaurante_id) salen SOLO de los resultados de las herramientas de este mismo turno. \
Nunca los adivines ni los recuerdes de mensajes anteriores: si no tienes el id, llama a buscar_menu con el nombre \
del producto y SIN restaurante_id. Busca con palabras cortas ("sandwich", "capuchino"). Si una búsqueda no encuentra \
algo que antes ofreciste, vuelve a buscar sin filtros antes de decir que no existe.
18. Formas de pago: efectivo (se paga al domiciliario al recibir) o transferencia. Con transferencia, después de \
crear_pedido comunica tal cual 'instrucciones_pago' (a dónde y cuánto transferir) y comparte 'recibo_url', el \
enlace del recibo en PDF. No pidas datos bancarios al cliente.

Pedidos de este cliente:
{pedidos}"""


def _historial(sesion_id, limite=8):
    filas = q("SELECT rol, contenido FROM mensajes WHERE sesion_id=%s ORDER BY id DESC LIMIT %s",
              (sesion_id, limite))
    out = []
    for f in reversed(filas):
        rol = "assistant" if f["rol"] == "agente" else "user"
        out.append({"role": rol, "content": f["contenido"]})
    return out


def _llamadas_en_texto(contenido):
    """Los modelos pequeños a veces escriben la llamada como JSON en el texto."""
    if not contenido:
        return []
    limpio = re.sub(r"</?tool_call>", "", contenido).strip()
    if not limpio.startswith("{"):
        return []
    try:
        d = json.loads(limpio)
    except ValueError:
        return []
    if isinstance(d, dict) and d.get("name") in FUNCIONES:
        return [{"function": {"name": d["name"], "arguments": d.get("arguments") or {}}}]
    return []


def _ejecutar_herramienta(ctx, nombre, args):
    f = FUNCIONES.get(nombre)
    if not f:
        return {"ok": False, "error": f"Herramienta desconocida: {nombre}"}
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            args = {}
    validos = set(inspect.signature(f).parameters) - {"ctx"}
    args = {k: v for k, v in (args or {}).items() if k in validos}
    try:
        return f(ctx, **args)
    except TypeError as e:  # faltan argumentos obligatorios
        return {"ok": False, "error": f"Argumentos inválidos: {e}"}


def ejecutar(sesion_id, despacho=False):
    """Corre el agente sobre el historial de la sesión (el último mensaje ya está guardado).

    Devuelve (texto_final, ms_total)."""
    if config.PROVEEDOR == "gemini":
        from . import gemini, guiado
        try:
            return gemini.ejecutar(sesion_id, despacho)
        except gemini.NoDisponible as e:
            logica.registrar_error(sesion_id, "gemini", e)
            if despacho:
                raise  # el llamador (main) decide cómo atender el evento con el modelo local
            if config.GEMINI_FALLBACK_OLLAMA:
                gemini.marcar_respaldo()
                logica.evento(sesion_id, "respaldo_ollama", {"motivo": "Gemini no disponible"},
                              {"modelo": config.LLM_MODEL})
                return guiado.ejecutar(sesion_id, despacho)
            logica.escalar_a_humano({"sesion_id": sesion_id}, f"Gemini no disponible: {e}")
            return ("Tuve un problema técnico y no puedo atenderte ahora. Pasé tu caso a un despachador "
                    "humano.", 0)
    if config.MODO == "guiado":
        from . import guiado
        return guiado.ejecutar(sesion_id, despacho)
    cli = q("SELECT * FROM clientes WHERE sesion_id=%s", (sesion_id,), uno=True)
    ctx = {"sesion_id": sesion_id, "cliente_id": cli["id"] if cli else None}
    herramientas = HERRAMIENTAS_CLIENTE + (HERRAMIENTAS_DESPACHO if despacho else [])
    msgs = [{"role": "system", "content": prompt_sistema(cli["nombre"] if cli else "cliente", sesion_id)}]
    msgs += _historial(sesion_id)
    t_total = time.time()
    final, fallos = "", 0

    with LOCK_LLM:
        for it in range(7):
            verificar()
            t0 = time.time()
            try:
                r = requests.post(f"{config.OLLAMA_URL}/api/chat", timeout=600, json={
                    "model": config.LLM_MODEL, "messages": msgs, "tools": herramientas, "stream": False,
                    "keep_alive": "30m", "options": {"temperature": 0.2, "num_ctx": 4096}})
                r.raise_for_status()
                m = r.json()["message"]
            except Exception as e:  # noqa: BLE001
                logica.registrar_error(sesion_id, "llm", e)
                final = "Tuve un problema técnico. Voy a pasar tu caso a un despachador humano."
                logica.escalar_a_humano(ctx, f"Fallo del LLM: {e}")
                break
            ms = int((time.time() - t0) * 1000)
            llamadas = m.get("tool_calls") or _llamadas_en_texto(m.get("content"))
            logica.evento(sesion_id, "llm", {"iteracion": it + 1},
                          {"decision": [c["function"]["name"] for c in llamadas] if llamadas else "responder"},
                          ms)
            if not llamadas:
                final = (m.get("content") or "").strip()
                break
            msgs.append({"role": "assistant", "content": m.get("content") or "", "tool_calls": llamadas})
            for c in llamadas:
                nombre, args = c["function"]["name"], c["function"].get("arguments") or {}
                t1 = time.time()
                try:
                    res = _ejecutar_herramienta(ctx, nombre, args)
                except Exception as e:  # noqa: BLE001
                    logica.registrar_error(sesion_id, "herramienta", f"{nombre}: {e}")
                    res = {"ok": False, "error": "Fallo interno de la herramienta."}
                if isinstance(res, dict) and res.get("ok") is False:
                    fallos += 1
                if nombre != "escalar_a_humano":  # esa herramienta ya deja su propio registro
                    logica.evento(sesion_id, nombre, args, res, int((time.time() - t1) * 1000),
                                  pedido_id=res.get("pedido_id") if isinstance(res, dict) else None)
                msgs.append({"role": "tool", "tool_name": nombre, "content": dumps(res)})
            if fallos >= 3:
                final = "No logro completar esto por mi cuenta. Pasaré tu caso a un despachador humano."
                logica.escalar_a_humano(ctx, "Varias herramientas fallaron seguidas")
                break
        else:
            final = "No pude completar la solicitud. Un despachador humano te ayudará."
            logica.escalar_a_humano(ctx, "El agente superó el máximo de pasos")

    if not final:
        final = "¿Me puedes repetir eso, por favor?"
    return final, int((time.time() - t_total) * 1000)
