"""
Arthav Infra LLP — Rent Invoice Generator
Standalone app: generates GST rent invoices (SGST 9% + CGST 9%) for all tenants.
Features: automatic rent escalation, Google Drive upload into per-tenant subfolders.
"""

import io
import zipfile
from datetime import date, datetime
from dateutil.relativedelta import relativedelta

import streamlit as st
import pandas as pd
from sqlalchemy import (
    create_engine, Column, Integer, String, Float, Date, Text, ForeignKey
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
LANDLORD_NAME  = "Arthav Infra LLP"
LANDLORD_PAN   = "ACKFA1087B"
LANDLORD_GSTIN = "36ACKFA1087B1ZO"
LANDLORD_ADDR1 = "R/o. 3-6-305/81, Avanti Nagar Colony,"
LANDLORD_ADDR2 = "Basheerbagh, Hyderabad - 500029"
BANK_NAME      = "M/s. ARTHA INFRA LLP"
BANK_ACCOUNT   = "C A/c No.: 50200115403426"
BANK_BRANCH    = "HDFC Bank Ltd., Film Nagar Branch"
BANK_IFSC      = "IFSC: HDFC0003974"
HSN_CODE       = "997212"
GST_RATE       = 0.09
NAVY           = "#0A1628"
GOLD           = "#C9A84C"

# ─────────────────────────────────────────────
# 2. DATABASE
# ─────────────────────────────────────────────
Base = declarative_base()

class Tenant(Base):
    __tablename__       = "tenants"
    id                  = Column(Integer, primary_key=True, autoincrement=True)
    name                = Column(String,  nullable=False)
    short_code          = Column(String,  nullable=False)
    address             = Column(String,  default="")
    property_addr       = Column(String,  default="")
    gstin               = Column(String,  default="")
    pan                 = Column(String,  default="")
    default_rent        = Column(Float,   default=0.0)
    rent_start_date     = Column(Date,    nullable=True)
    escalation_pct      = Column(Float,   default=0.0)
    escalation_months   = Column(Integer, default=12)
    drive_folder_id     = Column(String,  default="")
    active              = Column(Integer, default=1)


class Invoice(Base):
    __tablename__  = "invoices"
    id             = Column(Integer, primary_key=True, autoincrement=True)
    tenant_id      = Column(Integer, ForeignKey("tenants.id"), nullable=False)
    bill_number    = Column(String,  nullable=False)
    invoice_date   = Column(Date,    nullable=False)
    month_label    = Column(String,  nullable=False)
    rent_amount    = Column(Float,   nullable=False)
    sgst           = Column(Float,   nullable=False)
    cgst           = Column(Float,   nullable=False)
    total          = Column(Float,   nullable=False)
    drive_file_id  = Column(String,  default="")
    created_at     = Column(Date,    default=date.today)


@st.cache_resource
def get_engine():
    engine = create_engine(
        "sqlite:///rent_invoices.db",
        connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    _migrate(engine)
    return engine


def _migrate(engine):
    """Safely add new columns to existing deployments."""
    from sqlalchemy import inspect, text
    inspector = inspect(engine)
    t_cols    = {c["name"] for c in inspector.get_columns("tenants")}
    i_cols    = {c["name"] for c in inspector.get_columns("invoices")}
    new_t = {
        "rent_start_date":   "DATE",
        "escalation_pct":    "FLOAT DEFAULT 0.0",
        "escalation_months": "INTEGER DEFAULT 12",
        "drive_folder_id":   "TEXT DEFAULT ''",
    }
    with engine.connect() as conn:
        for col, typ in new_t.items():
            if col not in t_cols:
                conn.execute(text(f"ALTER TABLE tenants ADD COLUMN {col} {typ}"))
        if "drive_file_id" not in i_cols:
            conn.execute(text("ALTER TABLE invoices ADD COLUMN drive_file_id TEXT DEFAULT ''"))
        conn.commit()


# ── CRUD ─────────────────────────────────────────────────────────────────────
def get_tenants(engine):
    with Session(engine) as s:
        return s.query(Tenant).filter(Tenant.active == 1).order_by(Tenant.name).all()

def get_all_tenants(engine):
    with Session(engine) as s:
        return s.query(Tenant).order_by(Tenant.name).all()

def add_tenant(engine, name, short_code, address, property_addr,
               gstin, pan, default_rent, rent_start_date,
               escalation_pct, escalation_months):
    with Session(engine) as s:
        s.add(Tenant(
            name=name, short_code=short_code, address=address,
            property_addr=property_addr, gstin=gstin, pan=pan,
            default_rent=default_rent, rent_start_date=rent_start_date,
            escalation_pct=escalation_pct, escalation_months=escalation_months,
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
    fy_start = invoice_date.year if invoice_date.month >= 4 else invoice_date.year - 1
    fy_label = f"{str(fy_start)[-2:]}-{str(fy_start + 1)[-2:]}"
    prefix, suffix = f"{short_code} ", f"/{fy_label}"
    with Session(engine) as s:
        count = s.query(Invoice).filter(
            Invoice.bill_number.like(f"{prefix}%{suffix}")
        ).count()
    return f"{prefix}{count + 1:03d}{suffix}"

def save_invoice_record(engine, tenant_id, bill_number, invoice_date,
                        month_label, rent_amount, sgst, cgst, total,
                        drive_file_id=""):
    with Session(engine) as s:
        s.add(Invoice(
            tenant_id=tenant_id, bill_number=bill_number,
            invoice_date=invoice_date, month_label=month_label,
            rent_amount=rent_amount, sgst=sgst, cgst=cgst, total=total,
            drive_file_id=drive_file_id,
        ))
        s.commit()

def get_invoice_history(engine):
    with Session(engine) as s:
        rows = (
            s.query(Invoice, Tenant.name)
            .join(Tenant, Invoice.tenant_id == Tenant.id)
            .order_by(Invoice.invoice_date.desc()).all()
        )
        return [{
            "ID": inv.id, "Bill No.": inv.bill_number, "Tenant": name,
            "Month": inv.month_label, "Date": inv.invoice_date,
            "Rent (₹)": inv.rent_amount, "SGST (₹)": inv.sgst,
            "CGST (₹)": inv.cgst, "Total (₹)": inv.total,
            "Drive": "☁️" if inv.drive_file_id else "—",
        } for inv, name in rows]


# ─────────────────────────────────────────────
# 3. ESCALATION ENGINE
# ─────────────────────────────────────────────
def compute_escalated_rent(tenant: Tenant, billing_date: date) -> float:
    """
    Compound escalation: base_rent × (1 + pct/100) ^ (intervals_elapsed)
    Intervals are counted from rent_start_date in steps of escalation_months.
    """
    base     = tenant.default_rent or 0.0
    start    = tenant.rent_start_date
    pct      = tenant.escalation_pct or 0.0
    interval = tenant.escalation_months or 12

    if not start or pct == 0 or interval == 0:
        return base

    months_elapsed    = (billing_date.year - start.year) * 12 + (billing_date.month - start.month)
    if months_elapsed < 0:
        return base
    intervals_elapsed = months_elapsed // interval
    return round(base * ((1 + pct / 100) ** intervals_elapsed), 2)


def next_escalation_date(tenant: Tenant) -> str:
    if not tenant.rent_start_date or not tenant.escalation_pct or not tenant.escalation_months:
        return "—"
    today    = date.today()
    start    = tenant.rent_start_date
    interval = tenant.escalation_months
    months_elapsed = (today.year - start.year) * 12 + (today.month - start.month)
    intervals_done = months_elapsed // interval
    nxt = start + relativedelta(months=interval * (intervals_done + 1))
    return nxt.strftime("%b %Y")


# ─────────────────────────────────────────────
# 4. GOOGLE DRIVE
# ─────────────────────────────────────────────
def get_drive_service():
    try:
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build
        cfg = st.secrets.get("gdrive_oauth", {})
        if not cfg:
            return None
        creds = Credentials(
            token=None,
            refresh_token=cfg["refresh_token"],
            token_uri="https://oauth2.googleapis.com/token",
            client_id=cfg["client_id"],
            client_secret=cfg["client_secret"],
            scopes=["https://www.googleapis.com/auth/drive"],
        )
        return build("drive", "v3", credentials=creds, cache_discovery=False)
    except Exception:
        return None


def get_or_create_subfolder(service, parent_id: str, folder_name: str) -> str:
    safe = folder_name.replace("/", "-").replace("\\", "-").strip()
    q    = (f"'{parent_id}' in parents and name='{safe}' "
            f"and mimeType='application/vnd.google-apps.folder' and trashed=false")
    res  = service.files().list(q=q, fields="files(id)", supportsAllDrives=True).execute()
    if res.get("files"):
        return res["files"][0]["id"]
    meta   = {"name": safe, "mimeType": "application/vnd.google-apps.folder",
               "parents": [parent_id]}
    folder = service.files().create(body=meta, fields="id",
                                     supportsAllDrives=True).execute()
    return folder["id"]


def drive_upload_invoice(engine, service, tenant: Tenant,
                         pdf_bytes: bytes, filename: str,
                         root_folder_id: str) -> str:
    """Upload to root/tenant_name/filename. Caches subfolder ID in DB."""
    from googleapiclient.http import MediaIoBaseUpload
    try:
        folder_id = tenant.drive_folder_id or ""
        if not folder_id:
            folder_id = get_or_create_subfolder(service, root_folder_id, tenant.name)
            update_tenant(engine, tenant.id, drive_folder_id=folder_id)
        media  = MediaIoBaseUpload(io.BytesIO(pdf_bytes), mimetype="application/pdf")
        result = service.files().create(
            body={"name": filename, "parents": [folder_id]},
            media_body=media, fields="id", supportsAllDrives=True
        ).execute()
        return result.get("id", "")
    except Exception as e:
        st.warning(f"Drive upload failed for {tenant.name}: {e}")
        return ""


# ─────────────────────────────────────────────
# 5. AMOUNT IN WORDS
# ─────────────────────────────────────────────
def amount_in_words(amount: float) -> str:
    ones = ["","One","Two","Three","Four","Five","Six","Seven","Eight","Nine",
            "Ten","Eleven","Twelve","Thirteen","Fourteen","Fifteen","Sixteen",
            "Seventeen","Eighteen","Nineteen"]
    tens = ["","","Twenty","Thirty","Forty","Fifty","Sixty","Seventy","Eighty","Ninety"]

    def _two(n):
        return ones[n] if n < 20 else tens[n//10] + (" "+ones[n%10] if n%10 else "")
    def _three(n):
        return (ones[n//100]+" Hundred"+(" "+_two(n%100) if n%100 else "")) if n>=100 else _two(n)

    r = int(round(amount))
    if r == 0:
        return "Zero Rupees Only"
    parts = []
    cr=r//10_000_000; r%=10_000_000
    lac=r//100_000;   r%=100_000
    th=r//1_000;      r%=1_000
    if cr:  parts.append(_three(cr)+" Crore")
    if lac: parts.append(_three(lac)+" Lakh")
    if th:  parts.append(_three(th)+" Thousand")
    if r:   parts.append(_three(r))
    return "INR: "+" ".join(parts)+" Only"


# ─────────────────────────────────────────────
# 6. PDF GENERATOR
# ─────────────────────────────────────────────
def generate_invoice_pdf(tenant: Tenant, bill_number: str,
                         invoice_date: date, month_label: str,
                         rent_amount: float) -> bytes:
    sgst  = round(rent_amount * GST_RATE, 2)
    cgst  = round(rent_amount * GST_RATE, 2)
    total = rent_amount + sgst + cgst

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4,
                             leftMargin=18*mm, rightMargin=18*mm,
                             topMargin=14*mm, bottomMargin=14*mm)
    NAVYc  = colors.HexColor("#0A1628")
    GOLDc  = colors.HexColor("#C9A84C")
    LGREYc = colors.HexColor("#F4F4F4")
    GREYc  = colors.HexColor("#555555")
    W      = 174 * mm

    title_s = ParagraphStyle("t",  fontSize=16, fontName="Helvetica-Bold",
                              textColor=GOLDc, spaceAfter=2, alignment=1)
    small_s = ParagraphStyle("sm", fontSize=8.5, fontName="Helvetica",
                              textColor=GREYc, spaceAfter=1)
    label_s = ParagraphStyle("lb", fontSize=8,  fontName="Helvetica-Bold",
                              textColor=NAVYc)
    words_s = ParagraphStyle("w",  fontSize=8.5, fontName="Helvetica-Oblique",
                              textColor=NAVYc, spaceAfter=2)
    bank_s  = ParagraphStyle("bk", fontSize=8,  fontName="Helvetica",
                              textColor=GREYc, spaceAfter=1)

    story = []
    story.append(Paragraph("RENT INVOICE", title_s))
    story.append(HRFlowable(width="100%", thickness=1.5, color=GOLDc, spaceAfter=4))

    info_t = Table([[
        Paragraph(f"<b>{LANDLORD_NAME}</b><br/>PAN: {LANDLORD_PAN}<br/>"
                  f"GSTIN: {LANDLORD_GSTIN}<br/>{LANDLORD_ADDR1}<br/>{LANDLORD_ADDR2}", small_s),
        Paragraph(f"<b>Bill No.:</b> {bill_number}<br/>"
                  f"<b>Date:</b> {invoice_date.strftime('%d/%m/%y')}<br/>"
                  f"<b>HSN/SAC:</b> {HSN_CODE}", small_s),
    ]], colWidths=[W*0.6, W*0.4])
    info_t.setStyle(TableStyle([
        ("VALIGN",(0,0),(-1,-1),"TOP"),("ALIGN",(1,0),(1,0),"RIGHT"),
        ("BOTTOMPADDING",(0,0),(-1,-1),6),
    ]))
    story.append(info_t)
    story.append(HRFlowable(width="100%", thickness=0.5,
                             color=colors.HexColor("#CCCCCC"), spaceAfter=4))

    story.append(Paragraph("Billed To:", label_s))
    story.append(Paragraph(
        f"<b>M/s. {tenant.name}</b><br/>{tenant.address}<br/>"
        + (f"GSTIN: {tenant.gstin}<br/>" if tenant.gstin else "")
        + (f"PAN: {tenant.pan}" if tenant.pan else ""), small_s))
    story.append(Spacer(1, 4*mm))

    def rpara(txt, bold=False):
        fn = "Helvetica-Bold" if bold else "Helvetica"
        return Paragraph(f"<b>{txt}</b>" if bold else txt,
                         ParagraphStyle("rp", fontSize=9, fontName=fn, alignment=2))

    rows = [
        [Paragraph("S.No.",label_s), Paragraph("Particulars",label_s),
         Paragraph("Amount (₹)",label_s)],
        ["1",
         Paragraph(f"Rent for the Month of {month_label}<br/>"
                   f"<font size='8' color='#777777'>{tenant.property_addr}</font>", small_s),
         rpara(f"{rent_amount:,.2f}")],
        ["2","SGST @ 9%",  rpara(f"{sgst:,.2f}")],
        ["3","CGST @ 9%",  rpara(f"{cgst:,.2f}")],
        ["", Paragraph("<b>Total</b>",label_s),     rpara(f"{total:,.2f}", bold=True)],
        ["", Paragraph("<b>Round Off</b>",label_s), rpara(f"{round(total):,.2f}", bold=True)],
    ]
    items_t = Table(rows, colWidths=[12*mm, 118*mm, 44*mm], repeatRows=1)
    items_t.setStyle(TableStyle([
        ("BACKGROUND",(0,0),(-1,0),NAVYc), ("TEXTCOLOR",(0,0),(-1,0),GOLDc),
        ("FONTNAME",(0,0),(-1,0),"Helvetica-Bold"), ("FONTSIZE",(0,0),(-1,-1),9),
        ("ALIGN",(2,0),(2,0),"RIGHT"), ("TOPPADDING",(0,0),(-1,0),5),
        ("BOTTOMPADDING",(0,0),(-1,0),5), ("ALIGN",(0,1),(0,-1),"CENTER"),
        ("ALIGN",(2,1),(2,-1),"RIGHT"),
        ("ROWBACKGROUNDS",(0,1),(-1,-3),[colors.white,LGREYc]),
        ("LINEABOVE",(0,-2),(-1,-2),1,GOLDc),
        ("BACKGROUND",(0,-2),(-1,-1),colors.HexColor("#FFF8E7")),
        ("GRID",(0,0),(-1,-1),0.3,colors.HexColor("#DDDDDD")),
        ("TOPPADDING",(0,1),(-1,-1),4), ("BOTTOMPADDING",(0,1),(-1,-1),4),
        ("LEFTPADDING",(1,1),(1,-1),6),
    ]))
    story.append(items_t)
    story.append(Spacer(1, 3*mm))
    story.append(Paragraph(amount_in_words(round(total)), words_s))
    story.append(Paragraph("Rent for immovable property", small_s))
    story.append(HRFlowable(width="100%", thickness=0.5,
                             color=colors.HexColor("#CCCCCC"), spaceAfter=4))

    footer_t = Table([[
        Paragraph(f"<b>Bank Details:</b><br/>{BANK_NAME}<br/>"
                  f"{BANK_ACCOUNT}<br/>{BANK_BRANCH}<br/>{BANK_IFSC}", bank_s),
        Paragraph(f"<br/><br/><br/><br/>For <b>{LANDLORD_NAME}</b><br/>"
                  f"(Authorised Signatory)",
                  ParagraphStyle("sig", fontSize=8, fontName="Helvetica",
                                 textColor=NAVYc, alignment=2)),
    ]], colWidths=[W*0.55, W*0.45])
    footer_t.setStyle(TableStyle([("VALIGN",(0,0),(-1,-1),"BOTTOM")]))
    story.append(footer_t)
    story.append(Spacer(1, 3*mm))
    story.append(Paragraph(
        "GST Note: SGST and CGST payable at 9% each on rent value. "
        "No ITC available to landlord. HSN/SAC: 997212.",
        ParagraphStyle("gn", fontSize=7, fontName="Helvetica-Oblique",
                       textColor=colors.HexColor("#888888"))))
    doc.build(story)
    return buf.getvalue()


# ─────────────────────────────────────────────
# 7. CSS
# ─────────────────────────────────────────────
st.markdown(f"""
<style>
  @import url('https://fonts.googleapis.com/css2?family=Syne:wght@400;700&family=DM+Mono&display=swap');
  html,body,[class*="css"]{{font-family:'Syne',sans-serif;background:{NAVY};color:#e8e6df;}}
  .stApp{{background-color:{NAVY};}}
  .section-header{{font-family:'Syne',sans-serif;font-weight:700;font-size:1.1rem;
      color:{GOLD};border-left:4px solid {GOLD};padding:6px 14px;margin:18px 0 10px;
      background:#0f2040;border-radius:0 6px 6px 0;}}
  div[data-testid="stSidebar"]{{background-color:#071020;}}
  .stButton>button{{background-color:{NAVY};color:{GOLD};border:1.5px solid {GOLD};
      border-radius:6px;font-weight:700;width:100%;}}
  .stButton>button:hover{{background-color:{GOLD};color:{NAVY};}}
  .stDownloadButton>button{{background:linear-gradient(90deg,{GOLD},#e8c96a);
      color:{NAVY};border:none;font-weight:700;border-radius:6px;width:100%;}}
  .stDataFrame{{border:1px solid #1e3050!important;border-radius:8px;}}
  .stTabs [data-baseweb="tab"]{{color:#7a8aaa;font-weight:700;}}
  .stTabs [aria-selected="true"]{{color:{GOLD}!important;border-bottom-color:{GOLD}!important;}}
  .metric-box{{background:#0f2040;border:1px solid #1e3050;border-radius:10px;
      padding:14px 18px;text-align:center;}}
  .metric-val{{font-size:1.5rem;font-weight:700;color:{GOLD};}}
  .metric-lbl{{font-size:0.78rem;color:#7a8aaa;margin-top:2px;}}
  .esc-badge{{display:inline-block;background:#1a2a1a;color:#4caf50;
      border:1px solid #4caf50;border-radius:4px;font-size:0.72rem;padding:1px 7px;margin-left:8px;}}
</style>
""", unsafe_allow_html=True)


# ─────────────────────────────────────────────
# 8. SIDEBAR
# ─────────────────────────────────────────────
def render_sidebar(engine):
    st.sidebar.markdown("# 🏢 Arthav Infra LLP")
    st.sidebar.markdown("### Rent Invoice Generator")
    st.sidebar.markdown("---")

    st.sidebar.markdown("## ☁️ Google Drive")
    root_folder = st.sidebar.text_input(
        "Root Folder ID",
        value=st.session_state.get("drive_root_folder", ""),
        placeholder="Paste Drive folder ID here",
        key="drive_root_input",
        help="All invoices are saved here in per-tenant subfolders.",
    )
    st.session_state["drive_root_folder"] = root_folder
    if root_folder and st.sidebar.button("🔍 Test Connection", key="test_drive"):
        svc = get_drive_service()
        if not svc:
            st.sidebar.error("❌ Drive not configured — add [gdrive_oauth] to Streamlit secrets.")
        else:
            try:
                f = svc.files().get(fileId=root_folder, fields="name").execute()
                st.sidebar.success(f"✅ Connected: **{f['name']}**")
            except Exception as e:
                st.sidebar.error(f"❌ {e}")

    st.sidebar.markdown("---")
    st.sidebar.markdown("## ➕ Add Tenant")
    with st.sidebar.form("add_tenant_form", clear_on_submit=True):
        name          = st.text_input("Tenant Name *",       placeholder="M/s. Prime Impex INC.")
        short_code    = st.text_input("Short Code *",        placeholder="Prime Impex")
        address       = st.text_input("Tenant Address",      placeholder="Hyderabad.")
        property_addr = st.text_input("Property Address *",  placeholder="Survey No. 2, NH7...")
        gstin         = st.text_input("Tenant GSTIN",        placeholder="36XXXXXXX")
        pan           = st.text_input("Tenant PAN",          placeholder="XXXXXXXXXX")
        default_rent  = st.number_input("Base Monthly Rent (₹)", min_value=0.0, step=1000.0)
        rent_start    = st.date_input("Rent Effective From", value=date.today())
        st.markdown("**Escalation** *(optional)*")
        esc_pct    = st.number_input("Escalation %",    min_value=0.0, max_value=50.0,
                                      value=0.0, step=0.5,
                                      help="e.g. 5 = 5% increase per interval")
        esc_months = st.number_input("Every N months",  min_value=1, max_value=60,
                                      value=12, step=1,
                                      help="e.g. 12 = annual, 6 = semi-annual")
        if st.form_submit_button("Add Tenant", use_container_width=True):
            if not name.strip() or not short_code.strip():
                st.sidebar.error("Name and Short Code are required.")
            else:
                add_tenant(engine, name.strip(), short_code.strip(),
                           address.strip(), property_addr.strip(),
                           gstin.strip(), pan.strip(), default_rent,
                           rent_start, esc_pct, int(esc_months))
                st.sidebar.success(f"✅ {name} added.")
                st.rerun()

    st.sidebar.markdown("---")
    st.sidebar.markdown("## ✏️ Edit Tenant")
    tenants = get_all_tenants(engine)
    if tenants:
        t_map = {f"{t.name} (#{t.id})": t for t in tenants}
        sel   = st.sidebar.selectbox("Select", list(t_map.keys()), key="edit_sel")
        t     = t_map[sel]
        with st.sidebar.form("edit_tenant_form", clear_on_submit=False):
            e_name   = st.text_input("Name",             value=t.name)
            e_code   = st.text_input("Short Code",       value=t.short_code)
            e_addr   = st.text_input("Address",          value=t.address or "")
            e_prop   = st.text_input("Property Address", value=t.property_addr or "")
            e_gstin  = st.text_input("GSTIN",            value=t.gstin or "")
            e_pan    = st.text_input("PAN",              value=t.pan or "")
            e_rent   = st.number_input("Base Rent (₹)",  value=float(t.default_rent or 0),
                                        step=1000.0)
            e_start  = st.date_input("Effective From",
                                      value=t.rent_start_date or date.today())
            st.markdown("**Escalation**")
            e_pct    = st.number_input("Escalation %",   min_value=0.0, max_value=50.0,
                                        value=float(t.escalation_pct or 0), step=0.5)
            e_months = st.number_input("Every N months", min_value=1, max_value=60,
                                        value=int(t.escalation_months or 12), step=1)
            e_active = st.checkbox("Active", value=bool(t.active))
            if st.form_submit_button("Save Changes", use_container_width=True):
                update_tenant(engine, t.id,
                              name=e_name, short_code=e_code,
                              address=e_addr, property_addr=e_prop,
                              gstin=e_gstin, pan=e_pan,
                              default_rent=e_rent, rent_start_date=e_start,
                              escalation_pct=e_pct, escalation_months=int(e_months),
                              active=int(e_active))
                st.sidebar.success("✅ Tenant updated.")
                st.rerun()


# ─────────────────────────────────────────────
# 9. TABS
# ─────────────────────────────────────────────
def render_generate_tab(engine):
    st.markdown('<div class="section-header">Generate Monthly Invoices</div>',
                unsafe_allow_html=True)
    tenants = get_tenants(engine)
    if not tenants:
        st.info("No active tenants. Add tenants from the sidebar.")
        return

    c1, c2, c3 = st.columns(3)
    with c1:
        today    = date.today()
        inv_date = st.date_input("Invoice Date", value=today.replace(day=1))
    with c2:
        month_names = ["January","February","March","April","May","June",
                       "July","August","September","October","November","December"]
        month_idx = st.selectbox("Billing Month", range(12),
                                  format_func=lambda i: month_names[i],
                                  index=today.month - 1)
    with c3:
        bill_year = st.number_input("Year", min_value=2020, max_value=2040,
                                     value=today.year, step=1)

    month_label  = f"{month_names[month_idx][:3]}'{str(bill_year)[-2:]}"
    billing_ref  = date(int(bill_year), month_idx + 1, 1)

    st.markdown("---")
    st.markdown('<div class="section-header">Rent Amounts (Auto-Escalated)</div>',
                unsafe_allow_html=True)
    st.caption("Rents calculated from escalation rules. Override any amount if needed.")

    rent_inputs = {}
    for t in tenants:
        auto_rent = compute_escalated_rent(t, billing_ref)
        ca, cb    = st.columns([3, 2])
        with ca:
            badge = ""
            if t.escalation_pct and t.rent_start_date:
                nxt   = next_escalation_date(t)
                badge = (f'<span class="esc-badge">'
                         f'↑{t.escalation_pct:.0f}% every {t.escalation_months}m'
                         f' · next {nxt}</span>')
            st.markdown(f"**{t.name}**{badge}", unsafe_allow_html=True)
            st.caption(t.property_addr or "No property address set")
        with cb:
            rent_inputs[t.id] = st.number_input(
                "Rent", value=float(auto_rent), min_value=0.0,
                step=1000.0, key=f"rent_{t.id}", label_visibility="collapsed",
            )
        sgst  = round(rent_inputs[t.id] * GST_RATE, 2)
        total = round(rent_inputs[t.id] + sgst * 2, 2)
        st.caption(f"SGST ₹{sgst:,.2f}  +  CGST ₹{sgst:,.2f}  →  **Total ₹{total:,.2f}**")
        st.markdown("---")

    total_rent = sum(rent_inputs.values())
    total_gst  = sum(round(r * GST_RATE * 2, 2) for r in rent_inputs.values())
    total_inv  = total_rent + total_gst

    drive_root    = st.session_state.get("drive_root_folder", "")
    drive_service = get_drive_service() if drive_root else None
    drive_ready   = bool(drive_root and drive_service)

    m1, m2, m3, m4 = st.columns(4)
    for col, lbl, val in [
        (m1, "Tenants",        str(len(tenants))),
        (m2, "Total Rent",     f"₹{total_rent:,.0f}"),
        (m3, "Total GST",      f"₹{total_gst:,.0f}"),
        (m4, "Total Invoiced", f"₹{total_inv:,.0f}"),
    ]:
        with col:
            st.markdown(f"""<div class="metric-box">
                <div class="metric-val">{val}</div>
                <div class="metric-lbl">{lbl}</div>
            </div>""", unsafe_allow_html=True)

    if drive_ready:
        st.success("☁️ Drive connected — invoices will auto-save to per-tenant subfolders.")
    else:
        st.info("☁️ Paste your Drive folder ID in the sidebar to enable auto-upload.")

    st.markdown("<br>", unsafe_allow_html=True)

    if st.button("🖨️ Generate All Invoices", use_container_width=True):
        if all(v == 0 for v in rent_inputs.values()):
            st.error("All rent amounts are ₹0.")
            return

        generated = []
        progress  = st.progress(0, text="Starting...")

        for i, t in enumerate(tenants):
            rent = rent_inputs[t.id]
            if rent == 0:
                continue
            sgst    = round(rent * GST_RATE, 2)
            cgst    = round(rent * GST_RATE, 2)
            total   = rent + sgst + cgst
            bill_no = next_bill_number(engine, t.short_code, inv_date)

            # Filename: YYYYMMDD_BillNo_TenantCode.pdf
            date_prefix = billing_ref.strftime("%Y%m%d")
            safe_bill   = bill_no.replace("/", "-").replace(" ", "_")
            fname       = f"{date_prefix}_{safe_bill}.pdf"

            progress.progress((i + 0.3) / len(tenants), text=f"Generating {t.name}...")
            pdf_bytes = generate_invoice_pdf(t, bill_no, inv_date, month_label, rent)

            drive_file_id = ""
            if drive_ready:
                progress.progress((i + 0.7) / len(tenants),
                                   text=f"Uploading {t.name} to Drive...")
                drive_file_id = drive_upload_invoice(
                    engine, drive_service, t, pdf_bytes, fname, drive_root
                )

            save_invoice_record(engine, t.id, bill_no, inv_date, month_label,
                                rent, sgst, cgst, total, drive_file_id)
            generated.append((fname, pdf_bytes, t.name, bill_no, total, drive_file_id))
            progress.progress((i + 1) / len(tenants), text=f"Done: {t.name}")

        progress.empty()
        if not generated:
            st.warning("No invoices generated — all rents were ₹0.")
            return

        drive_count = sum(1 for *_, did in generated if did)
        st.success(
            f"✅ {len(generated)} invoice(s) generated for {month_label}!"
            + (f"  ☁️ {drive_count}/{len(generated)} saved to Drive." if drive_ready else "")
        )

        st.markdown('<div class="section-header">Download Invoices</div>',
                    unsafe_allow_html=True)
        for fname, pdf_bytes, tname, bill_no, total, drive_file_id in generated:
            dc1, dc2, dc3 = st.columns([3, 2, 2])
            with dc1:
                cloud = " ☁️" if drive_file_id else ""
                st.markdown(f"**{tname}**{cloud}  \n`{bill_no}`")
            with dc2:
                st.markdown(f"₹{total:,.2f}")
            with dc3:
                st.download_button("⬇ PDF", data=pdf_bytes, file_name=fname,
                                   mime="application/pdf", key=f"dl_{fname}",
                                   use_container_width=True)

        st.markdown("---")
        zip_buf = io.BytesIO()
        with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for fname, pdf_bytes, *_ in generated:
                zf.writestr(fname, pdf_bytes)
        zip_buf.seek(0)
        st.download_button(
            f"📦 Download All {len(generated)} Invoices as ZIP",
            data=zip_buf.getvalue(),
            file_name=f"Arthav_Infra_Rent_Invoices_{month_label}.zip",
            mime="application/zip", use_container_width=True,
        )


def render_tenants_tab(engine):
    st.markdown('<div class="section-header">Tenant Directory</div>',
                unsafe_allow_html=True)
    tenants = get_all_tenants(engine)
    if not tenants:
        st.info("No tenants added yet.")
        return

    today = date.today()
    rows  = []
    for t in tenants:
        curr = compute_escalated_rent(t, today)
        rows.append({
            "ID":                t.id,
            "Name":              t.name,
            "Base Rent (₹)":    f"₹{t.default_rent:,.0f}",
            "Current Rent (₹)": f"₹{curr:,.0f}",
            "Escalation":       (f"{t.escalation_pct:.0f}% / {t.escalation_months}m"
                                  if t.escalation_pct else "None"),
            "Next Escalation":  next_escalation_date(t),
            "Effective From":   str(t.rent_start_date or "—"),
            "GSTIN":            t.gstin or "—",
            "Active":           "✅" if t.active else "❌",
        })
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

    active = [t for t in tenants if t.active]
    if active:
        st.markdown("---")
        st.markdown('<div class="section-header">Monthly GST Summary</div>',
                    unsafe_allow_html=True)
        total_rent = sum(compute_escalated_rent(t, today) for t in active)
        total_gst  = round(total_rent * GST_RATE * 2, 2)
        g1, g2, g3 = st.columns(3)
        for col, lbl, val in [
            (g1, "Total Monthly Rent", f"₹{total_rent:,.0f}"),
            (g2, "GST Payable (18%)",  f"₹{total_gst:,.0f}"),
            (g3, "Total Invoiced",     f"₹{total_rent + total_gst:,.0f}"),
        ]:
            with col:
                st.markdown(f"""<div class="metric-box">
                    <div class="metric-val">{val}</div>
                    <div class="metric-lbl">{lbl}</div>
                </div>""", unsafe_allow_html=True)

        # 12-month escalation schedule
        st.markdown("---")
        st.markdown('<div class="section-header">Escalation Schedule — Next 12 Months</div>',
                    unsafe_allow_html=True)
        esc_tenants = [t for t in active if t.escalation_pct and t.rent_start_date]
        if esc_tenants:
            sched = []
            for m in range(0, 13):
                future = today + relativedelta(months=m)
                row    = {"Month": future.strftime("%b %Y")}
                for t in esc_tenants:
                    row[t.short_code] = f"₹{compute_escalated_rent(t, future):,.0f}"
                sched.append(row)
            st.dataframe(pd.DataFrame(sched), use_container_width=True, hide_index=True)
        else:
            st.info("No escalation rules configured yet. Add % and interval in tenant settings.")


def render_history_tab(engine):
    st.markdown('<div class="section-header">Invoice History</div>',
                unsafe_allow_html=True)
    history = get_invoice_history(engine)
    if not history:
        st.info("No invoices generated yet.")
        return

    df = pd.DataFrame(history)
    st.dataframe(df, use_container_width=True, hide_index=True, height=380)

    st.markdown("---")
    st.markdown('<div class="section-header">Reprint an Invoice</div>',
                unsafe_allow_html=True)
    with Session(engine) as s:
        all_inv = s.query(Invoice).order_by(Invoice.invoice_date.desc()).all()
        all_ten = {t.id: t for t in s.query(Tenant).all()}
    if all_inv:
        inv_map = {f"{inv.bill_number}  ({inv.month_label})": inv for inv in all_inv}
        sel_inv = st.selectbox("Select Invoice", list(inv_map.keys()))
        if st.button("🖨️ Reprint PDF", use_container_width=False):
            inv    = inv_map[sel_inv]
            tenant = all_ten.get(inv.tenant_id)
            if tenant:
                pdf_bytes   = generate_invoice_pdf(
                    tenant, inv.bill_number, inv.invoice_date,
                    inv.month_label, inv.rent_amount
                )
                billing_ref = date(inv.invoice_date.year, inv.invoice_date.month, 1)
                fname       = (f"{billing_ref.strftime('%Y%m%d')}_"
                               f"{inv.bill_number.replace('/', '-').replace(' ', '_')}.pdf")
                st.download_button("⬇ Download Reprint", data=pdf_bytes,
                                   file_name=fname, mime="application/pdf",
                                   use_container_width=True)

    st.markdown("---")
    st.markdown('<div class="section-header">Quarterly GST Liability</div>',
                unsafe_allow_html=True)
    df["Quarter"] = pd.to_datetime(df["Date"]).dt.to_period("Q").astype(str)
    q = (df.groupby("Quarter")
         .agg(Invoices=("ID","count"), Rent=("Rent (₹)","sum"),
              SGST=("SGST (₹)","sum"), CGST=("CGST (₹)","sum"),
              Total=("Total (₹)","sum"))
         .reset_index().sort_values("Quarter", ascending=False))
    for col in ["Rent","SGST","CGST","Total"]:
        q[col] = q[col].map("₹{:,.2f}".format)
    st.dataframe(q, use_container_width=True, hide_index=True)

    csv = df.to_csv(index=False).encode("utf-8")
    st.download_button("⬇ Export History CSV", data=csv,
                       file_name="arthav_rent_invoice_history.csv", mime="text/csv")


# ─────────────────────────────────────────────
# 10. MAIN
# ─────────────────────────────────────────────
def main():
    engine = get_engine()
    render_sidebar(engine)
    st.markdown(f"""
    <h2 style="color:{GOLD};font-family:Syne,sans-serif;margin-bottom:0;">
        🏢 Rent Invoice Generator
    </h2>
    <p style="color:#7a8aaa;font-size:0.9rem;margin-top:4px;">
        Arthav Infra LLP &nbsp;|&nbsp; GSTIN: {LANDLORD_GSTIN}
        &nbsp;|&nbsp; HSN/SAC: {HSN_CODE}
    </p>
    """, unsafe_allow_html=True)

    tab_gen, tab_tenants, tab_history = st.tabs([
        "🖨️  Generate Invoices",
        "🏢  Tenants & Escalation",
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
