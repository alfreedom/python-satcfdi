from collections.abc import Mapping, Sequence
from datetime import date
from itertools import groupby
from typing import Iterable

import xlsxwriter
from tabulate import tabulate

from ._ansi_colors import *
from .formatters import *
from .models import *

logger = logging.getLogger(__name__)

ISR = "001"
IVA = "002"
IEPS = "003"

IVA16 = "002|Tasa|0.160000"
IVA08 = "002|Tasa|0.080000"

RET_ISR = "01"
RET_IVA = "02"
RET_IEPS = "03"


def complement_invoices_data(invoices: Mapping[UUID, SatCFDI]):
    for c in invoices.values():
        for cfdi_rel in iterate(c.get("CfdiRelacionados")):
            for uuid in cfdi_rel["CfdiRelacionado"]:
                if cfdi := invoices.get(UUID(uuid)):
                    cfdi.relations.append(
                        Relation(
                            cfdi_relacionados=cfdi_rel,
                            comprobante=c
                        )
                    )

        if c['TipoDeComprobante'] != "P":
            continue

        for p in c["Complemento"]["Pagos"]["Pago"]:
            for doc_rel in p.get('DoctoRelacionado', []):
                if cfdi := invoices.get(UUID(doc_rel["IdDocumento"])):
                    cfdi.payments.append(
                        Payment(
                            docto_relacionado=doc_rel,
                            pago=p,
                            comprobante=c
                        )
                    )


def _compare(value, compare_value):
    if compare_value:
        if callable(compare_value):
            return compare_value(value)
        return value == compare_value
    return True


def filter_invoices_iter(
        invoices: Iterable[SatCFDI],
        rfc_emisor=None,
        rfc_receptor=None,
        estatus=None,
        fecha=None,
        invoice_type=None,
        payment_method=None,
        pending_balance=None
):
    for r in invoices:
        if _compare(r["Emisor"]["Rfc"], rfc_emisor) \
                and _compare(r["Receptor"]["Rfc"], rfc_receptor) \
                and _compare(r.estatus, estatus) \
                and _compare(r["Fecha"], fecha) \
                and _compare(r.get("MetodoPago"), payment_method) \
                and _compare(r["TipoDeComprobante"], invoice_type) \
                and _compare(r.saldo_pendiente, pending_balance):
            yield r


def filter_payments_iter(invoices: Mapping[UUID, SatCFDI], rfc_emisor=None, rfc_receptor=None, fecha=None) -> Sequence[PaymentsDetails]:
    for r in filter_invoices_iter(invoices.values(), rfc_emisor=rfc_emisor, rfc_receptor=rfc_receptor, estatus='1', fecha=None):
        match r['TipoDeComprobante']:
            case "I":
                if r['MetodoPago'] == PUE:
                    if not r.payments:
                        if _compare(r["Fecha"], fecha):
                            yield PaymentsDetails(comprobante=r)
            case "P":
                for p in r["Complemento"]["Pagos"]["Pago"]:
                    if _compare(p['FechaPago'], fecha):
                        for dr in p['DoctoRelacionado']:
                            yield PaymentsDetails(
                                comprobante=r,
                                pago=p,
                                docto_relacionado=dr,
                                comprobante_pagado=invoices[UUID(dr["IdDocumento"])]
                            )


def filter_retenciones_iter(invoices: Mapping[UUID, SatCFDI], ejerc: int, rfc_emisor=None, rfc_receptor=None) -> Sequence[SatCFDI]:
    for a in invoices.values():
        if not _compare(a["Periodo"]["Ejerc"], ejerc):
            continue

        if "Intereses" in a["Complemento"]:
            yield a


def invoice_def():
    # Width, Sum, Header, Value Lambda
    return {
        'Factura': (35, False, format_head),
        'Fecha': (35, False, format_fecha),
        'Emisor': (40, False, format_emisor),
        'Receptor': (40, False, format_receptor),
        'Forma Pago': (40, False, format_forma_pago),

        'SubTotal': (12, True, lambda i: i["SubTotal"]),
        'Descuento': (12, True, lambda i: i.get("Descuento")),
        'IVA16 Tras': (12, True, lambda i: i.get("Impuestos", {}).get("Traslados", {}).get(IVA16, {}).get("Importe")),
        'IVA Ret': (12, True, lambda i: i.get("Impuestos", {}).get("Retenciones", {}).get(IVA, {}).get("Importe")),
        'ISR Ret': (12, True, lambda i: i.get("Impuestos", {}).get("Retenciones", {}).get(ISR, {}).get("Importe")),
        'Total': (12, True, lambda i: i["Total"]),
        'Pendiente': (12, True, lambda i: i.saldo_pendiente or None),
        'Pagos': (35, False, format_pagos),
        'Relaciones': (35, False, format_relaciones),
        'Estado CFDI': (35, False, format_estado_cfdi),
        'Conceptos': (60, False, format_conceptos),
    }


