# Agente despachador de domicilios (UniPutumayo · agente #26)

Un cliente pide comida por **chat web (texto o voz)**. El agente consulta menús y horarios, calcula tarifas,
envía el pedido al restaurante, asigna un domiciliario y mantiene informado al cliente. La interfaz muestra
en una sola pantalla el chat, el restaurante, el domiciliario, el tablero y **cada paso del agente**.

## Ejecutar
1. Abre **Docker Desktop** y **Ollama**.
2. En PowerShell, dentro de esta carpeta: `.\iniciar.ps1` (abre `http://127.0.0.1:8000`).

## Usar Gemini (opcional)
1. Abre el archivo `.env` y pega tus claves después del `=`:
   `GEMINI_API_KEY_1=…`, `GEMINI_API_KEY_2=…`, `GEMINI_API_KEY_3=…`
2. Valídalas (no muestra las claves): `.venv\Scripts\python probar_gemini.py`.
   También lista los modelos Flash disponibles para tu cuenta; el elegido va en `GEMINI_MODEL`.
3. Reinicia con `.\iniciar.ps1`. Con al menos una clave, `LLM_PROVEEDOR=auto` usa Gemini.

**Rotación de claves.** Se usa la clave 1 hasta que falle; entonces se pasa a la 2 y luego a la 3.
Límite por minuto → esa clave descansa 65 s; cupo diario agotado → 6 h; clave inválida → 24 h; error de Google → 15 s.
Cada fallo queda en la tabla `errores` y en el panel de proceso ("clave #2: HTTP 429…") **sin imprimir la clave**.
Si todas fallan, con `GEMINI_FALLBACK_OLLAMA=1` el agente sigue con el modelo local (y lo registra).

## Cambiar de modelo para comparar
| Quiero… | En `.env` |
|---|---|
| Gemini (herramientas / function calling) | `LLM_PROVEEDOR=gemini` |
| Modelo local pequeño (modo guiado) | `LLM_PROVEEDOR=ollama` y `LLM_MODEL=qwen2.5:1.5b` |
| Modelo local con tool calling | `LLM_PROVEEDOR=ollama`, `AGENTE_MODO=herramientas`, `LLM_MODEL=qwen2.5:3b` (o mayor) |

El panel "Proceso del agente" y `/api/metricas` muestran herramientas llamadas, tiempos y errores de cada corrida.

## Cómo está organizado
- `app/gemini.py` — llamadas a Gemini por REST, function calling y rotación de claves.
- `app/agente.py` — herramientas, prompt del sistema y ciclo con Ollama (tool calling).
- `app/guiado.py` — modo para modelos pequeños: el LLM interpreta, el código orquesta.
- `app/logica.py` — reglas de negocio y SQL (totales, estados, asignación). **El modelo nunca calcula totales.**
- `app/voz.py` — STT (faster-whisper) y TTS (Piper), siempre locales.
- Base de datos: PostgreSQL + pgvector (Docker). Tablas `mensajes` (historial), `acciones_agente`
  (herramienta, entrada, salida, ms) y `errores`.

## Privacidad y ética
- **Con Gemini, el texto de los mensajes se envía a Google.** La interfaz lo avisa con un banner. Usa **solo datos de prueba**
  (nombres y direcciones ficticios). Voz (STT/TTS), embeddings, PostgreSQL y pgvector no salen del equipo.
- Con el modelo local nada sale del equipo.
- Las claves viven solo en `.env` (excluido por `.gitignore`) o en variables de entorno; no están en el código,
  ni en la interfaz, ni en los registros. No compartas ni subas `.env`.
- Solo se guardan los datos necesarios para entregar (nombre, dirección, forma de pago).
- Lo de salud se trata solo como una preferencia ("comida liviana"); el agente no da consejos médicos.
- Ante reclamos, falta de información o fallas, el agente escala a un humano.

