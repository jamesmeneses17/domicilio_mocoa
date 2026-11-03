"""Agente sobre la API de Gemini (Google) usando el SDK oficial `google-genai` y su API de Interactions.

    client = genai.Client(api_key=...)
    interaction = client.interactions.create(model=..., input=..., tools=[...], system_instruction=...)

* Las claves se leen de GEMINI_API_KEY_1..N (variables de entorno o .env). Se usan en orden y, si una
  falla por límite de uso o por ser inválida, se pasa a la siguiente. Nunca se imprimen ni se guardan.
* Si el modelo principal está saturado (503) o retirado (404) se prueba GEMINI_MODELOS_RESPALDO con la
  misma clave antes de rendirse.
* Se usa `store=False`: Google no guarda las interacciones. El historial vive en nuestra PostgreSQL y se
  reenvía en cada turno, igual que con el modelo local.
* Embeddings, voz, PostgreSQL y pgvector siguen siendo locales; a Google solo viaja el diálogo.
"""
import re
import threading
import time

from google import genai
from google.genai import types
from google.genai._gaos.utils import RetryConfig

from . import agente, config, logica
from .db import dumps

_cooldown = {}  # número de clave (1..N) -> instante hasta el cual no se usa
_mod_cooldown = {}  # nombre de modelo -> instante hasta el cual se salta (falló hace poco)
_ultima = {"clave": None, "modelo": None, "respaldo": 0.0, "cambio": None}
_meta = {}  # número de clave -> {"usos", "fallos", "motivo", "ultimo_uso"} (solo para mostrar en pantalla)
_par_malo = {}  # (número de clave, modelo) -> instante hasta el cual se prueba de último (se colgó o falló)
_cuota = {}  # (número de clave, modelo) -> instante en que se renueva su cupo (Google respondió 429)
_clientes = {}  # número de clave -> genai.Client (se crea al primer uso)
_lock = threading.Lock()


class NoDisponible(Exception):
    """Ninguna clave de Gemini pudo atender la petición."""


def _limpiar(texto):
    """Quita cualquier clave del texto antes de registrarlo."""
    t = str(texto)
    for k in config.GEMINI_KEYS:
        t = t.replace(k, "***")
    return re.sub(r"AIza[0-9A-Za-z_\-]{20,}", "***", t)[:260]


def cliente(idx):
    with _lock:
        if idx not in _clientes:
            # Solo se reintenta el 503. Con 429 Google manda "reintenta en 19 h" y el SDK dormía ese tiempo
            # entero (la petición parecía colgada): el 429 debe volver al instante para rotar de modelo/clave.
            opts = types.HttpOptions(base_url=config.GEMINI_BASE or None,
                                     retry_options=types.HttpRetryOptions(attempts=1, http_status_codes=[503]))
            c = genai.Client(api_key=config.GEMINI_KEYS[idx - 1], http_options=opts)
            # El SDK obedece «Retry-After» y se queda dormido (hasta horas) ante un 429: parecía una conexión
            # colgada. Sin reintentos automáticos el error llega al instante y lo manejamos nosotros.
            c.interactions.sdk_configuration.retry_config = RetryConfig("none", None, False)
            _clientes[idx] = c
        return _clientes[idx]


def _m(idx):
    return _meta.setdefault(idx, {"usos": 0, "fallos": 0, "motivo": None, "ultimo_uso": None})


def marcar_respaldo():
    """Anota que se atendió con el modelo local porque Gemini no estuvo disponible."""
    _ultima["respaldo"] = time.time()


