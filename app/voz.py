"""Voz: STT con faster-whisper y TTS con Piper (todo local).

La voz de salida se puede elegir (varias voces en español), y ajustar la velocidad, el tono y el volumen.
La elección se guarda en voz_config.json. Antes de pronunciar, el texto pasa por texto_voz.para_voz
(quita asteriscos y emojis, lee precios y horas de forma natural).
"""
import glob
import json
import os
import tempfile
import threading
import uuid
import wave

from . import config
from .texto_voz import para_voz

_RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIR_VOCES = os.path.join(_RAIZ, "voz_modelos")
RUTA_CONFIG = os.path.join(_RAIZ, "voz_config.json")
POR_DEFECTO = {"voz": "es_MX-claude-high", "velocidad": 1.0, "tono": 1.0, "volumen": 1.0}
LIMITES = {"velocidad": (0.6, 1.5), "tono": (0.8, 1.25), "volumen": (0.3, 1.5)}
REGIONES = {"es_MX": "México", "es_ES": "España", "es_AR": "Argentina", "es_CO": "Colombia", "es_CL": "Chile"}
FRASE_PRUEBA = ("Hola, soy tu despachador virtual. Tu pedido de catorce mil pesos ya va en camino. "
                "¿Necesitas algo más?")

_stt = None
_voces = {}  # nombre del modelo -> PiperVoice
_lock = threading.Lock()


# ------------------------------------------------------------------ catálogo y ajustes de voz
def listar_voces():
    """Voces instaladas. Las de varios hablantes aparecen como 'modelo#n'."""
    out = []
    for onnx in sorted(glob.glob(os.path.join(DIR_VOCES, "*.onnx"))):
        nombre = os.path.basename(onnx)[:-5]
        try:
            with open(onnx + ".json", encoding="utf-8") as f:
                meta = json.load(f)
        except OSError:
            continue
        region, _, resto = nombre.partition("-")
        persona, _, calidad = resto.rpartition("-")
        base = f"{REGIONES.get(region, region)} · {persona.replace('_', ' ').title()}"
        mapa = meta.get("speaker_id_map") or {}
        if meta.get("num_speakers", 1) > 1 and mapa:
            for clave, n in sorted(mapa.items(), key=lambda kv: kv[1]):
                out.append({"id": f"{nombre}#{n}", "etiqueta": f"{base} (voz {clave})", "calidad": calidad})
        else:
            out.append({"id": nombre, "etiqueta": base, "calidad": calidad})
    return out


def leer_config():
    cfg = dict(POR_DEFECTO)
    try:
        with open(RUTA_CONFIG, encoding="utf-8") as f:
            cfg.update(json.load(f))
    except (OSError, ValueError):
        pass
    ids = {v["id"] for v in listar_voces()}
    if ids and cfg["voz"] not in ids:  # la voz guardada ya no existe: se usa la primera disponible
        cfg["voz"] = sorted(ids)[0]
    return cfg


def guardar_config(cambios):
    cfg = leer_config()
    ids = {v["id"] for v in listar_voces()}
    if cambios.get("voz") in ids:
        cfg["voz"] = cambios["voz"]
    for k, (lo, hi) in LIMITES.items():
        if cambios.get(k) is not None:
            cfg[k] = round(min(hi, max(lo, float(cambios[k]))), 2)
    with open(RUTA_CONFIG, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=1)
    return cfg


# ------------------------------------------------------------------ STT
def _modelo_stt():
    global _stt
    with _lock:
        if _stt is None:
            from faster_whisper import WhisperModel
            _stt = WhisperModel(config.WHISPER_MODELO, device="cpu", compute_type="int8")
    return _stt


def transcribir(datos: bytes, sufijo=".webm") -> str:
    # delete=False + cerrar antes de leer: en Windows no se puede reabrir un temporal abierto.
    fd, ruta = tempfile.mkstemp(suffix=sufijo)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(datos)
        segmentos, _ = _modelo_stt().transcribe(ruta, language="es", vad_filter=True)
        return " ".join(s.text.strip() for s in segmentos).strip()
    finally:
        os.remove(ruta)


# ------------------------------------------------------------------ TTS
def _cargar(nombre):
    with _lock:
        if nombre not in _voces:
            from piper import PiperVoice
            _voces[nombre] = PiperVoice.load(os.path.join(DIR_VOCES, nombre + ".onnx"))
        return _voces[nombre]


def sintetizar(texto: str, ajustes=None) -> str:
    """Genera un WAV con la voz elegida y devuelve el nombre del archivo dentro de AUDIO_DIR.

    El tono se logra cambiando la frecuencia de reproducción y compensando la duración con la velocidad
    de habla, de modo que la voz suena más grave o más aguda sin ir más rápido ni más lento."""
    from piper import SynthesisConfig
    a = {**leer_config(), **{k: v for k, v in (ajustes or {}).items() if v is not None}}
    nombre, _, hablante = a["voz"].partition("#")
    voz = _cargar(nombre)
    tono = min(LIMITES["tono"][1], max(LIMITES["tono"][0], float(a["tono"])))
    cfg = SynthesisConfig(speaker_id=int(hablante) if hablante else None,
                          length_scale=tono / min(LIMITES["velocidad"][1], max(LIMITES["velocidad"][0], float(a["velocidad"]))),
                          volume=min(LIMITES["volumen"][1], max(LIMITES["volumen"][0], float(a["volumen"]))))
    os.makedirs(config.AUDIO_DIR, exist_ok=True)
    archivo = f"{uuid.uuid4().hex}.wav"
    with wave.open(os.path.join(config.AUDIO_DIR, archivo), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(int(voz.config.sample_rate * tono))
        voz.synthesize_wav(para_voz(texto), w, cfg, set_wav_format=False)
    return archivo


def precalentar():
    try:
        _modelo_stt()
        _cargar(leer_config()["voz"].partition("#")[0])
    except Exception:  # noqa: BLE001 - se reintentará al primer uso y se registrará el error
        pass