## Interfaz (Vue 3, sin build)
`app/static/index.html` + `app/static/vendor/vue.global.prod.js` (Vue 3 guardado en local, no necesita internet para cargar).
- **Izquierda – banco de conversaciones:** todas las conversaciones con clientes (buscador, filtros «con pedido en curso» y
  «escaladas», estado del último pedido). Puedes abrir cualquiera, continuarla o eliminarla.
- **Centro – chat** con texto y voz (🎤 graba, 🔊 hace que el agente responda también con audio).
- **Derecha – paneles:** *Proceso* (cada paso del agente de la conversación elegida), *Pedido* (avance y desglose),
  *Restaurante*, *Domiciliario*, *Tablero* (pedidos, domiciliarios y métricas de evaluación) y *Voz*.
- Parámetros útiles en la URL: `?c=<id de conversación>` y `&tab=pedido|voz|tablero…`.

## Voz de salida
- 6 voces en español (Piper) en `voz_modelos/`. En la pestaña **Voz** eliges voz, velocidad, **tono** (grave/agudo, sin cambiar la
  velocidad) y volumen; la elección queda en `voz_config.json`. Más voces: `.venv\Scripts\python -m piper.download_voices <nombre> --download-dir voz_modelos`.
- Lo que se muestra en el chat no cambia, pero **lo que se pronuncia se limpia** (`app/texto_voz.py`): sin asteriscos, emojis ni
  símbolos, y los precios/horas se leen como una persona («$14.000» → «catorce mil pesos», «12:30» → «las doce y treinta»).

## Menú y recomendaciones
- `db/init/03_menu_ampliado.sql` amplía el menú (4 restaurantes, 23 productos) con categoría, temperatura (caliente/fría) y
  etiquetas (liviano, económico, para el frío, refrescante…). En una base ya creada: `docker exec -i domicilios_db psql -U domicilios < db/init/03_menu_ampliado.sql`.
- Después de cambiar el menú, regenera el conocimiento semántico: `.venv\Scripts\python cargar_conocimiento.py`.
- Gemini recomienda filtrando `buscar_menu` por temperatura/etiqueta según el clima o el antojo; el modo local puntúa por etiquetas y
  pgvector, prefiere restaurantes abiertos y no repite lo ya recomendado en la conversación.

## Importar menús desde Word (`Menus_Restaurantes_Mocoa.docx`)
1. Revisa qué cargaría, sin escribir nada: `.venv\Scripts\python importar_menu.py --simular`
2. Cárgalo: `.venv\Scripts\python importar_menu.py` (primero aplica `db/init/04_restaurantes_reales.sql` si la base ya existía:
   `docker exec -i domicilios_db psql -U domicilios < db/init/04_restaurantes_reales.sql`).
3. Regenera el conocimiento semántico: `.venv\Scripts\python cargar_conocimiento.py`
4. Reinicia: `.\reiniciar.ps1`

- Es **repetible**: si actualizas el Word y lo vuelves a ejecutar, se actualizan precios y descripciones, se agregan los productos
  nuevos y los que ya no aparecen quedan como no disponibles (no se borran).
- Lee tablas y secciones del Word: cada **tamaño o porción** (9/12 onz, 16/22 oz, personal/mediana, cuarto/medio, unidad/combo)
  es un producto distinto, y los adicionales (toppings, adiciones) quedan como categoría «adicional».
- Se omiten los productos sin precio en el Word (p. ej. «Postres de temporada») y los «según base» se resumen como ejemplos dentro
  de «Combinación de frutas».
- **Horarios:** el Word solo trae el de Carbón Paisa. Los demás usan el horario provisional 07:00–23:00. Cámbialos en `HORARIOS`
  (arriba de `importar_menu.py`) o con SQL (`UPDATE restaurantes SET hora_apertura='08:00', hora_cierre='21:00' WHERE nombre='La Brasa';`).
- Los 4 restaurantes de demostración quedaron **ocultos** (`activo=false`), no borrados. Para volver a verlos:
  `UPDATE restaurantes SET activo=true WHERE fuente='demo';`
- Las bebidas alcohólicas existen en la base pero el agente no las ofrece ni recomienda; solo las muestra si el cliente las pide.