def estado_claves():
    """Estado de cada clave, sin secretos: cuál está activa, cuál descansa y por qué.
    Los números son los de las casillas del .env (GEMINI_API_KEY_<n>)."""
    ahora = time.time()
    n = len(config.GEMINI_KEYS)
    libres = [i for i in range(1, n + 1) if _cooldown.get(i, 0) <= ahora]
    en_uso = libres[0] if libres else None  # la que se usará en la próxima petición
    claves = []
    for i in range(1, n + 1):
        m = _m(i)
        espera = max(0, int(_cooldown.get(i, 0) - ahora))
        claves.append({"n": config.GEMINI_SLOTS[i - 1],
                       "estado": "activa" if i == en_uso else ("lista" if i in libres else "en_espera"),
                       "motivo": m["motivo"] if i not in libres else None, "espera_s": espera,
                       "usos": m["usos"], "fallos": m["fallos"], "ultimo_uso": m["ultimo_uso"],
                       "agotada_hoy": bool(i not in libres and espera > 3600)})
    return {"configuradas": n, "disponibles": len(libres),
            "en_uso": config.GEMINI_SLOTS[en_uso - 1] if en_uso else None,
            "ultima_usada": _ultima["clave"], "modelo": _ultima["modelo"] or config.GEMINI_MODEL,
            "todas_agotadas": n > 0 and not libres,
            "respaldo_local_reciente": ahora - _ultima["respaldo"] < 180,
            "cambio_reciente": (_ultima["cambio"] if _ultima["cambio"] and ahora - _ultima["cambio"]["en"] < 300 else None),
            "claves": claves}


def _penalizar(idx, segundos, motivo=""):
    with _lock:
        _cooldown[idx] = time.time() + segundos
        m = _m(idx)
        m["motivo"] = motivo
        m["fallos"] += 1


class _TiempoAgotado(Exception):
    """La petición a Gemini no respondió en GEMINI_TIMEOUT segundos."""


def _con_limite(fn, segundos):
    """Ejecuta fn() y se rinde a los `segundos`. El `timeout` del SDK no corta algunas peticiones colgadas
    (se observó gemini-3.8-flash sin responder por minutos), así que el límite se impone desde fuera."""
    res = {}

    def _correr():
        try:
            res["v"] = fn()
        except BaseException as e:  # noqa: BLE001
            res["e"] = e

    t = threading.Thread(target=_correr, daemon=True)
    t.start()
    t.join(segundos)
    if t.is_alive():
        raise _TiempoAgotado(f"sin respuesta en {segundos}s")
    if "e" in res:
        raise res["e"]
    return res["v"]


def _segundos_de_espera(texto):
    """'Please retry in 19h10m22s' -> segundos (None si el mensaje no lo dice)."""
    m = re.search(r"retry in\s*(?:(\d+)h)?\s*(?:(\d+)m)?\s*(?:([\d.]+)s)?", texto)
    if not m or not any(m.groups()):
        return None
    h, mi, s = (float(x) if x else 0.0 for x in m.groups())
    return int(h * 3600 + mi * 60 + s)


def _pares_en_orden():
    """Combinaciones (clave, modelo) a probar: primero las sanas, y las que fallaron hace poco al final.

    El fallo suele ser de una combinación concreta (p. ej. un modelo con una clave), no de todo Gemini."""
    todos = [config.GEMINI_MODEL] + [m for m in config.GEMINI_MODELOS_RESPALDO if m != config.GEMINI_MODEL]
    ahora = time.time()
    pares = [(idx, m) for m in todos for idx in range(1, len(config.GEMINI_KEYS) + 1)]
    return sorted(pares, key=lambda p: _par_malo.get(p, 0) > ahora), todos[0]  # sorted es estable


