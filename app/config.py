import os
from datetime import datetime, timedelta, timezone

RAIZ = os.path.dirname(os.path.dirname(__file__))


def _cargar_env():
    """Lee RAIZ/.env (CLAVE=valor) sin dependencias. Las variables del sistema tienen prioridad."""
    ruta = os.path.join(RAIZ, ".env")
    if not os.path.isfile(ruta):
        return
    with open(ruta, encoding="utf-8-sig") as f:
        for linea in f:
            linea = linea.strip()
            if not linea or linea.startswith("#") or "=" not in linea:
                continue
            k, v = linea.split("=", 1)
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if k and v and k not in os.environ:
                os.environ[k] = v


_cargar_env()

# ----------------------------------------------------------------- proveedor del LLM
# "gemini": razonamiento y diálogo por la API de Gemini (los mensajes se envían a Google).
# "ollama": modelo local. "auto": Gemini si hay al menos una clave, si no Ollama.
_prov = os.getenv("LLM_PROVEEDOR", "auto").strip().lower()

# Claves: GEMINI_API_KEY_1, _2, _3 ... Se prueban en orden; si una falla se usa la siguiente.
# Nunca se escriben en el código, ni se muestran en la interfaz o en la bitácora.
GEMINI_KEYS = []
GEMINI_SLOTS = []  # número de casilla del .env de cada clave (GEMINI_API_KEY_<n>), para mostrar la numeración real
GEMINI_MAX_CLAVES = 4  # casillas GEMINI_API_KEY_1..4 en el .env
for _i in range(1, GEMINI_MAX_CLAVES + 1):
    _k = os.getenv(f"GEMINI_API_KEY_{_i}", "").strip()
    if len(_k) >= 20 and not _k.upper().startswith("PEGA"):
        GEMINI_KEYS.append(_k)
        GEMINI_SLOTS.append(_i)

GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
# Modelos a probar (con la misma clave) si el principal está saturado (503) o fue retirado (404).
GEMINI_MODELOS_RESPALDO = [m.strip() for m in os.getenv("GEMINI_MODELOS_RESPALDO", "gemini-3.5-flash").split(",") if m.strip()]
# Vacío = servidor oficial de Google. Solo se cambia para pruebas con un servidor simulado.
GEMINI_BASE = os.getenv("GEMINI_BASE", "")
GEMINI_TIMEOUT = int(os.getenv("GEMINI_TIMEOUT", "25"))
# Tiempo máximo (s) que se insiste con Gemini en cada llamada antes de pasar al modelo local.
GEMINI_PRESUPUESTO = int(os.getenv("GEMINI_PRESUPUESTO", "60"))
# Si todas las claves fallan, seguir con el modelo local en lugar de dejar al cliente sin respuesta.
GEMINI_FALLBACK_OLLAMA = os.getenv("GEMINI_FALLBACK_OLLAMA", "1") == "1"

PROVEEDOR = "gemini" if (_prov == "gemini" or (_prov == "auto" and GEMINI_KEYS)) else "ollama"

# ----------------------------------------------------------------- Ollama (local)
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
LLM_MODEL = os.getenv("LLM_MODEL", "qwen2.5:1.5b")
EMBED_MODEL = os.getenv("EMBED_MODEL", "embeddinggemma")  # siempre local
UMBRAL_SIMILITUD = float(os.getenv("UMBRAL_SIMILITUD", "0.5"))

# "guiado": el LLM interpreta y el código orquesta (modelos pequeños, p. ej. 1.5B).
# "herramientas": el LLM elige las herramientas con tool calling (Gemini o modelos de 7B o más).
MODO = os.getenv("AGENTE_MODO", "herramientas" if PROVEEDOR == "gemini" else "guiado")

NOMBRE_EMPRESA = os.getenv("NOMBRE_EMPRESA", "DomiMocoa")
# Datos para pagar por transferencia (se muestran en el chat y en el recibo PDF). Cámbialos en .env: son de prueba.
PAGO_TRANSFERENCIA = os.getenv("PAGO_TRANSFERENCIA",
                               "Nequi o Daviplata al 300 000 0000, a nombre de DomiMocoa (datos de prueba).")
WHISPER_MODELO = os.getenv("WHISPER_MODELO", "base")
VOZ_PIPER = os.path.join(RAIZ, "voz_modelos", "es_MX-ald-medium.onnx")
AUDIO_DIR = os.path.join(RAIZ, "audio_tmp")

TZ = timezone(timedelta(hours=-5))  # Colombia (sin horario de verano)


def ahora() -> datetime:
    return datetime.now(TZ)


def descripcion_modelo() -> str:
    if PROVEEDOR == "gemini":
        return f"Gemini · {GEMINI_MODEL}"
    return f"Ollama · {LLM_MODEL} · {MODO}"
