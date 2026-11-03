"""Importa los restaurantes y productos del Word «Menus_Restaurantes_Mocoa.docx» a PostgreSQL.

Uso:
    .venv\\Scripts\\python importar_menu.py --simular            # solo muestra qué cargaría (no escribe nada)
    .venv\\Scripts\\python importar_menu.py                      # carga / actualiza la base de datos
    .venv\\Scripts\\python importar_menu.py otro_menu.docx       # otro archivo

Es repetible: si actualizas el Word y lo vuelves a ejecutar, actualiza precios y descripciones y agrega lo nuevo.
Los productos que ya no aparecen en el Word se marcan como no disponibles (no se borran).
Después de importar, regenera el conocimiento semántico:  .venv\\Scripts\\python cargar_conocimiento.py
"""
import argparse
import html
import re
import sys
import unicodedata
import zipfile
from collections import Counter

# ----------------------------------------------------------------------------- ajustes editables
RUTA_DEFECTO = "Menus_Restaurantes_Mocoa.docx"
# El Word solo trae el horario de Carbón Paisa. Para el resto se usa un horario AMPLIO por defecto
# (solo para que el agente pueda tomar pedidos); cámbialos aquí con los horarios reales y vuelve a ejecutar.
HORARIO_DEFECTO = ("07:00", "23:00")
HORARIOS = {
    "Carbón Paisa": ("18:30", "01:30"),   # «todos los días de 6:30 pm a 1:30 am» (cierra después de medianoche)
}
NOMBRES = {"Menú Pizzas": "Pizzas (nombre por confirmar)"}  # el nombre real no aparece en el menú original
ZONA_ID = 1  # Centro

# ----------------------------------------------------------------------------- lectura del Word
def norm(s):
    s = unicodedata.normalize("NFD", str(s or "").lower())
    return re.sub(r"[^a-z0-9$+/ ]+", " ", "".join(c for c in s if unicodedata.category(c) != "Mn")).strip()


def _texto(frag):
    frag = re.sub(r"<w:br[^>]*/>", " / ", frag)
    return html.unescape("".join(re.findall(r"<w:t[^>]*>([^<]*)</w:t>", frag))).strip()


def leer_bloques(ruta):
    """Devuelve [('h1'|'h2'|'p', texto) | ('tabla', [[celdas]])] en orden de aparición."""
    xml = zipfile.ZipFile(ruta).read("word/document.xml").decode("utf8")
    cuerpo = re.search(r"<w:body>(.*)</w:body>", xml, re.S).group(1)
    out = []
    for m in re.finditer(r"<w:tbl>.*?</w:tbl>|<w:p[ >].*?</w:p>", cuerpo, re.S):
        b = m.group(0)
        if b.startswith("<w:tbl>"):
            filas = [[_texto(c) for c in re.findall(r"<w:tc>.*?</w:tc>", f, re.S)]
                     for f in re.findall(r"<w:tr[ >].*?</w:tr>", b, re.S)]
            out.append(("tabla", filas))
            continue
        t = _texto(b)
        if not t:
            continue
        est = re.search(r'<w:pStyle w:val="([^"]+)"', b)
        est = est.group(1) if est else ""
        out.append(("h1" if est == "Heading1" else "h2" if est == "Heading2" else "titulo" if est == "Title" else "p", t))
    return out


def precios(celda):
    """'$10.500 / $11.500' -> [10500, 11500]; '—' o texto sin precio -> []."""
    return [int(x.replace(".", "")) for x in re.findall(r"\$\s*([\d.]+\d)", celda or "")]


# ----------------------------------------------------------------------------- estructura por restaurante
def estructurar(bloques):
    indice, rests, rest, sec = {}, [], None, None
    for tipo, dato in bloques:
        if tipo == "h1":
            m = re.match(r"(\d+)\.\s*(.+)", dato)
            rest = {"numero": int(m.group(1)) if m else len(rests) + 1, "nombre": (m.group(2) if m else dato).strip(),
                    "intro": [], "secciones": []}
            rests.append(rest)
            sec = None
        elif tipo == "h2" and rest:
            sec = {"titulo": dato, "notas": [], "tablas": []}
            rest["secciones"].append(sec)
        elif tipo == "p" and rest:
            (sec["notas"] if sec else rest["intro"]).append(dato)
        elif tipo == "tabla":
            if rest is None:  # la primera tabla (antes del primer restaurante) es el índice
                for fila in dato[1:]:
                    if len(fila) >= 3 and fila[0].isdigit():
                        indice[int(fila[0])] = {"nombre": fila[1], "tipo": fila[2]}
            elif sec:
                sec["tablas"].append(dato)
    return indice, rests