def llamar(sesion_id, **peticion):
    """client.interactions.create(...) probando claves y modelos. Devuelve (interaction, clave, modelo).

    Se rinde (NoDisponible) a los GEMINI_PRESUPUESTO segundos para que el agente pase al modelo local."""
    if not config.GEMINI_KEYS:
        raise NoDisponible("No hay claves de Gemini configuradas (GEMINI_API_KEY_1..3).")
    pares, principal = _pares_en_orden()
    hasta = time.time() + config.GEMINI_PRESUPUESTO
    motivos, claves_limitadas, modelos_caidos = [], set(), set()
    for idx, modelo in pares:
        if _cooldown.get(idx, 0) > time.time() or idx in claves_limitadas or modelo in modelos_caidos \
                or _cuota.get((idx, modelo), 0) > time.time():
            continue
        if time.time() > hasta:
            motivos.append(f"se agotó el tiempo máximo ({config.GEMINI_PRESUPUESTO}s)")
            break
        slot = config.GEMINI_SLOTS[idx - 1]  # número de casilla en el .env (lo que ve el usuario)
        err = None
        for intento in range(2):  # 500/503 suelen ser picos de demanda: un reintento con espera corta
            try:
                limite = min(config.GEMINI_TIMEOUT, max(3.0, hasta - time.time()))
                inter = _con_limite(
                    lambda i=idx, m=modelo: cliente(i).interactions.create(model=m, store=False,
                                                                          timeout=limite, **peticion),
                    limite)
                if _ultima["clave"] not in (None, slot):
                    logica.evento(sesion_id, "gemini_cambio_de_clave", {"de": _ultima["clave"], "a": slot})
                _ultima["clave"], _ultima["modelo"] = slot, modelo
                _par_malo.pop((idx, modelo), None)
                with _lock:
                    m = _m(idx)
                    m["usos"] += 1
                    m["ultimo_uso"] = time.time()
                    m["motivo"] = None
                if modelo != principal:
                    logica.evento(sesion_id, "gemini_modelo_de_respaldo", {"principal": principal, "usado": modelo})
                return inter, slot, modelo
            except Exception as e:  # noqa: BLE001 - el SDK lanza distintos tipos según el error HTTP
                err = e
                codigo = getattr(e, "status_code", None)
                if codigo is not None and codigo >= 500 and intento == 0 and time.time() < hasta - 5:
                    logica.evento(sesion_id, "gemini_reintento", {"clave": slot, "modelo": modelo, "http": codigo})
                    time.sleep(2)
                    continue
                break
        codigo = getattr(err, "status_code", None)
        texto_err = str(err).lower()
        if codigo is None:  # sin conexión, tiempo agotado...
            motivos.append(f"clave #{slot}, {modelo}: sin respuesta ({type(err).__name__})")
            logica.registrar_error(sesion_id, "gemini", motivos[-1])
            _par_malo[(idx, modelo)] = time.time() + 600  # 10 min al final de la fila
            continue
        if codigo >= 500 or codigo == 404:  # modelo saturado o retirado: es del modelo, no de la clave
            motivos.append(f"clave #{slot}, {modelo}: HTTP {codigo}")
            logica.registrar_error(sesion_id, "gemini", motivos[-1])
            modelos_caidos.add(modelo)
            for i in range(1, len(config.GEMINI_KEYS) + 1):
                _par_malo[(i, modelo)] = time.time() + 300
            continue
        if codigo == 429:
            # El cupo es por clave Y por modelo: se marca solo esa combinación y se prueba la siguiente.
            diaria = "perday" in texto_err.replace(" ", "") or "per day" in texto_err
            causa = "límite diario agotado" if diaria else "límite por minuto"
            espera = min(_segundos_de_espera(texto_err) or (6 * 3600 if diaria else 65), 24 * 3600)
            _cuota[(idx, modelo)] = time.time() + espera
            motivos.append(f"clave #{slot}, {modelo}: {causa}")
            logica.registrar_error(sesion_id, "gemini", motivos[-1])
            hasta_libre = [_cuota.get((idx, m), 0) for m in {mm for _, mm in pares}]
            if all(h > time.time() for h in hasta_libre):   # ningún modelo le queda a esta clave
                _penalizar(idx, max(hasta_libre) - time.time(), causa)
            continue
        elif codigo == 403 and ("free tier" in texto_err or "billing" in texto_err):
            causa = "sin cupo gratuito: el proyecto de Google requiere activar facturación"
            _penalizar(idx, 6 * 3600, causa)
        elif codigo in (401, 403) or (codigo == 400 and "api key" in texto_err):
            causa = "clave inválida o sin permiso"
            _penalizar(idx, 1800, causa)  # 30 min: si fue un falso positivo, la clave se recupera sola
        else:  # 400: la petición está mal; otra clave no lo arregla
            logica.registrar_error(sesion_id, "gemini", f"HTTP {codigo}: {_limpiar(err)}")
            raise NoDisponible(f"Gemini rechazó la solicitud (HTTP {codigo}).")
        claves_limitadas.add(idx)  # la clave está limitada: ningún modelo servirá con ella
        motivos.append(f"clave #{slot}: HTTP {codigo} ({causa})")
        logica.registrar_error(sesion_id, "gemini", motivos[-1])
    raise NoDisponible("; ".join(motivos) or "Todas las claves están en espera por límite de uso.")


