"""
Invoice Scanner Pro — Production Backend
=========================================
Vercel Serverless + Neon PostgreSQL + OCR.Space API

Environment Variables Required:
  DATABASE_URL       — Neon PostgreSQL connection string
  OCR_SPACE_API_KEY  — OCR.Space API key (free: https://ocr.space)
"""

import os
import re
import csv
import json
import uuid
import io
import base64
import hashlib
from datetime import datetime
from typing import Optional, List
from contextlib import contextmanager
from xml.sax.saxutils import escape

import psycopg2
import psycopg2.extras
import requests
import jwt
from jwt import PyJWKClient
from fastapi import FastAPI, File, UploadFile, Request, Query, HTTPException, Depends
from fastapi.responses import JSONResponse, StreamingResponse, HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from mangum import Mangum

# ─── Configuration ───────────────────────────────────────────────────────────
DATABASE_URL = os.environ.get("DATABASE_URL", "")
OCR_SPACE_API_KEY = os.environ.get("OCR_SPACE_API_KEY", "")
CLERK_PUBLISHABLE_KEY = os.environ.get("CLERK_PUBLISHABLE_KEY", "")
CLERK_SECRET_KEY = os.environ.get("CLERK_SECRET_KEY", "")
AUTH_ENABLED = bool(CLERK_SECRET_KEY) and bool(CLERK_PUBLISHABLE_KEY)  # Need both keys
USE_SQLITE_FALLBACK = not DATABASE_URL  # Auto-use SQLite locally if no Neon URL

# Optional AI-powered categorization. Add AI_API_KEY (or OPENAI_API_KEY) to
# Vercel to enable live AI categorization. Without it, the app automatically
# falls back to a fast local keyword/scoring classifier.
AI_API_KEY = os.environ.get("AI_API_KEY") or os.environ.get("OPENAI_API_KEY", "")
AI_API_BASE = os.environ.get("AI_API_BASE", "https://api.openai.com/v1").rstrip("/")
AI_MODEL = os.environ.get("AI_MODEL", "gpt-4o-mini")

# Vercel serverless filesystems are read-only except for /tmp, so the SQLite
# fallback must use /tmp (or a local file when running outside Vercel).
IS_SERVERLESS = bool(
    os.environ.get("VERCEL") == "1"
    or os.environ.get("VERCEL_ENV")
    or os.environ.get("AWS_LAMBDA_FUNCTION_NAME")
)
_SQLITE_DB_PATH = (
    "/tmp/invoice_scanner_pro.db"
    if IS_SERVERLESS
    else os.path.join(os.path.dirname(__file__), "..", "invoices_local.db")
)

# The FastAPI startup event is not guaranteed to run under Mangum with
# lifespan="off", so we also bootstrap the database lazily on first use.
_db_initialized = False

# Exchange rates to INR (1 unit of currency = X INR)
EXCHANGE_RATES_TO_INR = {
    "INR": 1.0,
    "USD": 83.5,
    "EUR": 90.2,
    "GBP": 105.8,
    "JPY": 0.56,
    "AUD": 54.2,
    "CAD": 61.8,
    "SGD": 62.5,
    "AED": 22.7,
    "SAR": 22.2,
}

