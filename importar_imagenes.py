r"""Asigna fotos a los productos desde una carpeta de imágenes.

Uso:
    .venv\Scripts\python importar_imagenes.py fotos --simular    # muestra qué asignaría, sin escribir
    .venv\Scripts\python importar_imagenes.py fotos              # copia las fotos y actualiza la base de datos

Cómo se reconoce cada foto (PNG, JPG o WEBP, máximo 3 MB):
  * `12.jpg`                      -> el producto con id 12 (el id se ve en la pestaña Menú o en la tabla productos).
  * `Hamburguesa doble.jpg`       -> todos los productos que se llamen exactamente así (sin importar tildes ni mayúsculas).
  * `La Brasa - Sopa del día.jpg` -> solo ese producto de ese restaurante.
También puedes subir una por una desde la pestaña Menú de la web.
"""
import argparse
import os
import re
import shutil
import sys
import unicodedata

from app import config
from app.db import q

EXT = {".png", ".jpg", ".jpeg", ".webp"}
DESTINO = os.path.join(config.RAIZ, "app", "static", "productos")


def norm(s):
    s = unicodedata.normalize("NFD", str(s or "").lower())
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]+", " ", "".join(c for c in s if unicodedata.category(c) != "Mn"))).strip()


def buscar(stem):
    if stem.isdigit():
        return q("SELECT id, nombre FROM productos WHERE id=%s", (int(stem),))
    if " - " in stem:
        rest, prod = stem.split(" - ", 1)
        filas = q("SELECT p.id, p.nombre, r.nombre AS r FROM productos p JOIN restaurantes r ON r.id=p.restaurante_id")
        return [f for f in filas if norm(f["nombre"]) == norm(prod) and norm(f["r"]) == norm(rest)]
    return [f for f in q("SELECT id, nombre FROM productos") if norm(f["nombre"]) == norm(stem)]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("carpeta")
    ap.add_argument("--simular", action="store_true")
    a = ap.parse_args()
    if not os.path.isdir(a.carpeta):
        sys.exit(f"No existe la carpeta {a.carpeta}")
    os.makedirs(DESTINO, exist_ok=True)
    ok = sin = 0
    for nombre in sorted(os.listdir(a.carpeta)):
        stem, ext = os.path.splitext(nombre)
        if ext.lower() not in EXT:
            continue
        prods = buscar(stem)
        if not prods:
            print(f"  SIN PRODUCTO  {nombre}")
            sin += 1
            continue
        if os.path.getsize(os.path.join(a.carpeta, nombre)) > 3 * 1024 * 1024:
            print(f"  MUY PESADA    {nombre} (máximo 3 MB)")
            sin += 1
            continue
        for p in prods:
            print(f"  {'(simulado) ' if a.simular else ''}{nombre} -> #{p['id']} {p['nombre']}")
            if not a.simular:
                for viejo in os.listdir(DESTINO):
                    if os.path.splitext(viejo)[0] == str(p["id"]):
                        os.remove(os.path.join(DESTINO, viejo))
                shutil.copy(os.path.join(a.carpeta, nombre), os.path.join(DESTINO, f"{p['id']}{ext.lower()}"))
                q("UPDATE productos SET imagen=%s WHERE id=%s", (f"/productos/{p['id']}{ext.lower()}", p["id"]))
            ok += 1
    print(f"\n{ok} producto(s) con foto, {sin} archivo(s) sin asignar.")


if __name__ == "__main__":
    main()
