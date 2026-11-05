"""Recibo en PDF de un pedido, con las instrucciones de pago cuando es por transferencia."""
import io

from reportlab.lib import colors
from reportlab.lib.pagesizes import A5
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from . import config
from .db import q


def _cop(n):
    return "$" + f"{float(n):,.0f}".replace(",", ".")


def datos_pedido(pedido_id):
    p = q("SELECT p.*, c.nombre AS cliente, r.nombre AS restaurante, z.nombre AS zona, d.nombre AS domiciliario "
          "FROM pedidos p JOIN clientes c ON c.id=p.cliente_id JOIN restaurantes r ON r.id=p.restaurante_id "
          "LEFT JOIN zonas z ON z.id=p.zona_id LEFT JOIN domiciliarios d ON d.id=p.domiciliario_id "
          "WHERE p.id=%s", (pedido_id,), uno=True)
    if not p:
        return None
    p["items"] = q("SELECT pr.nombre, pi.cantidad, pi.precio_unitario, pi.observacion FROM pedido_items pi "
                   "JOIN productos pr ON pr.id=pi.producto_id WHERE pi.pedido_id=%s ORDER BY pi.id", (pedido_id,))
    return p


def generar(pedido_id):
    """Devuelve los bytes del PDF, o None si el pedido no existe."""
    p = datos_pedido(pedido_id)
    if not p:
        return None
    ink, mute, line = colors.HexColor("#1e293b"), colors.HexColor("#64748b"), colors.HexColor("#e2e8f0")
    base = getSampleStyleSheet()["Normal"]
    h = ParagraphStyle("h", parent=base, fontName="Helvetica-Bold", fontSize=15, textColor=ink, spaceAfter=2)
    n = ParagraphStyle("n", parent=base, fontSize=9.5, textColor=ink, leading=13)
    m = ParagraphStyle("m", parent=n, textColor=mute, fontSize=8.5)
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A5, leftMargin=14 * mm, rightMargin=14 * mm, topMargin=14 * mm,
                            bottomMargin=12 * mm, title=f"Recibo pedido {pedido_id}")
    t = [Paragraph(config.NOMBRE_EMPRESA, h), Paragraph(f"Recibo del pedido n.º {pedido_id}", n),
         Paragraph(f"{p['creado_en']:%Y-%m-%d %H:%M} · Cliente: {p['cliente']}", m), Spacer(1, 6 * mm)]
    filas = [["Producto", "Cant.", "Valor"]]
    for i in p["items"]:
        nombre = i["nombre"] + (f" ({i['observacion']})" if i["observacion"] else "")
        filas.append([Paragraph(nombre, n), i["cantidad"], _cop(i["precio_unitario"] * i["cantidad"])])
    filas += [["Subtotal", "", _cop(p["subtotal"])], [f"Domicilio ({p['zona'] or '-'})", "", _cop(p["costo_domicilio"])],
              ["Total", "", _cop(p["total"])]]
    tabla = Table(filas, colWidths=[62 * mm, 14 * mm, 28 * mm])
    tabla.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"), ("FONTSIZE", (0, 0), (-1, -1), 9.5),
        ("TEXTCOLOR", (0, 0), (-1, -1), ink), ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
        ("LINEBELOW", (0, 0), (-1, 0), 0.8, ink), ("LINEBELOW", (0, 1), (-1, -4), 0.3, line),
        ("LINEABOVE", (0, -3), (-1, -3), 0.5, line), ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
        ("LINEABOVE", (0, -1), (-1, -1), 0.8, ink), ("VALIGN", (0, 0), (-1, -1), "TOP")]))
    t += [tabla, Spacer(1, 6 * mm),
          Paragraph(f"<b>Restaurante:</b> {p['restaurante']}", n),
          Paragraph(f"<b>Entrega:</b> {p['direccion_entrega']}", n),
          Paragraph(f"<b>Forma de pago:</b> {p['metodo_pago'] or 'sin definir'}", n)]
    if p["domiciliario"]:
        t.append(Paragraph(f"<b>Domiciliario:</b> {p['domiciliario']}", n))
    if p["metodo_pago"] == "transferencia":
        t += [Spacer(1, 5 * mm), Paragraph("<b>Cómo pagar por transferencia</b>", n),
              Paragraph(config.PAGO_TRANSFERENCIA, n),
              Paragraph(f"Valor exacto a transferir: <b>{_cop(p['total'])}</b>. Usa como referencia el número de "
                        f"pedido ({pedido_id}) y muestra el comprobante al domiciliario al recibir.", n)]
    else:
        t += [Spacer(1, 5 * mm), Paragraph(f"Paga <b>{_cop(p['total'])}</b> en efectivo al domiciliario al recibir.", n)]
    doc.build(t)
    return buf.getvalue()
