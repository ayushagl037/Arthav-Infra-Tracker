"""
Arthav Infra LLP — Rent Invoice Generator
Standalone app: generates GST rent invoices (SGST 9% + CGST 9%) for all tenants.
"""

import io
import zipfile
from datetime import date, datetime
from pathlib import Path

import streamlit as st
import pandas as pd
from sqlalchemy import (
    create_engine, Column, Integer, String, Float, Date, Text,
    ForeignKey
)
from sqlalchemy.orm import declarative_base, Session
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.units import mm
from reportlab.platypus import (
    SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, HRFlowable
)
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle

# ─────────────────────────────────────────────
# 0. PAGE CONFIG
# ─────────────────────────────────────────────
st.set_page_config(
    page_title="Arthav Infra — Rent Invoices",
    page_icon="🏢",
    layout="centered",
    initial_sidebar_state="expanded",
)

# ─────────────────────────────────────────────
# 1. CONSTANTS
# ─────────────────────────────────────────────
LANDLORD_NAME    = "Arthav Infra LLP"
LANDLORD_PAN     = "ACKFA1087B"
LANDLORD_GSTIN   = "36ACKFA1087B1ZO"
LANDLORD_ADDR1   = "R/o. 3-6-305/81, Avanti Nagar Colony,"
LANDLORD_ADDR2   = "Basheerbagh, Hyderabad - 500029"
BANK_NAME        = "M/s. ARTHA INFRA LLP"
BANK_ACCOUNT     = "C A/c No.: 50200115403426"
BANK_BRANCH      = "HDFC Bank Ltd., Film Nagar Branch"
BANK_IFSC        = "IFSC: HDFC0003974"
HSN_CODE         = "997212"
GST_RATE         = 0.09          # 9% SGST + 9% CGST = 18% total
NAVY             = "#0A1628"
GOLD             = "#C9A84C"

# ─────────────────────────────────────────────
# 2. DATABASE
# ─────────────────────────────────────────────
Base = declarative_base()

class Tenant(Base):
    __tablename__ = "tenants"
    id            = Column(Integer, primary_key=True, autoincrement=True)
    name          = Column(String, nullable=False)          # e.g. "Prime Impex INC."
    short_code    = Column(String, nullable=False)          # e.g. "Prime Impex" (used in bill no.)
    address       = Column(String, default="")             # city / full address
    property_addr = Column(String, default="")             # property being rented
    gstin         = Column(String, default="")
    pan           = Column(String, default="")
    default_rent  = Column(Float,  default=0.0)
    active        = Column(Integer, default=1)             # 1=active, 0=inactive

class Invoice(Base):
    __tablename__ = "invoices"
    id            = Column(Integer, primary_key=True, autoincrement=True)
    tenant_id     = Column(Integer, ForeignKey("tenants.id"), nullable=False)
    bill_number   = Column(String, nullable=False)         # e.g. "Prime Impex 001/25-26"
    invoice_date  = Column(Date,   nullable=False)
    month_label   = Column(String, nullable=False)         # e.g. "Jan'26"
    rent_amount   = Column(Float,  nullable=False)
    sgst          = Column(Float,  nullable=False)
    cgst          = Column(Float,  nullable=False)
    total         = Column(Float,  nullable=False)
    created_at    = Column(Date,   default=date.today)