# ------------------------------------------------------------------ conversión de formatos
def _herramientas(herramientas):
    out = []
    for h in herramientas:
        f = h["function"]
        d = {"type": "function", "name": f["name"], "description": f["description"]}
        params = dict(f["parameters"])
        if params.get("properties"):
            if not params.get("required"):
                params.pop("required", None)
            d["parameters"] = params
        out.append(d)
    return out


def _pasos(sesion_id):
    """Historial de la BD -> pasos de Interactions (user_input / model_output, empezando por user)."""
    out = []
    for h in agente._historial(sesion_id):
        tipo = "model_output" if h["role"] == "assistant" else "user_input"
        if out and out[-1]["type"] == tipo:
            out[-1]["content"][0]["text"] += "\n" + h["content"]
        else:
            out.append({"type": tipo, "content": [{"type": "text", "text": h["content"]}]})
    while out and out[0]["type"] != "user_input":
        out.pop(0)
    return out


def _texto(inter):
    if getattr(inter, "output_text", None):
        return inter.output_text.strip()
    partes = []
    for s in inter.steps or []:
        if s.type == "model_output":
            for c in s.content or []:
                if getattr(c, "type", None) == "text" and getattr(c, "text", None):
                    partes.append(c.text)
    return "".join(partes).strip()


# ------------------------------------------------------------------ ciclo del agente
def ejecutar(sesion_id, despacho=False):
    """Un turno del agente con Gemini. Devuelve (texto_final, ms_total)."""
    cli = logica.q("SELECT * FROM clientes WHERE sesion_id=%s", (sesion_id,), uno=True)
    ctx = {"sesion_id": sesion_id, "cliente_id": cli["id"] if cli else None}
    herramientas = agente.HERRAMIENTAS_CLIENTE + (agente.HERRAMIENTAS_DESPACHO if despacho else [])
    base = {
        "system_instruction": agente.prompt_sistema(cli["nombre"] if cli else "cliente"),
        "tools": _herramientas(herramientas),
        "generation_config": {"temperature": 0.3, "max_output_tokens": 600},
    }
    entrada = _pasos(sesion_id)
    t_total = time.time()
    final, fallos = "", 0

    for it in range(8):
        t0 = time.time()
        inter, clave, modelo = llamar(sesion_id, input=entrada, **base)
        ms = int((time.time() - t0) * 1000)
        pasos = inter.steps or []
        llamadas = [s for s in pasos if s.type == "function_call"]
        logica.evento(sesion_id, "llm", {"iteracion": it + 1, "proveedor": "gemini", "api": "interactions",
                                         "modelo": modelo, "clave": clave},
                      {"decision": [c.name for c in llamadas] if llamadas else "responder",
                       "estado": str(getattr(inter, "status", ""))}, ms)
        if not llamadas:
            final = _texto(inter)
            if not final:
                logica.registrar_error(sesion_id, "gemini", f"Respuesta vacía (estado {getattr(inter, 'status', '?')})")
            break

        # Se devuelven tal cual los pasos del modelo (incluye las firmas de razonamiento) y luego los resultados.
        entrada = entrada + [s.model_dump(exclude_none=True, by_alias=True) for s in pasos]
        for c in llamadas:
            args = c.arguments or {}
            t1 = time.time()
            try:
                res = agente._ejecutar_herramienta(ctx, c.name, args)
            except Exception as e:  # noqa: BLE001
                logica.registrar_error(sesion_id, "herramienta", f"{c.name}: {e}")
                res = {"ok": False, "error": "Fallo interno de la herramienta."}
            if isinstance(res, dict) and res.get("ok") is False:
                fallos += 1
            if c.name != "escalar_a_humano":  # esa herramienta ya deja su propio registro
                logica.evento(sesion_id, c.name, args, res, int((time.time() - t1) * 1000),
                              pedido_id=res.get("pedido_id") if isinstance(res, dict) else None)
            entrada.append({"type": "function_result", "call_id": c.id, "name": c.name, "result": dumps(res)})
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