# ----------------------------------------------------------------------------- clasificación de productos
ALCOHOL = ("cerveza", "michelada", "poker", "aguila", "club colombia", "corona", "bud weiser", "redd")
SOPA = ("sopa", "consome", "crema de", "cazuela", "caldo")
POSTRE = ("helado", "brownie", "parfait", "torta", "pay de", "bowl", "ensalada de frutas", "nevado")
BEBIDA_SEC = ("granizad", "soda", "michelada", "bebida", "jugo", "frappe", "limonada", "combinacion", "premium",
              "precio por tamano", "smoothies")
FRIA_SEC = ("granizad", "soda", "michelada", "smoothie", "parfait", "bowl", "premium", "precio por tamano", "jugo",
            "frappe", "limonada", "combinacion", "bebidas")


def _ya(texto, *palabras):
    """¿Aparece alguna de las palabras (completas) en el texto normalizado?"""
    return any(re.search(r"\b" + re.escape(p) + r"\b", texto) for p in palabras)


CARNES = ("jamon", "salchicha", "pepperoni", "coppa", "tocineta", "carne", "pollo", "chorizo", "res", "cerdo", "stagionata")


def clasificar(rest, sec_titulo, nombre, desc, precio):
    """-> (categoria, temperatura, etiquetas) a partir de la sección, el nombre y la descripción.

    Los atributos del producto (liviano, desayuno, etc.) se deducen solo del NOMBRE y la SECCIÓN, no de la
    descripción, para no atribuir a un plato lo que solo es un acompañante (p. ej. «ensalada»)."""
    r, s, n, d = norm(rest), norm(sec_titulo), norm(nombre), norm(desc)
    ns = f"{s} {n}"
    todo = f"{ns} {d}"
    es_alcohol = any(k in ns for k in ALCOHOL) or "michelada" in s

    # ------------------------------------------------------------- categoría
    if re.match(r"(adicional|adicion|topping)", s) or n.startswith(("adicion", "topping")):
        cat = "adicional"
    elif "oro morado" in r:   # tamaños, parfaits y bowls son postres; los smoothies y el agua, bebidas
        cat = "bebida" if ("smoothie" in s and "brownie" not in n) else "postre"
    elif es_alcohol:
        cat = "bebida"
    elif any(k in s for k in ("entradas", "acompanantes", "otros productos", "gomas", "conos")) and "picada" not in n:
        cat = "snack"
    elif any(k in n for k in SOPA):
        cat = "sopa"
    elif any(k in n for k in ("brownie", "parfait", "torta", "pay de", "ensalada de frutas", "helado suave", "bowl")):
        cat = "postre"
    elif any(k in s for k in BEBIDA_SEC) or "bebidas calientes" in s or \
            any(k in n for k in ("botella de agua", "gaseosa", "coca cola", "soda")):
        cat = "bebida"
    else:
        cat = "plato fuerte"

    # ------------------------------------------------------------- temperatura
    if "bebidas calientes" in s or "caliente" in n or any(k in n for k in ("capuccino", "americano", "aromaticas")):
        temp = "caliente"
    elif cat == "sopa":
        temp = "caliente"
    elif cat in ("bebida", "postre") and (any(k in s for k in FRIA_SEC) or es_alcohol or "oro morado" in r
                                           or any(k in n for k in ("gaseosa", "coca cola", "soda", "helado", "agua", "nevado"))):
        temp = "fria"
    elif cat == "plato fuerte" and "ensalada" in n:
        temp = "fria"
    elif cat in ("plato fuerte", "snack") and "gomas" not in n:
        temp = "caliente"
    else:
        temp = None

    # ------------------------------------------------------------- etiquetas
    e = set()
    if es_alcohol:
        e.add("alcohol")
    if temp == "fria" and cat in ("bebida", "postre") and not es_alcohol:
        e.add("refrescante")
    if _ya(ns, "detox", "vital", "antioxidante", "aloe", "proteina", "defensas", "vegetariano", "fit", "ensalada") \
            and cat != "snack":
        e |= {"liviano", "saludable"}
    if cat == "bebida" and not es_alcohol and _ya(ns, "jugo", "jugos", "naranja", "mandarina", "lulada", "maracuyada",
                                                  "limonada", "agua"):
        e.add("liviano")
    if cat == "postre" or any(k in ns for k in ("gomas", "nevado", "milo", "chocolate", "brownie", "torta", "granizad",
                                                 "frutales", "parfait", "helado", "cococett", "nucita", "oreo", "pay de",
                                                 "choco", "frappe", "limonada de coco")):
        e.add("dulce")
    if cat == "plato fuerte" and (precio >= 25000 or any(k in ns for k in ("parrillada", "churrasco", "costilla", "picada",
                                  "entero", "doble", "triple", "especial", "lengua", "padrino", "corleone", "cara cortada"))):
        e.add("contundente")
        e -= {"liviano", "saludable"}
    if (cat in ("bebida", "snack", "adicional") and precio <= 8000) or (cat in ("plato fuerte", "sopa") and precio <= 15000) \
            or (cat == "postre" and precio <= 10000):
        e.add("economico")
    if temp == "caliente" and cat in ("sopa", "bebida"):
        e.add("para_el_frio")
    if any(k in todo for k in ("picante", "jalapeno", "tajin", "chamoy", "bufalo", "enchilad")):
        e.add("picante")
    es_pizza = "pizza" in n
    if _ya(ns, "vegetariano", "quesuda", "parfait", "bowl") or "ensalada de frutas" in ns or \
            (es_pizza and cat == "plato fuerte" and d and not any(re.search(r"\b" + c + r"\b", d) for c in CARNES)):
        if cat != "bebida":
            e.add("vegetariano")
    if any(k in n for k in ("capuccino", "americano", "croissant", "empanada", "pastel", "galleta", "huevos",
                            "torta de banano", "milo caliente", "chocolate caliente", "caldo")):
        e.add("desayuno")
    if any(k in ns for k in ("parrillada", "picada", "pollo asado entero", "pollo entero", "jarra", "mediana", "cara cortada",
                             "salchipapa especial", "salchipapa mix")) or n.startswith("pollo broaster"):
        e.add("para_compartir")
    return cat, temp, sorted(e)