# ─── FastAPI App ─────────────────────────────────────────────────────────────
app = FastAPI(title="Invoice Scanner Pro", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Serve static files locally (Vercel handles this in production)
from starlette.staticfiles import StaticFiles
from starlette.responses import FileResponse
import pathlib
_public_dir = pathlib.Path(__file__).parent.parent / "public"
if _public_dir.exists():
    app.mount("/css", StaticFiles(directory=str(_public_dir / "css")), name="css")
    app.mount("/js", StaticFiles(directory=str(_public_dir / "js")), name="js")

@app.get("/index.html")
async def serve_index():
    index_path = pathlib.Path(__file__).parent.parent / "public" / "index.html"
    if index_path.exists():
        return HTMLResponse(index_path.read_text())
    return HTMLResponse("<h1>Invoice Scanner Pro</h1><p>Frontend files not found</p>")

# ─── Clerk Authentication ────────────────────────────────────────────────────

class ClerkAuth:
    """Handle Clerk JWT verification."""
    
    def __init__(self):
        self.jwks_client = None
        if AUTH_ENABLED:
            try:
                # Clerk's JWKS URL for token verification
                jwks_url = "https://api.clerk.com/v1/jwks"
                self.jwks_client = PyJWKClient(jwks_url, cache_jwk_set=True, lifespan=3600)
            except Exception as e:
                print(f"⚠️  Clerk JWKS init error: {e}")
    
    def verify_token(self, token: str) -> Optional[dict]:
        """Verify Clerk JWT and return user info."""
        if not AUTH_ENABLED or not self.jwks_client:
            return None
        
        try:
            # Get signing key from Clerk's JWKS
            signing_key = self.jwks_client.get_signing_key_from_jwt(token)
            
            # Decode and verify the token
            payload = jwt.decode(
                token,
                signing_key.key,
                algorithms=["RS256"],
                audience=os.environ.get("CLERK_APP_ID", ""),  # Optional audience check
                options={"verify_aud": False}  # Clerk tokens may not have audience
            )
            
            return {
                "user_id": payload.get("sub"),
                "email": payload.get("email"),
                "name": payload.get("name"),
            }
        except jwt.ExpiredSignatureError:
            raise HTTPException(status_code=401, detail="Token expired")
        except jwt.InvalidTokenError as e:
            raise HTTPException(status_code=401, detail=f"Invalid token: {str(e)}")
        except Exception as e:
            raise HTTPException(status_code=401, detail=f"Auth failed: {str(e)}")

# Global auth instance
clerk_auth = ClerkAuth()

def get_current_user(request: Request) -> Optional[str]:
    """Extract user ID from Clerk token. Returns None if auth disabled."""
    if not AUTH_ENABLED:
        return None  # No auth required
    
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing authorization token")
    
    token = auth_header.split(" ")[1]
    user_info = clerk_auth.verify_token(token)
    
    if not user_info or not user_info.get("user_id"):
        raise HTTPException(status_code=401, detail="Invalid user")
    
    return user_info["user_id"]


def get_optional_current_user(request: Request) -> Optional[str]:
    """Like get_current_user, but allows anonymous requests.

    Used for scanning, pasting OCR text, template browsing/generation and
    other actions that can be performed before signing in. Presents an invalid
    token as a 401, but treats a missing header as anonymous.
    """
    if not AUTH_ENABLED:
        return None
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return None  # anonymous
    token = auth_header.split(" ")[1]
    user_info = clerk_auth.verify_token(token)
    if not user_info or not user_info.get("user_id"):
        raise HTTPException(status_code=401, detail="Invalid user")
    return user_info["user_id"]


# ─── Database Layer ──────────────────────────────────────────────────────────

def get_db_connection():
    """Get a database connection — Neon PostgreSQL on Vercel, SQLite locally."""
    if DATABASE_URL:
        conn = psycopg2.connect(DATABASE_URL)
        conn.autocommit = False
        return conn
    else:
        # Local / serverless SQLite fallback
        import sqlite3
        conn = sqlite3.connect(_SQLITE_DB_PATH)
        conn.row_factory = sqlite3.Row
        return conn


def _ensure_database_initialized():
    """Initialize the database once per function instance (and retry on failure).

    Vercel runs the Python function in a serverless environment, so the FastAPI
    startup event is not a reliable place for schema creation. This helper is
    called on module import and on each request through a middleware so the
    `invoices` table exists before any API route touches it.
    """
    global _db_initialized
    if _db_initialized:
        return True
    try:
        init_database()
        _db_initialized = True
        return True
    except Exception as e:
        print(f"⚠️  Database initialization failed: {e}")
        return False


@app.middleware("http")
async def _bootstrap_database_middleware(request: Request, call_next):
    """Ensure tables are created before handling API requests."""
    _ensure_database_initialized()
    return await call_next(request)


def init_database():
    """Create tables if they don't exist."""
    try:
        conn = get_db_connection()
        
        if USE_SQLITE_FALLBACK:
            # SQLite schema
            conn.execute("""
                CREATE TABLE IF NOT EXISTS invoices (
                    id TEXT PRIMARY KEY,
                    user_id TEXT,
                    filename TEXT,
                    original_name TEXT,
                    vendor_name TEXT NOT NULL DEFAULT 'Unknown',
                    invoice_number TEXT NOT NULL DEFAULT '',
                    invoice_date TEXT,
                    due_date TEXT,
                    subtotal REAL DEFAULT 0,
                    tax_amount REAL DEFAULT 0,
                    tax_rate REAL DEFAULT 0,
                    cgst_amount REAL DEFAULT 0,
                    sgst_amount REAL DEFAULT 0,
                    igst_amount REAL DEFAULT 0,
                    total_amount REAL DEFAULT 0,
                    currency VARCHAR(5) DEFAULT 'INR',
                    total_inr REAL DEFAULT 0,
                    category TEXT DEFAULT 'Uncategorized',
                    categorization_source TEXT DEFAULT 'rules',
                    category_confidence REAL DEFAULT 0,
                    payment_status TEXT DEFAULT 'Pending',
                    notes TEXT,
                    raw_text TEXT,
                    line_items TEXT DEFAULT '[]',
                    confidence_score REAL DEFAULT 0,
                    created_at TEXT DEFAULT (datetime('now')),
                    updated_at TEXT DEFAULT (datetime('now'))
                )
            """)
            # Add columns that may be missing on existing databases (migration)
            for col, cd in [("user_id", "TEXT"), ("cgst_amount", "REAL DEFAULT 0"),
                            ("sgst_amount", "REAL DEFAULT 0"), ("igst_amount", "REAL DEFAULT 0"),
                            ("categorization_source", "TEXT DEFAULT 'rules'"),
                            ("category_confidence", "REAL DEFAULT 0")]:
                try:
                    conn.execute(f"ALTER TABLE invoices ADD COLUMN {col} {cd}")
                except:
                    pass  # Column already exists
            conn.execute("""
                CREATE TABLE IF NOT EXISTS invoice_templates (
                    id TEXT PRIMARY KEY,
                    user_id TEXT,
                    name TEXT NOT NULL DEFAULT 'Business Default',
                    from_name TEXT DEFAULT '',
                    from_address TEXT DEFAULT '',
                    from_email TEXT DEFAULT '',
                    from_phone TEXT DEFAULT '',
                    from_gstin TEXT DEFAULT '',
                    to_name TEXT DEFAULT '',
                    to_address TEXT DEFAULT '',
                    to_email TEXT DEFAULT '',
                    to_phone TEXT DEFAULT '',
                    to_gstin TEXT DEFAULT '',
                    currency TEXT DEFAULT 'INR',
                    tax_rate REAL DEFAULT 18,
                    discount_rate REAL DEFAULT 0,
                    terms TEXT DEFAULT 'Payment due within 30 days',
                    footer TEXT DEFAULT 'Thank you for your business!',
                    accent_color TEXT DEFAULT '#4f46e5',
                    line_items TEXT DEFAULT '[]',
                    is_builtin INTEGER DEFAULT 0,
                    created_at TEXT DEFAULT (datetime('now')),
                    updated_at TEXT DEFAULT (datetime('now'))
                )
            """)
            try:
                conn.execute("ALTER TABLE invoice_templates ADD COLUMN is_builtin INTEGER DEFAULT 0")
            except:
                pass
            try:
                conn.execute("UPDATE invoice_templates SET is_builtin = 1 WHERE user_id IS NULL AND name = 'Business Default' AND is_builtin = 0")
            except:
                pass
            conn.commit()
        else:
            # PostgreSQL schema
            cur = conn.cursor()
            cur.execute("""
                CREATE TABLE IF NOT EXISTS invoices (
                    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    user_id TEXT,
                    filename TEXT,
                    original_name TEXT,
                    vendor_name TEXT NOT NULL DEFAULT 'Unknown',
                    invoice_number TEXT NOT NULL DEFAULT '',
                    invoice_date TEXT,
                    due_date TEXT,
                    subtotal DECIMAL(14,2) DEFAULT 0,
                    tax_amount DECIMAL(14,2) DEFAULT 0,
                    tax_rate DECIMAL(6,2) DEFAULT 0,
                    cgst_amount DECIMAL(14,2) DEFAULT 0,
                    sgst_amount DECIMAL(14,2) DEFAULT 0,
                    igst_amount DECIMAL(14,2) DEFAULT 0,
                    total_amount DECIMAL(14,2) DEFAULT 0,
                    currency VARCHAR(5) DEFAULT 'INR',
                    total_inr DECIMAL(14,2) DEFAULT 0,
                    category TEXT DEFAULT 'Uncategorized',
                    categorization_source TEXT DEFAULT 'rules',
                    category_confidence DECIMAL(5,1) DEFAULT 0,
                    payment_status TEXT DEFAULT 'Pending',
                    notes TEXT,
                    raw_text TEXT,
                    line_items JSONB DEFAULT '[]'::jsonb,
                    confidence_score DECIMAL(5,1) DEFAULT 0,
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
                )
            """)
            # Add columns that may be missing on existing databases (migration)
            for col in ["user_id", "cgst_amount", "sgst_amount", "igst_amount",
                        "categorization_source", "category_confidence"]:
                try:
                    if col == "user_id":
                        ddl = "TEXT"
                    elif col == "categorization_source":
                        ddl = "TEXT DEFAULT 'rules'"
                    elif col == "category_confidence":
                        ddl = "DECIMAL(5,1) DEFAULT 0"
                    else:
                        ddl = "DECIMAL(14,2) DEFAULT 0"
                    cur.execute(f"ALTER TABLE invoices ADD COLUMN IF NOT EXISTS {col} {ddl}")
                except:
                    pass
            cur.execute("CREATE INDEX IF NOT EXISTS idx_invoices_created ON invoices(created_at DESC)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_invoices_vendor ON invoices(vendor_name)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_invoices_category ON invoices(category)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_invoices_status ON invoices(payment_status)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_invoices_date ON invoices(invoice_date)")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS invoice_templates (
                    id TEXT PRIMARY KEY,
                    user_id TEXT,
                    name TEXT NOT NULL DEFAULT 'Business Default',
                    from_name TEXT DEFAULT '',
                    from_address TEXT DEFAULT '',
                    from_email TEXT DEFAULT '',
                    from_phone TEXT DEFAULT '',
                    from_gstin TEXT DEFAULT '',
                    to_name TEXT DEFAULT '',
                    to_address TEXT DEFAULT '',
                    to_email TEXT DEFAULT '',
                    to_phone TEXT DEFAULT '',
                    to_gstin TEXT DEFAULT '',
                    currency TEXT DEFAULT 'INR',
                    tax_rate DECIMAL(6,2) DEFAULT 18,
                    discount_rate DECIMAL(6,2) DEFAULT 0,
                    terms TEXT DEFAULT 'Payment due within 30 days',
                    footer TEXT DEFAULT 'Thank you for your business!',
                    accent_color TEXT DEFAULT '#4f46e5',
                    line_items JSONB DEFAULT '[]'::jsonb,
                    is_builtin INTEGER DEFAULT 0,
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
                )
            """)
            try:
                cur.execute("ALTER TABLE invoice_templates ADD COLUMN IF NOT EXISTS is_builtin INTEGER DEFAULT 0")
                cur.execute("UPDATE invoice_templates SET is_builtin = 1 WHERE user_id IS NULL AND name = 'Business Default' AND is_builtin = 0")
            except:
                pass

        conn.commit()
        
        # Seed demo data if empty
        if USE_SQLITE_FALLBACK:
            count = conn.execute("SELECT COUNT(*) FROM invoices").fetchone()[0]
        else:
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM invoices")
            count = cur.fetchone()[0]
            cur.close()
        
        if count == 0:
            _seed_demo_data(conn)
            conn.commit()

        # Seed a default invoice template so the generator is ready to use.
        if USE_SQLITE_FALLBACK:
            t_count = conn.execute("SELECT COUNT(*) FROM invoice_templates").fetchone()[0]
        else:
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM invoice_templates")
            t_count = cur.fetchone()[0]
            cur.close()
        if t_count == 0:
            _seed_default_template(conn)
            conn.commit()

        conn.close()
        print("✅ Database initialized successfully" + (" (SQLite local mode)" if USE_SQLITE_FALLBACK else " (Neon PostgreSQL)"))
    except Exception as e:
        print(f"⚠️  Database init error: {e}")
        raise


def _seed_demo_data(conn):
    """Insert demo invoices for a great first experience."""
    import json as json_mod
    demo_data = [
        {"vendor": "Rajesh Electronics Pvt Ltd", "number": "INV-2024-0847", "date": "2024-12-15",
         "subtotal": 45000, "tax_rate": 18, "tax": 8100, "total": 53100, "currency": "INR",
         "category": "Electronics", "status": "Paid"},
        {"vendor": "CloudHost Solutions", "number": "CH-88421", "date": "2025-01-01",
         "subtotal": 149.99, "tax_rate": 0, "tax": 0, "total": 149.99, "currency": "USD",
         "category": "Cloud & Infrastructure", "status": "Paid"},
        {"vendor": "Jaipur Print Works", "number": "JPW/2025/312", "date": "2025-01-10",
         "subtotal": 12500, "tax_rate": 18, "tax": 2250, "total": 14750, "currency": "INR",
         "category": "Marketing & Printing", "status": "Pending"},
        {"vendor": "Amazon Web Services", "number": "AWS-INV-782341", "date": "2025-01-05",
         "subtotal": 234.56, "tax_rate": 0, "tax": 0, "total": 234.56, "currency": "USD",
         "category": "Cloud & Infrastructure", "status": "Paid"},
        {"vendor": "Tata Power Solar", "number": "TPS/2025/JAN/0045", "date": "2025-01-18",
         "subtotal": 285000, "tax_rate": 18, "tax": 51300, "total": 336300, "currency": "INR",
         "category": "Utilities & Energy", "status": "Pending"},
        {"vendor": "Figma Inc.", "number": "FIG-2025-00234", "date": "2025-01-20",
         "subtotal": 75, "tax_rate": 0, "tax": 0, "total": 75, "currency": "USD",
         "category": "Software & SaaS", "status": "Paid"},
    ]

    for d in demo_data:
        inv_id = str(uuid.uuid4())
        rate = EXCHANGE_RATES_TO_INR.get(d['currency'], 1.0)
        total_inr = round(d['total'] * rate, 2)

        if USE_SQLITE_FALLBACK:
            conn.execute("""
                INSERT INTO invoices (id, vendor_name, invoice_number, invoice_date, subtotal, tax_rate,
                    tax_amount, total_amount, currency, total_inr, category, payment_status, line_items, confidence_score)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (inv_id, d['vendor'], d['number'], d['date'], d['subtotal'], d['tax_rate'],
                  d['tax'], d['total'], d['currency'], total_inr, d['category'], d['status'],
                  json_mod.dumps([]), 95.0))
        else:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO invoices (id, vendor_name, invoice_number, invoice_date, subtotal, tax_rate,
                    tax_amount, total_amount, currency, total_inr, category, payment_status, line_items, confidence_score)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """, (inv_id, d['vendor'], d['number'], d['date'], d['subtotal'], d['tax_rate'],
                  d['tax'], d['total'], d['currency'], total_inr, d['category'], d['status'],
                  json_mod.dumps([]), 95.0))
            cur.close()


def _seed_default_template(conn):
    """Insert a starter invoice template so the generator works out of the box."""
    import json as json_mod
    template_id = str(uuid.uuid4())
    values = (
        template_id,
        None,
        "Business Default",
        "Your Company Pvt Ltd",
        "12 MG Road, Jaipur, Rajasthan 302001",
        "billing@yourcompany.in",
        "+91 98765 43210",
        "08AABCD1234F1Z5",
        "", "", "", "", "",
        "INR",
        18.0,
        0.0,
        "Payment due within 30 days. Please quote the invoice number with your payment.",
        "Thank you for your business! For questions, contact billing@yourcompany.in",
        "#4f46e5",
        json_mod.dumps([
            {"description": "Consulting Service", "quantity": 1, "rate": 0.0},
        ]),
        1,
    )
    if USE_SQLITE_FALLBACK:
        conn.execute("""
            INSERT INTO invoice_templates (
                id, user_id, name, from_name, from_address, from_email, from_phone,
                from_gstin, to_name, to_address, to_email, to_phone, to_gstin,
                currency, tax_rate, discount_rate, terms, footer, accent_color, line_items, is_builtin
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, values)
    else:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO invoice_templates (
                id, user_id, name, from_name, from_address, from_email, from_phone,
                from_gstin, to_name, to_address, to_email, to_phone, to_gstin,
                currency, tax_rate, discount_rate, terms, footer, accent_color, line_items, is_builtin
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s)
        """, values)
        cur.close()


# ─── Invoice Parser ──────────────────────────────────────────────────────────
_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
_AMOUNT_RE = r"[0-9][0-9,]*(?:\.[0-9]{1,2})?"


class InvoiceParser:
    """Extract structured data from OCR text using flexible line & regex parsing."""

    # Labels that mark the end of the line-item table.
    _SUMMARY_RE = re.compile(
        r"^(sub\s*total|subtotal|total\b|grand\s*total|net\s*total|net\s*payable|"
        r"amount\s*(?:due|payable|paid)|balance\s*due|"
        r"tax\s*amount|total\s*tax|taxable\s*(?:amount|value)|"
        r"gst\b|cgst\b|sgst\b|igst\b|vat\b|discount\b|round(?:\s*off)?\b)",
        re.IGNORECASE,
    )

    # Lines that look like a table header for line items.
    _ITEM_HEADER_RE = re.compile(
        r"^(s(?:r|\.)?\s*(?:no|#)|description|item|particulars?|"
        r"product|service|qty|quantity|rate|amount|price)",
        re.IGNORECASE,
    )

    _CURRENCY_LABELS = {
        "INR": r"₹|Rs\.?|INR|Indian\s+Rupee",
        "USD": r"\$\s*\d|USD|US\s*\$|US\s*Dollar",
        "EUR": r"€|EUR|Euro",
        "GBP": r"£|GBP|Pounds?",
    }

    @staticmethod
    def _lines(text: str) -> list:
        return text.replace("\r", "\n").split("\n")

    @staticmethod
    def _fix_line(line: str) -> str:
        return re.sub(r"\s+", " ", line.strip())

    @classmethod
    def _amount_on_line(cls, line: str):
        """Return (label_before_number, amount) for lines such as
        'Subtotal 1,100.00' or 'Amount Payable:  ₹ 1,230.50'."""
        line = cls._fix_line(line)
        m = re.search(r"(" + _AMOUNT_RE + r")", line)
        if not m:
            return None, None
        number = m.group(1)
        value = cls.parse_amount(number)
        idx = m.start()
        label = line[:idx].strip().rstrip(":.-₹$€£")
        return label.strip(), value

    @classmethod
    def _find_amount(cls, lines: list, labels: list) -> float:
        """Find the most specific labelled amount in priority order.

        Labels like 'grand total' / 'total amount' are checked before the
        generic 'total' label, and the generic 'total' intentionally does not
        match 'Total Tax', 'Total GST', 'Total VAT' or 'Total Discount'.
        The amount is read immediately after the matched label so flattened
        OCR text (e.g. 'Invoice 123 Total 1000.00') still returns the right
        number rather than the first number on the line.
        """
        for label in labels:
            if label.lower() == "total":
                rx = re.compile(
                    r"(?:^|(?<=[\s:;.-]))total\b(?!\s*(?:tax|gst|vat|discount|before\s*tax))",
                    re.IGNORECASE,
                )
            else:
                rx = re.compile(
                    r"(?:^|(?<=[\s:;.-]))(?:" + re.escape(label) + r")\b",
                    re.IGNORECASE,
                )
            for raw in lines:
                line = cls._fix_line(raw)
                if not line:
                    continue
                m = rx.search(line)
                if not m:
                    continue
                tail = line[m.end():]
                amt = re.search(r"(?:[$₹€£]\s*)?(" + _AMOUNT_RE + r")", tail)
                if amt:
                    return cls.parse_amount(amt.group(0))
                # Some OCR puts the label at the end of the line/column; fall
                # back to the first amount on the line.
                _, value = cls._amount_on_line(line)
                if value is not None:
                    return value
            # fallback: scan the whole text for label followed by an amount
            for raw in lines:
                line = cls._fix_line(raw)
                m = rx.search(line)
                if m:
                    tail = line[m.end():]
                    amt = re.search(r"(?:[$₹€£]\s*)?(" + _AMOUNT_RE + r")", tail)
                    if amt:
                        return cls.parse_amount(amt.group(0))
        return 0.0

    @classmethod
    def _extract_with_context(cls, lines: list, labels: list) -> Optional[str]:
        rx = re.compile(
            r"(?:^|(?<=[\s:;.-]))(?:"
            + "|".join(re.escape(l) for l in labels)
            + r")\b",
            re.IGNORECASE,
        )
        for raw in lines:
            line = cls._fix_line(raw)
            if rx.search(line):
                value = re.split(rx, line)[-1].strip().lstrip(":-. \t#")
                if value:
                    return value
        return None

    @staticmethod
    def _looks_like_date(value: str) -> bool:
        return bool(re.search(
            r"\d{1,4}[./-]\d{1,4}[./-]\d{1,4}|"
            r"\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[A-Za-z]*\.?\s+\d{2,4}|"
            r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[A-Za-z]*\.?\s+\d{1,2}(?:st|nd|rd|th)?,?\s+\d{2,4}|"
            r"\d{1,2}[-/](?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[A-Za-z]*[-/]\d{2,4}",
            value,
            re.IGNORECASE,
        ))

    @staticmethod
    def parse_amount(s: Optional[str]) -> float:
        if not s:
            return 0.0
        s = s.replace(",", "")
        m = re.search(r"-?\d+(?:\.\d+)?", s)
        if not m:
            return 0.0
        try:
            return float(m.group(0))
        except ValueError:
            return 0.0

    @staticmethod
    def _normalize_date(value: Optional[str]) -> Optional[str]:
        if not value:
            return None
        value = value.strip().strip(".,;").replace("  ", " ")
        if len(value) > 30:
            value = value[:30]
        formats = [
            "%d-%m-%Y", "%d/%m/%Y", "%d.%m.%Y", "%d-%m-%y", "%d/%m/%y",
            "%m-%d-%Y", "%m/%d/%Y", "%m.%d.%Y", "%Y-%m-%d", "%Y/%m/%d",
            "%d %b %Y", "%d-%b-%Y", "%d/%b/%Y", "%d %B %Y", "%d-%B-%Y",
            "%b %d, %Y", "%B %d, %Y", "%b %d %Y", "%B %d %Y",
        ]
        for fmt in formats:
            try:
                return datetime.strptime(value, fmt).strftime("%Y-%m-%d")
            except ValueError:
                continue
        # Try swapping ambiguous dd/mm with mm/dd when one side > 12
        m = re.match(r"^(\d{1,2})[-/.](\d{1,2})[-/.](\d{2,4})$", value)
        if m:
            a, b, y = int(m.group(1)), int(m.group(2)), m.group(3)
            if len(y) == 2:
                y = "20" + y if int(y) < 70 else "19" + y
            if b > 12 and a <= 12:  # dd/mm
                try:
                    return datetime.strptime(f"{y}-{b:02d}-{a:02d}", "%Y-%m-%d").strftime("%Y-%m-%d")
                except ValueError:
                    pass
        # Try textual month names with hyphens/slashes, e.g. 20-Aug-2026
        m = re.match(r"^(\d{1,2})[-/. ]([A-Za-z]{3,9})[-/. ](\d{2,4})$", value)
        if m:
            day = int(m.group(1))
            mon = _MONTHS.get(m.group(2)[:3].lower())
            year = int(m.group(3))
            if year < 100:
                year += 2000 if year < 70 else 1900
            if mon:
                try:
                    return datetime(year, mon, day).strftime("%Y-%m-%d")
                except ValueError:
                    return None
        return None

    @classmethod
    def extract_date(cls, text: str, labels: list, allow_fallback: bool = True) -> Optional[str]:
        lines = cls._lines(text)
        date_label = cls._extract_with_context(lines, labels)
        if date_label:
            # If the context value itself is already a date (e.g. 19-Sep-2026).
            if cls._looks_like_date(date_label):
                m = re.search(
                    r"\d{1,4}[./-]\d{1,4}[./-]\d{1,4}|"
                    r"\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[A-Za-z]*\.?\s+\d{2,4}|"
                    r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[A-Za-z]*\.?\s+\d{1,2}(?:st|nd|rd|th)?,?\s+\d{2,4}|"
                    r"\d{1,2}[-/](?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[A-Za-z]*[-/]\d{2,4}",
                    date_label,
                    re.IGNORECASE,
                )
                if m:
                    return cls._normalize_date(m.group(0))
        if not allow_fallback:
            return None
        # Fallback: find any date in the document (useful when label is missing)
        for raw in lines:
            line = cls._fix_line(raw)
            if len(line) < 5:
                continue
            m = re.search(
                r"\d{1,4}[./-]\d{1,4}[./-]\d{1,4}|"
                r"\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[A-Za-z]*\.?\s+\d{2,4}|"
                r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[A-Za-z]*\.?\s+\d{1,2}(?:st|nd|rd|th)?,?\s+\d{2,4}|"
                r"\d{1,2}[-/](?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[A-Za-z]*[-/]\d{2,4}",
                line,
                re.IGNORECASE,
            )
            if m:
                return cls._normalize_date(m.group(0))
        return None

    @classmethod
    def extract_invoice_number(cls, text: str) -> Optional[str]:
        # Always avoid matching a label word as the invoice number.
        blacklist = {"invoice", "inv", "bill", "receipt", "voucher", "date",
                     "total", "amount", "due", "no", "number", "tax", "gst"}
        patterns = [
            # Invoice No: X / Invoice # X / Bill No X / Receipt No X
            r"(?:invoice|bill|receipt|voucher|transaction)\s*(?:no|number|#)\s*[:\-.#]?\s*([A-Z0-9][A-Z0-9\-/_]{2,40})",
            # Invoice: X / Invoice X where a separator makes it unambiguous
            r"(?:invoice|bill|receipt)\s*[:\-#]\s*([A-Z0-9][A-Z0-9\-/_]{2,40})",
            # #12345 / INV-12345 / No. 12345
            r"(?:#|(?:^|[\s:(])(?:inv|invoice|bill|receipt|no)\s*[.\-#:]?\s*)([A-Z0-9][A-Z0-9\-/_]{2,40})",
            # TPS/2026/AUG/0045 etc.
            r"\b([A-Z]{2,6}[-/][0-9]{2,6}(?:[-/][0-9]{2,6})+(?:[-/][A-Z]{1,5})?)\b",
        ]
        for pat in patterns:
            m = re.search(pat, text, re.IGNORECASE | re.MULTILINE)
            if m:
                candidate = m.group(1).strip()
                if candidate.lower() not in blacklist and re.search(r"\d", candidate):
                    return candidate
        return None

    @staticmethod
    def _looks_like_address(line: str) -> bool:
        """Best-effort detection of address/contact lines (not vendor names)."""
        if not line:
            return True
        if re.match(r"^\s*\d+[\s,.-]+", line):   # 12 MG Road / 123 Main St
            return True
        if re.search(r",\s*[A-Z]{2}\s+\d{5}(?:-\d{4})?$", line):  # US ZIP
            return True
        if re.search(r"\b\d{6}(?:\s*[-–]\s*\d{4})?$", line):   # IN PIN
            return True
        if re.search(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", line, re.IGNORECASE):
            return True
        return False

    @staticmethod
    def _clean_vendor_candidate(candidate: str) -> str:
        """Trim address/contact detail from the end of a vendor candidate.

        OCR often keeps an address on the same logical line, e.g.
        'Rajesh Electronics Pvt Ltd 12 MG Road, Jaipur 302001'. Keep only the
        name portion before the street/address starts.
        """
        c = (candidate or "").strip()
        c = re.split(
            r"\s+(?:\d{1,5}(?:[\s,.][A-Za-z]+)?|\+\d|\(?\d{3}\)?[-.\s]\d{3,})"
            r"[^\s,]*[,\s]?|,?\s*[A-Z]{2}\s+\d{5}(?:-\d{4})?|"
            r"@|www\.|https?://",
            c,
            maxsplit=1,
        )[0]
        c = re.sub(
            r"^(?:invoice|bill|receipt|tax\s*invoice|quotation|estimate|statement|payment|"
            r"date|due|total|subtotal|amount|from|to|bill\s*from|bill\s*to)"
            r"\b[\s:.#\-]*",
            "", c, flags=re.IGNORECASE,
        )
        c = re.sub(r"^\s*[#.\-:\s]+", "", c).strip()
        c = re.sub(r"\s+", " ", c)
        if len(c) > 80:
            c = c[:80].rsplit(" ", 1)[0]
        return c

    @classmethod
    def extract_vendor(cls, text: str) -> str:
        lines = cls._lines(text)
        # Labeled seller/vendor lines are the most reliable.
        for raw in lines:
            line = cls._fix_line(raw)
            m = re.search(
                r"(?:from|seller|vendor|company|business|issued\s*by|billed\s*by|supplier|"
                r"bill\s*from)\s*[:\-.]?\s*(.+)",
                line,
                re.IGNORECASE,
            )
            if m:
                candidate = cls._clean_vendor_candidate(m.group(1).strip())
                if len(candidate) >= 3 and re.search(r"[A-Za-z]", candidate) and not cls._looks_like_address(candidate):
                    return candidate[:80]
        strong = re.compile(
            r"(?:Pvt|Private|Ltd|Limited|LLC|Inc|Corp|S\.A|Solutions|Enterprises|Ventures|"
            r"Services|Industries|Traders|Trading|Works|Technologies|Tech|Energy|Power|"
            r"Print|Mart|Shop|Store|Hotel|Hospital|Clinic|Pharmacy|Associates|&)",
            re.IGNORECASE,
        )
        header_label = re.compile(
            r"(^|\s)(invoice|bill|receipt|tax\s*invoice|quotation|estimate|statement|"
            r"payment|date|due|total|subtotal|amount|tax\b|gst\b|vat\b|cgst\b|sgst\b|"
            r"igst\b|discount\b|round\s*off|thank|gstin|address|phone|email)"
            r"(\b|$)",
            re.IGNORECASE,
        )
        customer_label = re.compile(
            r"(^|\s)(bill\s*to|billed\s*to|ship\s*to|shipped\s*to|customer|client|"
            r"consignee|recipient|attn|attention)(\b|$)",
            re.IGNORECASE,
        )
        for raw in lines[:15]:
            line = cls._fix_line(raw)
            if not line or header_label.search(line) or customer_label.search(line) or cls._looks_like_address(line):
                continue
            if re.fullmatch(r"[^0-9@]*[A-Za-z][A-Za-z0-9 &'.,()\-]{2,60}", line):
                if strong.search(line) or re.search(r"[A-Z]{2,}", line):
                    return line[:80]
        # Flattened OCR: find an early "Company Name LLC/Inc/Ltd/..." even when
        # the whole document is one line.
        name_suffix_re = re.compile(
            r"\b([A-Z][A-Za-z0-9&'.,()\-]*(?:\s+[A-Za-z0-9&'.,()\-]+){0,5}"
            r"\s+(?:LLC|Ltd|Limited|Inc|Pvt|Private|Corp|Corporation|"
            r"Solutions|Enterprises|Ventures|Services|Technologies|Associates|"
            r"Industries|Traders|Works|Pharmacy|Hospital|Hotel|Mart|Shop|Store))\b"
        )
        m = name_suffix_re.search(text)
        if m:
            candidate = cls._clean_vendor_candidate(m.group(1))
            if len(candidate) >= 3 and not cls._looks_like_address(candidate):
                return candidate[:80]
        # If no seller is present, fall back to the "Bill To"/customer line so
        # scanned/generated invoices still have a recognizable party name.
        for raw in lines:
            line = cls._fix_line(raw)
            m = re.search(
                r"(?:bill\s*to|billed\s*to|ship\s*to|shipped\s*to|customer|client|consignee|recipient|attn|attention)\s*[:.]?\s*(.+)",
                line,
                re.IGNORECASE,
            )
            if m:
                candidate = cls._clean_vendor_candidate(m.group(1).strip())
                if len(candidate) >= 3 and not cls._looks_like_address(candidate) and re.search(r"[A-Za-z]", candidate):
                    return candidate[:80]
        for raw in lines[:8]:
            line = cls._fix_line(raw)
            if (line and re.search(r"[A-Za-z]{2,}", line) and len(line) <= 60
                    and not header_label.search(line) and not customer_label.search(line)
                    and not cls._looks_like_address(line)
                    and not re.search(r"\b\d+\b", line)  # avoid line-item/phone/date lines
                    and re.fullmatch(r"[^0-9@]*[A-Za-z][A-Za-z0-9 &'.,()\-]{2,60}", line)):
                return line[:80]
        return "Unknown Vendor"

    @staticmethod
    def detect_currency(text: str) -> str:
        for code, pattern in InvoiceParser._CURRENCY_LABELS.items():
            if re.search(pattern, text, re.IGNORECASE):
                return code
        return "INR"

    @staticmethod
    def _extract_tax_breakdown(text: str) -> tuple:
        """Return (tax_amount, tax_rate, cgst_amount, sgst_amount, igst_amount).

        Scans GST/CGST/SGST/IGST/VAT labels anywhere in the text (not only on
        newline-separated lines), so flattened OCR output still works. When
        component amounts (CGST/SGST/IGST) are present they are authoritative
        and a combined 'Total Tax' label is not added again, preventing double
        counting.
        """
        text = text or ""
        cgst = 0.0
        sgst = 0.0
        igst = 0.0
        component_rates = []
        combined_tax = 0.0
        combined_rates = []

        def _add(match_label: str, rate: float, amt: float):
            nonlocal cgst, sgst, igst, combined_tax
            if not amt:
                return
            # Only the split-table labels are treated as components here.
            if re.search(r"\bcgst\b", match_label, re.IGNORECASE):
                cgst += amt
            elif re.search(r"\bsgst\b", match_label, re.IGNORECASE):
                sgst += amt
            elif re.search(r"\bigst\b", match_label, re.IGNORECASE):
                igst += amt
            else:
                combined_tax += amt

        # "(CGST 9%) 4500" / "CGST 9% 4500" / "Tax (8.25%) 90.75"
        rate_re = (
            r"(?:(?:cgst|sgst|igst|gst|vat|tax)\s*\()[^:]{0,18}?"
            r"(?P<rate>\d+(?:\.\d+)?)\s*%[^\d]*(?P<amt>" + _AMOUNT_RE + r")?"
            r"|(?:cgst|sgst|igst|gst|vat|tax)\s*\(?(?P<rate2>\d+(?:\.\d+)?)%\)?\s*"
            r"[:#.-]?\s*(?:(?:[$₹€£])\s*)?(?P<amt2>" + _AMOUNT_RE + r")?"
        )
        for m in re.finditer(rate_re, text, re.IGNORECASE):
            rate = float(m.group("rate") or m.group("rate2") or 0)
            amt = InvoiceParser.parse_amount(m.group("amt") or m.group("amt2"))
            label = m.group(0)
            if re.search(r"\bcgst\b|\bsgst\b|\bigst\b", label, re.IGNORECASE):
                if rate > 0:
                    component_rates.append(rate)
                _add(label, rate, amt)
            else:
                if rate > 0:
                    combined_rates.append(rate)
                _add(label, rate, amt)

        # "Tax Amount 90.75" / "Total Tax 9000"
        bare_re = (
            r"(?:cgst\s*amount|sgst\s*amount|igst\s*amount|tax\s*amount|"
            r"total\s*tax|total\s*vat|vat)\s*[:\-\s]*(?:(?:[$₹€£])\s*)?"
            r"(" + _AMOUNT_RE + r")"
        )
        for m in re.finditer(bare_re, text, re.IGNORECASE):
            amt = InvoiceParser.parse_amount(m.group(1))
            _add(m.group(0), 0.0, amt)

        component_tax = round(cgst + sgst + igst, 2)
        if component_tax > 0:
            total_tax = component_tax
            rate = round(sum(component_rates), 2) if component_rates else 0.0
        elif combined_tax > 0:
            total_tax = round(combined_tax, 2)
            rate = round(sum(combined_rates), 2) if combined_rates else 0.0
        else:
            total_tax = 0.0
            rate = 0.0

        # If the invoice only shows a combined GST amount, fall back to the
        # standard 50/50 domestic split. The caller clears this for non-INR docs.
        if cgst == 0 and sgst == 0 and igst == 0 and total_tax > 0:
            cgst = round(total_tax / 2, 2)
            sgst = round(total_tax / 2, 2)
        return total_tax, rate, round(cgst, 2), round(sgst, 2), round(igst, 2)

    @staticmethod
    def _looks_like_non_item_line(line: str) -> bool:
        """Heuristic to avoid treating totals/tax/dates as line items."""
        return bool(re.match(
            r"^(sub\s*total|subtotal|total\s*(?:amount|tax|gst|vat|discount|due|payable)?|"
            r"grand\s*total|net\s*(?:total|payable)|amount\s*(?:due|payable|paid)|"
            r"taxable\s*(?:amount|value)|tax\s*amount|total\s*tax|"
            r"tax\b|gst\b|cgst\b|sgst\b|igst\b|vat\b|discount\b|round\s*off|"
            r"invoice\b|bill\s*(?:no|date|to|from)|receipt\b|date\b|due\s*date|"
            r"payment\b|thanks?\b|bank\b|gstin\b|phone\b|email\b|address\b)",
            line, re.IGNORECASE,
        ))

    @classmethod
    def parse_line_items(cls, text: str) -> list:
        items = []
        lines = [cls._fix_line(l) for l in cls._lines(text)]
        in_section = False
        for line in lines:
            if not line or len(line) < 4:
                continue
            # Amount columns, allowing optional currency symbols.
            amount_match = list(re.finditer(r"(?:[$₹€£]\s*)?" + _AMOUNT_RE, line))
            # A summary/totals line always ends the item table first.
            if cls._SUMMARY_RE.match(line):
                break
            # A pure header row ("Item Qty Rate") has no amounts; a line like
            # "Service 500" starts with a header word but is an actual item.
            if cls._ITEM_HEADER_RE.match(line) and not amount_match:
                in_section = True
                continue
            if cls._looks_like_non_item_line(line) or cls._looks_like_address(line) or cls._looks_like_date(line):
                continue
            if not amount_match:
                continue
            # Without a header row we only trust lines with at least two numbers
            # (qty + rate + amount), which avoids dates and single-number totals.
            if not in_section:
                # Flattened OCR can put the summary on the same line as the
                # items; don't turn that whole line into one bogus item.
                if len(line) > 160 or re.search(
                    r"\b(?:subtotal|total|tax|gst|cgst|sgst|igst|vat|discount|balance|amount due)\b",
                    line, re.IGNORECASE,
                ):
                    continue
                if len(amount_match) < 2:
                    continue
            last = amount_match[-1]
            amount = cls.parse_amount(last.group(0))
            # Description = everything before the final amount column.
            desc = line[:last.start()]
            desc = re.sub(r"\s+", " ", desc).strip(" |:.-")
            # Remove trailing qty/rate/currency columns.
            parts = desc.split()
            while parts:
                tok = parts[-1]
                if re.fullmatch(r"(?:[$₹€£]\s*)?\d[\d,]*\.?\d*", tok):
                    parts.pop()
                elif tok.lower() in ("qty", "qty.", "rate", "rs", "inr", "&", "@", "x", "x1"):
                    parts.pop()
                else:
                    break
            desc = " ".join(parts).strip(" |:.-")
            if len(desc) >= 2 and amount > 0 and not cls._SUMMARY_RE.match(desc) and not cls._looks_like_non_item_line(desc):
                items.append({"description": desc[:80], "amount": amount})
        return items[:15]

    @classmethod
    def parse(cls, raw_text: str) -> dict:
        lines = cls._lines(raw_text)
        result = {}

        result['invoice_number'] = (
            cls.extract_invoice_number(raw_text) or f"INV-{uuid.uuid4().hex[:6].upper()}"
        )
        result['invoice_date'] = cls.extract_date(
            raw_text, ["invoice date", "invoice dated", "bill date", "receipt date",
                       "date of issue", "dated", "date"]
        )
        result['due_date'] = cls.extract_date(
            raw_text, ["due date", "payment due", "pay by", "due by", "net due"],
            allow_fallback=False,
        )
        result['vendor_name'] = cls.extract_vendor(raw_text)
        result['currency'] = cls.detect_currency(raw_text)

        result['total_amount'] = cls._find_amount(
            lines,
            ["grand total", "total due", "amount due", "amount payable", "total payable",
             "net payable", "total amount", "invoice total", "balance due", "total"],
        )
        result['subtotal'] = cls._find_amount(
            lines,
            ["sub total", "subtotal", "taxable value", "taxable amount", "net amount",
             "amount before tax", "total before tax", "total excluding tax", "total excl tax"],
        )
        tax_amount, tax_rate, cgst_amount, sgst_amount, igst_amount = cls._extract_tax_breakdown(raw_text)
        result['tax_amount'] = tax_amount
        result['tax_rate'] = tax_rate
        result['cgst_amount'] = cgst_amount
        result['sgst_amount'] = sgst_amount
        result['igst_amount'] = igst_amount
        # CGST/SGST/IGST only apply to Indian (INR) tax invoices.
        if result['currency'] != 'INR':
            result['cgst_amount'] = 0.0
            result['sgst_amount'] = 0.0
            result['igst_amount'] = 0.0
        result['category'] = "Uncategorized"

        if result['subtotal'] <= 0 and result['total_amount'] > 0 and result['tax_amount'] > 0:
            result['subtotal'] = round(result['total_amount'] - result['tax_amount'], 2)
        if result['total_amount'] <= 0:
            if result['subtotal'] > 0:
                result['total_amount'] = round(result['subtotal'] + result['tax_amount'], 2)
            elif result['tax_amount'] > 0:
                result['total_amount'] = round(result['tax_amount'], 2)
        if result['tax_rate'] == 0 and result['subtotal'] > 0 and result['tax_amount'] > 0:
            result['tax_rate'] = round((result['tax_amount'] / result['subtotal']) * 100, 2)

        if result['total_amount'] > 0:
            rate = EXCHANGE_RATES_TO_INR.get(result['currency'], 1.0)
            result['total_inr'] = round(result['total_amount'] * rate, 2)
        else:
            result['total_inr'] = 0

        result['line_items'] = cls.parse_line_items(raw_text)

        found = sum(1 for k in ['invoice_number', 'invoice_date', 'vendor_name', 'total_amount']
                    if result.get(k) and result[k] not in [None, 0, '', 'Unknown Vendor'])
        result['confidence_score'] = round((found / 4) * 100)
        return result


# ─── OCR Engine ──────────────────────────────────────────────────────────────

async def perform_ocr(file_bytes: bytes, filename: str) -> str:
    """Perform OCR using OCR.Space API. Returns extracted text."""
    if not OCR_SPACE_API_KEY:
        return ""

    ext = os.path.splitext(filename)[1].lower()

    # Determine overlay mode
    overlay = 0
    if ext == '.pdf':
        overlay = 0  # OCR.space handles PDFs
    elif ext in ['.jpg', '.jpeg', '.png', '.bmp', '.tiff', '.gif', '.webp']:
        overlay = 0
    else:
        return ""

    # OCR.space API limit is 1MB for free tier, 5MB for paid
    if len(file_bytes) > 1_000_000:
        # Compress image if possible
        try:
            from PIL import Image
            img = Image.open(io.BytesIO(file_bytes))
            buf = io.BytesIO()
            img.save(buf, format='JPEG', quality=70, optimize=True)
            file_bytes = buf.getvalue()
        except ImportError:
            pass

    try:
        # Convert to base64 for API
        b64 = base64.b64encode(file_bytes).decode('utf-8')

        response = requests.post(
            "https://api.ocr.space/parse/image",
            data={
                "apikey": OCR_SPACE_API_KEY,
                "language": "eng",
                "isOverlayRequired": "false",
                "scale": "true",
                "OCREngine": "2",
                "base64Image": f"data:{get_mime_type(ext)};base64,{b64}",
            },
            timeout=60,
        )

        data = response.json()
        if data.get("IsErrored"):
            print(f"OCR Error: {data.get('ErrorMessage', 'Unknown')}")
            return ""

        results = data.get("ParsedResults", [])
        if results:
            return results[0].get("ParsedText", "").strip()
        return ""

    except Exception as e:
        print(f"OCR exception: {e}")
        return ""


def get_mime_type(ext: str) -> str:
    types = {
        '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.png': 'image/png',
        '.gif': 'image/gif', '.bmp': 'image/bmp', '.tiff': 'image/tiff',
        '.webp': 'image/webp', '.pdf': 'application/pdf',
    }
    return types.get(ext, 'application/octet-stream')


# ─── Smart Category Detection ────────────────────────────────────────────────

CATEGORY_KEYWORDS = {
    "Electronics": ["laptop", "computer", "phone", "monitor", "keyboard", "mouse", "electronics", "dell", "hp", "apple", "samsung", "gadget", "hardware"],
    "Cloud & Infrastructure": ["aws", "azure", "google cloud", "hosting", "server", "cloud", "domain", "ssl", "cdn", "vps", "kubernetes"],
    "Office Supplies": ["paper", "pen", "stapler", "folder", "printer", "toner", "ink", "office", "stationery", "furniture"],
    "Marketing & Printing": ["print", "brochure", "banner", "business card", "marketing", "advertising", "flyer", "adwords", "seo"],
    "Software & SaaS": ["software", "subscription", "license", "saas", "figma", "slack", "notion", "canva", "zoom", "microsoft"],
    "Utilities & Energy": ["electricity", "power", "water", "gas", "internet", "broadband", "phone bill", "mobile recharge", "diesel", "fuel"],
    "Travel & Transport": ["flight", "hotel", "uber", "ola", "taxi", "travel", "train", "airline", "cab", "fuel", "parking"],
    "Food & Catering": ["food", "restaurant", "catering", "lunch", "dinner", "meal", "swiggy", "zomato", "hotel", "grocery"],
    "Professional Services": ["consulting", "legal", "audit", "accounting", "CA", "lawyer", "advisory", "chartered", "tax consultant", "registration"],
    "Health & Medical": ["medical", "hospital", "pharmacy", "medicine", "health", "clinic", "doctor", "lab", "insurance"],
    "Logistics & Shipping": ["logistics", "courier", "shipping", "freight", "delivery", "dhl", "fedex", "blue dart", "transport"],
    "Banking & Finance": ["bank", "loan", "interest", "emi", "insurance premium", "credit card", "emi", "payment gateway", "razorpay"],
    "Construction & Repairs": ["construction", "repair", "maintenance", "plumbing", "electrical contractor", "painting", "labour", "installation"],
    "Rent & Lease": ["rent", "lease", "maintenance charges", "society", "property"],
}

CATEGORY_LIST = sorted(CATEGORY_KEYWORDS.keys())


def keyword_categorize(text: str, vendor: str) -> str:
    """Local, fast keyword/scoring classifier. Returns (category, confidence)."""
    combined = (f"{text} {vendor}").lower()
    best, best_score = "Uncategorized", 0
    for category, keywords in CATEGORY_KEYWORDS.items():
        score = 0
        for kw in keywords:
            if kw in combined:
                score += 3 if len(kw) >= 5 else 1
        if score > best_score:
            best, best_score = category, score
    confidence = round(min(0.99, 0.45 + 0.12 * best_score) * 100)
    return best, confidence


def ai_categorize(text: str, vendor: str) -> tuple:
    """AI-powered category classification with a local fallback.

    Uses an OpenAI-compatible chat-completions endpoint when AI_API_KEY is set.
    Otherwise (or on any error) returns the fast local classifier result so the
    app keeps working without an AI key.
    """
    local_category, local_conf = keyword_categorize(text, vendor)
    if not AI_API_KEY:
        return local_category, "rules", local_conf

    allowed = ", ".join(f'"{c}"' for c in CATEGORY_LIST)
    snippet = (text or "")[:4000]
    prompt = (
        "Classify the following invoice text into exactly one of these business expense categories: "
        f"{allowed}. "
        "Respond only with a JSON object: {\"category\": \"...\", \"confidence\": 0-100}. "
        "If uncertain, still choose the best category and keep the confidence below 60. "
        f"\n\nVendor: {vendor or 'Unknown'}\nInvoice text:\n{snippet or '(no OCR text available)'}"
    )

    try:
        response = requests.post(
            f"{AI_API_BASE}/chat/completions",
            headers={
                "Authorization": f"Bearer {AI_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": AI_MODEL,
                "messages": [
                    {"role": "system", "content": "You are a finance category assistant for invoice bookkeeping."},
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0,
                "max_tokens": 60,
            },
            timeout=20,
        )
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"].strip()
        m = re.search(r"\{.*\}", content, re.DOTALL)
        if not m:
            print("⚠️  AI categorization: no JSON in response")
            return local_category, "rules", local_conf
        payload = json.loads(m.group(0))
        category = str(payload.get("category", "")).strip()
        if category not in CATEGORY_LIST:
            return local_category, "rules", local_conf
        confidence = int(float(payload.get("confidence", local_conf)))
        confidence = max(0, min(100, confidence))
        return category, "ai", confidence
    except Exception as e:
        print(f"⚠️  AI categorization failed ({e}); using local rules")
        return local_category, "rules", local_conf


def auto_categorize(text: str, vendor: str) -> str:
    """Keep the legacy helper for compatibility (returns just the category)."""
    return keyword_categorize(text, vendor)[0]


# ─── API Routes ──────────────────────────────────────────────────────────────

@app.on_event("startup")
async def startup():
    """Initialize database on startup (local dev server)."""
    _ensure_database_initialized()


@app.get("/")
async def root():
    """Redirect to index.html"""
    return HTMLResponse("""
    <!DOCTYPE html>
    <html><head><meta http-equiv="refresh" content="0;url=/index.html"></head>
    <body></body></html>
    """)


@app.get("/api/health")
async def health():
    """Health check endpoint."""
    db_ok = False
    try:
        conn = get_db_connection()
        if USE_SQLITE_FALLBACK:
            conn.execute("SELECT 1")
        else:
            conn.cursor().execute("SELECT 1")
        conn.close()
        db_ok = True
    except:
        pass
    return {
        "status": "healthy" if db_ok else "degraded",
        "database": "connected (SQLite)" if db_ok and USE_SQLITE_FALLBACK else "connected (Neon)" if db_ok else "not configured",
        "ocr": "configured" if OCR_SPACE_API_KEY else "not configured (demo mode)",
        "auth": "enabled (Clerk)" if AUTH_ENABLED else "disabled (no auth)",
        "version": "2.0.0"
    }


@app.get("/api/auth/config")
async def auth_config():
    """Return Clerk config for frontend (public — only the publishable key)."""
    return {
        "enabled": AUTH_ENABLED,
        "publishable_key": CLERK_PUBLISHABLE_KEY,
    }


@app.post("/api/scan/text")
async def scan_text(request: Request):
    """Extract invoice fields directly from pasted OCR text (debug/testing)."""
    user_id = get_optional_current_user(request)
    body = await request.json()
    raw_text = (body.get("text") or "").strip()
    if len(raw_text) < 10:
        raise HTTPException(400, "Paste at least a few lines of OCR text")
    parsed = InvoiceParser.parse(raw_text)

    # GST components (CGST/SGST/IGST) only apply to Indian INR invoices.
    if parsed.get('currency') != 'INR':
        parsed['cgst_amount'] = 0.0
        parsed['sgst_amount'] = 0.0
        parsed['igst_amount'] = 0.0

    if parsed.get('category', 'Uncategorized') == 'Uncategorized':
        category, source, confidence = ai_categorize(raw_text, parsed.get('vendor_name', ''))
        parsed['category'] = category
        parsed['categorization_source'] = source
        parsed['category_confidence'] = confidence

    if parsed.get('total_inr', 0) == 0 and parsed.get('total_amount', 0) > 0:
        rate = EXCHANGE_RATES_TO_INR.get(parsed.get('currency', 'INR'), 1.0)
        parsed['total_inr'] = round(parsed['total_amount'] * rate, 2)

    invoice_id = None
    save_error = None
    if body.get("save") is True:
        try:
            invoice_id = str(uuid.uuid4())
            _save_parsed_invoice(parsed, user_id, invoice_id, raw_text, "pasted-ocr-text.txt")
        except Exception as e:
            print(f"⚠️  Text scan DB save failed: {e}")
            save_error = str(e)

    return {
        "success": True,
        "invoice_id": invoice_id,
        "save_error": save_error,
        "data": {
            "vendor_name": parsed.get("vendor_name", "Unknown"),
            "invoice_number": parsed.get("invoice_number", ""),
            "invoice_date": parsed.get("invoice_date"),
            "due_date": parsed.get("due_date"),
            "subtotal": parsed.get("subtotal", 0),
            "tax_amount": parsed.get("tax_amount", 0),
            "tax_rate": parsed.get("tax_rate", 0),
            "cgst_amount": parsed.get("cgst_amount", 0),
            "sgst_amount": parsed.get("sgst_amount", 0),
            "igst_amount": parsed.get("igst_amount", 0),
            "total_amount": parsed.get("total_amount", 0),
            "currency": parsed.get("currency", "INR"),
            "total_inr": parsed.get("total_inr", 0),
            "category": parsed.get("category", "Uncategorized"),
            "categorization_source": parsed.get("categorization_source", "rules"),
            "category_confidence": parsed.get("category_confidence", 0),
            "confidence_score": parsed.get("confidence_score", 75),
            "line_items": parsed.get("line_items", []),
            "raw_text": raw_text[:2000],
        },
        "is_demo": False,
        "debug": {
            "ocr_configured": bool(OCR_SPACE_API_KEY),
            "ocr_length": len(raw_text),
            "demo_mode": False,
        },
        "message": "OCR text parsed successfully" + (" and saved to your library" if invoice_id else ""),
    }


@app.post("/api/scan")
async def scan_invoice(file: UploadFile = File(...), request: Request = None):
    """Upload and scan an invoice/receipt."""
    user_id = get_optional_current_user(request) if request else None
    try:
        content = await file.read()
        filename = file.filename or "unknown.jpg"

        if len(content) > 10 * 1024 * 1024:
            raise HTTPException(400, "File too large. Max 10MB.")

        # Perform OCR
        raw_text = await perform_ocr(content, filename)

        # Parse the text
        if raw_text and len(raw_text) > 20:
            parsed = InvoiceParser.parse(raw_text)
            is_demo = False
        else:
            # Demo mode — generate realistic data
            parsed = _generate_demo_result(filename)
            is_demo = True

        # GST components (CGST/SGST/IGST) only apply to Indian INR invoices.
        if parsed.get('currency') != 'INR':
            parsed['cgst_amount'] = 0.0
            parsed['sgst_amount'] = 0.0
            parsed['igst_amount'] = 0.0

        # Auto-categorize (AI when configured, local rules otherwise)
        if parsed.get('category', 'Uncategorized') == 'Uncategorized':
            category, source, confidence = ai_categorize(
                raw_text or "", parsed.get('vendor_name', '')
            )
            parsed['category'] = category
            parsed['categorization_source'] = source
            parsed['category_confidence'] = confidence

        # Ensure total_inr is calculated
        if parsed.get('total_inr', 0) == 0 and parsed.get('total_amount', 0) > 0:
            rate = EXCHANGE_RATES_TO_INR.get(parsed.get('currency', 'INR'), 1.0)
            parsed['total_inr'] = round(parsed['total_amount'] * rate, 2)

        # Save to database. If the DB write fails (e.g. a Neon/Postgres issue),
        # still return the extracted fields so the user isn't shown a blank
        # result; include the save error separately.
        invoice_id = None
        save_error = None
        try:
            invoice_id = str(uuid.uuid4())
            _save_parsed_invoice(
                parsed, user_id, invoice_id,
                raw_text=raw_text or "Demo scan",
                original_name=filename,
                filename=hashlib.md5(content).hexdigest() + os.path.splitext(filename)[1],
            )
        except Exception as e:
            print(f"⚠️  Scan DB save failed: {e}")
            save_error = str(e)

        return {
            "success": True,
            "invoice_id": invoice_id,
            "save_error": save_error,
            "data": {
                "vendor_name": parsed['vendor_name'],
                "invoice_number": parsed['invoice_number'],
                "invoice_date": parsed.get('invoice_date'),
                "due_date": parsed.get('due_date'),
                "subtotal": parsed.get('subtotal', 0),
                "tax_amount": parsed.get('tax_amount', 0),
                "tax_rate": parsed.get('tax_rate', 0),
                "cgst_amount": parsed.get('cgst_amount', 0),
                "sgst_amount": parsed.get('sgst_amount', 0),
                "igst_amount": parsed.get('igst_amount', 0),
                "total_amount": parsed.get('total_amount', 0),
                "currency": parsed.get('currency', 'INR'),
                "total_inr": parsed.get('total_inr', 0),
                "category": parsed.get('category', 'Uncategorized'),
                "categorization_source": parsed.get('categorization_source', 'rules'),
                "category_confidence": parsed.get('category_confidence', 0),
                "confidence_score": parsed.get('confidence_score', 75),
                "line_items": parsed.get('line_items', []),
                "raw_text": (raw_text or "")[:2000],
            },
            "is_demo": is_demo,
            "debug": {
                "ocr_configured": bool(OCR_SPACE_API_KEY),
                "ocr_length": len(raw_text or ""),
                "demo_mode": is_demo,
            },
            "message": "Invoice scanned successfully!" if not is_demo
                       else "Demo mode — add OCR_SPACE_API_KEY for live scanning"
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Scan failed: {str(e)}")


def _generate_demo_result(filename: str) -> dict:
    """Generate realistic demo data when OCR is not configured."""
    import random
    vendors = [
        "Office Depot India", "TechMart Solutions", "CloudHost Solutions",
        "Quick Print Services", "Metro Hardware Pvt Ltd", "Digital World Pvt Ltd",
        "Star Catering Services", "NetConnect Broadband", "SecureIT Solutions",
        "Amazon Web Services", "Figma Inc.", "Jaipur Print Works",
    ]
    amount = round(random.uniform(500, 80000), 2)
    tax_rate = random.choice([0, 5, 12, 18, 18, 18, 28])
    tax_amount = round(amount * tax_rate / 100, 2)

    return {
        "vendor_name": random.choice(vendors),
        "invoice_number": f"INV-{uuid.uuid4().hex[:8].upper()}",
        "invoice_date": datetime.now().strftime('%Y-%m-%d'),
        "subtotal": amount,
        "tax_amount": tax_amount,
        "tax_rate": tax_rate,
        "cgst_amount": round(tax_amount / 2, 2),
        "sgst_amount": round(tax_amount / 2, 2),
        "igst_amount": 0,
        "total_amount": round(amount + tax_amount, 2),
        "currency": random.choice(["INR", "INR", "INR", "USD"]),
        "total_inr": 0,  # will be calculated
        "category": "Uncategorized",
        "confidence_score": round(random.uniform(72, 94)),
        "line_items": [
            {"description": "Professional Service", "amount": round(amount * 0.6, 2)},
            {"description": "Processing Fee", "amount": round(amount * 0.4, 2)},
        ]
    }


# ─── Invoice Templates & Generator ──────────────────────────────────────────

_TEMPLATE_FIELDS = [
    "name", "from_name", "from_address", "from_email", "from_phone", "from_gstin",
    "to_name", "to_address", "to_email", "to_phone", "to_gstin",
    "currency", "tax_rate", "discount_rate", "terms", "footer", "accent_color",
    "line_items",
]

_CURRENCY_SYMBOLS = {
    "INR": "₹", "USD": "$", "EUR": "€", "GBP": "£", "JPY": "¥",
    "AUD": "A$", "CAD": "C$", "SGD": "S$", "AED": "AED", "SAR": "SAR",
}


def _to_float(value, default=0.0) -> float:
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _serialize_template(row) -> dict:
    row = dict(row)
    if isinstance(row.get("line_items"), str):
        try:
            row["line_items"] = json.loads(row["line_items"])
        except Exception:
            row["line_items"] = []
    for k in ("tax_rate", "discount_rate"):
        if row.get(k) is not None:
            row[k] = float(row[k])
    for k in ("created_at", "updated_at"):
        if row.get(k) and hasattr(row[k], "isoformat"):
            row[k] = row[k].isoformat()
    return row


def _fetch_template(conn, template_id: str, user_id) -> dict:
    if USE_SQLITE_FALLBACK:
        if user_id:
            row = conn.execute(
                "SELECT * FROM invoice_templates WHERE id = ? AND (user_id = ? OR user_id IS NULL)",
                (template_id, user_id),
            ).fetchone()
        else:
            row = conn.execute("SELECT * FROM invoice_templates WHERE id = ?", (template_id,)).fetchone()
    else:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        if user_id:
            cur.execute(
                "SELECT * FROM invoice_templates WHERE id = %s AND (user_id = %s OR user_id IS NULL)",
                (template_id, user_id),
            )
        else:
            cur.execute("SELECT * FROM invoice_templates WHERE id = %s", (template_id,))
        row = cur.fetchone()
        cur.close()
    if not row:
        raise HTTPException(404, "Template not found")
    return _serialize_template(row)


def _insert_template_record(conn, template_id: str, user_id, body: dict) -> dict:
    line_items = body.get("line_items") or []
    if isinstance(line_items, str):
        try:
            line_items = json.loads(line_items)
        except Exception:
            line_items = []
    values = (
        template_id,
        user_id,
        body.get("name") or "New Template",
        body.get("from_name", ""),
        body.get("from_address", ""),
        body.get("from_email", ""),
        body.get("from_phone", ""),
        body.get("from_gstin", ""),
        body.get("to_name", ""),
        body.get("to_address", ""),
        body.get("to_email", ""),
        body.get("to_phone", ""),
        body.get("to_gstin", ""),
        body.get("currency") or "INR",
        _to_float(body.get("tax_rate"), 18.0),
        _to_float(body.get("discount_rate"), 0.0),
        body.get("terms") or "Payment due within 30 days",
        body.get("footer") or "Thank you for your business!",
        body.get("accent_color") or "#4f46e5",
        json.dumps(line_items),
        0,
    )
    if USE_SQLITE_FALLBACK:
        conn.execute("""
            INSERT INTO invoice_templates (
                id, user_id, name, from_name, from_address, from_email, from_phone,
                from_gstin, to_name, to_address, to_email, to_phone, to_gstin,
                currency, tax_rate, discount_rate, terms, footer, accent_color, line_items, is_builtin
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, values)
    else:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO invoice_templates (
                id, user_id, name, from_name, from_address, from_email, from_phone,
                from_gstin, to_name, to_address, to_email, to_phone, to_gstin,
                currency, tax_rate, discount_rate, terms, footer, accent_color, line_items, is_builtin
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s)
        """, values)
        cur.close()
    conn.commit()
    return _fetch_template(conn, template_id, user_id)


@app.get("/api/templates")
async def list_templates(request: Request):
    """List saved invoice templates (user templates plus the built-in default)."""
    user_id = get_optional_current_user(request)
    conn = get_db_connection()
    if USE_SQLITE_FALLBACK:
        if user_id:
            rows = conn.execute(
                "SELECT * FROM invoice_templates WHERE user_id = ? OR user_id IS NULL ORDER BY user_id IS NULL DESC, name"
            , (user_id,)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM invoice_templates ORDER BY name").fetchall()
    else:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        if user_id:
            cur.execute(
                "SELECT * FROM invoice_templates WHERE user_id = %s OR user_id IS NULL ORDER BY user_id IS NULL DESC, name",
                (user_id,),
            )
        else:
            cur.execute("SELECT * FROM invoice_templates ORDER BY name")
        rows = cur.fetchall()
        cur.close()
    conn.close()
    templates = [_serialize_template(r) for r in rows]
    for t in templates:
        t["is_default"] = bool(t.get("is_builtin"))
    return {"templates": templates}


@app.post("/api/templates")
async def create_template(request: Request):
    """Create an invoice template."""
    user_id = get_optional_current_user(request)
    body = await request.json()
    template_id = str(uuid.uuid4())
    line_items = body.get("line_items") or []
    if isinstance(line_items, str):
        try:
            line_items = json.loads(line_items)
        except Exception:
            line_items = []

    values = (
        template_id,
        user_id,
        body.get("name") or "New Template",
        body.get("from_name", ""),
        body.get("from_address", ""),
        body.get("from_email", ""),
        body.get("from_phone", ""),
        body.get("from_gstin", ""),
        body.get("to_name", ""),
        body.get("to_address", ""),
        body.get("to_email", ""),
        body.get("to_phone", ""),
        body.get("to_gstin", ""),
        body.get("currency") or "INR",
        _to_float(body.get("tax_rate"), 18.0),
        _to_float(body.get("discount_rate"), 0.0),
        body.get("terms") or "Payment due within 30 days",
        body.get("footer") or "Thank you for your business!",
        body.get("accent_color") or "#4f46e5",
        json.dumps(line_items),
    )

    conn = get_db_connection()
    if USE_SQLITE_FALLBACK:
        insert_sql = """
            INSERT INTO invoice_templates (
                id, user_id, name, from_name, from_address, from_email, from_phone,
                from_gstin, to_name, to_address, to_email, to_phone, to_gstin,
                currency, tax_rate, discount_rate, terms, footer, accent_color, line_items
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """
        conn.execute(insert_sql, values)
    else:
        insert_sql = """
            INSERT INTO invoice_templates (
                id, user_id, name, from_name, from_address, from_email, from_phone,
                from_gstin, to_name, to_address, to_email, to_phone, to_gstin,
                currency, tax_rate, discount_rate, terms, footer, accent_color, line_items
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)
        """
        cur = conn.cursor()
        cur.execute(insert_sql, values)
        cur.close()
    conn.commit()

    row = _fetch_template(conn, template_id, user_id)
    conn.close()
    row["is_default"] = False
    return {"success": True, "template": row}


@app.get("/api/templates/{template_id}")
async def get_template(template_id: str, request: Request):
    user_id = get_optional_current_user(request)
    conn = get_db_connection()
    row = _fetch_template(conn, template_id, user_id)
    conn.close()
    row["is_default"] = bool(row.get("is_builtin"))
    return {"template": row}


@app.put("/api/templates/{template_id}")
async def update_template(template_id: str, request: Request):
    """Update an invoice template."""
    user_id = get_optional_current_user(request)
    body = await request.json()
    conn = get_db_connection()
    row = _fetch_template(conn, template_id, user_id)
    if row.get("user_id") and user_id and row["user_id"] != user_id:
        conn.close()
        raise HTTPException(403, "You cannot edit this template")

    # When a signed-in user edits the built-in default, clone it into their own
    # template instead of mutating the shared default.
    if row.get("is_builtin") and user_id:
        merged = {k: row.get(k) for k in _TEMPLATE_FIELDS}
        merged.update({k: v for k, v in body.items() if k in _TEMPLATE_FIELDS})
        new_id = str(uuid.uuid4())
        inserted = _insert_template_record(conn, new_id, user_id, merged)
        inserted["is_default"] = False
        conn.close()
        return {"success": True, "template": inserted}

    updates = []
    params = []
    placeholder = "?" if USE_SQLITE_FALLBACK else "%s"
    for field in _TEMPLATE_FIELDS:
        if field in body:
            if field == "line_items":
                updates.append("line_items = %s::jsonb" if not USE_SQLITE_FALLBACK else "line_items = ?")
                params.append(json.dumps(body[field] or []))
            elif field == "tax_rate" or field == "discount_rate":
                updates.append(f"{field} = {placeholder}")
                params.append(_to_float(body[field]))
            else:
                updates.append(f"{field} = {placeholder}")
                params.append(body[field])

    if not updates:
        conn.close()
        return {"success": True, "template": _serialize_template(row)}

    ts = "updated_at = datetime('now')" if USE_SQLITE_FALLBACK else "updated_at = NOW()"
    updates.append(ts)
    params.append(template_id)
    query = f"UPDATE invoice_templates SET {', '.join(updates)} WHERE id = {placeholder}"
    if USE_SQLITE_FALLBACK:
        conn.execute(query, params)
    else:
        cur = conn.cursor()
        cur.execute(query, params)
        cur.close()
    conn.commit()
    row = _fetch_template(conn, template_id, user_id)
    conn.close()
    row["is_default"] = bool(row.get("is_builtin"))
    return {"success": True, "template": row}


@app.delete("/api/templates/{template_id}")
async def delete_template(template_id: str, request: Request):
    """Delete a user template. Built-in defaults cannot be deleted."""
    user_id = get_optional_current_user(request)
    conn = get_db_connection()
    row = _fetch_template(conn, template_id, user_id)
    if row.get("is_builtin"):
        conn.close()
        raise HTTPException(400, "The built-in default template cannot be deleted")
    if user_id and row.get("user_id") and row["user_id"] != user_id:
        conn.close()
        raise HTTPException(403, "You cannot delete this template")

    placeholder = "?" if USE_SQLITE_FALLBACK else "%s"
    if USE_SQLITE_FALLBACK:
        conn.execute("DELETE FROM invoice_templates WHERE id = ?", (template_id,))
    else:
        cur = conn.cursor()
        cur.execute(f"DELETE FROM invoice_templates WHERE id = {placeholder}", (template_id,))
        cur.close()
    conn.commit()
    conn.close()
    return {"success": True, "message": "Template deleted"}


def _normalize_line_items(items) -> list:
    out = []
    for it in (items or []):
        if not isinstance(it, dict):
            continue
        desc = str(it.get("description") or "").strip()
        if not desc:
            continue
        qty = _to_float(it.get("quantity"), 1.0)
        rate = _to_float(it.get("rate"))
        amt = _to_float(it.get("amount"))
        if amt == 0 and rate != 0:
            amt = round(qty * rate, 2)
        out.append({
            "description": desc[:120],
            "quantity": round(qty, 2),
            "rate": round(rate, 2),
            "amount": round(amt, 2),
        })
    return out


def _save_parsed_invoice(parsed: dict, user_id, invoice_id: str, raw_text: str, original_name: str, filename: str = "") -> None:
    """Persist a scanned/pasted invoice into the library (SQLite + Postgres)."""
    conn = get_db_connection()
    insert_sql_pg = """
        INSERT INTO invoices (
            id, user_id, filename, original_name, vendor_name, invoice_number,
            invoice_date, due_date, subtotal, tax_amount, tax_rate, cgst_amount,
            sgst_amount, igst_amount, total_amount, currency, total_inr, category,
            categorization_source, category_confidence, payment_status, raw_text,
            line_items, confidence_score
        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s)
    """
    insert_sql_lite = """
        INSERT INTO invoices (
            id, user_id, filename, original_name, vendor_name, invoice_number,
            invoice_date, due_date, subtotal, tax_amount, tax_rate, cgst_amount,
            sgst_amount, igst_amount, total_amount, currency, total_inr, category,
            categorization_source, category_confidence, payment_status, raw_text,
            line_items, confidence_score
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """
    values = (
        invoice_id,
        user_id,
        filename or hashlib.md5((raw_text or "").encode()).hexdigest() + ".txt",
        original_name,
        parsed.get('vendor_name', 'Unknown'),
        parsed.get('invoice_number', ''),
        parsed.get('invoice_date'),
        parsed.get('due_date'),
        parsed.get('subtotal', 0),
        parsed.get('tax_amount', 0),
        parsed.get('tax_rate', 0),
        parsed.get('cgst_amount', 0),
        parsed.get('sgst_amount', 0),
        parsed.get('igst_amount', 0),
        parsed.get('total_amount', 0),
        parsed.get('currency', 'INR'),
        parsed.get('total_inr', 0),
        parsed.get('category', 'Uncategorized'),
        parsed.get('categorization_source', 'rules'),
        parsed.get('category_confidence', 0),
        'Pending',
        (raw_text or '')[:10000],
        json.dumps(parsed.get('line_items', [])),
        parsed.get('confidence_score', 75),
    )
    try:
        if USE_SQLITE_FALLBACK:
            conn.execute(insert_sql_lite, values)
        else:
            cur = conn.cursor()
            cur.execute(insert_sql_pg, values)
            cur.close()
        conn.commit()
    finally:
        conn.close()


def _save_generated_invoice(parsed: dict, user_id, invoice_id: str) -> None:
    conn = get_db_connection()
    insert_sql_pg = """
        INSERT INTO invoices (
            id, user_id, filename, original_name, vendor_name, invoice_number,
            invoice_date, due_date, subtotal, tax_amount, tax_rate, cgst_amount,
            sgst_amount, igst_amount, total_amount, currency, total_inr, category,
            categorization_source, category_confidence, payment_status, notes, raw_text,
            line_items, confidence_score
        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s)
    """
    insert_sql_lite = """
        INSERT INTO invoices (
            id, user_id, filename, original_name, vendor_name, invoice_number,
            invoice_date, due_date, subtotal, tax_amount, tax_rate, cgst_amount,
            sgst_amount, igst_amount, total_amount, currency, total_inr, category,
            categorization_source, category_confidence, payment_status, notes, raw_text,
            line_items, confidence_score
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """
    values = (
        invoice_id,
        user_id,
        "generated-invoice.html",
        "generated-invoice.html",
        parsed.get("vendor_name") or "Customer",
        parsed.get("invoice_number") or "",
        parsed.get("invoice_date"),
        parsed.get("due_date"),
        parsed.get("subtotal", 0),
        parsed.get("tax_amount", 0),
        parsed.get("tax_rate", 0),
        parsed.get("cgst_amount", 0),
        parsed.get("sgst_amount", 0),
        parsed.get("igst_amount", 0),
        parsed.get("total_amount", 0),
        parsed.get("currency") or "INR",
        parsed.get("total_inr", 0),
        parsed.get("category") or "Uncategorized",
        parsed.get("categorization_source") or "rules",
        parsed.get("category_confidence", 0),
        parsed.get("payment_status") or "Pending",
        (parsed.get("notes") or "")[:2000],
        "Generated invoice via Invoice Scanner Pro",
        json.dumps(parsed.get("line_items") or []),
        parsed.get("confidence_score", 98),
    )
    try:
        if USE_SQLITE_FALLBACK:
            conn.execute(insert_sql_lite, values)
        else:
            cur = conn.cursor()
            cur.execute(insert_sql_pg, values)
            cur.close()
        conn.commit()
    finally:
        conn.close()


def _render_invoice_html(data: dict) -> str:
    """Render a standalone, printable HTML invoice."""
    cur_sym = _CURRENCY_SYMBOLS.get(data.get("currency") or "INR", "")

    def fmt(v):
        v = float(v or 0)
        return f"{cur_sym}{v:,.2f}" if data.get("currency") != "INR" else f"{cur_sym}{v:,.2f}"

    line_items = data.get("line_items") or []
    item_rows = "".join(
        f"<tr><td>{escape(it.get('description') or 'Item')}</td>"
        f"<td class='num'>{int(it.get('quantity') or 1)}</td>"
        f"<td class='num'>{fmt(it.get('rate') or 0)}</td>"
        f"<td class='num'>{fmt(it.get('amount') or 0)}</td></tr>"
        for it in line_items
    ) or "<tr><td colspan='4' class='empty'>No line items</td></tr>"

    gst_row = ""
    if float(data.get("cgst_amount") or 0) or float(data.get("sgst_amount") or 0) or float(data.get("igst_amount") or 0):
        gst_row = (
            f"<tr><td class='muted small'>CGST</td><td class='num muted small'>{fmt(data.get('cgst_amount'))}</td></tr>"
            f"<tr><td class='muted small'>SGST</td><td class='num muted small'>{fmt(data.get('sgst_amount'))}</td></tr>"
            f"<tr><td class='muted small'>IGST</td><td class='num muted small'>{fmt(data.get('igst_amount'))}</td></tr>"
        )

    accent = (data.get("accent_color") or "#4f46e5").strip()
    if not re.match(r"^#[0-9a-fA-F]{3,8}$", accent):
        accent = "#4f46e5"

    css = """
    <style>
      * { box-sizing: border-box; margin: 0; padding: 0; }
      body { font-family: 'Inter', Arial, Helvetica, sans-serif; color: #111827; padding: 40px; -webkit-print-color-adjust: exact; }
      .invoice { max-width: 820px; margin: 0 auto; background: #fff; border: 1px solid #e5e7eb; border-radius: 12px; overflow: hidden; }
      .head { padding: 28px 34px; color: #fff; background: ACCENT; }
      .head h1 { font-size: 26px; letter-spacing: -0.5px; }
      .head p { opacity: .85; font-size: 13px; margin-top: 4px; }
      .head .inv-no { float: right; text-align: right; font-size: 13px; opacity: .9; }
      .body { padding: 28px 34px; }
      .parties { display: flex; gap: 30px; margin-bottom: 28px; }
      .party { flex: 1; }
      .party h4 { font-size: 11px; text-transform: uppercase; letter-spacing: .8px; color: #6b7280; margin-bottom: 6px; }
      .party p { font-size: 13px; line-height: 1.6; }
      .meta { display: flex; gap: 16px; flex-wrap: wrap; margin-bottom: 24px; }
      .meta .chip { background: #f9fafb; border: 1px solid #e5e7eb; border-radius: 8px; padding: 8px 12px; font-size: 12px; }
      .meta .chip b { display: block; color: #6b7280; font-size: 10px; text-transform: uppercase; letter-spacing: .5px; }
      table { width: 100%; border-collapse: collapse; margin-bottom: 24px; }
      th { background: #f9fafb; text-align: left; padding: 10px 12px; font-size: 11px; text-transform: uppercase; letter-spacing: .6px; color: #6b7280; }
      td { padding: 10px 12px; border-bottom: 1px solid #f3f4f6; font-size: 13px; }
      td.num, th.num { text-align: right; }
      .empty { text-align: center; color: #9ca3af; padding: 20px; }
      .totals { margin-left: auto; width: 280px; }
      .totals table { margin: 0; }
      .totals td { border: none; padding: 6px 12px; }
      .totals .grand td { font-weight: 800; font-size: 16px; color: ACCENT; border-top: 2px solid #e5e7eb; }
      .muted { color: #6b7280; font-size: 12px; }
      .small { font-size: 11px; }
      .notes { margin-top: 26px; padding-top: 18px; border-top: 1px dashed #e5e7eb; font-size: 13px; color: #374151; }
      .feet { padding: 18px 34px; background: #f9fafb; font-size: 12px; color: #6b7280; }
      @media print { body { padding: 0; } .invoice { border: none; border-radius: 0; } }
    </style>
    """.replace("ACCENT", accent)

    business_email = data.get("from_email") or ""
    business_phone = data.get("from_phone") or ""
    contact_parts = " · ".join(x for x in [business_email, business_phone] if x)

    body = f"""
    <div class="invoice">
      <div class="head">
        <div class="inv-no">
          <div>INVOICE</div>
          <div><b>{escape(data.get('invoice_number') or '—')}</b></div>
        </div>
        <h1>{escape(data.get('from_name') or 'Your Business')}</h1>
        <p>{escape(data.get('from_address') or '')}{(' · ' + escape(contact_parts)) if contact_parts else ''}</p>
        {('<p>GSTIN: ' + escape(data.get('from_gstin') or '')) if data.get('from_gstin') else ''}</p>
      </div>
      <div class="body">
        <div class="parties">
          <div class="party"><h4>Billed To</h4>
            <p><b>{escape(data.get('to_name') or 'Customer')}</b><br>{escape(data.get('to_address') or '')}<br>
            {escape(data.get('to_email') or '')}{(' · ' + escape(data.get('to_phone') or '')) if data.get('to_phone') else ''}
            {('<br>GSTIN: ' + escape(data.get('to_gstin') or '')) if data.get('to_gstin') else ''}</p>
          </div>
          <div class="party"><h4>Details</h4>
            <div class="meta" style="margin-bottom:0;">
              <div class="chip"><b>Invoice Date</b>{escape(data.get('invoice_date') or '—')}</div>
              <div class="chip"><b>Due Date</b>{escape(data.get('due_date') or '—')}</div>
              <div class="chip"><b>Currency</b>{escape(data.get('currency') or 'INR')}</div>
            </div>
          </div>
        </div>
        <table>
          <thead><tr><th>Description</th><th class="num">Qty</th><th class="num">Rate</th><th class="num">Amount</th></tr></thead>
          <tbody>{item_rows}</tbody>
        </table>
        <div class="totals">
          <table>
            <tr><td class="muted">Subtotal</td><td class="num">{fmt(data.get('subtotal'))}</td></tr>
            {f"<tr><td class='muted'>Discount ({_to_float(data.get('discount'), 0):.2f}%)</td><td class='num'>-{fmt(data.get('discount_amount'))}</td></tr>" if float(data.get('discount_amount') or 0) > 0 else ""}
            <tr><td class="muted">Tax ({_to_float(data.get('tax_rate'), 0):.2f}%)</td><td class="num">{fmt(data.get('tax_amount'))}</td></tr>
            {gst_row}
            <tr class="grand"><td>Total</td><td class="num">{fmt(data.get('total_amount', data.get('total')))}</td></tr>
          </table>
        </div>
        {('''<div class="notes"><b>Notes:</b> ''' + escape(data.get('notes') or '') + "</div>") if data.get('notes') else ""}
        {('''<div class="notes"><b>Terms:</b> ''' + escape(data.get('terms') or '')) if data.get('terms') else ""}</div>
      </div>
      <div class="feet">{escape(data.get('footer') or 'Thank you for your business!')}</div>
    </div>
    """

    return f"<!DOCTYPE html><html lang='en'><head><meta charset='utf-8'><title>Invoice {escape(data.get('invoice_number') or '')}</title>{css}</head><body>{body}</body></html>"


@app.post("/api/invoices/generate")
async def generate_invoice(request: Request):
    """Generate (and optionally save) an invoice from a template + form data."""
    user_id = get_optional_current_user(request)
    body = await request.json()
    template = None
    template_id = body.get("template_id") or ""
    if template_id:
        conn = get_db_connection()
        try:
            template = _fetch_template(conn, template_id, user_id)
        finally:
            conn.close()

    def t(key, fallback=""):
        if template and template.get(key) not in (None, ""):
            return template[key]
        return body.get(key, fallback)

    customer = body.get("customer") or {}
    from_name = t("from_name", "") or "Your Business"
    from_address = t("from_address", "")
    from_email = t("from_email", "")
    from_phone = t("from_phone", "")
    from_gstin = t("from_gstin", "")
    to_name = customer.get("name") or t("to_name", "")
    to_address = customer.get("address") or t("to_address", "")
    to_email = customer.get("email") or t("to_email", "")
    to_phone = customer.get("phone") or t("to_phone", "")
    to_gstin = customer.get("gstin") or t("to_gstin", "")

    currency = body.get("currency") or (template.get("currency") if template else "INR") or "INR"
    tax_rate = _to_float(body.get("tax_rate"), _to_float(template.get("tax_rate") if template else 18.0, 18.0))
    discount = _to_float(body.get("discount"), _to_float(template.get("discount_rate") if template else 0.0, 0.0))
    terms = body.get("terms") or t("terms", "")
    footer = body.get("footer") or t("footer", "")
    accent = body.get("accent_color") or t("accent_color", "#4f46e5")

    items = _normalize_line_items(body.get("line_items") or (template.get("line_items") if template else []))

    invoice_number = (body.get("invoice_number") or "").strip() or f"INV-{uuid.uuid4().hex[:8].upper()}"
    invoice_date = (body.get("invoice_date") or "").strip() or datetime.now().strftime("%Y-%m-%d")
    due_date = (body.get("due_date") or "").strip()

    subtotal = round(sum(it["amount"] for it in items), 2)
    discount_amount = round(subtotal * discount / 100.0, 2)
    taxable = round(subtotal - discount_amount, 2)
    tax_amount = round(taxable * tax_rate / 100.0, 2)
    total = round(taxable + tax_amount, 2)
    total_inr = round(total * EXCHANGE_RATES_TO_INR.get(currency, 1.0), 2)

    cgst = sgst = igst = 0.0
    if currency == "INR" and tax_amount > 0:
        cgst = round(tax_amount / 2, 2)
        sgst = round(tax_amount / 2, 2)

    category = body.get("category") or ""
    cat_src = "manual"
    cat_conf = float(body.get("category_confidence") or 90.0)
    if not category:
        cat_text = f"{to_name} " + " ".join(it["description"] for it in items)
        category, cat_src, cat_conf = ai_categorize(cat_text, to_name)

    notes = body.get("notes") or ""
    parsed = {
        "vendor_name": to_name or "Customer",
        "invoice_number": invoice_number,
        "invoice_date": invoice_date,
        "due_date": due_date,
        "subtotal": subtotal,
        "tax_amount": tax_amount,
        "tax_rate": tax_rate,
        "cgst_amount": cgst,
        "sgst_amount": sgst,
        "igst_amount": igst,
        "total_amount": total,
        "currency": currency,
        "total_inr": total_inr,
        "category": category or "Uncategorized",
        "categorization_source": cat_src,
        "category_confidence": cat_conf,
        "payment_status": body.get("payment_status") or "Pending",
        "notes": notes,
        "line_items": items,
        "confidence_score": 98,
    }

    save = body.get("save_to_library") is not False
    if save:
        invoice_id = str(uuid.uuid4())
        _save_generated_invoice(parsed, user_id, invoice_id)
    else:
        invoice_id = None

    html = _render_invoice_html({
        **parsed,
        "from_name": from_name, "from_address": from_address,
        "from_email": from_email, "from_phone": from_phone, "from_gstin": from_gstin,
        "to_name": to_name, "to_address": to_address, "to_email": to_email,
        "to_phone": to_phone, "to_gstin": to_gstin,
        "discount": discount, "discount_amount": discount_amount,
        "terms": terms, "footer": footer, "accent_color": accent,
    })

    return {
        "success": True,
        "invoice_id": invoice_id,
        "saved": save,
        "data": parsed,
        "html": html,
        "message": "Invoice generated" + (" and saved to your library" if save else ""),
    }


@app.get("/api/invoices")
async def list_invoices(
    request: Request,
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    search: str = Query(""),
    category: str = Query(""),
    status: str = Query(""),
    sort_by: str = Query("created_at"),
    sort_order: str = Query("desc"),
):
    """List invoices with filtering, search, and pagination."""
    user_id = get_current_user(request)
    conn = get_db_connection()
    
    # Build WHERE clause
    conditions = []
    params = []
    
    # Filter by user if auth is enabled
    if user_id:
        if USE_SQLITE_FALLBACK:
            conditions.append("user_id = ?")
        else:
            conditions.append("user_id = %s")
        params.append(user_id)
    
    if search:
        if USE_SQLITE_FALLBACK:
            conditions.append("(vendor_name LIKE ? OR invoice_number LIKE ?)")
        else:
            conditions.append("(vendor_name ILIKE %s OR invoice_number ILIKE %s)")
        params.extend([f"%{search}%", f"%{search}%"])
    if category:
        if USE_SQLITE_FALLBACK:
            conditions.append("category = ?")
        else:
            conditions.append("category = %s")
        params.append(category)
    if status:
        if USE_SQLITE_FALLBACK:
            conditions.append("payment_status = ?")
        else:
            conditions.append("payment_status = %s")
        params.append(status)
    
    where = " AND ".join(conditions) if conditions else "1=1"
    
    # Get total count
    if USE_SQLITE_FALLBACK:
        count_sql = f"SELECT COUNT(*) FROM invoices WHERE {where}"
        row = conn.execute(count_sql, params).fetchone()
        total = row[0] if row else 0
    else:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        count_sql = f"SELECT COUNT(*) as total FROM invoices WHERE {where}"
        cur.execute(count_sql, params)
        total = cur.fetchone()['total']
        cur.close()
    
    # Sort
    allowed_sorts = {"created_at", "total_amount", "invoice_date", "vendor_name", "total_inr"}
    if sort_by not in allowed_sorts:
        sort_by = "created_at"
    order = "DESC" if sort_order.lower() == "desc" else "ASC"
    
    # Get paginated results
    offset = (page - 1) * limit
    
    if USE_SQLITE_FALLBACK:
        query_sql = f"SELECT * FROM invoices WHERE {where} ORDER BY {sort_by} {order} LIMIT ? OFFSET ?"
        rows = conn.execute(query_sql, params + [limit, offset]).fetchall()
        invoices = [dict(row) for row in rows]
    else:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        query_sql = f"SELECT * FROM invoices WHERE {where} ORDER BY {sort_by} {order} LIMIT %s OFFSET %s"
        cur.execute(query_sql, params + [limit, offset])
        rows = cur.fetchall()
        invoices = [dict(row) for row in rows]
        cur.close()
    
    # Convert types
    for inv in invoices:
        for k in ['subtotal', 'tax_amount', 'tax_rate', 'cgst_amount', 'sgst_amount', 'igst_amount', 'total_amount', 'total_inr', 'category_confidence', 'confidence_score']:
            if inv.get(k) is not None:
                inv[k] = float(inv[k])
        if isinstance(inv.get('line_items'), str):
            try:
                inv['line_items'] = json.loads(inv['line_items'])
            except:
                inv['line_items'] = []
        if inv.get('created_at') and hasattr(inv['created_at'], 'isoformat'):
            inv['created_at'] = inv['created_at'].isoformat()
        if inv.get('updated_at') and hasattr(inv['updated_at'], 'isoformat'):
            inv['updated_at'] = inv['updated_at'].isoformat()
    
    conn.close()
    
    return {
        "invoices": invoices,
        "total": total,
        "page": page,
        "limit": limit,
        "total_pages": max(1, (total + limit - 1) // limit)
    }


@app.get("/api/invoices/{invoice_id}")
async def get_invoice(invoice_id: str, request: Request):
    """Get a single invoice by ID."""
    user_id = get_current_user(request)
    conn = get_db_connection()
    
    if USE_SQLITE_FALLBACK:
        if user_id:
            row = conn.execute("SELECT * FROM invoices WHERE id = ? AND user_id = ?", (invoice_id, user_id)).fetchone()
        else:
            row = conn.execute("SELECT * FROM invoices WHERE id = ?", (invoice_id,)).fetchone()
    else:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        if user_id:
            cur.execute("SELECT * FROM invoices WHERE id = %s AND user_id = %s", (invoice_id, user_id))
        else:
            cur.execute("SELECT * FROM invoices WHERE id = %s", (invoice_id,))
        row = cur.fetchone()
        cur.close()
    
    conn.close()
    
    if not row:
        raise HTTPException(404, "Invoice not found")
    
    inv = dict(row)
    for k in ['subtotal', 'tax_amount', 'tax_rate', 'cgst_amount', 'sgst_amount', 'igst_amount', 'total_amount', 'total_inr', 'category_confidence', 'confidence_score']:
        if inv.get(k) is not None:
            inv[k] = float(inv[k])
    if isinstance(inv.get('line_items'), str):
        try:
            inv['line_items'] = json.loads(inv['line_items'])
        except:
            inv['line_items'] = []
    if inv.get('created_at') and hasattr(inv['created_at'], 'isoformat'):
        inv['created_at'] = inv['created_at'].isoformat()
    if inv.get('updated_at') and hasattr(inv['updated_at'], 'isoformat'):
        inv['updated_at'] = inv['updated_at'].isoformat()
    
    return {"invoice": inv}


@app.put("/api/invoices/{invoice_id}")
async def update_invoice(invoice_id: str, request: Request):
    """Update invoice fields."""
    body = await request.json()
    conn = get_db_connection()
    
    allowed = ['vendor_name', 'invoice_number', 'invoice_date', 'category',
               'payment_status', 'total_amount', 'tax_amount', 'notes', 'currency', 'due_date']
    updates = []
    params = []
    placeholder = "?" if USE_SQLITE_FALLBACK else "%s"
    
    for field in allowed:
        if field in body:
            updates.append(f"{field} = {placeholder}")
            params.append(body[field])
    
    if not updates:
        conn.close()
        return {"error": "No valid fields to update"}
    
    # Recalculate total_inr if total_amount or currency changed
    if 'total_amount' in body or 'currency' in body:
        if USE_SQLITE_FALLBACK:
            row = conn.execute("SELECT total_amount, currency FROM invoices WHERE id = ?", (invoice_id,)).fetchone()
        else:
            cur = conn.cursor()
            cur.execute("SELECT total_amount, currency FROM invoices WHERE id = %s", (invoice_id,))
            row = cur.fetchone()
            cur.close()
        if row:
            amt = float(body.get('total_amount', row[0] if isinstance(row, tuple) else row['total_amount']))
            cur_code = body.get('currency', row[1] if isinstance(row, tuple) else row['currency'])
            rate = EXCHANGE_RATES_TO_INR.get(cur_code, 1.0)
            updates.append(f"total_inr = {placeholder}")
            params.append(round(amt * rate, 2))
    
    ts_update = "updated_at = datetime('now')" if USE_SQLITE_FALLBACK else "updated_at = NOW()"
    updates.append(ts_update)
    params.append(invoice_id)
    
    query = f"UPDATE invoices SET {', '.join(updates)} WHERE id = {placeholder}"
    
    if USE_SQLITE_FALLBACK:
        conn.execute(query, params)
    else:
        cur = conn.cursor()
        cur.execute(query, params)
        cur.close()
    
    conn.commit()
    conn.close()
    return {"success": True, "message": "Invoice updated"}


@app.delete("/api/invoices/{invoice_id}")
async def delete_invoice(invoice_id: str, request: Request):
    """Delete an invoice."""
    user_id = get_current_user(request)
    conn = get_db_connection()
    placeholder = "?" if USE_SQLITE_FALLBACK else "%s"
    
    if user_id:
        uid_placeholder = "?" if USE_SQLITE_FALLBACK else "%s"
        query = f"DELETE FROM invoices WHERE id = {placeholder} AND user_id = {uid_placeholder}"
        qparams = (invoice_id, user_id)
    else:
        query = f"DELETE FROM invoices WHERE id = {placeholder}"
        qparams = (invoice_id,)
    
    if USE_SQLITE_FALLBACK:
        cursor = conn.execute(query, qparams)
        if cursor.rowcount == 0:
            conn.close()
            raise HTTPException(404, "Invoice not found")
    else:
        cur = conn.cursor()
        cur.execute(query, qparams)
        if cur.rowcount == 0:
            cur.close()
            conn.close()
            raise HTTPException(404, "Invoice not found")
        cur.close()
    
    conn.commit()
    conn.close()
    return {"success": True, "message": "Invoice deleted"}


@app.get("/api/dashboard")
async def dashboard_stats(request: Request):
    """Get dashboard statistics."""
    user_id = get_current_user(request)
    conn = get_db_connection()
    
    # Add user filter
    user_where = ""
    user_params = []
    if user_id:
        if USE_SQLITE_FALLBACK:
            user_where = "WHERE user_id = ?"
        else:
            user_where = "WHERE user_id = %s"
        user_params = [user_id]
    
    if USE_SQLITE_FALLBACK:
        total_invoices = conn.execute(f"SELECT COUNT(*) FROM invoices {user_where}", user_params).fetchone()[0]
        total_amount = conn.execute(f"SELECT COALESCE(SUM(total_inr), 0) FROM invoices {user_where}", user_params).fetchone()[0]
        uw = user_where + (" AND " if user_where else "WHERE ")
        pending_amount = conn.execute(f"SELECT COALESCE(SUM(total_inr), 0) FROM invoices {uw}payment_status = 'Pending'", user_params).fetchone()[0]
        paid_amount = conn.execute(f"SELECT COALESCE(SUM(total_inr), 0) FROM invoices {uw}payment_status = 'Paid'", user_params).fetchone()[0]
        
        cat_rows = conn.execute(f"""
            SELECT category, COUNT(*) as count, COALESCE(SUM(total_inr), 0) as amount
            FROM invoices {user_where} GROUP BY category ORDER BY amount DESC
        """, user_params).fetchall()
        categories = [{"category": r[0], "count": r[1], "amount": float(r[2])} for r in cat_rows]
        
        uw = user_where + (" AND " if user_where else "WHERE ")
        month_rows = conn.execute(f"""
            SELECT strftime('%Y-%m', invoice_date) as month, COUNT(*) as count, COALESCE(SUM(total_inr), 0) as amount
            FROM invoices {uw}invoice_date IS NOT NULL
            GROUP BY month ORDER BY month DESC LIMIT 6
        """, user_params).fetchall()
        monthly = [{"month": r[0], "count": r[1], "amount": float(r[2])} for r in month_rows]
        monthly.reverse()
        
        recent_rows = conn.execute(f"SELECT * FROM invoices {user_where} ORDER BY created_at DESC LIMIT 5", user_params).fetchall()
        recent = []
        for r in recent_rows:
            inv = dict(r)
            for k in ['subtotal', 'tax_amount', 'tax_rate', 'cgst_amount', 'sgst_amount', 'igst_amount', 'total_amount', 'total_inr', 'category_confidence', 'confidence_score']:
                if inv.get(k) is not None:
                    inv[k] = float(inv[k])
            if isinstance(inv.get('line_items'), str):
                try: inv['line_items'] = json.loads(inv['line_items'])
                except: inv['line_items'] = []
            recent.append(inv)
    else:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        
        cur.execute(f"SELECT COUNT(*) as c FROM invoices {user_where}", user_params)
        total_invoices = cur.fetchone()['c']
        
        cur.execute(f"SELECT COALESCE(SUM(total_inr), 0) as t FROM invoices {user_where}", user_params)
        total_amount = float(cur.fetchone()['t'])
        
        uw = user_where + (" AND " if user_where else "WHERE ")
        cur.execute(f"SELECT COALESCE(SUM(total_inr), 0) as t FROM invoices {uw}payment_status = 'Pending'", user_params)
        pending_amount = float(cur.fetchone()['t'])
        
        cur.execute(f"SELECT COALESCE(SUM(total_inr), 0) as t FROM invoices {uw}payment_status = 'Paid'", user_params)
        paid_amount = float(cur.fetchone()['t'])
        
        cur.execute(f"""
            SELECT category, COUNT(*) as count, COALESCE(SUM(total_inr), 0) as amount
            FROM invoices {user_where} GROUP BY category ORDER BY amount DESC
        """, user_params)
        categories = [{"category": r['category'], "count": r['count'], "amount": float(r['amount'])} for r in cur.fetchall()]
        
        cur.execute(f"""
            SELECT TO_CHAR(invoice_date::date, 'YYYY-MM') as month, COUNT(*) as count, COALESCE(SUM(total_inr), 0) as amount
            FROM invoices {uw}invoice_date IS NOT NULL
            GROUP BY month ORDER BY month DESC LIMIT 6
        """, user_params)
        monthly = [{"month": r['month'], "count": r['count'], "amount": float(r['amount'])} for r in cur.fetchall()]
        monthly.reverse()
        
        cur.execute(f"SELECT * FROM invoices {user_where} ORDER BY created_at DESC LIMIT 5", user_params)
        recent = []
        for r in cur.fetchall():
            inv = dict(r)
            for k in ['subtotal', 'tax_amount', 'tax_rate', 'cgst_amount', 'sgst_amount', 'igst_amount', 'total_amount', 'total_inr', 'category_confidence', 'confidence_score']:
                if inv.get(k) is not None:
                    inv[k] = float(inv[k])
            if isinstance(inv.get('line_items'), str):
                try: inv['line_items'] = json.loads(inv['line_items'])
                except: inv['line_items'] = []
            if inv.get('created_at') and hasattr(inv['created_at'], 'isoformat'):
                inv['created_at'] = inv['created_at'].isoformat()
            recent.append(inv)
        cur.close()
    
    conn.close()
    
    return {
        "total_invoices": total_invoices,
        "total_amount": round(float(total_amount), 2),
        "pending_amount": round(float(pending_amount), 2),
        "paid_amount": round(float(paid_amount), 2),
        "categories": categories,
        "monthly": monthly,
        "recent": recent,
    }


@app.get("/api/export/csv")
async def export_csv():
    """Export all invoices as CSV."""
    conn = get_db_connection()
    
    if USE_SQLITE_FALLBACK:
        rows = conn.execute("SELECT * FROM invoices ORDER BY created_at DESC").fetchall()
        all_rows = [dict(r) for r in rows]
    else:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM invoices ORDER BY created_at DESC")
        all_rows = [dict(r) for r in cur.fetchall()]
        cur.close()
    
    conn.close()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "Invoice #", "Vendor", "Date", "Subtotal", "Tax Rate %", "Tax Amount",
        "Total", "Currency", "Total (INR)", "Category", "Status"
    ])

    for r in all_rows:
        writer.writerow([
            r['invoice_number'], r['vendor_name'], r['invoice_date'],
            float(r['subtotal'] or 0), float(r['tax_rate'] or 0), float(r['tax_amount'] or 0),
            float(r['total_amount'] or 0), r['currency'], float(r['total_inr'] or 0),
            r['category'], r['payment_status']
        ])

    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=invoice_scanner_export.csv"}
    )


# ─── GST & Accounting Exports ────────────────────────────────────────────────

def _fetch_all_invoices():
    """Return all stored invoices as dicts (works for SQLite and PostgreSQL)."""
    conn = get_db_connection()
    if USE_SQLITE_FALLBACK:
        rows = [dict(r) for r in conn.execute("SELECT * FROM invoices ORDER BY invoice_date DESC, created_at DESC").fetchall()]
    else:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM invoices ORDER BY invoice_date DESC, created_at DESC")
        rows = [dict(r) for r in cur.fetchall()]
        cur.close()
    conn.close()
    for inv in rows:
        for k in ['subtotal', 'tax_amount', 'tax_rate', 'cgst_amount', 'sgst_amount',
                  'igst_amount', 'total_amount', 'total_inr', 'category_confidence',
                  'confidence_score']:
            if inv.get(k) is not None:
                inv[k] = float(inv[k])
        if isinstance(inv.get('line_items'), str):
            try:
                inv['line_items'] = json.loads(inv['line_items'])
            except:
                inv['line_items'] = []
    return rows


def _gst_components(inv: dict):
    """Return (taxable, cgst, sgst, igst, total_tax) for an invoice.

    For invoices that do not store a CGST/SGST/IGST split, the code estimates a
    50/50 domestic split from the total tax amount.
    """
    tax = float(inv.get('tax_amount') or 0)
    cgst = float(inv.get('cgst_amount') or 0)
    sgst = float(inv.get('sgst_amount') or 0)
    igst = float(inv.get('igst_amount') or 0)
    if cgst == 0 and sgst == 0 and igst == 0:
        cgst = round(tax / 2, 2)
        sgst = round(tax / 2, 2)
    taxable = float(inv.get('subtotal') or 0)
    if taxable <= 0:
        taxable = max(0.0, float(inv.get('total_amount') or 0) - tax)
    return taxable, cgst, sgst, igst, round(cgst + sgst + igst, 2)


@app.get("/api/gst/report")
async def gst_report(
    request: Request,
    year: str = Query(""),
    month: str = Query(""),
    status: str = Query(""),
    category: str = Query(""),
):
    """GST summary for Indian businesses: rate-wise, category-wise, monthly."""
    invoices = _fetch_all_invoices()
    summary = {
        "invoices": 0, "taxable_value": 0.0, "cgst": 0.0, "sgst": 0.0,
        "igst": 0.0, "total_tax": 0.0, "total_invoice_amount": 0.0,
    }
    by_rate = {}
    by_category = {}
    by_month = {}

    for inv in invoices:
        # GST reports are for INR invoices only.
        if (inv.get('currency') or 'INR') != 'INR':
            continue
        inv_date = inv.get('invoice_date') or ''
        y = inv_date[:4]
        m = inv_date[5:7]
        if year and y != year:
            continue
        if month and m != month.zfill(2):
            continue
        if status and (inv.get('payment_status') or '') != status:
            continue
        if category and (inv.get('category') or '') != category:
            continue

        taxable, cgst, sgst, igst, total_tax = _gst_components(inv)
        total_amount = float(inv.get('total_amount') or taxable + total_tax)
        rate = float(inv.get('tax_rate') or 0)
        cat = inv.get('category') or 'Uncategorized'
        ym = y + ("-" + m if m else "")

        summary["invoices"] += 1
        summary["taxable_value"] += taxable
        summary["cgst"] += cgst
        summary["sgst"] += sgst
        summary["igst"] += igst
        summary["total_tax"] += total_tax
        summary["total_invoice_amount"] += total_amount

        key_rate = round(rate, 2)
        item = by_rate.setdefault(key_rate, {
            "rate": key_rate, "invoices": 0, "taxable_value": 0.0,
            "cgst": 0.0, "sgst": 0.0, "igst": 0.0, "total_tax": 0.0,
        })
        item["invoices"] += 1
        item["taxable_value"] += taxable
        item["cgst"] += cgst
        item["sgst"] += sgst
        item["igst"] += igst
        item["total_tax"] += total_tax

        item_c = by_category.setdefault(cat, {
            "category": cat, "invoices": 0, "taxable_value": 0.0,
            "cgst": 0.0, "sgst": 0.0, "igst": 0.0, "total_tax": 0.0,
        })
        item_c["invoices"] += 1
        item_c["taxable_value"] += taxable
        item_c["cgst"] += cgst
        item_c["sgst"] += sgst
        item_c["igst"] += igst
        item_c["total_tax"] += total_tax

        item_m = by_month.setdefault(ym, {
            "month": ym, "invoices": 0, "taxable_value": 0.0,
            "cgst": 0.0, "sgst": 0.0, "igst": 0.0, "total_tax": 0.0,
        })
        item_m["invoices"] += 1
        item_m["taxable_value"] += taxable
        item_m["cgst"] += cgst
        item_m["sgst"] += sgst
        item_m["igst"] += igst
        item_m["total_tax"] += total_tax

    summary["taxable_value"] = round(summary["taxable_value"], 2)
    summary["cgst"] = round(summary["cgst"], 2)
    summary["sgst"] = round(summary["sgst"], 2)
    summary["igst"] = round(summary["igst"], 2)
    summary["total_tax"] = round(summary["total_tax"], 2)
    summary["total_invoice_amount"] = round(summary["total_invoice_amount"], 2)

    def _finish(items):
        for it in items.values():
            for k in ("taxable_value", "cgst", "sgst", "igst", "total_tax"):
                it[k] = round(it[k], 2)
        return sorted(items.values(), key=lambda x: x.get("rate", x.get("month", x.get("category", ""))))

    return {
        "summary": summary,
        "by_rate": _finish(by_rate),
        "by_category": sorted(by_category.values(), key=lambda x: x["total_tax"], reverse=True),
        "by_month": sorted(by_month.values(), key=lambda x: x["month"]),
    }


@app.get("/api/export/tally")
async def export_tally():
    """Export invoices as a Tally-compatible XML voucher file (.xml)."""
    invoices = _fetch_all_invoices()
    today = datetime.now().strftime("%Y%m%d")
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        "<ENVELOPE>",
        "<HEADER><TALLYREQUEST>Import Data</TALLYREQUEST></HEADER>",
        "<BODY><IMPORTDATA><REQUESTDESC><REPORTNAME>Vouchers</REPORTNAME></REQUESTDESC><REQUESTDATA>",
    ]
    for inv in invoices:
        date_str = (inv.get('invoice_date') or today)[:10].replace("-", "")
        invoice_no = escape(str(inv.get('invoice_number') or ''))
        vendor = escape(str(inv.get('vendor_name') or 'Unknown'))
        guid = escape(str(inv.get('id') or ''))
        subtotal = float(inv.get('subtotal') or 0)
        tax = float(inv.get('tax_amount') or 0)
        total = float(inv.get('total_amount') or subtotal + tax)
        lines.append('<TALLYMESSAGE xmlns:UDF="">')
        lines.append('<VOUCHER VCHTYPE="Purchase" ACTION="Create" OBJVIEW="Invoice">')
        lines.append(f"<DATE>{date_str}</DATE>")
        lines.append(f"<GUID>{guid}</GUID>")
        lines.append("<VOUCHERTYPENAME>Purchase</VOUCHERTYPENAME>")
        lines.append(f"<VOUCHERNUMBER>{invoice_no}</VOUCHERNUMBER>")
        lines.append(f"<PARTYLEDGERNAME>{vendor}</PARTYLEDGERNAME>")
        lines.append(f"<AMOUNT>{total:.2f}</AMOUNT>")
        lines.append('<ALLLEDGERENTRIES.LIST>')
        lines.append('<LEDGERNAME>Purchase Account</LEDGERNAME>')
        lines.append('<ISDEEMEDPOSITIVE>No</ISDEEMEDPOSITIVE>')
        lines.append(f"<AMOUNT>{subtotal:.2f}</AMOUNT>")
        lines.append('</ALLLEDGERENTRIES.LIST>')
        if tax > 0:
            lines.append('<ALLLEDGERENTRIES.LIST>')
            lines.append('<LEDGERNAME>GST Payable</LEDGERNAME>')
            lines.append('<ISDEEMEDPOSITIVE>No</ISDEEMEDPOSITIVE>')
            lines.append(f"<AMOUNT>{tax:.2f}</AMOUNT>")
            lines.append('</ALLLEDGERENTRIES.LIST>')
        lines.append('</VOUCHER>')
        lines.append('</TALLYMESSAGE>')
    lines.append("</REQUESTDATA></IMPORTDATA></BODY></ENVELOPE>")

    content = "\n".join(lines)
    return StreamingResponse(
        iter([content]),
        media_type="application/xml",
        headers={"Content-Disposition": "attachment; filename=invoice_tally_export.xml"},
    )


@app.get("/api/export/quickbooks")
async def export_quickbooks():
    """Export invoices as a QuickBooks IIF import file (.iif)."""
    invoices = _fetch_all_invoices()
    rows = ['!TRNS\tTRNSID\tTRNSTYPE\tDATE\tNAME\tAMOUNT\tDOCNUM\tMEMO\tCLEAR\tTOPRINT']
    rows.append('!SPL\tSPLID\tTRNSTYPE\tDATE\tNAME\tAMOUNT\tMEMO')

    def txt(s):
        return str(s).replace("\t", " ").replace("\n", " ")

    for idx, inv in enumerate(invoices, start=1):
        trnsid = idx
        date_val = (inv.get('invoice_date') or datetime.now().strftime('%Y-%m-%d'))[:10]
        try:
            qb_date = datetime.strptime(date_val, "%Y-%m-%d").strftime("%m/%d/%Y")
        except ValueError:
            qb_date = date_val
        vendor = txt(inv.get('vendor_name') or 'Unknown Vendor')
        inv_no = txt(inv.get('invoice_number') or f'INV-{idx}')
        memo = txt(f"{inv.get('category') or ''} {inv_no}".strip())
        subtotal = float(inv.get('subtotal') or 0)
        tax = float(inv.get('tax_amount') or 0)
        total = float(inv.get('total_amount') or subtotal + tax)

        rows.append(f"!TRNS\t{trnsid}\tBILL\t{qb_date}\t{vendor}\t{total:.2f}\t{inv_no}\t{memo}\tN\tN")
        if subtotal:
            rows.append(f"!SPL\t{trnsid}\tBILL\t{qb_date}\tExpenses\t{subtotal:.2f}\tBill amount")
        if tax > 0:
            rows.append(f"!SPL\t{trnsid}\tBILL\t{qb_date}\tGST Payable\t{tax:.2f}\tGST")
        rows.append(f"!ENDTRNS\t{trnsid}")

    content = "\n".join(rows) + "\n"
    return StreamingResponse(
        iter([content]),
        media_type="text/plain",
        headers={"Content-Disposition": "attachment; filename=invoice_quickbooks_export.iif"},
    )


@app.get("/api/currencies")
async def get_currencies():
    """Get supported currencies."""
    return {
        "base": "INR",
        "rates": EXCHANGE_RATES_TO_INR,
        "currencies": list(EXCHANGE_RATES_TO_INR.keys())
    }


# ─── Vercel Serverless Handler ───────────────────────────────────────────────

# Bootstrap the database during the serverless cold start so the tables are
# ready before the first request is handled (Mangum lifespan is disabled).
_ensure_database_initialized()

handler = Mangum(app, lifespan="off")
