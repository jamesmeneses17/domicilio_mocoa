"""Prepara el texto de una respuesta para que la voz (TTS) lo lea de forma natural.

* Quita el formato Markdown (asteriscos, almohadillas, viñetas) y los emojis.
* Lee los precios como una persona: "$14.000" -> "catorce mil pesos".
* Lee horas y fechas: "12:30" -> "las doce y treinta", "2026-11-03" -> "3 de noviembre".
El texto que se muestra en el chat NO cambia: esto solo se aplica a lo que se pronuncia.
"""
import re

_UNIDADES = ["cero", "uno", "dos", "tres", "cuatro", "cinco", "seis", "siete", "ocho", "nueve", "diez", "once",
             "doce", "trece", "catorce", "quince", "dieciséis", "diecisiete", "dieciocho", "diecinueve", "veinte",
             "veintiuno", "veintidós", "veintitrés", "veinticuatro", "veinticinco", "veintiséis", "veintisiete",
             "veintiocho", "veintinueve"]
_DECENAS = {3: "treinta", 4: "cuarenta", 5: "cincuenta", 6: "sesenta", 7: "setenta", 8: "ochenta", 9: "noventa"}
_CENTENAS = {1: "ciento", 2: "doscientos", 3: "trescientos", 4: "cuatrocientos", 5: "quinientos",
             6: "seiscientos", 7: "setecientos", 8: "ochocientos", 9: "novecientos"}
_MESES = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre", "octubre",
          "noviembre", "diciembre"]


def numero_a_palabras(n: int, apocope: bool = False) -> str:
    """Número entero (0 a 999.999.999) en palabras. apocope=True: 'un' en lugar de 'uno' (un mil, un millón)."""
    if n < 0:
        return "menos " + numero_a_palabras(-n, apocope)
    if n < 30:
        p = _UNIDADES[n]
    elif n < 100:
        d, u = divmod(n, 10)
        p = _DECENAS[d] + (f" y {_UNIDADES[u]}" if u else "")
    elif n < 1000:
        c, r = divmod(n, 100)
        p = "cien" if (c == 1 and r == 0) else _CENTENAS[c] + (f" {numero_a_palabras(r)}" if r else "")
    elif n < 1_000_000:
        m, r = divmod(n, 1000)
        p = ("mil" if m == 1 else numero_a_palabras(m, True) + " mil") + (f" {numero_a_palabras(r)}" if r else "")
    elif n < 1_000_000_000:
        m, r = divmod(n, 1_000_000)
        p = ("un millón" if m == 1 else numero_a_palabras(m, True) + " millones") + \
            (f" {numero_a_palabras(r)}" if r else "")
    else:
        return str(n)
    if apocope:
        p = re.sub(r"veintiuno$", "veintiún", p)
        p = re.sub(r"uno$", "un", p)
    return p


def _pesos(m):
    n = int(re.sub(r"[.,]", "", m.group(1)))
    return "un peso" if n == 1 else numero_a_palabras(n, True if n % 1000 == 1 and n > 1000 else False) + " pesos"


def _hora(m):
    h, mi = int(m.group(1)), int(m.group(2))
    if h > 23 or mi > 59:
        return m.group(0)
    texto = ("la una" if h == 1 else "las " + numero_a_palabras(h)) + (f" y {numero_a_palabras(mi)}" if mi else "")
    return texto


def _fecha(m):
    mes = int(m.group(2))
    if not 1 <= mes <= 12:
        return m.group(0)
    return f"{int(m.group(3))} de {_MESES[mes - 1]}"


_EMOJI = re.compile("[\U0001F000-\U0001FAFF☀-➿⬀-⯿️‍⃣]")


def para_voz(texto: str) -> str:
    t = str(texto or "")
    t = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", t)            # [texto](enlace) -> texto
    t = re.sub(r"```.*?```", " ", t, flags=re.S)                # bloques de código
    t = _EMOJI.sub("", t)
    t = re.sub(r"(\d{4})-(\d{2})-(\d{2})", _fecha, t)           # fechas ISO
    t = re.sub(r"(?<!\d)(\d{1,2}):(\d{2})(?!\d)", _hora, t)      # horas 12:30
    t = re.sub(r"\$\s?(\d{1,3}(?:[.,]\d{3})+|\d+)", _pesos, t)   # precios $14.000
    t = re.sub(r"(?<![\d.])(\d{1,3}(?:\.\d{3})+)(?![\d.])",       # 3.500 -> tres mil quinientos
               lambda m: numero_a_palabras(int(m.group(1).replace(".", ""))), t)
    t = re.sub(r"(\d)\s?%", r"\1 por ciento", t)
    t = re.sub(r"#\s?(\d+)", r"número \1", t)                   # #12 -> número 12
    t = t.replace("c/u", "cada uno").replace("×", " ").replace("→", ", ").replace("·", ", ")
    t = t.replace("—", ", ").replace("–", ", ")
    # Markdown: negritas, cursivas, almohadillas, citas y viñetas
    t = re.sub(r"(\*\*|__|\*|`)", "", t)
    t = re.sub(r"(?m)^\s*#{1,6}\s*", "", t)
    t = re.sub(r"(?m)^\s*>\s*", "", t)
    t = re.sub(r"(?m)^\s*[-•▪◦]\s+", "", t)
    t = re.sub(r"(?m)^\s*\d+[.)]\s+", "", t)
    # Saltos de línea -> pausas naturales
    t = re.sub(r"\s*\n\s*", ". ", t.strip())
    t = re.sub(r"([.!?¿¡:;,])\s*\.\s*", r"\1 ", t)               # ".." ", ." -> uno solo
    t = re.sub(r"([!?])\.", r"\1", t)
    t = re.sub(r"\s{2,}", " ", t)
    t = re.sub(r"\s+([.,;:!?])", r"\1", t)
    return t.strip(" .,;") + "." if t.strip() and t.strip()[-1] not in ".!?" else t.strip()