# ----------------------------------------------------------------------------- extracción de productos
# Nombres poco claros («Mango», «Sencilla»...) se completan con la sección: {restaurante, sección sin paréntesis}
PREFIJOS = {
    ("Sorbitos Fresh", "Granizados"): "Granizado de {}",
    ("Sorbitos Fresh", "Granizado Frutal"): "Granizado frutal de {}",
    ("Sorbitos Fresh", "Sodas Frutales"): "{} (soda frutal)",
    ("Sorbitos Fresh", "Micheladas"): "Michelada {}",
    ("Sorbitos Fresh", "Gomas Enchiladas"): "Gomas enchiladas: {}",
    ("Más Que Arepa", "Arepas y patacón"): "Arepa {}",
    ("Jugoos Mocoa", "Jugos exprimidos"): "Jugo exprimido de {}",
    ("Marro's Conos Rellenos", "Conos"): "Cono {}",
    ("Marro's Conos Rellenos", "Perros calientes"): "Perro caliente {}",
}
SIN_PREFIJO = ("soda sencilla", "patacon")


def productos_de_restaurante(rest, avisos):
    prods, ejemplos_frappes, frutas = [], [], ""
    for sec in rest["secciones"]:
        titulo, notas = sec["titulo"], " ".join(sec["notas"])
        for n_ in sec["notas"]:
            if n_.lower().startswith("frutas disponibles"):
                frutas = n_
        # adiciones escritas en la nota de la sección: «carne gratinada (+$3.000) · carne hawaiana (+$5.000)»
        for m in re.finditer(r"([A-Za-zÁÉÍÓÚáéíóúñ][A-Za-zÁÉÍÓÚáéíóúñ ]+?)\s*\(\+\$\s*([\d.]+)\)", notas):
            prods.append({"nombre": f"Adición: {m.group(1).strip()}", "descripcion": f"Se agrega a {titulo.lower()}",
                          "precio": int(m.group(2).replace(".", "")), "seccion": "Adicionales"})
        for tabla in sec["tablas"]:
            if not tabla or len(tabla) < 2:
                continue
            enc = [norm(c) for c in tabla[0]]
            filas = tabla[1:]
            # a) pares nombre/precio:  Topping|Precio|Topping|Precio   y   Adición|Precio|Adición|Precio
            if len(enc) == 4 and enc[1] == "precio" and enc[3] == "precio":
                for f in filas:
                    for a, b in ((0, 1), (2, 3)):
                        if len(f) > b and f[a] and precios(f[b]):
                            prods.append({"nombre": f"{tabla[0][0]}: {f[a]}", "descripcion": "", "precio": precios(f[b])[0],
                                          "seccion": "Adicionales"})
                continue
            # b) ejemplos de combinaciones (sin precio propio)
            if enc[:2] == ["base", "combinaciones"]:
                continue
            # c) columnas de descripción y de precios
            d_col = next((i for i, h in enumerate(enc) if h in ("descripcion", "ingredientes")), None)
            p_cols = [i for i in range(1, len(enc)) if i != d_col]
            es_base = enc[0] == "base"
            for f in filas:
                f = f + [""] * (len(enc) - len(f))
                nombre = f[0].strip()
                if not nombre:
                    continue
                desc = f[d_col].strip() if d_col is not None else ""
                for i in p_cols:
                    etiqueta = tabla[0][i].strip()
                    celda = f[i]
                    ps = precios(celda)
                    if not ps:
                        if "segun base" in norm(celda):
                            if (nombre, desc) not in ejemplos_frappes:
                                ejemplos_frappes.append((nombre, desc))
                        elif celda.strip() and celda.strip() not in ("—", "-"):
                            avisos.append(f"{rest['nombre']}: «{nombre}» sin precio ({celda.strip()[:50]}) -> omitido")
                        continue
                    for k, precio in enumerate(ps):
                        # «$10.500 / $11.500» en una columna 'Precio' = 16 oz / 22 oz
                        suf = etiqueta if norm(etiqueta) not in ("precio", "unidad") else ""
                        if len(ps) == 2 and norm(etiqueta) == "precio":
                            suf = ("16 oz", "22 oz")[k]
                        nom = nombre
                        fmt = PREFIJOS.get((rest["nombre"], re.sub(r"\s*\(.*?\)", "", titulo).strip()))
                        if fmt and not norm(nombre).startswith(SIN_PREFIJO):
                            nom = fmt.format(nombre)
                            tam = re.search(r"\((\d+ onz)\)", titulo)   # «Granizados (12 onz)» -> tamaño de cada producto
                            if tam and norm(etiqueta) == "precio":
                                suf = tam.group(1)
                        if es_base:
                            nom = "Combinación de frutas con base de " + {"helado + leche": "helado y leche"}.get(nombre.lower(), nombre.lower())
                            desc = "Elige 3 frutas y el endulzante (sin azúcar, con azúcar o stevia). " + frutas
                        if enc[0] in ("pizza",):
                            nom = f"Pizza {nombre}"
                        if (rest["nombre"], titulo) == ("Oro Morado", "Tamaños"):
                            nom = f"Base de açaí, araza o combinado ({nombre})"
                            desc = "Elige base: asaí, araza o combinado. Se le agregan toppings aparte."
                            suf = ""
                        if suf:
                            nom += f" ({suf.lower()})"
                        prods.append({"nombre": nom, "descripcion": desc, "precio": precio, "seccion": titulo})
    # los frappes «según base» quedan como ejemplos dentro de las combinaciones
    if ejemplos_frappes:
        ej = "Recomendados: " + "; ".join(f"{n} ({d.lower()})" for n, d in ejemplos_frappes) + "."
        for p in prods:
            if p["nombre"].startswith("Combinación de frutas"):
                p["descripcion"] += " " + ej
    return prods