def invoice_confirm_def():
    # Width, Sum, Header, Value Lambda
    return {
        'Factura': (35, False, format_head_pre),
        'Fecha': (35, False, format_fecha),
        'Emisor': (40, False, format_emisor),
        'Receptor': (40, False, format_receptor),
        'Forma Pago': (40, False, format_forma_pago),

        'SubTotal': (12, True, lambda i: i["SubTotal"]),
        'Descuento': (12, True, lambda i: i.get("Descuento")),
        'IVA16 Tras': (12, True, lambda i: i.get("Impuestos", {}).get("Traslados", {}).get(IVA16, {}).get("Importe")),
        'IVA Ret': (12, True, lambda i: i.get("Impuestos", {}).get("Retenciones", {}).get(IVA, {}).get("Importe")),
        'ISR Ret': (12, True, lambda i: i.get("Impuestos", {}).get("Retenciones", {}).get(ISR, {}).get("Importe")),
        'Total': (12, True, lambda i: i["Total"]),
        'Conceptos': (60, False, format_conceptos),
    }


def payment_def():
    # Width, Sum, Header, Value Lambda
    # Lambda 0:payment, 1:factura_pagada
    return {
        'Factura': (35, False, lambda i: format_head(i.comprobante)),
        'Fecha': (35, False, lambda i: format_fecha(i.comprobante)),
        'Emisor': (40, False, lambda i: format_emisor(i.comprobante)),
        'Receptor': (40, False, lambda i: format_receptor(i.comprobante)),
        'Forma Pago': (40, False, lambda i: format_forma_pago_dr(i)),

        'Factura Pagada': (12, False, lambda i: format_head(i.comprobante_pagado) if i.pago else "misma"),
        'Fecha de Pago': (12, False, lambda i: format_fecha_pago(i)),

        'Pagado': (12, False, lambda i: i.total),
        'Saldo Ant': (12, False, lambda i: i.docto_relacionado["ImpSaldoAnt"] if i.pago else i.comprobante["Total"]),
        'Saldo Insoluto': (12, False, lambda i: i.docto_relacionado["ImpSaldoInsoluto"] if i.pago else 0),
        'Parcialidad': (12, False, lambda i: i.docto_relacionado["NumParcialidad"] if i.pago else None),
        'Subtotal': (12, True, lambda i: i.sub_total),
        'Descuento': (12, True, lambda i: i.descuento),
        'IVA16 Tras': (12, True, lambda i: i.impuestos.get("Traslados", {}).get(IVA16, {}).get("Importe")),
        'IVA Ret': (12, True, lambda i: i.impuestos.get("Retenciones", {}).get(IVA, {}).get("Importe")),
        'ISR Ret': (12, True, lambda i: i.impuestos.get("Retenciones", {}).get(ISR, {}).get("Importe")),

        'Estatus SAT': (35, False, lambda i: format_estado_cfdi(i.comprobante)),
    }


def retenciones_def():
    return {
        'RFC de la Institucion': (12, False, lambda i: i["Emisor"]["RFCEmisor"]),
        'Monto de los intereses nominales': (12, False, lambda i: i["Complemento"]["Intereses"]["MontIntNominal"]),
        'Monto de los intereses reales': (12, False, lambda i: i["Complemento"]["Intereses"]["MontIntReal"]),
        'Perdida': (12, False, lambda i: i["Complemento"]["Intereses"]["Perdida"]),
        'ISR Retenido': (12, False, lambda i: sum(x["MontoRet"] for x in i["Totales"]['ImpRetenidos'] if x.get("Impuesto") == RET_ISR) if 'ImpRetenidos' in i["Totales"] else None)
    }