@st.cache_resource
def get_engine():
    engine = create_engine("sqlite:///rent_invoices.db", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    return engine


def get_tenants(engine):
    with Session(engine) as s:
        return s.query(Tenant).filter(Tenant.active == 1).order_by(Tenant.name).all()


def get_all_tenants(engine):
    with Session(engine) as s:
        return s.query(Tenant).order_by(Tenant.name).all()


def add_tenant(engine, name, short_code, address, property_addr,
               gstin, pan, default_rent):
    with Session(engine) as s:
        s.add(Tenant(
            name=name, short_code=short_code, address=address,
            property_addr=property_addr, gstin=gstin, pan=pan,
            default_rent=default_rent,
        ))
        s.commit()


def update_tenant(engine, tenant_id, **kwargs):
    with Session(engine) as s:
        t = s.get(Tenant, tenant_id)
        if t:
            for k, v in kwargs.items():
                setattr(t, k, v)
            s.commit()


def next_bill_number(engine, short_code: str, invoice_date: date) -> str:
    """Generate next sequential bill number for this tenant in this FY."""
    fy_start = invoice_date.year if invoice_date.month >= 4 else invoice_date.year - 1
    fy_end   = fy_start + 1
    fy_label = f"{str(fy_start)[-2:]}-{str(fy_end)[-2:]}"   # e.g. "25-26"
    prefix   = f"{short_code} "
    suffix   = f"/{fy_label}"
    with Session(engine) as s:
        existing = s.query(Invoice).filter(
            Invoice.bill_number.like(f"{prefix}%{suffix}")
        ).all()
        next_num = len(existing) + 1
    return f"{prefix}{next_num:03d}{suffix}"


def save_invoice_record(engine, tenant_id, bill_number, invoice_date,
                        month_label, rent_amount, sgst, cgst, total):
    with Session(engine) as s:
        s.add(Invoice(
            tenant_id=tenant_id, bill_number=bill_number,
            invoice_date=invoice_date, month_label=month_label,
            rent_amount=rent_amount, sgst=sgst, cgst=cgst, total=total,
        ))
        s.commit()


def get_invoice_history(engine):
    with Session(engine) as s:
        rows = (
            s.query(Invoice, Tenant.name)
            .join(Tenant, Invoice.tenant_id == Tenant.id)
            .order_by(Invoice.invoice_date.desc())
            .all()
        )
        return [
            {
                "ID":           inv.id,
                "Bill No.":     inv.bill_number,
                "Tenant":       name,
                "Month":        inv.month_label,
                "Date":         inv.invoice_date,
                "Rent (₹)":    inv.rent_amount,
                "SGST (₹)":    inv.sgst,
                "CGST (₹)":    inv.cgst,
                "Total (₹)":   inv.total,
            }
            for inv, name in rows
        ]

# ─────────────────────────────────────────────
# 3. AMOUNT IN WORDS (Indian format)
# ─────────────────────────────────────────────
def amount_in_words(amount: float) -> str:
    ones = ["", "One", "Two", "Three", "Four", "Five", "Six", "Seven",
            "Eight", "Nine", "Ten", "Eleven", "Twelve", "Thirteen",
            "Fourteen", "Fifteen", "Sixteen", "Seventeen", "Eighteen", "Nineteen"]
    tens = ["", "", "Twenty", "Thirty", "Forty", "Fifty",
            "Sixty", "Seventy", "Eighty", "Ninety"]

    def _two(n):
        return ones[n] if n < 20 else tens[n // 10] + (" " + ones[n % 10] if n % 10 else "")

    def _three(n):
        if n >= 100:
            return ones[n // 100] + " Hundred" + (" " + _two(n % 100) if n % 100 else "")
        return _two(n)

    rupees = int(round(amount))
    if rupees == 0:
        return "Zero Rupees Only"

    parts = []
    cr  = rupees // 10_000_000; rupees %= 10_000_000
    lac = rupees // 100_000;    rupees %= 100_000
    th  = rupees // 1_000;      rupees %= 1_000
    hun = rupees

    if cr:  parts.append(_three(cr)  + " Crore")
    if lac: parts.append(_three(lac) + " Lakh")
    if th:  parts.append(_three(th)  + " Thousand")
    if hun: parts.append(_three(hun))
    return "INR: " + " ".join(parts) + " Only"

# ─────────────────────────────────────────────
# 4. PDF INVOICE GENERATOR
# ─────────────────────────────────────────────
def generate_invoice_pdf(
    tenant: Tenant,
    bill_number: str,
    invoice_date: date,
    month_label: str,
    rent_amount: float,
) -> bytes:
    sgst  = round(rent_amount * GST_RATE, 2)
    cgst  = round(rent_amount * GST_RATE, 2)
    total = rent_amount + sgst + cgst

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=18*mm, rightMargin=18*mm,
        topMargin=14*mm, bottomMargin=14*mm,
    )

    NAVYc  = colors.HexColor("#0A1628")
    GOLDc  = colors.HexColor("#C9A84C")
    LGREYc = colors.HexColor("#F4F4F4")
    BLACKc = colors.black
    GREYc  = colors.HexColor("#555555")

    styles = getSampleStyleSheet()
    title_s  = ParagraphStyle("t",  fontSize=16, fontName="Helvetica-Bold",
                               textColor=GOLDc,  spaceAfter=2,  alignment=1)
    head_s   = ParagraphStyle("h",  fontSize=10, fontName="Helvetica-Bold",
                               textColor=NAVYc,  spaceAfter=1)
    small_s  = ParagraphStyle("sm", fontSize=8.5, fontName="Helvetica",
                               textColor=GREYc,  spaceAfter=1)
    label_s  = ParagraphStyle("lb", fontSize=8,  fontName="Helvetica-Bold",
                               textColor=NAVYc)
    words_s  = ParagraphStyle("w",  fontSize=8.5, fontName="Helvetica-Oblique",
                               textColor=NAVYc,  spaceAfter=2)
    bank_s   = ParagraphStyle("bk", fontSize=8,  fontName="Helvetica",
                               textColor=GREYc,  spaceAfter=1)

    bdr  = {"style": "SINGLE", "width": 0.4, "color": colors.HexColor("#CCCCCC")}
    hbdr = {"style": "SINGLE", "width": 0.5, "color": GOLDc}

    W = 174 * mm   # usable width

    story = []

    # ── Header bar ────────────────────────────────────────────────
    story.append(Paragraph("RENT INVOICE", title_s))
    story.append(HRFlowable(width="100%", thickness=1.5, color=GOLDc, spaceAfter=4))

    # ── Landlord + Bill info ──────────────────────────────────────
    info_data = [
        [
            Paragraph(f"<b>{LANDLORD_NAME}</b><br/>"
                      f"PAN: {LANDLORD_PAN}<br/>"
                      f"GSTIN: {LANDLORD_GSTIN}<br/>"
                      f"{LANDLORD_ADDR1}<br/>{LANDLORD_ADDR2}", small_s),
            Paragraph(f"<b>Bill No.:</b> {bill_number}<br/>"
                      f"<b>Date:</b> {invoice_date.strftime('%d/%m/%y')}<br/>"
                      f"<b>HSN/SAC:</b> {HSN_CODE}", small_s),
        ]
    ]
    info_t = Table(info_data, colWidths=[W * 0.6, W * 0.4])
    info_t.setStyle(TableStyle([
        ("VALIGN",      (0, 0), (-1, -1), "TOP"),
        ("ALIGN",       (1, 0), (1, 0),   "RIGHT"),
        ("BOTTOMPADDING",(0,0), (-1,-1),  6),
    ]))
    story.append(info_t)
    story.append(HRFlowable(width="100%", thickness=0.5, color=colors.HexColor("#CCCCCC"), spaceAfter=4))

    # ── Billed To ─────────────────────────────────────────────────
    story.append(Paragraph("Billed To:", label_s))
    story.append(Paragraph(
        f"<b>M/s. {tenant.name}</b><br/>"
        f"{tenant.address}<br/>"
        + (f"GSTIN: {tenant.gstin}<br/>" if tenant.gstin else "")
        + (f"PAN: {tenant.pan}" if tenant.pan else ""),
        small_s
    ))
    story.append(Spacer(1, 4*mm))

    # ── Line items table ──────────────────────────────────────────
    col_w = [12*mm, 118*mm, 44*mm]
    rows  = [
        # Header
        [
            Paragraph("S.No.", label_s),
            Paragraph("Particulars", label_s),
            Paragraph("Amount (₹)", label_s),
        ],
        # Rent row
        ["1",
         Paragraph(f"Rent for the Month of {month_label}<br/>"
                   f"<font size='8' color='#777777'>{tenant.property_addr}</font>", small_s),
         Paragraph(f"{rent_amount:,.2f}", ParagraphStyle("r", fontSize=9,
                   fontName="Helvetica", alignment=2))],
        # SGST
        ["2", "SGST @ 9%",
         Paragraph(f"{sgst:,.2f}", ParagraphStyle("r2", fontSize=9,
                   fontName="Helvetica", alignment=2))],
        # CGST
        ["3", "CGST @ 9%",
         Paragraph(f"{cgst:,.2f}", ParagraphStyle("r3", fontSize=9,
                   fontName="Helvetica", alignment=2))],
        # Total
        ["", Paragraph("<b>Total</b>", label_s),
         Paragraph(f"<b>{total:,.2f}</b>",
                   ParagraphStyle("rt", fontSize=9, fontName="Helvetica-Bold", alignment=2))],
        # Round off (same as total — no fractions)
        ["", Paragraph("<b>Round Off</b>", label_s),
         Paragraph(f"<b>{round(total):,.2f}</b>",
                   ParagraphStyle("rr", fontSize=9, fontName="Helvetica-Bold", alignment=2))],
    ]

    items_t = Table(rows, colWidths=col_w, repeatRows=1)
    items_t.setStyle(TableStyle([
        # Header row
        ("BACKGROUND",    (0, 0), (-1, 0), NAVYc),
        ("TEXTCOLOR",     (0, 0), (-1, 0), GOLDc),
        ("FONTNAME",      (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE",      (0, 0), (-1, 0), 9),
        ("ALIGN",         (2, 0), (2, 0),  "RIGHT"),
        ("TOPPADDING",    (0, 0), (-1, 0), 5),
        ("BOTTOMPADDING", (0, 0), (-1, 0), 5),
        # Data rows
        ("FONTSIZE",      (0, 1), (-1, -1), 9),
        ("ALIGN",         (0, 1), (0, -1),  "CENTER"),
        ("ALIGN",         (2, 1), (2, -1),  "RIGHT"),
        ("ROWBACKGROUNDS",(0, 1), (-1, -3), [colors.white, LGREYc]),
        # Total rows — gold top border
        ("LINEABOVE",     (0, -2), (-1, -2), 1, GOLDc),
        ("BACKGROUND",    (0, -2), (-1, -1), colors.HexColor("#FFF8E7")),
        # Grid
        ("GRID",          (0, 0), (-1, -1), 0.3, colors.HexColor("#DDDDDD")),
        ("TOPPADDING",    (0, 1), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 1), (-1, -1), 4),
        ("LEFTPADDING",   (1, 1), (1, -1),  6),
    ]))
    story.append(items_t)
    story.append(Spacer(1, 3*mm))

    # ── Amount in words ───────────────────────────────────────────
    story.append(Paragraph(amount_in_words(round(total)), words_s))
    story.append(Paragraph("Rent for immovable property", small_s))
    story.append(HRFlowable(width="100%", thickness=0.5,
                             color=colors.HexColor("#CCCCCC"), spaceAfter=4))

    # ── Footer: bank details + signatory ─────────────────────────
    footer_data = [[
        # Bank details
        Paragraph(
            f"<b>Bank Details:</b><br/>"
            f"{BANK_NAME}<br/>"
            f"{BANK_ACCOUNT}<br/>"
            f"{BANK_BRANCH}<br/>"
            f"{BANK_IFSC}",
            bank_s
        ),
        # Signatory
        Paragraph(
            f"<br/><br/><br/><br/>"
            f"For <b>{LANDLORD_NAME}</b><br/>"
            f"(Authorised Signatory)",
            ParagraphStyle("sig", fontSize=8, fontName="Helvetica",
                           textColor=NAVYc, alignment=2)
        ),
    ]]
    footer_t = Table(footer_data, colWidths=[W * 0.55, W * 0.45])
    footer_t.setStyle(TableStyle([
        ("VALIGN",  (0, 0), (-1, -1), "BOTTOM"),
    ]))
    story.append(footer_t)

    # ── GST notice ────────────────────────────────────────────────
    story.append(Spacer(1, 3*mm))
    story.append(Paragraph(
        "GST Note: This invoice is subject to GST under RCM / Forward Charge as applicable. "
        "SGST and CGST are payable at 9% each on the rent value. "
        "No ITC is available to the landlord on this transaction.",
        ParagraphStyle("gn", fontSize=7, fontName="Helvetica-Oblique",
                       textColor=colors.HexColor("#888888"))
    ))

    doc.build(story)
    return buf.getvalue()


# ─────────────────────────────────────────────
# 5. CUSTOM CSS
# ─────────────────────────────────────────────
st.markdown(f"""
<style>
  @import url('https://fonts.googleapis.com/css2?family=Syne:wght@400;700&family=DM+Mono&display=swap');
  html, body, [class*="css"] {{ font-family: 'Syne', sans-serif; background-color: {NAVY}; color: #e8e6df; }}
  .stApp {{ background-color: {NAVY}; }}
  .section-header {{
      font-family: 'Syne', sans-serif; font-weight: 700; font-size: 1.1rem;
      color: {GOLD}; border-left: 4px solid {GOLD};
      padding: 6px 14px; margin: 18px 0 10px; background: #0f2040;
      border-radius: 0 6px 6px 0;
  }}
  div[data-testid="stSidebar"] {{ background-color: #071020; }}
  .stButton > button {{
      background-color: {NAVY}; color: {GOLD}; border: 1.5px solid {GOLD};
      border-radius: 6px; font-weight: 700; width: 100%;
  }}
  .stButton > button:hover {{ background-color: {GOLD}; color: {NAVY}; }}
  .stDownloadButton > button {{
      background: linear-gradient(90deg, {GOLD}, #e8c96a);
      color: {NAVY}; border: none; font-weight: 700; border-radius: 6px; width: 100%;
  }}
  .stDataFrame {{ border: 1px solid #1e3050 !important; border-radius: 8px; }}
  .stTabs [data-baseweb="tab"] {{ color: #7a8aaa; font-weight: 700; }}
  .stTabs [aria-selected="true"] {{ color: {GOLD} !important; border-bottom-color: {GOLD} !important; }}
  .metric-box {{
      background: #0f2040; border: 1px solid #1e3050; border-radius: 10px;
      padding: 14px 18px; text-align: center;
  }}
  .metric-val {{ font-size: 1.5rem; font-weight: 700; color: {GOLD}; }}
  .metric-lbl {{ font-size: 0.78rem; color: #7a8aaa; margin-top: 2px; }}
</style>
""", unsafe_allow_html=True)


# ─────────────────────────────────────────────
# 6. SIDEBAR — TENANT MANAGEMENT
# ─────────────────────────────────────────────
def render_sidebar(engine):
    st.sidebar.markdown(f"# 🏢 Arthav Infra LLP")
    st.sidebar.markdown(f"### Rent Invoice Generator")
    st.sidebar.markdown("---")

    st.sidebar.markdown("## ➕ Add Tenant")
    with st.sidebar.form("add_tenant_form", clear_on_submit=True):
        name          = st.text_input("Tenant Name *",        placeholder="M/s. Prime Impex INC.")
        short_code    = st.text_input("Short Code *",         placeholder="Prime Impex  (used in Bill No.)")
        address       = st.text_input("Tenant City/Address",  placeholder="Hyderabad.")
        property_addr = st.text_input("Property Address *",   placeholder="1-96/2, Survey No. 2, NH7, Satamrai...")
        gstin         = st.text_input("Tenant GSTIN",         placeholder="36XXXXXXX")
        pan           = st.text_input("Tenant PAN",           placeholder="XXXXXXXXXX")
        default_rent  = st.number_input("Default Monthly Rent (₹)", min_value=0.0, step=1000.0)
        if st.form_submit_button("Add Tenant", use_container_width=True):
            if not name.strip() or not short_code.strip():
                st.sidebar.error("Name and Short Code are required.")
            else:
                add_tenant(engine, name.strip(), short_code.strip(),
                           address.strip(), property_addr.strip(),
                           gstin.strip(), pan.strip(), default_rent)
                st.sidebar.success(f"✅ {name} added.")
                st.rerun()

    st.sidebar.markdown("---")
    st.sidebar.markdown("## ✏️ Edit Tenant")
    tenants = get_all_tenants(engine)
    if tenants:
        t_map = {f"{t.name} (#{t.id})": t for t in tenants}
        sel   = st.sidebar.selectbox("Select Tenant", list(t_map.keys()), key="edit_sel")
        t     = t_map[sel]
        with st.sidebar.form("edit_tenant_form", clear_on_submit=False):
            e_name    = st.text_input("Name",            value=t.name)
            e_code    = st.text_input("Short Code",      value=t.short_code)
            e_addr    = st.text_input("Address",         value=t.address)
            e_prop    = st.text_input("Property Address",value=t.property_addr)
            e_gstin   = st.text_input("GSTIN",           value=t.gstin)
            e_pan     = st.text_input("PAN",             value=t.pan)
            e_rent    = st.number_input("Default Rent",  value=float(t.default_rent), step=1000.0)
            e_active  = st.checkbox("Active", value=bool(t.active))
            if st.form_submit_button("Save Changes", use_container_width=True):
                update_tenant(engine, t.id,
                              name=e_name, short_code=e_code,
                              address=e_addr, property_addr=e_prop,
                              gstin=e_gstin, pan=e_pan,
                              default_rent=e_rent, active=int(e_active))
                st.sidebar.success("✅ Tenant updated.")
                st.rerun()


# ─────────────────────────────────────────────
# 7. MAIN TABS
# ─────────────────────────────────────────────
def render_generate_tab(engine):
    st.markdown('<div class="section-header">Generate Monthly Invoices</div>',
                unsafe_allow_html=True)

    tenants = get_tenants(engine)
    if not tenants:
        st.info("No active tenants yet. Add tenants from the sidebar to get started.")
        return

    # ── Month & date selector ──────────────────────────────────────
    c1, c2, c3 = st.columns(3)
    with c1:
        today        = date.today()
        # default to first of current month
        inv_date     = st.date_input("Invoice Date", value=today.replace(day=1))
    with c2:
        month_names  = ["January","February","March","April","May","June",
                        "July","August","September","October","November","December"]
        month_idx    = st.selectbox("Billing Month", range(12),
                                    format_func=lambda i: month_names[i],
                                    index=today.month - 1)
    with c3:
        bill_year    = st.number_input("Year", min_value=2020, max_value=2040,
                                        value=today.year, step=1)

    month_label = f"{month_names[month_idx][:3]}'{str(bill_year)[-2:]}"  # e.g. Jan'26

    st.markdown("---")
    st.markdown('<div class="section-header">Adjust Rent Amounts</div>',
                unsafe_allow_html=True)
    st.caption("Default amounts loaded from tenant profiles. Change any that differ this month.")

    # ── Rent amount inputs per tenant ──────────────────────────────
    rent_inputs = {}
    for t in tenants:
        col_a, col_b = st.columns([3, 2])
        with col_a:
            st.markdown(f"**{t.name}**")
            st.caption(t.property_addr or "No property address set")
        with col_b:
            rent_inputs[t.id] = st.number_input(
                f"Rent (₹)",
                value=float(t.default_rent),
                min_value=0.0,
                step=1000.0,
                key=f"rent_{t.id}",
                label_visibility="collapsed",
            )
        # Show GST breakdown inline
        rent  = rent_inputs[t.id]
        sgst  = round(rent * GST_RATE, 2)
        total = round(rent + sgst * 2, 2)
        st.caption(f"SGST ₹{sgst:,.2f}  +  CGST ₹{sgst:,.2f}  =  **Total ₹{total:,.2f}**")
        st.markdown("---")

    # ── Summary metrics ────────────────────────────────────────────
    total_rent  = sum(rent_inputs.values())
    total_gst   = sum(round(r * GST_RATE * 2, 2) for r in rent_inputs.values())
    total_inv   = total_rent + total_gst

    m1, m2, m3, m4 = st.columns(4)
    for col, label, val in [
        (m1, "Tenants", str(len(tenants))),
        (m2, "Total Rent", f"₹{total_rent:,.0f}"),
        (m3, "Total GST", f"₹{total_gst:,.0f}"),
        (m4, "Total Invoiced", f"₹{total_inv:,.0f}"),
    ]:
        with col:
            st.markdown(f"""
            <div class="metric-box">
                <div class="metric-val">{val}</div>
                <div class="metric-lbl">{label}</div>
            </div>""", unsafe_allow_html=True)

    st.markdown("<br>", unsafe_allow_html=True)

    # ── Generate button ────────────────────────────────────────────
    if st.button("🖨️ Generate All Invoices", use_container_width=True):
        if all(v == 0 for v in rent_inputs.values()):
            st.error("All rent amounts are ₹0. Please enter at least one amount.")
            return

        generated = []
        with st.spinner(f"Generating {len(tenants)} invoice(s)..."):
            for t in tenants:
                rent = rent_inputs[t.id]
                if rent == 0:
                    continue
                sgst      = round(rent * GST_RATE, 2)
                cgst      = round(rent * GST_RATE, 2)
                total     = rent + sgst + cgst
                bill_no   = next_bill_number(engine, t.short_code, inv_date)
                pdf_bytes = generate_invoice_pdf(t, bill_no, inv_date, month_label, rent)
                save_invoice_record(engine, t.id, bill_no, inv_date, month_label,
                                    rent, sgst, cgst, total)
                fname = f"{bill_no.replace('/', '-').replace(' ', '_')}_{month_label}.pdf"
                generated.append((fname, pdf_bytes, t.name, bill_no, total))

        if not generated:
            st.warning("No invoices generated — all rents were ₹0.")
            return

        st.success(f"✅ {len(generated)} invoice(s) generated for {month_label}!")

        # ── Individual download buttons ────────────────────────────
        st.markdown('<div class="section-header">Download Invoices</div>',
                    unsafe_allow_html=True)
        for fname, pdf_bytes, tname, bill_no, total in generated:
            dc1, dc2, dc3 = st.columns([3, 2, 2])
            with dc1:
                st.markdown(f"**{tname}**  \n`{bill_no}`")
            with dc2:
                st.markdown(f"₹{total:,.2f}")
            with dc3:
                st.download_button(
                    label="⬇ Download PDF",
                    data=pdf_bytes,
                    file_name=fname,
                    mime="application/pdf",
                    key=f"dl_{fname}",
                    use_container_width=True,
                )

        # ── ZIP download ───────────────────────────────────────────
        st.markdown("---")
        zip_buf = io.BytesIO()
        with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for fname, pdf_bytes, *_ in generated:
                zf.writestr(fname, pdf_bytes)
        zip_buf.seek(0)
        st.download_button(
            label=f"📦 Download All {len(generated)} Invoices as ZIP",
            data=zip_buf.getvalue(),
            file_name=f"Arthav_Infra_Rent_Invoices_{month_label}.zip",
            mime="application/zip",
            use_container_width=True,
        )


def render_tenants_tab(engine):
    st.markdown('<div class="section-header">Tenant Directory</div>',
                unsafe_allow_html=True)
    tenants = get_all_tenants(engine)
    if not tenants:
        st.info("No tenants added yet.")
        return

    rows = [{
        "ID":              t.id,
        "Name":            t.name,
        "Short Code":      t.short_code,
        "Property":        t.property_addr,
        "GSTIN":           t.gstin or "—",
        "PAN":             t.pan or "—",
        "Default Rent (₹)": f"₹{t.default_rent:,.0f}",
        "Active":          "✅" if t.active else "❌",
    } for t in tenants]
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

    # GST summary
    active = [t for t in tenants if t.active]
    if active:
        st.markdown("---")
        st.markdown('<div class="section-header">Monthly GST Summary (Active Tenants)</div>',
                    unsafe_allow_html=True)
        total_rent = sum(t.default_rent for t in active)
        total_gst  = round(total_rent * GST_RATE * 2, 2)
        total_inv  = total_rent + total_gst
        g1, g2, g3 = st.columns(3)
        for col, lbl, val in [
            (g1, "Total Monthly Rent", f"₹{total_rent:,.0f}"),
            (g2, "GST Payable (18%)",  f"₹{total_gst:,.0f}"),
            (g3, "Total Invoiced",     f"₹{total_inv:,.0f}"),
        ]:
            with col:
                st.markdown(f"""
                <div class="metric-box">
                    <div class="metric-val">{val}</div>
                    <div class="metric-lbl">{lbl}</div>
                </div>""", unsafe_allow_html=True)


def render_history_tab(engine):
    st.markdown('<div class="section-header">Invoice History</div>',
                unsafe_allow_html=True)
    history = get_invoice_history(engine)
    if not history:
        st.info("No invoices generated yet.")
        return

    df = pd.DataFrame(history)
    st.dataframe(df, use_container_width=True, hide_index=True, height=400)

    st.markdown("---")
    st.markdown('<div class="section-header">Reprint an Invoice</div>',
                unsafe_allow_html=True)
    st.caption("Load any past invoice record and regenerate the PDF.")

    with Session(get_engine()) as s:
        all_inv = s.query(Invoice).order_by(Invoice.invoice_date.desc()).all()
        all_ten = {t.id: t for t in s.query(Tenant).all()}

    if all_inv:
        inv_map = {f"{inv.bill_number}  ({inv.month_label})": inv for inv in all_inv}
        sel_inv = st.selectbox("Select Invoice", list(inv_map.keys()))
        if st.button("🖨️ Reprint PDF", use_container_width=False):
            inv    = inv_map[sel_inv]
            tenant = all_ten.get(inv.tenant_id)
            if tenant:
                pdf_bytes = generate_invoice_pdf(
                    tenant, inv.bill_number, inv.invoice_date,
                    inv.month_label, inv.rent_amount
                )
                fname = f"{inv.bill_number.replace('/', '-').replace(' ', '_')}_{inv.month_label}.pdf"
                st.download_button(
                    "⬇ Download Reprint",
                    data=pdf_bytes,
                    file_name=fname,
                    mime="application/pdf",
                    use_container_width=True,
                )

    # ── GST quarterly summary ──────────────────────────────────────
    st.markdown("---")
    st.markdown('<div class="section-header">Quarterly GST Liability</div>',
                unsafe_allow_html=True)
    df["Quarter"] = pd.to_datetime(df["Date"]).dt.to_period("Q").astype(str)
    q_summary = (
        df.groupby("Quarter")
        .agg(
            Invoices=("ID", "count"),
            Rent=("Rent (₹)", "sum"),
            SGST=("SGST (₹)", "sum"),
            CGST=("CGST (₹)", "sum"),
            Total=("Total (₹)", "sum"),
        )
        .reset_index()
        .sort_values("Quarter", ascending=False)
    )
    for col in ["Rent", "SGST", "CGST", "Total"]:
        q_summary[col] = q_summary[col].map("₹{:,.2f}".format)
    st.dataframe(q_summary, use_container_width=True, hide_index=True)

    # Export history
    csv = df.to_csv(index=False).encode("utf-8")
    st.download_button("⬇ Export History CSV", data=csv,
                       file_name="arthav_rent_invoice_history.csv",
                       mime="text/csv")


# ─────────────────────────────────────────────
# 8. MAIN
# ─────────────────────────────────────────────
def main():
    engine = get_engine()
    render_sidebar(engine)

    st.markdown(f"""
    <h2 style="color:{GOLD}; font-family:Syne,sans-serif; margin-bottom:0;">
        🏢 Rent Invoice Generator
    </h2>
    <p style="color:#7a8aaa; font-size:0.9rem; margin-top:4px;">
        Arthav Infra LLP &nbsp;|&nbsp; GSTIN: {LANDLORD_GSTIN} &nbsp;|&nbsp; HSN/SAC: {HSN_CODE}
    </p>
    """, unsafe_allow_html=True)

    tab_gen, tab_tenants, tab_history = st.tabs([
        "🖨️  Generate Invoices",
        "🏢  Tenants",
        "📋  History & GST",
    ])
    with tab_gen:
        render_generate_tab(engine)
    with tab_tenants:
        render_tenants_tab(engine)
    with tab_history:
        render_history_tab(engine)


if __name__ == "__main__":
    main()