def meta_restaurante(rest, tipo):
    intro = rest["intro"]
    texto = " · ".join(intro)
    tels = []
    for t in re.findall(r"\b3\d{2} \d{3} \d{4}\b", texto):
        if t not in tels:
            tels.append(t)
    dire = next((l for l in intro if re.search(r"carrera|avenida|av\.|vía|mocoa:", l, re.I)), None)
    return {"tipo": tipo, "telefono": ", ".join(tels) or None, "direccion": dire, "notas": texto or None}


# ----------------------------------------------------------------------------- programa principal
def construir(ruta):
    indice, rests = estructurar(leer_bloques(ruta))
    avisos, salida = [], []
    for r in rests:
        r["nombre"] = NOMBRES.get(r["nombre"], r["nombre"])
        meta = meta_restaurante(r, indice.get(r["numero"], {}).get("tipo"))
        prods = productos_de_restaurante(r, avisos)
        vistos = Counter()
        for p in prods:
            p["cat"], p["temp"], p["etq"] = clasificar(r["nombre"], p["seccion"], p["nombre"], p["descripcion"], p["precio"])
            vistos[p["nombre"]] += 1
        for p in prods:   # nombres repetidos dentro del mismo restaurante: se distinguen con la sección
            if vistos[p["nombre"]] > 1:
                avisos.append(f"{r['nombre']}: nombre repetido «{p['nombre']}» -> se agrega la sección «{p['seccion']}»")
                p["nombre"] += f" ({p['seccion'].lower()})"
        if "primera página" in meta["notas"].lower() if meta["notas"] else False:
            avisos.append(f"{r['nombre']}: el menú está incompleto en el Word (solo la primera página)")
        if "letra muy pequeña" in " ".join(s for sec in r["secciones"] for s in sec["notas"]).lower() or \
                "verificar" in (meta["notas"] or "").lower():
            avisos.append(f"{r['nombre']}: el Word indica verificar precios con el menú original")
        if "nombre no" in (indice.get(r["numero"], {}).get("nombre", "").lower()):
            avisos.append(f"{r['nombre']}: el nombre real no aparece en el menú original; póngalo en la base de datos")
        horario = HORARIOS.get(r["nombre"])
        salida.append({**r, **meta, "productos": prods, "horario": horario or HORARIO_DEFECTO, "horario_real": bool(horario)})
    return salida, avisos