def console_print(invoices, columns):
    headers = ['', *(c for c in columns.keys())]

    def row(n, i):
        return [
            paint("{:0>2}".format(n + 1), COLOR_BACKGROUND_BRIGHT_BLACK if n % 2 == 0 else COLOR_BACKGROUND_BLACK),
            *(f(i) for w, s, f in columns.values())
        ]

    print(
        tabulate(
            [
                row(n, i) for n, i in enumerate(invoices)
            ],
            floatfmt=".2f",
            headers=headers
        )
    )
    print(paint("", COLOR_RESET))


# CONSOLE PRINT
def invoices_confirmation_print(invoices: Sequence[CFDI]):
    console_print(invoices, invoice_confirm_def())


def invoices_print(invoices: Sequence[SatCFDI]):
    console_print(invoices, invoice_def())


def payments_print(payments: Sequence[PaymentsDetails]):
    console_print(payments, payment_def())


def retenciones_print(retentions: Sequence[SatCFDI]):
    console_print(retentions, retenciones_def())


def num2col(n):
    """Number to Excel-style column name, e.g., 0 = A, 25 = Z, 26 = AA, 702 = AAA."""
    n += 1

    name = ''
    while n > 0:
        n, r = divmod(n - 1, 26)
        name = chr(r + ord('A')) + name
    return name


def excel_export(workbook: xlsxwriter.workbook.Workbook, name, invoices, columns):
    worksheet = workbook.add_worksheet(name)

    for n, (w, s, f) in enumerate(columns.values()):
        worksheet.set_column(n, n, w)

    # Title Format
    title_format = workbook.add_format()
    title_format.set_bold()
    title_format.set_pattern(1)
    title_format.set_bg_color('black')
    title_format.set_font_color('white')

    headers = [c for c in columns.keys()]
    for c, cell in enumerate(headers):
        worksheet.write(0, c, cell, title_format)

    text_wrap = workbook.add_format()
    text_wrap.set_text_wrap()
    text_wrap.set_align('top')

    date_format = workbook.add_format({'num_format': 'yyyy-mm-dd'})
    date_format.set_align('top')

    number_format = workbook.add_format({'num_format': '0.00'})
    number_format.set_align('top')

    row_height = 20 * 3
    r = 0
    for r, i in enumerate(invoices):
        worksheet.set_row_pixels(1 + r, row_height)

        for c, cell in enumerate(f(i) for w, s, f in columns.values()):
            if isinstance(cell, Decimal):
                worksheet.write(1 + r, c, cell, number_format)
            elif isinstance(cell, date):
                worksheet.write(1 + r, c, cell, date_format)
            else:
                worksheet.write(1 + r, c, cell, text_wrap)

    title_format.set_num_format('0.00')
    for i, (w, s, f) in enumerate(columns.values()):
        if s:
            col = num2col(i)
            worksheet.write(r + 2, i, '=SUM(' + col + '2:' + col + str(1 + r + 1) + ')', title_format)


# EXPORT TO EXCEL
def invoices_export(workbook, name, invoices: Sequence[SatCFDI]):
    excel_export(workbook, name, invoices, invoice_def())


def payments_export(workbook, name, payments: Sequence[PaymentsDetails]):
    excel_export(workbook, name, payments, payment_def())


def retentions_export(workbook, name, retentions: Sequence[SatCFDI]):
    excel_export(workbook, name, retentions, retenciones_def())


# EXPORT TO TXT
def payments_groupby_receptor(payments: Sequence[PaymentsDetails]):
    res = []
    for receptor, group in groupby(
            sorted(payments, key=lambda r: r.comprobante["Receptor"]["Rfc"]),
            lambda r: r.comprobante["Receptor"]["Rfc"]
    ):
        p = list(group)
        res.append({
            "Receptor": receptor,
            "SubTotal": sum(p.sub_total for p in p),
            "Descuento": sum(p.descuento or 0 for p in p),
            "ISR Ret": sum(p.impuestos.get("Retenciones", {}).get(ISR, {}).get("Importe") or 0 for p in p)
        })
    return res


def payments_retentions_export(file_name, grouped_payments: Sequence):
    with open(file_name, "w", encoding="utf-8") as f:
        def write(line):
            f.write(line)
            f.write("\n")

        write("RFC retenedor|Monto del ingreso recibido|ISR retenido")
        for r in grouped_payments:
            write("{receptor}|{ingreso_recibido}|{isr_retenido}".format(
                receptor=r["Receptor"],
                ingreso_recibido=round(r["SubTotal"] - r["Descuento"], 2),
                isr_retenido=round(r["ISR Ret"], 2)
            ))
