"""
Arthav Infra LLP — Expense & Invoice Tracker
A modular Streamlit application with SQLite/SQLAlchemy backend.
"""

import os
import io
import json
import base64
import shutil
import requests
from datetime import date, datetime
from pathlib import Path

import streamlit as st
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
from plotly.subplots import make_subplots
from sqlalchemy import (
    create_engine, Column, Integer, Text, Float, Date,
    ForeignKey, CheckConstraint, event, text
)
from sqlalchemy.orm import declarative_base, Session, relationship

# ─────────────────────────────────────────────
# 0. PAGE CONFIG  (must be first Streamlit call)
# ─────────────────────────────────────────────
st.set_page_config(
    page_title="Arthav Infra — Expense Tracker",
    page_icon="🏗️",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ─────────────────────────────────────────────
# 1. CONSTANTS & DIRECTORIES
# ─────────────────────────────────────────────
DB_PATH     = "arthav_expenses.db"
INVOICE_DIR = Path("invoices")
INVOICE_DIR.mkdir(exist_ok=True)

DEFAULT_CATEGORIES = ["Operational", "Utilities", "Raw Materials", "Marketing",
                      "Labour", "Legal & Professional", "Travel", "Miscellaneous",
                      "Commercial", "Construction Services"]

# ── 2026 Real-estate GST rules ────────────────────────────────────────────────
GST_RULES = {
    "Affordable Housing":           {"rate": 0.01, "on_fraction": 2/3, "itc": False,  "label": "1% on ⅔ value"},
    "Residential (Non-Affordable)": {"rate": 0.05, "on_fraction": 2/3, "itc": False,  "label": "5% on ⅔ value"},
    "Commercial":                   {"rate": 0.12, "on_fraction": 1.0, "itc": True,   "label": "12% with ITC"},
    "Construction Services":        {"rate": 0.18, "on_fraction": 1.0, "itc": True,   "label": "18% on full value"},
}

# Categories whose GST paid to vendors qualifies as ITC
ITC_ELIGIBLE_CATEGORIES = {"Commercial", "Construction Services", "Operational",
                            "Raw Materials", "Legal & Professional"}

DEFAULT_PROJECTS = [
    "Axis Bank Shamshabad",
    "Royal Oak Shamshabad",
    "Chaitanya Chengicherla",
    "Ayush Agarwal",
    "Ashwin Agarwal",
    "Other",
]

# ─────────────────────────────────────────────
# 2. DATABASE SETUP (SQLAlchemy ORM)
# ─────────────────────────────────────────────
Base = declarative_base()

class Vendor(Base):
    __tablename__ = "vendors"
    id             = Column(Integer, primary_key=True, autoincrement=True)
    name           = Column(Text, nullable=False)
    gst_number     = Column(Text)
    contact_person = Column(Text)
    expenses       = relationship("Expense", back_populates="vendor")

class Category(Base):
    __tablename__ = "categories"
    id       = Column(Integer, primary_key=True, autoincrement=True)
    name     = Column(Text, nullable=False, unique=True)
    expenses = relationship("Expense", back_populates="category")

class Project(Base):
    __tablename__ = "projects"
    id       = Column(Integer, primary_key=True, autoincrement=True)
    name     = Column(Text, nullable=False, unique=True)
    expenses = relationship("Expense", back_populates="project")

class ReceiptCounter(Base):
    """Tracks the last used receipt number for auto-increment."""
    __tablename__ = "receipt_counter"
    id            = Column(Integer, primary_key=True, autoincrement=True)
    last_number   = Column(Integer, default=0, nullable=False)

class GstTransaction(Base):
    """Tracks output GST collected on sales/services rendered."""
    __tablename__ = "gst_transactions"
    id              = Column(Integer, primary_key=True, autoincrement=True)
    date            = Column(Date, nullable=False)
    project_id      = Column(Integer, ForeignKey("projects.id"))
    transaction_type = Column(Text)          # e.g. 'Affordable Housing', 'Commercial'
    base_value      = Column(Float, nullable=False)   # contract/sale value
    taxable_value   = Column(Float, nullable=False)   # after fraction applied
    gst_rate        = Column(Float, nullable=False)
    output_gst      = Column(Float, nullable=False)
    description     = Column(Text)
    project         = relationship("Project")

class Expense(Base):
    __tablename__ = "expenses"
    id             = Column(Integer, primary_key=True, autoincrement=True)
    date           = Column(Date, nullable=False)
    vendor_id      = Column(Integer, ForeignKey("vendors.id"))
    category_id    = Column(Integer, ForeignKey("categories.id"))
    project_id     = Column(Integer, ForeignKey("projects.id"))
    description    = Column(Text)
    gross_amount   = Column(Float, nullable=False)
    gst_amount     = Column(Float, default=0)
    payment_status = Column(
        Text,
        CheckConstraint("payment_status IN ('Pending','Paid')"),
    )
    invoice_path   = Column(Text)
    vendor         = relationship("Vendor",   back_populates="expenses")
    category       = relationship("Category", back_populates="expenses")
    project        = relationship("Project",  back_populates="expenses")


@st.cache_resource
def get_engine():
    engine = create_engine(f"sqlite:///{DB_PATH}", echo=False)
    Base.metadata.create_all(engine)
    _seed_categories(engine)
    _seed_projects(engine)
    _seed_receipt_counter(engine)
    return engine


def _seed_receipt_counter(engine):
    with Session(engine) as s:
        if not s.query(ReceiptCounter).first():
            s.add(ReceiptCounter(last_number=0))
            s.commit()


def _seed_categories(engine):
    with Session(engine) as s:
        for cat in DEFAULT_CATEGORIES:
            exists = s.query(Category).filter_by(name=cat).first()
            if not exists:
                s.add(Category(name=cat))
        s.commit()


def _seed_projects(engine):
    with Session(engine) as s:
        for proj in DEFAULT_PROJECTS:
            exists = s.query(Project).filter_by(name=proj).first()
            if not exists:
                s.add(Project(name=proj))
        s.commit()


# ─────────────────────────────────────────────
# 3. DATA-ACCESS HELPERS
# ─────────────────────────────────────────────

def get_vendors(engine):
    with Session(engine) as s:
        return s.query(Vendor).order_by(Vendor.name).all()


def get_categories(engine):
    with Session(engine) as s:
        return s.query(Category).order_by(Category.name).all()


def get_projects(engine):
    with Session(engine) as s:
        return s.query(Project).order_by(Project.id).all()


def add_vendor(engine, name, gst, contact):
    with Session(engine) as s:
        v = Vendor(name=name.strip(), gst_number=gst.strip(), contact_person=contact.strip())
        s.add(v)
        s.commit()


def add_expense(engine, exp_date, vendor_id, category_id, project_id, description,
                gross, gst, status, invoice_path):
    with Session(engine) as s:
        e = Expense(
            date=exp_date,
            vendor_id=vendor_id,
            category_id=category_id,
            project_id=project_id,
            description=description,
            gross_amount=gross,
            gst_amount=gst,
            payment_status=status,
            invoice_path=str(invoice_path) if invoice_path else None,
        )
        s.add(e)
        s.commit()


def get_expenses_df(engine) -> pd.DataFrame:
    query = """
        SELECT
            e.id,
            e.date,
            p.name          AS project,
            v.name          AS vendor,
            c.name          AS category,
            e.description,
            e.gross_amount,
            e.gst_amount,
            (e.gross_amount + e.gst_amount) AS total_amount,
            e.payment_status,
            e.invoice_path
        FROM expenses e
        LEFT JOIN vendors    v ON e.vendor_id   = v.id
        LEFT JOIN categories c ON e.category_id = c.id
        LEFT JOIN projects   p ON e.project_id  = p.id
        ORDER BY e.date DESC
    """
    with engine.connect() as conn:
        return pd.read_sql(text(query), conn)


def update_payment_status(engine, expense_id: int, new_status: str):
    with Session(engine) as s:
        exp = s.get(Expense, expense_id)
        if exp:
            exp.payment_status = new_status
            s.commit()


def delete_expense(engine, expense_id: int):
    with Session(engine) as s:
        exp = s.get(Expense, expense_id)
        if exp:
            s.delete(exp)
            s.commit()


# ─────────────────────────────────────────────
# 3b. GST CALCULATION ENGINE
# ─────────────────────────────────────────────

def calculate_output_gst(transaction_type: str, base_value: float) -> dict:
    """
    Apply 2026 real-estate GST rules and return a breakdown dict.
    """
    rule = GST_RULES.get(transaction_type)
    if not rule:
        return {}
    taxable_value = base_value * rule["on_fraction"]
    output_gst    = taxable_value * rule["rate"]
    return {
        "transaction_type": transaction_type,
        "base_value":       base_value,
        "taxable_value":    taxable_value,
        "gst_rate":         rule["rate"],
        "output_gst":       output_gst,
        "itc_eligible":     rule["itc"],
        "rule_label":       rule["label"],
    }


def add_gst_transaction(engine, txn_date, project_id, txn_type, base_value, description):
    calc = calculate_output_gst(txn_type, base_value)
    if not calc:
        return
    with Session(engine) as s:
        gt = GstTransaction(
            date=txn_date,
            project_id=project_id,
            transaction_type=txn_type,
            base_value=calc["base_value"],
            taxable_value=calc["taxable_value"],
            gst_rate=calc["gst_rate"],
            output_gst=calc["output_gst"],
            description=description,
        )
        s.add(gt)
        s.commit()


def get_gst_transactions_df(engine) -> pd.DataFrame:
    query = """
        SELECT
            g.id,
            g.date,
            p.name           AS project,
            g.transaction_type,
            g.base_value,
            g.taxable_value,
            g.gst_rate,
            g.output_gst,
            g.description
        FROM gst_transactions g
        LEFT JOIN projects p ON g.project_id = p.id
        ORDER BY g.date DESC
    """
    with engine.connect() as conn:
        return pd.read_sql(text(query), conn)


def delete_gst_transaction(engine, txn_id: int):
    with Session(engine) as s:
        gt = s.get(GstTransaction, txn_id)
        if gt:
            s.delete(gt)
            s.commit()


# ─────────────────────────────────────────────
# 3c. RECEIPT HELPERS
# ─────────────────────────────────────────────

def next_receipt_number(engine) -> str:
    """Generate next sequential receipt number e.g. AIRC-2026-0047"""
    with Session(engine) as s:
        counter = s.query(ReceiptCounter).first()
        counter.last_number += 1
        num = counter.last_number
        s.commit()
    year = datetime.now().year
    return f"AIRC-{year}-{num:04d}"


def amount_in_words(amount: float) -> str:
    """Convert a float rupee amount to Indian English words."""
    ones = ["", "One", "Two", "Three", "Four", "Five", "Six", "Seven",
            "Eight", "Nine", "Ten", "Eleven", "Twelve", "Thirteen",
            "Fourteen", "Fifteen", "Sixteen", "Seventeen", "Eighteen", "Nineteen"]
    tens = ["", "", "Twenty", "Thirty", "Forty", "Fifty",
            "Sixty", "Seventy", "Eighty", "Ninety"]

    def _two_digits(n):
        if n < 20:
            return ones[n]
        return tens[n // 10] + (" " + ones[n % 10] if n % 10 else "")

    def _three_digits(n):
        if n >= 100:
            return ones[n // 100] + " Hundred" + (" " + _two_digits(n % 100) if n % 100 else "")
        return _two_digits(n)

    rupees = int(amount)
    paise  = round((amount - rupees) * 100)

    if rupees == 0:
        words = "Zero"
    else:
        parts = []
        cr  = rupees // 10000000; rupees %= 10000000
        lac = rupees // 100000;   rupees %= 100000
        th  = rupees // 1000;     rupees %= 1000
        hun = rupees

        if cr:  parts.append(_three_digits(cr)  + " Crore")
        if lac: parts.append(_three_digits(lac) + " Lakh")
        if th:  parts.append(_three_digits(th)  + " Thousand")
        if hun: parts.append(_three_digits(hun))
        words = " ".join(parts)

    result = f"Rupees {words}"
    if paise:
        result += f" and {_two_digits(paise)} Paise"
    return result + " Only"


# ─────────────────────────────────────────────
# 4. FILE HELPERS
# ─────────────────────────────────────────────

def save_invoice(uploaded_file) -> Path | None:
    """Validate it's a PDF, save to /invoices, return path."""
    if uploaded_file is None:
        return None
    if not uploaded_file.name.lower().endswith(".pdf"):
        st.sidebar.error("⚠️ Only PDF files are accepted.")
        return None
    if uploaded_file.type not in ("application/pdf",):
        # extra MIME check
        if not uploaded_file.name.lower().endswith(".pdf"):
            st.sidebar.error("⚠️ Invalid file type. Please upload a PDF.")
            return None
    timestamp  = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_name  = uploaded_file.name.replace(" ", "_")
    dest       = INVOICE_DIR / f"{timestamp}_{safe_name}"
    with open(dest, "wb") as f:
        f.write(uploaded_file.getbuffer())
    return dest


def save_invoice_bytes(pdf_bytes: bytes, original_name: str) -> Path:
    """Save raw PDF bytes (from AI extraction flow) to /invoices/."""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_name = original_name.replace(" ", "_")
    dest      = INVOICE_DIR / f"{timestamp}_{safe_name}"
    with open(dest, "wb") as f:
        f.write(pdf_bytes)
    return dest


# ─────────────────────────────────────────────
# 4c. RECEIPT PDF GENERATOR
# ─────────────────────────────────────────────

def generate_receipt_pdf(
    receipt_no: str,
    receipt_date: date,
    payee_name: str,
    payee_contact: str,
    project: str,
    purpose: str,
    amount: float,
    payment_mode: str,
    category: str,
    notes: str,
    logo_path: str = "Arthav_Logo_File.jpg",
) -> bytes:
    """Generate a branded Arthav Infra LLP receipt PDF and return bytes."""
    from reportlab.lib.pagesizes import A4
    from reportlab.lib import colors
    from reportlab.lib.units import mm
    from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer,
                                    Table, TableStyle, HRFlowable, Image)
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.enums import TA_LEFT, TA_CENTER, TA_RIGHT
    from reportlab.pdfgen import canvas
    from reportlab.lib.utils import ImageReader

    # ── Colours matching brand ──────────────────────────────────
    NAVY   = colors.HexColor("#0d1b3e")
    GOLD   = colors.HexColor("#c9a84c")
    LGOLD  = colors.HexColor("#e2c07a")
    WHITE  = colors.white
    LGREY  = colors.HexColor("#f5f5f0")
    MGREY  = colors.HexColor("#cccccc")
    DGREY  = colors.HexColor("#444444")

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=18*mm, rightMargin=18*mm,
        topMargin=14*mm, bottomMargin=14*mm,
    )

    W = A4[0] - 36*mm   # usable width

    def style(size=10, bold=False, color=DGREY, align=TA_LEFT, leading=None):
        return ParagraphStyle(
            "s", fontSize=size, fontName="Helvetica-Bold" if bold else "Helvetica",
            textColor=color, alignment=align,
            leading=leading or size * 1.35,
        )

    story = []

    # ── Header bar ───────────────────────────────────────────────
    logo_img = None
    if Path(logo_path).exists():
        try:
            logo_img = Image(logo_path, width=18*mm, height=18*mm)
            logo_img.hAlign = "LEFT"
        except Exception:
            logo_img = None

    header_data = [[
        logo_img or Paragraph("", style()),
        Paragraph("ARTHAV INFRA LLP<br/>"
                  "<font size='8' color='#c9a84c'>Real Estate &amp; Construction</font>",
                  style(14, bold=True, color=WHITE, align=TA_LEFT)),
        Paragraph("PAYMENT RECEIPT",
                  style(18, bold=True, color=GOLD, align=TA_RIGHT)),
    ]]
    header_table = Table(header_data, colWidths=[22*mm, W*0.48, W*0.42])
    header_table.setStyle(TableStyle([
        ("BACKGROUND",  (0, 0), (-1, -1), NAVY),
        ("VALIGN",      (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING",(0, 0), (-1, -1), 8),
        ("TOPPADDING",  (0, 0), (-1, -1), 10),
        ("BOTTOMPADDING",(0,0), (-1, -1), 10),
        ("ROUNDEDCORNERS", (0, 0), (-1, -1), [6, 6, 0, 0]),
    ]))
    story.append(header_table)

    # ── Gold accent stripe ────────────────────────────────────────
    story.append(HRFlowable(width="100%", thickness=3, color=GOLD, spaceAfter=6))

    # ── Receipt meta row ─────────────────────────────────────────
    meta_data = [[
        Paragraph(f"<b>Receipt No:</b> {receipt_no}", style(10, color=NAVY)),
        Paragraph(f"<b>Date:</b> {receipt_date.strftime('%d %B %Y')}",
                  style(10, color=NAVY, align=TA_RIGHT)),
    ]]
    meta_table = Table(meta_data, colWidths=[W * 0.5, W * 0.5])
    meta_table.setStyle(TableStyle([
        ("BACKGROUND",   (0, 0), (-1, -1), LGREY),
        ("TOPPADDING",   (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING",(0, 0), (-1, -1), 8),
        ("LEFTPADDING",  (0, 0), (-1, -1), 10),
        ("RIGHTPADDING", (0, 0), (-1, -1), 10),
    ]))
    story.append(meta_table)
    story.append(Spacer(1, 6*mm))

    # ── Amount box ───────────────────────────────────────────────
    amt_data = [[
        Paragraph("AMOUNT PAID", style(9, color=GOLD, align=TA_CENTER)),
        Paragraph(f"₹ {amount:,.2f}", style(26, bold=True, color=WHITE, align=TA_CENTER)),
        Paragraph(amount_in_words(amount), style(9, color=LGOLD, align=TA_CENTER)),
    ]]
    amt_table = Table(amt_data, colWidths=[W])
    amt_table.setStyle(TableStyle([
        ("BACKGROUND",   (0, 0), (-1, -1), NAVY),
        ("TOPPADDING",   (0, 0), (-1, -1), 10),
        ("BOTTOMPADDING",(0, 0), (-1, -1), 10),
        ("ALIGN",        (0, 0), (-1, -1), "CENTER"),
        ("SPAN",         (0, 0), (-1, -1)),
    ]))
    # Use 3-row version instead
    amt_data2 = [
        [Paragraph("AMOUNT PAID", style(9, bold=True, color=GOLD, align=TA_CENTER))],
        [Paragraph(f"₹ {amount:,.2f}", style(28, bold=True, color=WHITE, align=TA_CENTER))],
        [Paragraph(amount_in_words(amount), style(9, color=LGOLD, align=TA_CENTER))],
    ]
    amt_table2 = Table(amt_data2, colWidths=[W])
    amt_table2.setStyle(TableStyle([
        ("BACKGROUND",    (0, 0), (-1, -1), NAVY),
        ("TOPPADDING",    (0, 0), (0, 0),   8),
        ("BOTTOMPADDING", (0, 2), (0, 2),   10),
        ("TOPPADDING",    (0, 1), (0, 2),   2),
        ("ALIGN",         (0, 0), (-1, -1), "CENTER"),
        ("ROUNDEDCORNERS",(0, 0), (-1, -1), [4, 4, 4, 4]),
    ]))
    story.append(amt_table2)
    story.append(Spacer(1, 6*mm))

    # ── Details table ─────────────────────────────────────────────
    def row(label, value):
        return [
            Paragraph(label, style(9, bold=True, color=NAVY)),
            Paragraph(str(value) if value else "—", style(9, color=DGREY)),
        ]

    details = [
        row("Received From",   payee_name),
        row("Contact",         payee_contact or "—"),
        row("Project",         project),
        row("Purpose",         purpose),
        row("Category",        category),
        row("Payment Mode",    payment_mode),
        row("Notes",           notes or "—"),
    ]
    det_table = Table(details, colWidths=[W * 0.28, W * 0.72])
    det_table.setStyle(TableStyle([
        ("BACKGROUND",    (0, 0), (0, -1), LGREY),
        ("BACKGROUND",    (1, 0), (1, -1), WHITE),
        ("ROWBACKGROUNDS",(1, 0), (1, -1), [WHITE, colors.HexColor("#fafaf7")]),
        ("TOPPADDING",    (0, 0), (-1, -1), 7),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
        ("LEFTPADDING",   (0, 0), (-1, -1), 10),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 10),
        ("LINEBELOW",     (0, 0), (-1, -2), 0.5, MGREY),
        ("BOX",           (0, 0), (-1, -1), 1,   MGREY),
    ]))
    story.append(det_table)
    story.append(Spacer(1, 10*mm))

    # ── Signature row ─────────────────────────────────────────────
    sig_data = [[
        Paragraph("Received By (Signature)\n\n\n___________________________\n"
                  "<font size='8' color='#888888'>Payee Signature &amp; Date</font>",
                  style(9, color=DGREY)),
        Paragraph("For Arthav Infra LLP\n\n\n___________________________\n"
                  "<font size='8' color='#888888'>Authorised Signatory</font>",
                  style(9, color=DGREY, align=TA_RIGHT)),
    ]]
    sig_table = Table(sig_data, colWidths=[W * 0.5, W * 0.5])
    sig_table.setStyle(TableStyle([
        ("TOPPADDING",   (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING",(0, 0), (-1, -1), 6),
        ("LEFTPADDING",  (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
    ]))
    story.append(sig_table)

    # ── Footer ────────────────────────────────────────────────────
    story.append(HRFlowable(width="100%", thickness=1, color=GOLD, spaceBefore=4))
    story.append(Paragraph(
        "Arthav Infra LLP · Hyderabad, Telangana · This is a computer-generated receipt",
        style(7, color=MGREY, align=TA_CENTER)
    ))

    doc.build(story)
    return buf.getvalue()


def df_to_excel_bytes(df: pd.DataFrame) -> bytes:
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Expenses")
    return buf.getvalue()


# ─────────────────────────────────────────────
# 4b. AI INVOICE EXTRACTION ENGINE
# ─────────────────────────────────────────────

EXTRACTION_PROMPT = """You are an expert accounting assistant for Arthav Infra LLP, a real estate company in Hyderabad, India.

Carefully read the attached invoice / order document and extract the following fields. 
Return ONLY a valid JSON object — no markdown fences, no explanation, just raw JSON.

Fields to extract:
{
  "vendor_name": "Name of the seller/supplier/vendor",
  "vendor_gst": "GST number of vendor if present, else null",
  "invoice_date": "Date in YYYY-MM-DD format. If only month+year, use the 1st of that month.",
  "invoice_number": "Invoice or order number if present, else null",
  "description": "Brief description of what was purchased (1 line)",
  "gross_amount": "Total amount before GST as a number (no commas, no currency symbol)",
  "gst_amount": "GST/tax amount as a number. If 'not a GST invoice' or no GST shown, use 0",
  "payment_method": "Payment method if mentioned (UPI, Cash, Bank Transfer, etc.), else null",
  "payment_status": "Paid or Pending — infer from context. Amazon orders = Paid. If invoice/bill with no payment confirmation = Pending",
  "suggested_category": "Pick ONE from: Operational, Utilities, Raw Materials, Marketing, Labour, Legal & Professional, Travel, Miscellaneous, Commercial, Construction Services",
  "suggested_project": "Pick ONE from: Axis Bank Shamshabad, Royal Oak Shamshabad, Chaitanya Chengicherla, Ayush Agarwal, Ashwin Agarwal, Other — infer from delivery address or context. If address mentions Ashwin Agarwal, pick Ashwin Agarwal. If Ayush, pick Ayush Agarwal. If unclear, pick Other.",
  "confidence_notes": "1-sentence note on anything uncertain or missing"
}

Important rules:
- gross_amount must be a plain float like 10014.0, never a string
- gst_amount must be a plain float, never a string  
- If the document says 'this is not a GST invoice', set gst_amount to 0
- Do not include ₹ or commas in numeric fields
"""

def extract_invoice_with_ai(pdf_bytes: bytes, api_key: str) -> dict | None:
    """
    Send PDF bytes to Claude claude-sonnet-4-20250514 via Anthropic API.
    Returns parsed dict of extracted fields, or None on failure.
    """
    if not api_key:
        st.error("⚠️ No API key provided.")
        return None

    b64_pdf = base64.standard_b64encode(pdf_bytes).decode("utf-8")

    payload = {
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 1000,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "document",
                        "source": {
                            "type": "base64",
                            "media_type": "application/pdf",
                            "data": b64_pdf,
                        },
                    },
                    {
                        "type": "text",
                        "text": EXTRACTION_PROMPT,
                    },
                ],
            }
        ],
    }

    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "anthropic-beta": "pdfs-2024-09-25",
                "content-type": "application/json",
            },
            json=payload,
            timeout=60,
        )
        if not resp.ok:
            st.error(f"API error {resp.status_code}: {resp.text}")
            return None
        resp.raise_for_status()
        raw_text = resp.json()["content"][0]["text"].strip()
        # Strip accidental markdown fences
        if raw_text.startswith("```"):
            raw_text = raw_text.split("```")[1]
            if raw_text.startswith("json"):
                raw_text = raw_text[4:]
        return json.loads(raw_text.strip())
    except requests.exceptions.RequestException as e:
        st.error(f"API request failed: {e}")
        return None
    except (json.JSONDecodeError, KeyError) as e:
        st.error(f"Failed to parse AI response: {e}")
        return None


# ─────────────────────────────────────────────
# 5. CUSTOM CSS
# ─────────────────────────────────────────────

def inject_css():
    st.markdown("""
    <style>
    @import url('https://fonts.googleapis.com/css2?family=Syne:wght@400;600;700;800&family=DM+Mono:wght@400;500&display=swap');

    html, body, [class*="css"] {
        font-family: 'Syne', sans-serif;
    }

    /* ── background ── */
    .stApp {
        background: #0a1628;
        color: #e8e6df;
    }

    /* ── sidebar ── */
    section[data-testid="stSidebar"] {
        background: #0d1d35 !important;
        border-right: 1px solid #1e3050;
    }
    section[data-testid="stSidebar"] * {
        color: #c9c6be !important;
    }

    /* ── metric cards ── */
    .metric-card {
        background: linear-gradient(135deg, #0f2040 0%, #0a1628 100%);
        border: 1px solid #1e3050;
        border-radius: 12px;
        padding: 24px 28px;
        position: relative;
        overflow: hidden;
    }
    .metric-card::before {
        content: '';
        position: absolute;
        top: 0; left: 0; right: 0;
        height: 2px;
    }
    .metric-card.green::before  { background: linear-gradient(90deg, #c9a84c, #e2c07a); }
    .metric-card.amber::before  { background: linear-gradient(90deg, #c9a84c, #a07830); }
    .metric-card.red::before    { background: linear-gradient(90deg, #ff5e62, #d63031); }

    .metric-label {
        font-size: 11px;
        font-weight: 600;
        letter-spacing: 2px;
        text-transform: uppercase;
        color: #7a8aaa !important;
        margin-bottom: 8px;
    }
    .metric-value {
        font-family: 'DM Mono', monospace;
        font-size: 28px;
        font-weight: 500;
        color: #f0ede6 !important;
        line-height: 1.1;
    }
    .metric-card.green .metric-value { color: #c9a84c !important; }
    .metric-card.amber .metric-value { color: #e2c07a !important; }
    .metric-card.red   .metric-value { color: #ff5e62 !important; }

    /* ── page title ── */
    .page-header {
        display: flex;
        align-items: center;
        gap: 18px;
        margin-bottom: 4px;
    }
    .page-title {
        font-size: 30px;
        font-weight: 800;
        color: #c9a84c;
        letter-spacing: 1px;
    }
    .page-subtitle {
        font-size: 13px;
        color: #7a8aaa;
        letter-spacing: 2px;
        text-transform: uppercase;
    }

    /* ── section headers ── */
    .section-header {
        font-size: 12px;
        font-weight: 700;
        letter-spacing: 2.5px;
        text-transform: uppercase;
        color: #7a8aaa;
        padding: 20px 0 10px;
        border-bottom: 1px solid #1e3050;
        margin-bottom: 16px;
    }

    /* ── dataframe tweaks ── */
    .stDataFrame { border-radius: 10px; overflow: hidden; }
    iframe { border-radius: 10px !important; }

    /* ── buttons ── */
    .stButton > button {
        background: linear-gradient(135deg, #c9a84c, #a07830) !important;
        color: #0a1628 !important;
        border: none !important;
        border-radius: 8px !important;
        font-family: 'Syne', sans-serif !important;
        font-weight: 700 !important;
        letter-spacing: 0.5px !important;
        padding: 0.5rem 1.4rem !important;
        transition: opacity 0.2s !important;
    }
    .stButton > button:hover { opacity: 0.85 !important; }

    /* ── inputs ── */
    .stTextInput > div > div > input,
    .stNumberInput > div > div > input,
    .stSelectbox > div > div,
    .stDateInput > div > div > input {
        background: #0f2040 !important;
        border: 1px solid #1e3050 !important;
        border-radius: 8px !important;
        color: #e8e6df !important;
    }

    /* ── divider ── */
    hr { border-color: #1e3050 !important; }

    /* ── status badges ── */
    .badge-paid    { background:#c9a84c22; color:#c9a84c; padding:2px 10px; border-radius:99px; font-size:12px; font-weight:600; }
    .badge-pending { background:#ff5e6222; color:#ff5e62; padding:2px 10px; border-radius:99px; font-size:12px; font-weight:600; }

    /* ── GST card variants ── */
    .metric-card.blue::before   { background: linear-gradient(90deg, #4e9eff, #2979e8); }
    .metric-card.blue .metric-value { color: #4e9eff !important; }
    .metric-card.purple::before { background: linear-gradient(90deg, #b06aff, #8b3dff); }
    .metric-card.purple .metric-value { color: #b06aff !important; }
    .metric-card.teal::before   { background: linear-gradient(90deg, #c9a84c, #e2c07a); }
    .metric-card.teal .metric-value  { color: #c9a84c !important; }

    /* ── GST rule chip ── */
    .gst-chip {
        display: inline-block;
        background: #0f2040;
        border: 1px solid #1e3050;
        border-radius: 6px;
        padding: 6px 14px;
        margin: 4px;
        font-size: 13px;
        font-family: 'DM Mono', monospace;
        color: #c9c6be;
    }
    .gst-chip .chip-rate { font-size: 18px; font-weight: 600; color: #c9a84c; }
    .gst-chip .chip-label { font-size: 10px; color: #7a8aaa; letter-spacing: 1px; text-transform: uppercase; }

    /* ── tab bar ── */
    .stTabs [data-baseweb="tab-list"] {
        background: #0d1d35;
        border-radius: 10px;
        gap: 4px;
        padding: 4px;
    }
    .stTabs [data-baseweb="tab"] {
        border-radius: 8px !important;
        color: #7a8aaa !important;
        font-weight: 600 !important;
    }
    .stTabs [aria-selected="true"] {
        background: #1e3050 !important;
        color: #c9a84c !important;
    }

    /* ── logo header bar ── */
    .logo-header {
        display: flex;
        align-items: center;
        gap: 20px;
        padding: 10px 0 16px;
        border-bottom: 1px solid #1e3050;
        margin-bottom: 20px;
    }
    .logo-header img {
        height: 64px;
        width: 64px;
        border-radius: 10px;
        object-fit: cover;
    }
    .logo-text-block { display: flex; flex-direction: column; }
    .logo-title { font-size: 26px; font-weight: 800; color: #c9a84c; letter-spacing: 1px; }
    .logo-sub   { font-size: 12px; color: #7a8aaa; letter-spacing: 2.5px; text-transform: uppercase; margin-top: 2px; }
    </style>
    """, unsafe_allow_html=True)


# ─────────────────────────────────────────────
# 6. UI COMPONENTS
# ─────────────────────────────────────────────

def get_logo_base64() -> str:
    logo_path = Path("Arthav_Logo_File.jpg")
    if logo_path.exists():
        with open(logo_path, "rb") as f:
            return base64.b64encode(f.read()).decode()
    return ""


def render_header():
    logo_b64 = get_logo_base64()
    if logo_b64:
        img_tag = f'<img src="data:image/jpeg;base64,{logo_b64}" />'
    else:
        img_tag = '<div style="width:64px;height:64px;background:#1e3050;border-radius:10px;display:flex;align-items:center;justify-content:center;font-size:28px;">🏗️</div>'

    st.markdown(f"""
    <div class="logo-header">
        {img_tag}
        <div class="logo-text-block">
            <div class="logo-title">ARTHAV INFRA LLP</div>
            <div class="logo-sub">Expense &amp; Invoice Tracker</div>
        </div>
    </div>
    """, unsafe_allow_html=True)


def render_summary_cards(df: pd.DataFrame):
    total_gross   = df["gross_amount"].sum() if not df.empty else 0
    total_gst     = df["gst_amount"].sum()   if not df.empty else 0
    total_pending = df[df["payment_status"] == "Pending"]["total_amount"].sum() if not df.empty else 0

    c1, c2, c3 = st.columns(3)
    with c1:
        st.markdown(f"""
        <div class="metric-card green">
            <div class="metric-label">Total Gross Spend</div>
            <div class="metric-value">₹{total_gross:,.2f}</div>
        </div>""", unsafe_allow_html=True)
    with c2:
        st.markdown(f"""
        <div class="metric-card amber">
            <div class="metric-label">Total GST</div>
            <div class="metric-value">₹{total_gst:,.2f}</div>
        </div>""", unsafe_allow_html=True)
    with c3:
        st.markdown(f"""
        <div class="metric-card red">
            <div class="metric-label">Outstanding (Pending)</div>
            <div class="metric-value">₹{total_pending:,.2f}</div>
        </div>""", unsafe_allow_html=True)
    st.markdown("<br>", unsafe_allow_html=True)


def render_sidebar_add_expense(engine):
    # Sidebar logo
    logo_b64 = get_logo_base64()
    if logo_b64:
        st.sidebar.markdown(f"""
        <div style="text-align:center; padding: 10px 0 16px;">
            <img src="data:image/jpeg;base64,{logo_b64}"
                 style="width:80px;height:80px;border-radius:12px;object-fit:cover;" />
        </div>
        """, unsafe_allow_html=True)
    st.sidebar.markdown("## ➕ Add Expense")
    vendors    = get_vendors(engine)
    categories = get_categories(engine)
    projects   = get_projects(engine)

    vendor_map   = {v.name: v.id for v in vendors}
    category_map = {c.name: c.id for c in categories}
    project_map  = {p.name: p.id for p in projects}

    with st.sidebar.form("add_expense_form", clear_on_submit=True):
        exp_date     = st.date_input("Date", value=date.today())
        project_name = st.selectbox("Project", list(project_map.keys()))
        vendor_name  = st.selectbox("Vendor", ["— select —"] + list(vendor_map.keys()))
        category     = st.selectbox("Category", list(category_map.keys()))
        description  = st.text_input("Description")
        gross        = st.number_input("Gross Amount (₹)", min_value=0.0, step=100.0)
        gst          = st.number_input("GST Amount (₹)",   min_value=0.0, step=10.0)
        status       = st.radio("Payment Status", ["Pending", "Paid"], horizontal=True)
        pdf_file     = st.file_uploader("Attach Invoice (PDF)", type=["pdf"])
        submitted    = st.form_submit_button("Save Expense")

        if submitted:
            if gross <= 0:
                st.sidebar.error("Gross amount must be > 0")
            else:
                vendor_id   = vendor_map.get(vendor_name)
                category_id = category_map.get(category)
                project_id  = project_map.get(project_name)
                inv_path    = save_invoice(pdf_file)
                add_expense(engine, exp_date, vendor_id, category_id, project_id,
                            description, gross, gst, status, inv_path)
                st.success("✅ Expense saved!")
                st.rerun()


def render_sidebar_add_vendor(engine):
    st.sidebar.markdown("---")
    st.sidebar.markdown("## 🏢 Add Vendor")
    with st.sidebar.form("add_vendor_form", clear_on_submit=True):
        name    = st.text_input("Vendor Name *")
        gst_no  = st.text_input("GST Number")
        contact = st.text_input("Contact Person")
        sub     = st.form_submit_button("Add Vendor")
        if sub:
            if not name.strip():
                st.sidebar.error("Vendor name is required.")
            else:
                add_vendor(engine, name, gst_no, contact)
                st.sidebar.success(f"✅ Vendor '{name}' added!")
                st.rerun()


def render_sidebar_export(df: pd.DataFrame):
    st.sidebar.markdown("---")
    st.sidebar.markdown("## 📤 Export Data")
    if df.empty:
        st.sidebar.info("No data to export yet.")
        return

    col1, col2 = st.sidebar.columns(2)
    with col1:
        csv_bytes = df.to_csv(index=False).encode("utf-8")
        st.download_button("⬇ CSV", data=csv_bytes,
                           file_name="arthav_expenses.csv", mime="text/csv")
    with col2:
        xl_bytes = df_to_excel_bytes(df)
        st.download_button("⬇ Excel", data=xl_bytes,
                           file_name="arthav_expenses.xlsx",
                           mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


def render_accounting_table(df: pd.DataFrame, engine):
    st.markdown('<div class="section-header">Ledger</div>', unsafe_allow_html=True)

    if df.empty:
        st.info("No expenses recorded yet. Use the sidebar to add your first entry.")
        return

    # ── Filters row ──────────────────────────────────────────────
    fc1, fc2, fc3, fc4 = st.columns([2, 2, 2, 2])
    with fc1:
        status_filter = st.selectbox("Filter by Status", ["All", "Paid", "Pending"])
    with fc2:
        proj_options  = ["All"] + sorted(df["project"].dropna().unique().tolist())
        proj_filter   = st.selectbox("Filter by Project", proj_options)
    with fc3:
        cat_options = ["All"] + sorted(df["category"].dropna().unique().tolist())
        cat_filter  = st.selectbox("Filter by Category", cat_options)
    with fc4:
        vendor_options = ["All"] + sorted(df["vendor"].dropna().unique().tolist())
        vendor_filter  = st.selectbox("Filter by Vendor", vendor_options)

    view = df.copy()
    if status_filter != "All":
        view = view[view["payment_status"] == status_filter]
    if proj_filter != "All":
        view = view[view["project"] == proj_filter]
    if cat_filter != "All":
        view = view[view["category"] == cat_filter]
    if vendor_filter != "All":
        view = view[view["vendor"] == vendor_filter]

    # ── Display columns ──────────────────────────────────────────
    display_cols = ["id", "date", "project", "vendor", "category", "description",
                    "gross_amount", "gst_amount", "total_amount",
                    "payment_status", "invoice_path"]
    st.dataframe(
        view[display_cols].rename(columns={
            "id": "ID", "date": "Date", "project": "Project",
            "vendor": "Vendor", "category": "Category",
            "description": "Description",
            "gross_amount": "Gross (₹)", "gst_amount": "GST (₹)",
            "total_amount": "Total (₹)", "payment_status": "Status",
            "invoice_path": "Invoice"
        }),
        use_container_width=True,
        height=420,
    )

    # ── Quick actions ─────────────────────────────────────────────
    st.markdown('<div class="section-header">Quick Actions</div>', unsafe_allow_html=True)
    qa1, qa2, qa3 = st.columns([1.5, 1.5, 1])
    with qa1:
        mark_id = st.number_input("Expense ID to mark Paid", min_value=1, step=1, key="mark_id")
        if st.button("Mark as Paid"):
            update_payment_status(engine, int(mark_id), "Paid")
            st.success(f"Expense #{mark_id} marked as Paid.")
            st.rerun()
    with qa2:
        del_id = st.number_input("Expense ID to delete", min_value=1, step=1, key="del_id")
        if st.button("🗑 Delete", type="secondary"):
            delete_expense(engine, int(del_id))
            st.warning(f"Expense #{del_id} deleted.")
            st.rerun()


def render_analytics_tab(df: pd.DataFrame):
    st.markdown('<div class="section-header">Spend Analytics</div>', unsafe_allow_html=True)
    if df.empty:
        st.info("Add some expenses to see analytics.")
        return

    ac1, ac2 = st.columns(2)
    with ac1:
        st.markdown("**Spend by Project**")
        proj_spend = df.groupby("project")["gross_amount"].sum().sort_values(ascending=False)
        st.bar_chart(proj_spend)

    with ac2:
        st.markdown("**Spend by Category**")
        cat_spend = df.groupby("category")["gross_amount"].sum().sort_values(ascending=False)
        st.bar_chart(cat_spend)

    ac3, ac4 = st.columns(2)
    with ac3:
        st.markdown("**Spend by Vendor**")
        vendor_spend = df.groupby("vendor")["gross_amount"].sum().sort_values(ascending=False).head(10)
        st.bar_chart(vendor_spend)

    with ac4:
        st.markdown("**Monthly Trend**")
        df["month"] = pd.to_datetime(df["date"]).dt.to_period("M").astype(str)
        monthly = df.groupby("month")[["gross_amount","gst_amount"]].sum()
        st.bar_chart(monthly)


def render_vendors_tab(engine):
    st.markdown('<div class="section-header">Vendor Directory</div>', unsafe_allow_html=True)
    vendors = get_vendors(engine)
    if not vendors:
        st.info("No vendors added yet.")
        return
    vdf = pd.DataFrame([{
        "ID": v.id, "Name": v.name,
        "GST Number": v.gst_number or "—",
        "Contact": v.contact_person or "—"
    } for v in vendors])
    st.dataframe(vdf, use_container_width=True, hide_index=True)


def render_sidebar_log_gst(engine):
    """Sidebar form to log an output GST transaction (sale / service rendered)."""
    st.sidebar.markdown("---")
    st.sidebar.markdown("## 🧾 Log Output GST")
    projects    = get_projects(engine)
    project_map = {p.name: p.id for p in projects}

    with st.sidebar.form("gst_txn_form", clear_on_submit=True):
        txn_date    = st.date_input("Transaction Date", value=date.today(), key="gst_date")
        proj_name   = st.selectbox("Project", list(project_map.keys()), key="gst_proj")
        txn_type    = st.selectbox("Transaction Type", list(GST_RULES.keys()), key="gst_type")
        base_value  = st.number_input("Contract / Sale Value (₹)", min_value=0.0, step=1000.0, key="gst_base")
        description = st.text_input("Description", key="gst_desc")
        preview_btn = st.form_submit_button("Calculate & Save")

        if preview_btn:
            if base_value <= 0:
                st.sidebar.error("Value must be > 0")
            else:
                calc = calculate_output_gst(txn_type, base_value)
                add_gst_transaction(engine, txn_date, project_map[proj_name],
                                    txn_type, base_value, description)
                st.sidebar.success(
                    f"✅ Output GST ₹{calc['output_gst']:,.2f} logged "
                    f"({calc['rule_label']})"
                )
                st.rerun()


def render_gst_tab(df: pd.DataFrame, engine):
    """Full Tax Dashboard page."""
    gst_df = get_gst_transactions_df(engine)
    projects = get_projects(engine)
    proj_names = ["All Projects"] + [p.name for p in projects]

    # ── GST Rule Reference Card ──────────────────────────────────
    st.markdown('<div class="section-header">2026 Real Estate GST Rate Reference</div>',
                unsafe_allow_html=True)
    chips_html = ""
    for txn_type, rule in GST_RULES.items():
        itc_badge = "✅ ITC" if rule["itc"] else "❌ No ITC"
        chips_html += f"""
        <div class="gst-chip">
            <div class="chip-rate">{int(rule['rate']*100)}%</div>
            <div>{txn_type}</div>
            <div class="chip-label">{rule['label']} · {itc_badge}</div>
        </div>"""
    st.markdown(chips_html, unsafe_allow_html=True)
    st.markdown("<br>", unsafe_allow_html=True)

    # ── Project Filter ───────────────────────────────────────────
    st.markdown('<div class="section-header">Tax Position</div>', unsafe_allow_html=True)
    selected_proj = st.selectbox("Filter by Project", proj_names, key="gst_proj_filter")

    # Apply project filter
    exp_view = df.copy()
    gst_view = gst_df.copy()
    if selected_proj != "All Projects":
        exp_view = exp_view[exp_view["project"] == selected_proj]
        gst_view = gst_view[gst_view["project"] == selected_proj]

    # ── ITC Calculation ──────────────────────────────────────────
    itc_df = exp_view[exp_view["category"].isin(ITC_ELIGIBLE_CATEGORIES)] if not exp_view.empty else pd.DataFrame()
    total_itc         = itc_df["gst_amount"].sum()        if not itc_df.empty else 0.0
    total_output_gst  = gst_view["output_gst"].sum()      if not gst_view.empty else 0.0
    net_gst_payable   = max(0.0, total_output_gst - total_itc)
    itc_surplus       = max(0.0, total_itc - total_output_gst)

    # ── Summary Cards ────────────────────────────────────────────
    c1, c2, c3, c4 = st.columns(4)
    cards = [
        (c1, "blue",   "Input Tax Credit (ITC)", total_itc),
        (c2, "amber",  "Output GST Liability",   total_output_gst),
        (c3, "red",    "Net GST Payable",         net_gst_payable),
        (c4, "green",  "ITC Surplus / Carry-fwd", itc_surplus),
    ]
    for col, colour, label, value in cards:
        with col:
            st.markdown(f"""
            <div class="metric-card {colour}">
                <div class="metric-label">{label}</div>
                <div class="metric-value">₹{value:,.2f}</div>
            </div>""", unsafe_allow_html=True)

    st.markdown("<br>", unsafe_allow_html=True)

    # ── GST Calculator Widget ─────────────────────────────────────
    with st.expander("🧮 Quick GST Calculator"):
        qc1, qc2 = st.columns(2)
        with qc1:
            calc_type  = st.selectbox("Transaction Type", list(GST_RULES.keys()), key="qc_type")
            calc_value = st.number_input("Contract Value (₹)", min_value=0.0, step=10000.0, key="qc_val")
        if calc_value > 0:
            result = calculate_output_gst(calc_type, calc_value)
            with qc2:
                st.markdown(f"""
                | Field | Value |
                |---|---|
                | Contract Value | ₹{result['base_value']:,.2f} |
                | Taxable Value ({int(result['gst_rate']*100)}% base) | ₹{result['taxable_value']:,.2f} |
                | GST Rate | {int(result['gst_rate']*100)}% |
                | **Output GST** | **₹{result['output_gst']:,.2f}** |
                | ITC Eligible | {'✅ Yes' if result['itc_eligible'] else '❌ No'} |
                | Rule | {result['rule_label']} |
                """)

    st.markdown("<br>", unsafe_allow_html=True)

    # ── Charts Row 1 ─────────────────────────────────────────────
    st.markdown('<div class="section-header">Visualisations</div>', unsafe_allow_html=True)
    ch1, ch2 = st.columns(2)

    with ch1:
        st.markdown("**GST Paid by Category (ITC View)**")
        if not exp_view.empty and exp_view["gst_amount"].sum() > 0:
            cat_gst = (
                exp_view[exp_view["gst_amount"] > 0]
                .groupby("category")["gst_amount"]
                .sum()
                .reset_index()
            )
            fig_pie = px.pie(
                cat_gst, values="gst_amount", names="category",
                color_discrete_sequence=px.colors.sequential.Teal,
                hole=0.45,
            )
            fig_pie.update_layout(
                paper_bgcolor="rgba(0,0,0,0)",
                plot_bgcolor="rgba(0,0,0,0)",
                font_color="#c9c6be",
                legend=dict(font=dict(color="#c9c6be")),
                margin=dict(t=20, b=20, l=10, r=10),
            )
            fig_pie.update_traces(textfont_color="#fff")
            st.plotly_chart(fig_pie, use_container_width=True)
        else:
            st.info("No GST data for categories yet.")

    with ch2:
        st.markdown("**Monthly ITC Accumulated**")
        if not itc_df.empty and "date" in itc_df.columns:
            itc_monthly = itc_df.copy()
            itc_monthly["month"] = pd.to_datetime(itc_monthly["date"]).dt.to_period("M").astype(str)
            itc_monthly = itc_monthly.groupby("month")["gst_amount"].sum().reset_index()
            fig_bar = px.bar(
                itc_monthly, x="month", y="gst_amount",
                labels={"month": "Month", "gst_amount": "ITC (₹)"},
                color_discrete_sequence=["#00c07f"],
            )
            fig_bar.update_layout(
                paper_bgcolor="rgba(0,0,0,0)",
                plot_bgcolor="rgba(0,0,0,0)",
                font_color="#c9c6be",
                xaxis=dict(gridcolor="#1e2230", tickfont=dict(color="#6b7080")),
                yaxis=dict(gridcolor="#1e2230", tickfont=dict(color="#6b7080")),
                margin=dict(t=20, b=20, l=10, r=10),
            )
            st.plotly_chart(fig_bar, use_container_width=True)
        else:
            st.info("No ITC-eligible expenses yet.")

    # ── Charts Row 2 ─────────────────────────────────────────────
    ch3, ch4 = st.columns(2)

    with ch3:
        st.markdown("**ITC vs Output GST — Monthly Comparison**")
        if not gst_view.empty or not itc_df.empty:
            # Build a unified monthly view
            if not itc_df.empty:
                itc_m = itc_df.copy()
                itc_m["month"] = pd.to_datetime(itc_m["date"]).dt.to_period("M").astype(str)
                itc_m = itc_m.groupby("month")["gst_amount"].sum().rename("ITC")
            else:
                itc_m = pd.Series(dtype=float, name="ITC")

            if not gst_view.empty:
                out_m = gst_view.copy()
                out_m["month"] = pd.to_datetime(out_m["date"]).dt.to_period("M").astype(str)
                out_m = out_m.groupby("month")["output_gst"].sum().rename("Output GST")
            else:
                out_m = pd.Series(dtype=float, name="Output GST")

            combined = pd.concat([itc_m, out_m], axis=1).fillna(0).reset_index()
            combined.columns = ["Month", "ITC", "Output GST"]
            fig_comp = go.Figure()
            fig_comp.add_bar(x=combined["Month"], y=combined["ITC"],
                             name="ITC", marker_color="#00c07f")
            fig_comp.add_bar(x=combined["Month"], y=combined["Output GST"],
                             name="Output GST", marker_color="#f5a623")
            fig_comp.update_layout(
                barmode="group",
                paper_bgcolor="rgba(0,0,0,0)",
                plot_bgcolor="rgba(0,0,0,0)",
                font_color="#c9c6be",
                xaxis=dict(gridcolor="#1e2230"),
                yaxis=dict(gridcolor="#1e2230"),
                legend=dict(font=dict(color="#c9c6be")),
                margin=dict(t=20, b=20, l=10, r=10),
            )
            st.plotly_chart(fig_comp, use_container_width=True)
        else:
            st.info("No data to compare yet.")

    with ch4:
        st.markdown("**Project-wise GST Exposure**")
        if not exp_view.empty:
            proj_gst = exp_view.groupby("project")["gst_amount"].sum().reset_index()
            fig_proj = px.bar(
                proj_gst, x="project", y="gst_amount",
                labels={"project": "Project", "gst_amount": "GST Paid (₹)"},
                color="gst_amount",
                color_continuous_scale="Teal",
            )
            fig_proj.update_layout(
                paper_bgcolor="rgba(0,0,0,0)",
                plot_bgcolor="rgba(0,0,0,0)",
                font_color="#c9c6be",
                xaxis=dict(gridcolor="#1e2230", tickangle=-20),
                yaxis=dict(gridcolor="#1e2230"),
                coloraxis_showscale=False,
                margin=dict(t=20, b=60, l=10, r=10),
            )
            st.plotly_chart(fig_proj, use_container_width=True)
        else:
            st.info("No project data available.")

    # ── Output GST Ledger ─────────────────────────────────────────
    st.markdown('<div class="section-header">Output GST Transactions Ledger</div>',
                unsafe_allow_html=True)
    if gst_view.empty:
        st.info("No output GST transactions logged yet. Use '🧾 Log Output GST' in the sidebar.")
    else:
        st.dataframe(
            gst_view.rename(columns={
                "id": "ID", "date": "Date", "project": "Project",
                "transaction_type": "Type",
                "base_value": "Contract Value (₹)", "taxable_value": "Taxable Value (₹)",
                "gst_rate": "Rate", "output_gst": "Output GST (₹)",
                "description": "Description",
            }),
            use_container_width=True, height=300,
        )
        del_col1, _ = st.columns([1, 3])
        with del_col1:
            del_gst_id = st.number_input("GST Txn ID to delete", min_value=1, step=1, key="del_gst")
            if st.button("🗑 Delete GST Entry"):
                delete_gst_transaction(engine, int(del_gst_id))
                st.warning(f"GST transaction #{del_gst_id} deleted.")
                st.rerun()

    # ── ITC Detail Table ──────────────────────────────────────────
    st.markdown('<div class="section-header">ITC-Eligible Expenses Detail</div>',
                unsafe_allow_html=True)
    if itc_df.empty:
        st.info("No ITC-eligible expenses found.")
    else:
        itc_display = itc_df[["date", "project", "vendor", "category",
                               "description", "gross_amount", "gst_amount"]].rename(columns={
            "date": "Date", "project": "Project", "vendor": "Vendor",
            "category": "Category", "description": "Description",
            "gross_amount": "Gross (₹)", "gst_amount": "GST / ITC (₹)",
        })
        st.dataframe(itc_display, use_container_width=True, height=280)


def render_invoice_scanner_tab(engine):
    """AI-powered invoice scanner — upload PDF, review extracted fields, save."""

    st.markdown('<div class="section-header">AI Invoice Scanner</div>', unsafe_allow_html=True)

    st.markdown("""
    <div style="background:#1a1e28; border:1px solid #2a2e3a; border-radius:10px;
                padding:16px 20px; margin-bottom:20px; border-left: 3px solid #00c07f;">
        <strong style="color:#00e5a0;">How it works:</strong>
        <span style="color:#c9c6be; font-size:14px;">
        Upload any invoice PDF → Claude reads it → fields are pre-filled →
        you review & confirm → expense is saved to the database.
        </span>
    </div>
    """, unsafe_allow_html=True)

    # ── API key check ─────────────────────────────────────────────
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")

    if not api_key:
        st.markdown('<div class="section-header">API Key Setup</div>', unsafe_allow_html=True)
        api_key_input = st.text_input(
            "Enter your Anthropic API Key",
            type="password",
            placeholder="sk-ant-api03-...",
            help="Get your key from https://console.anthropic.com/",
            key="anthropic_key_input"
        )
        if api_key_input:
            st.session_state["anthropic_api_key"] = api_key_input
            api_key = api_key_input
        elif "anthropic_api_key" in st.session_state:
            api_key = st.session_state["anthropic_api_key"]

    if not api_key:
        st.info("👆 Enter your Anthropic API key above to enable AI invoice extraction. "
                "Get one free at [console.anthropic.com](https://console.anthropic.com/)")
        return

    # ── Upload zone ───────────────────────────────────────────────
    uploaded = st.file_uploader(
        "Drop your invoice PDF here",
        type=["pdf"],
        key="ai_invoice_uploader",
        help="Supports GST invoices, Amazon orders, vendor bills, contractor receipts"
    )

    if uploaded is None:
        st.info("👆 Upload a PDF invoice above to begin AI extraction.")
        return

    pdf_bytes = uploaded.read()

    col_btn, col_info = st.columns([1, 3])
    with col_btn:
        extract_clicked = st.button("🤖 Extract with AI", use_container_width=True)
    with col_info:
        st.markdown(f"<span style='color:#6b7080; font-size:13px;'>📄 {uploaded.name} "
                    f"&nbsp;·&nbsp; {len(pdf_bytes)/1024:.1f} KB</span>",
                    unsafe_allow_html=True)

    # ── Run extraction ────────────────────────────────────────────
    if extract_clicked:
        with st.spinner("🔍 Claude is reading your invoice..."):
            result = extract_invoice_with_ai(pdf_bytes, api_key)
        if result:
            st.session_state["ai_extracted"] = result
            st.session_state["ai_pdf_bytes"] = pdf_bytes
            st.session_state["ai_pdf_name"]  = uploaded.name
            st.success("✅ Extraction complete! Review the fields below and confirm.")
        else:
            st.error("Extraction failed. Please check the error above.")
            return

    # ── Show editable pre-filled form ─────────────────────────────
    if "ai_extracted" not in st.session_state:
        return

    ex = st.session_state["ai_extracted"]

    st.markdown('<div class="section-header">Extracted Data — Review & Confirm</div>',
                unsafe_allow_html=True)

    if ex.get("confidence_notes"):
        st.info(f"💬 AI note: {ex['confidence_notes']}")

    vendors    = get_vendors(engine)
    categories = get_categories(engine)
    projects   = get_projects(engine)
    vendor_names   = [v.name for v in vendors]
    category_names = [c.name for c in categories]
    project_names  = [p.name for p in projects]
    vendor_map     = {v.name: v.id for v in vendors}
    category_map   = {c.name: c.id for c in categories}
    project_map    = {p.name: p.id for p in projects}

    # Parse date safely
    try:
        parsed_date = date.fromisoformat(ex.get("invoice_date", ""))
    except Exception:
        parsed_date = date.today()

    # Safe index helpers
    def safe_idx(lst, val, fallback=0):
        try:
            return lst.index(val) if val in lst else fallback
        except Exception:
            return fallback

    c1, c2 = st.columns(2)
    with c1:
        st.markdown("**📋 Invoice Details**")

        # Vendor — show extracted name but allow selecting existing or note as new
        ai_vendor = ex.get("vendor_name", "")
        vendor_options = ["➕ Add as new vendor"] + vendor_names
        vendor_default = safe_idx(vendor_options, ai_vendor, 0)
        chosen_vendor_opt = st.selectbox("Vendor", vendor_options,
                                          index=vendor_default, key="ai_vendor_sel")
        if chosen_vendor_opt == "➕ Add as new vendor":
            new_vendor_name = st.text_input("New Vendor Name", value=ai_vendor, key="ai_new_vendor")
            new_vendor_gst  = st.text_input("New Vendor GST (optional)",
                                             value=ex.get("vendor_gst") or "", key="ai_new_gst")
        else:
            new_vendor_name = None
            new_vendor_gst  = None

        exp_date = st.date_input("Date", value=parsed_date, key="ai_date")

        desc = st.text_input("Description",
                              value=ex.get("description", ""), key="ai_desc")
        invoice_num = ex.get("invoice_number") or ""
        if invoice_num:
            st.text_input("Invoice / Order Number", value=invoice_num,
                          disabled=True, key="ai_invnum")

    with c2:
        st.markdown("**💰 Amounts & Classification**")

        gross = st.number_input("Gross Amount (₹)",
                                 value=float(ex.get("gross_amount", 0) or 0),
                                 min_value=0.0, step=100.0, key="ai_gross")
        gst   = st.number_input("GST Amount (₹)",
                                  value=float(ex.get("gst_amount", 0) or 0),
                                  min_value=0.0, step=10.0, key="ai_gst")

        ai_cat     = ex.get("suggested_category", "Miscellaneous")
        cat_idx    = safe_idx(category_names, ai_cat, 0)
        chosen_cat = st.selectbox("Category", category_names,
                                   index=cat_idx, key="ai_cat")

        ai_proj     = ex.get("suggested_project", "Other")
        proj_idx    = safe_idx(project_names, ai_proj, 0)
        chosen_proj = st.selectbox("Project", project_names,
                                    index=proj_idx, key="ai_proj")

        ai_status   = ex.get("payment_status", "Pending")
        status_opts = ["Paid", "Pending"]
        stat_idx    = safe_idx(status_opts, ai_status, 1)
        chosen_status = st.radio("Payment Status", status_opts,
                                  index=stat_idx, horizontal=True, key="ai_status")

    # ── Confidence preview ────────────────────────────────────────
    with st.expander("🔍 View raw AI extraction output"):
        st.json(ex)

    st.markdown("<br>", unsafe_allow_html=True)
    save_col, clear_col, _ = st.columns([1, 1, 3])

    with save_col:
        save_clicked = st.button("💾 Save to Database", use_container_width=True, key="ai_save")
    with clear_col:
        if st.button("🗑 Clear", use_container_width=True, key="ai_clear"):
            for k in ["ai_extracted", "ai_pdf_bytes", "ai_pdf_name"]:
                st.session_state.pop(k, None)
            st.rerun()

    if save_clicked:
        # Handle new vendor creation
        if chosen_vendor_opt == "➕ Add as new vendor":
            if not new_vendor_name or not new_vendor_name.strip():
                st.error("Please enter a vendor name.")
                return
            add_vendor(engine, new_vendor_name,
                       new_vendor_gst or "", "")
            # Refresh and get new vendor id
            vendors    = get_vendors(engine)
            vendor_map = {v.name: v.id for v in vendors}
            final_vendor_id = vendor_map.get(new_vendor_name.strip())
        else:
            final_vendor_id = vendor_map.get(chosen_vendor_opt)

        final_category_id = category_map.get(chosen_cat)
        final_project_id  = project_map.get(chosen_proj)

        if gross <= 0:
            st.error("Gross amount must be greater than 0.")
            return

        # Save the PDF to /invoices/
        inv_path = save_invoice_bytes(
            st.session_state["ai_pdf_bytes"],
            st.session_state["ai_pdf_name"]
        )

        add_expense(
            engine, exp_date,
            final_vendor_id, final_category_id, final_project_id,
            desc, gross, gst, chosen_status, inv_path
        )

        # Clear session state
        for k in ["ai_extracted", "ai_pdf_bytes", "ai_pdf_name"]:
            st.session_state.pop(k, None)

        st.success("✅ Expense saved successfully from AI extraction!")
        st.balloons()
        st.rerun()


def render_receipt_generator_tab(engine):
    st.markdown('<div class="section-header">Receipt Generator</div>', unsafe_allow_html=True)

    st.markdown("""
    <div style="background:#0f2040; border:1px solid #1e3050; border-radius:10px;
                padding:14px 20px; margin-bottom:20px; border-left:3px solid #c9a84c;">
        <strong style="color:#c9a84c;">For cash payments without an invoice</strong>
        <span style="color:#c9c6be; font-size:14px;"> — daily labour, petty materials, site expenses, etc.
        Fill in the details below to generate a branded PDF receipt that gets saved to your records automatically.</span>
    </div>
    """, unsafe_allow_html=True)

    projects   = get_projects(engine)
    categories = get_categories(engine)
    project_names  = [p.name for p in projects]
    category_names = [c.name for c in categories]
    project_map    = {p.name: p.id for p in projects}
    category_map   = {c.name: c.id for c in categories}

    with st.form("receipt_form", clear_on_submit=False):
        st.markdown("#### 👤 Payee Details")
        r1c1, r1c2 = st.columns(2)
        with r1c1:
            payee_name    = st.text_input("Payee Name *", placeholder="e.g. Raju (Mason), Sri Sai Traders")
            payee_contact = st.text_input("Contact / Mobile", placeholder="Optional")
        with r1c2:
            receipt_date  = st.date_input("Date", value=date.today())
            payment_mode  = st.selectbox("Payment Mode",
                                          ["Cash", "UPI", "Bank Transfer", "Cheque", "Other"])

        st.markdown("#### 🏗️ Expense Details")
        r2c1, r2c2 = st.columns(2)
        with r2c1:
            project  = st.selectbox("Project *", project_names)
            category = st.selectbox("Category *", category_names,
                                     index=category_names.index("Labour")
                                     if "Labour" in category_names else 0)
        with r2c2:
            amount  = st.number_input("Amount Paid (₹) *", min_value=0.0, step=100.0)
            purpose = st.text_input("Purpose / Description *",
                                     placeholder="e.g. Daily labour charges, Sand supply, etc.")

        notes = st.text_area("Additional Notes", placeholder="Optional — any extra details",
                              height=80)

        st.markdown("#### 💾 Save Options")
        save_to_db = st.checkbox("Also log this as an expense in the database", value=True)

        submitted = st.form_submit_button("🖨️ Generate Receipt PDF", use_container_width=True)

    if submitted:
        # Validation
        errors = []
        if not payee_name.strip():
            errors.append("Payee name is required.")
        if not purpose.strip():
            errors.append("Purpose / description is required.")
        if amount <= 0:
            errors.append("Amount must be greater than 0.")
        if errors:
            for e in errors:
                st.error(e)
            return

        with st.spinner("Generating receipt..."):
            receipt_no  = next_receipt_number(engine)
            pdf_bytes   = generate_receipt_pdf(
                receipt_no   = receipt_no,
                receipt_date = receipt_date,
                payee_name   = payee_name.strip(),
                payee_contact= payee_contact.strip(),
                project      = project,
                purpose      = purpose.strip(),
                amount       = amount,
                payment_mode = payment_mode,
                category     = category,
                notes        = notes.strip(),
            )

        # Save to /invoices/ folder
        filename  = f"{receipt_no}_{payee_name.strip().replace(' ','_')}.pdf"
        inv_path  = save_invoice_bytes(pdf_bytes, filename)

        # Optionally log to expenses DB
        if save_to_db:
            vendors    = get_vendors(engine)
            vendor_map = {v.name: v.id for v in vendors}
            # Auto-create vendor if not exists
            if payee_name.strip() not in vendor_map:
                add_vendor(engine, payee_name.strip(), "", payee_contact.strip())
                vendors    = get_vendors(engine)
                vendor_map = {v.name: v.id for v in vendors}
            vendor_id   = vendor_map.get(payee_name.strip())
            category_id = category_map.get(category)
            project_id  = project_map.get(project)
            add_expense(engine, receipt_date, vendor_id, category_id, project_id,
                        purpose.strip(), amount, 0.0, "Paid", inv_path)

        # ── Preview + Download ────────────────────────────────────
        st.success(f"✅ Receipt **{receipt_no}** generated successfully!")

        col_dl, col_info = st.columns([1, 2])
        with col_dl:
            st.download_button(
                label="⬇️ Download Receipt PDF",
                data=pdf_bytes,
                file_name=filename,
                mime="application/pdf",
                use_container_width=True,
            )
        with col_info:
            st.markdown(f"""
            <div style="background:#0f2040; border:1px solid #1e3050; border-radius:8px; padding:12px 16px;">
                <div style="color:#c9a84c; font-weight:700; font-size:13px;">{receipt_no}</div>
                <div style="color:#e8e6df; font-size:15px; margin:4px 0;">₹{amount:,.2f} — {payee_name}</div>
                <div style="color:#7a8aaa; font-size:12px;">{project} · {category} · {payment_mode}</div>
                {'<div style="color:#00c07f; font-size:11px; margin-top:4px;">✓ Logged to expense database</div>' if save_to_db else ''}
            </div>
            """, unsafe_allow_html=True)

        st.markdown("---")

        # ── Recent receipts table ─────────────────────────────────
        st.markdown('<div class="section-header">Recent Receipts</div>', unsafe_allow_html=True)
        df = get_expenses_df(engine)
        if not df.empty:
            receipts_df = df[df["invoice_path"].str.contains("AIRC-", na=False)].copy()
            if not receipts_df.empty:
                st.dataframe(
                    receipts_df[["date", "project", "vendor", "category",
                                 "description", "gross_amount", "invoice_path"]]
                    .rename(columns={
                        "date": "Date", "project": "Project", "vendor": "Payee",
                        "category": "Category", "description": "Purpose",
                        "gross_amount": "Amount (₹)", "invoice_path": "Receipt File"
                    }),
                    use_container_width=True, height=280, hide_index=True,
                )
            else:
                st.info("No receipts generated yet.")


def render_projects_tab(engine, df: pd.DataFrame):
    st.markdown('<div class="section-header">Project Summary</div>', unsafe_allow_html=True)
    projects = get_projects(engine)
    if not df.empty and "project" in df.columns:
        summary = (
            df.groupby("project")
            .agg(
                Total_Expenses=("id", "count"),
                Gross_Spend=("gross_amount", "sum"),
                GST_Paid=("gst_amount", "sum"),
                Total_With_GST=("total_amount", "sum"),
                Pending=("payment_status", lambda x: (x == "Pending").sum()),
            )
            .reset_index()
            .rename(columns={"project": "Project"})
        )
        summary["Gross_Spend"]     = summary["Gross_Spend"].map("₹{:,.2f}".format)
        summary["GST_Paid"]        = summary["GST_Paid"].map("₹{:,.2f}".format)
        summary["Total_With_GST"]  = summary["Total_With_GST"].map("₹{:,.2f}".format)
        st.dataframe(summary, use_container_width=True, hide_index=True)
    else:
        pdf = pd.DataFrame([{"ID": p.id, "Project": p.name} for p in projects])
        st.dataframe(pdf, use_container_width=True, hide_index=True)
        st.info("No expenses linked to projects yet.")


# ─────────────────────────────────────────────
# 7. MAIN
# ─────────────────────────────────────────────

def main():
    inject_css()
    engine = get_engine()

    # ── Sidebar ──────────────────────────────
    render_sidebar_add_expense(engine)
    render_sidebar_add_vendor(engine)
    render_sidebar_log_gst(engine)

    df = get_expenses_df(engine)
    render_sidebar_export(df)

    # ── Main area ────────────────────────────
    render_header()
    render_summary_cards(df)

    tab_ledger, tab_scanner, tab_receipt, tab_analytics, tab_gst, tab_vendors, tab_projects = st.tabs([
        "📒  Ledger", "🤖  AI Scanner", "🖨️  Receipt Generator",
        "📊  Analytics", "🧾  Tax Dashboard", "🏢  Vendors", "🏗️  Projects"
    ])
    with tab_ledger:
        render_accounting_table(df, engine)
    with tab_scanner:
        render_invoice_scanner_tab(engine)
    with tab_receipt:
        render_receipt_generator_tab(engine)
    with tab_analytics:
        render_analytics_tab(df)
    with tab_gst:
        render_gst_tab(df, engine)
    with tab_vendors:
        render_vendors_tab(engine)
    with tab_projects:
        render_projects_tab(engine, df)


if __name__ == "__main__":
    main()