def resumen(rests, avisos):
    total = 0
    print(f"{'#':>2}  {'Restaurante':34} {'Prod.':>5}  Categorías")
    for r in rests:
        c = Counter(p["cat"] for p in r["productos"])
        total += len(r["productos"])
        print(f"{r['numero']:>2}  {r['nombre'][:34]:34} {len(r['productos']):>5}  " + ", ".join(f"{k} {v}" for k, v in c.most_common()))
    print(f"\nTotal: {len(rests)} restaurantes, {total} productos")
    sin_horario = [r["nombre"] for r in rests if not r["horario_real"]]
    print(f"Horario por defecto {HORARIO_DEFECTO[0]}–{HORARIO_DEFECTO[1]} (no viene en el Word) en {len(sin_horario)} restaurantes.")
    if avisos:
        print(f"\nAvisos ({len(avisos)}):")
        for a in avisos:
            print("  -", a)


def guardar(rests):
    from app.db import tx
    nuevos = actualizados = 0
    with tx() as cur:
        for r in rests:
            ap, ci = r["horario"]
            if r["horario_real"]:   # horario explícito en HORARIOS: se aplica siempre
                horas = "hora_apertura=EXCLUDED.hora_apertura, hora_cierre=EXCLUDED.hora_cierre, "
            else:                   # si no, se respeta el horario que ya tenga la base (por si lo editaste)
                horas = ""
            cur.execute(
                "INSERT INTO restaurantes (nombre, hora_apertura, hora_cierre, zona_id, tipo_comida, telefono, direccion, "
                "notas, fuente, activo) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'menu_mocoa',true) "
                "ON CONFLICT (nombre) DO UPDATE SET " + horas + "tipo_comida=EXCLUDED.tipo_comida, telefono=EXCLUDED.telefono, "
                "direccion=EXCLUDED.direccion, notas=EXCLUDED.notas, fuente='menu_mocoa', activo=true RETURNING id",
                (r["nombre"], ap, ci, ZONA_ID, r["tipo"], r["telefono"], r["direccion"], r["notas"]))
            rid = cur.fetchone()["id"]
            for p in r["productos"]:
                cur.execute(
                    "INSERT INTO productos (restaurante_id, nombre, descripcion, precio, categoria, temperatura, etiquetas, disponible) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,true) ON CONFLICT (restaurante_id, nombre) DO UPDATE SET "
                    "descripcion=EXCLUDED.descripcion, precio=EXCLUDED.precio, categoria=EXCLUDED.categoria, "
                    "temperatura=EXCLUDED.temperatura, etiquetas=EXCLUDED.etiquetas, disponible=true RETURNING (xmax = 0) AS nuevo",
                    (rid, p["nombre"], p["descripcion"] or None, p["precio"], p["cat"], p["temp"], p["etq"]))
                if cur.fetchone()["nuevo"]:
                    nuevos += 1
                else:
                    actualizados += 1
            # lo que ya no está en el Word se marca como no disponible
            cur.execute("UPDATE productos SET disponible=false WHERE restaurante_id=%s AND NOT (nombre = ANY(%s))",
                        (rid, [p["nombre"] for p in r["productos"]]))
    return nuevos, actualizados


if __name__ == "__main__":
    ap_ = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap_.add_argument("archivo", nargs="?", default=RUTA_DEFECTO)
    ap_.add_argument("--simular", action="store_true", help="solo muestra el resumen; no escribe en la base de datos")
    a = ap_.parse_args()
    rests, avisos = construir(a.archivo)
    resumen(rests, avisos)
    if a.simular:
        print("\n(simulación: no se escribió nada)")
        sys.exit(0)
    nuevos, actualizados = guardar(rests)
    print(f"\nListo: {nuevos} productos nuevos, {actualizados} actualizados.")
    print("Siguiente paso: .venv\\Scripts\\python cargar_conocimiento.py")
