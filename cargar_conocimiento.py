"""Carga y vectoriza el conocimiento del agente en pgvector (ejecutar con Ollama encendido).

Uso:  .venv\\Scripts\\python cargar_conocimiento.py
Es idempotente: borra y vuelve a cargar la tabla 'conocimiento'.
Las fichas del menú se generan solas desde la tabla 'productos' (con su temperatura y etiquetas), así que
si cambias el menú en la base de datos basta con volver a ejecutar este script.
"""
import requests

from app import config
from app.db import q, tx

DOCS = [
    # (tipo, titulo, contenido)
    ("politica", "Cancelación de pedidos",
     "Un pedido se puede cancelar mientras esté en borrador, programado, pendiente_restaurante o aceptado. "
     "Si ya fue asignado a un domiciliario, va en camino o fue entregado, no se puede cancelar."),
    ("politica", "Retrasos",
     "Si el pedido tarda más de 30 minutos, se le informa al cliente su estado actual, se le pide disculpas "
     "y el caso se escala al despachador humano para que lo revise."),
    ("politica", "Producto agotado",
     "Si el restaurante no tiene un producto, se informa al cliente y se le ofrece continuar sin ese producto "
     "o cambiarlo por otro del menú. El total se recalcula con el nuevo pedido."),
    ("politica", "Pedidos programados",
     "Se puede programar un pedido para una hora en que el restaurante esté abierto. El pedido se envía al "
     "restaurante 40 minutos antes de la hora indicada."),
    ("politica", "Privacidad de datos",
     "Solo se guardan los datos necesarios para entregar el pedido: nombre, dirección y forma de pago. "
     "No se comparten con terceros y no se piden documentos de identidad."),
    ("politica", "Bebidas alcohólicas",
     "Las bebidas alcohólicas (cerveza, micheladas) solo se venden a mayores de edad. El agente no las recomienda: "
     "se agregan únicamente si el cliente las pide y confirma que es mayor de edad."),
    ("faq", "Medios de pago",
     "Se recibe efectivo y transferencia. El pago en efectivo se hace al domiciliario al recibir el pedido."),
    ("faq", "Horarios de atención",
     "Los horarios dependen de cada restaurante y se consultan con el menú. "
     "El servicio de domicilios solo funciona mientras el restaurante esté abierto."),
    ("faq", "Cobertura y tarifas",
     "Tenemos cobertura en Centro (domicilio $3.000), Pablo VI ($4.500) y Condagua ($6.000)."),
    ("faq", "Tiempo de entrega",
     "El tiempo normal de entrega es de 30 a 40 minutos desde que el restaurante acepta el pedido."),
    ("lugar", "Esquina del colegio",
     "La 'esquina del colegio' corresponde al barrio Pablo VI."),
    ("lugar", "Frente a la droguería",
     "'Frente a la droguería' o 'la droguería del parque' corresponde al barrio Centro."),
    ("lugar", "Por el puente",
     "'Cerca del puente' o 'pasando el puente' corresponde al barrio Condagua."),
]

FRASES_ETIQUETA = {
    "para_el_frio": "Es ideal para el frío, la lluvia o cuando hace fresco.",
    "contundente": "Es contundente, para quien tiene mucha hambre.",
    "liviano": "Es liviano y ligero.",
    "refrescante": "Es refrescante, ideal para el calor o cuando hay sed.",
    "dulce": "Es dulce.",
    "economico": "Es una opción económica.",
    "sin_picante": "No es picante.",
    "picante": "Es picante.",
    "vegetariano": "Es vegetariano, sin carne.",
    "saludable": "Es una opción saludable.",
    "desayuno": "Es buena opción para el desayuno o la mañana.",
    "para_compartir": "Sirve para compartir.",
}


def fichas_de_menu():
    """Una ficha en lenguaje natural por producto, generada desde la base de datos."""
    docs = []
    for p in q("SELECT p.nombre, p.descripcion, p.categoria, p.temperatura, p.etiquetas, r.nombre AS restaurante "
               "FROM productos p JOIN restaurantes r ON r.id=p.restaurante_id "
               "WHERE r.activo AND p.disponible AND COALESCE(p.categoria,'') <> 'adicional' "
               "AND NOT ('alcohol' = ANY(p.etiquetas)) ORDER BY p.id"):
        partes = [f"{p['nombre']} de {p['restaurante']}.", (p["descripcion"] or "").rstrip(".") + "."]
        if p["categoria"]:
            partes.append(f"Es {p['categoria']}.")
        if p["temperatura"] == "caliente":
            partes.append("Se sirve caliente, reconfortante en días fríos o lluviosos.")
        elif p["temperatura"] == "fria":
            partes.append("Se sirve frío, refrescante en días calurosos.")
        partes += [FRASES_ETIQUETA[e] for e in (p["etiquetas"] or []) if e in FRASES_ETIQUETA]
        docs.append(("menu", p["nombre"], " ".join(partes)))
    return docs


def fichas_de_restaurantes():
    """Una ficha por restaurante activo (tipo de comida, teléfono y dirección) para responder «¿qué hay de...?»."""
    docs = []
    for r in q("SELECT nombre, tipo_comida, telefono, direccion FROM restaurantes WHERE activo ORDER BY id"):
        partes = [f"{r['nombre']} es un restaurante de Mocoa" + (f" con {r['tipo_comida'].lower()}." if r["tipo_comida"] else ".")]
        if r["telefono"]:
            partes.append(f"Teléfono: {r['telefono']}.")
        if r["direccion"]:
            partes.append(f"{r['direccion']}.")
        docs.append(("faq", f"Restaurante {r['nombre']}", " ".join(partes)))
    return docs


def embed(texto):
    r = requests.post(f"{config.OLLAMA_URL}/api/embed",
                      json={"model": config.EMBED_MODEL, "input": texto}, timeout=300)
    r.raise_for_status()
    return r.json()["embeddings"][0]


if __name__ == "__main__":
    todos = DOCS + fichas_de_restaurantes() + fichas_de_menu()
    q("TRUNCATE conocimiento RESTART IDENTITY")
    with tx() as cur:
        for tipo, titulo, contenido in todos:
            vec = embed(f"{titulo}. {contenido}")
            cur.execute("INSERT INTO conocimiento (tipo, titulo, contenido, embedding) "
                        "VALUES (%s,%s,%s,%s::vector)", (tipo, titulo, contenido, str(vec)))
            print("  +", tipo, "-", titulo, f"(dim {len(vec)})")
    print(f"Listo: {len(todos)} documentos vectorizados con {config.EMBED_MODEL}.")
