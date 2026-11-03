"""Comprueba cada clave de Gemini y el modelo configurado (API Interactions), sin mostrar nunca las claves.

Uso:  .venv\\Scripts\\python probar_gemini.py
"""
from google import genai

from app import config, gemini


def main():
    modelos = [config.GEMINI_MODEL] + [m for m in config.GEMINI_MODELOS_RESPALDO if m != config.GEMINI_MODEL]
    print(f"Modelo principal (GEMINI_MODEL): {config.GEMINI_MODEL} | respaldo: {config.GEMINI_MODELOS_RESPALDO}")
    if not config.GEMINI_KEYS:
        print("No hay claves. Pégalas en el archivo .env (GEMINI_API_KEY_1, _2, _3) y vuelve a ejecutar.")
        return
    buenas = []
    for i in range(1, len(config.GEMINI_KEYS) + 1):
        estado = []
        for m in modelos:
            try:
                r = gemini.cliente(i).interactions.create(model=m, input="Responde solo: ok", store=False, timeout=60)
                estado.append(f"{m}: OK")
                if i not in buenas:
                    buenas.append(i)
            except Exception as e:  # noqa: BLE001
                estado.append(f"{m}: ERROR HTTP {getattr(e, 'status_code', '?')} {gemini._limpiar(e)[60:140]}")
        print(f"  clave #{config.GEMINI_SLOTS[i - 1]}:\n     " + "\n     ".join(estado))
    print(f"\n{len(buenas)} de {len(config.GEMINI_KEYS)} claves funcionan.")
    if buenas:
        try:
            nombres = sorted(m.name.split("/")[-1] for m in gemini.cliente(buenas[0]).models.list()
                             if "flash" in m.name)
            print("Modelos Flash disponibles para tu cuenta (úsalos en GEMINI_MODEL):\n  " + "\n  ".join(nombres))
        except Exception as e:  # noqa: BLE001
            print("No pude listar los modelos:", type(e).__name__)


if __name__ == "__main__":
    main()
