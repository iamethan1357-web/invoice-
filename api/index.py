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
AUTH_ENABLED = bool(CLERK_SECRET_KEY)  # Auth only if secret key is set
USE_SQLITE_FALLBACK = not DATABASE_URL  # Auto-use SQLite locally if no Neon URL

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
                    total_amount REAL DEFAULT 0,
                    currency VARCHAR(5) DEFAULT 'INR',
                    total_inr REAL DEFAULT 0,
                    category TEXT DEFAULT 'Uncategorized',
                    payment_status TEXT DEFAULT 'Pending',
                    notes TEXT,
                    raw_text TEXT,
                    line_items TEXT DEFAULT '[]',
                    confidence_score REAL DEFAULT 0,
                    created_at TEXT DEFAULT (datetime('now')),
                    updated_at TEXT DEFAULT (datetime('now'))
                )
            """)
            # Add user_id column if it doesn't exist (migration)
            try:
                conn.execute("ALTER TABLE invoices ADD COLUMN user_id TEXT")
                conn.commit()
            except:
                pass  # Column already exists
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
                    total_amount DECIMAL(14,2) DEFAULT 0,
                    currency VARCHAR(5) DEFAULT 'INR',
                    total_inr DECIMAL(14,2) DEFAULT 0,
                    category TEXT DEFAULT 'Uncategorized',
                    payment_status TEXT DEFAULT 'Pending',
                    notes TEXT,
                    raw_text TEXT,
                    line_items JSONB DEFAULT '[]'::jsonb,
                    confidence_score DECIMAL(5,1) DEFAULT 0,
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
                )
            """)
            # Add user_id column if migration needed
            try:
                cur.execute("ALTER TABLE invoices ADD COLUMN IF NOT EXISTS user_id TEXT")
            except:
                pass
            cur.execute("CREATE INDEX IF NOT EXISTS idx_invoices_created ON invoices(created_at DESC)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_invoices_vendor ON invoices(vendor_name)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_invoices_category ON invoices(category)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_invoices_status ON invoices(payment_status)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_invoices_date ON invoices(invoice_date)")

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


# ─── Invoice Parser ──────────────────────────────────────────────────────────

class InvoiceParser:
    """Extract structured data from OCR text using smart regex patterns."""

    PATTERNS = {
        "invoice_number": [
            r"(?:invoice\s*no|inv\s*no|bill\s*no|receipt\s*no|invoice\s*number)[\s\.#:]*([A-Z0-9][A-Z0-9\-/]{2,24})",
            r"(?:invoice|inv|bill|receipt)[\s\.#:]*(?:no|number|#)?[\s\.#:]*([A-Z]{1,4}[\-/]?\d{2,4}(?:[\-/]\d{2,4})*)",
            r"#\s*([A-Z]{1,4}[\-/]?\d{2,4})",
        ],
        "date": [
            r"(?:date|dated|invoice\s*date|bill\s*date|receipt\s*date)[\s:]*([\d]{1,2}[\s/\-\.][\d]{1,2}[\s/\-\.][\d]{2,4})",
            r"(?:date)[\s:]*([\d]{4}[\-\/\.][\d]{1,2}[\-\/\.][\d]{1,2})",
            r"(\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{4})",
            r"((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{1,2},?\s+\d{4})",
            r"(\d{1,2}/\d{1,2}/\d{2,4})",
            r"(\d{4}-\d{2}-\d{2})",
        ],
        "due_date": [
            r"(?:due\s*date|payment\s*due|pay\s*by|due)[\s:]*([\d]{1,2}[\s/\-\.][\d]{1,2}[\s/\-\.][\d]{2,4})",
        ],
        "vendor": [
            r"(?:from|seller|vendor|company|business|issued\s*by|billed\s*by|supplier)[\s:]*\n?([A-Z][A-Za-z &\'.]{2,45})",
        ],
        "total": [
            r"(?:grand\s*total|total\s*due|net\s*total|total\s*amount|invoice\s*total|amount\s*due)[\s:₹$€£]*[\s]*([\d,]+\.?\d*)",
            r"(?:^|\n)\s*total[\s:₹$€£Rs\.]*[\s]*([\d,]+\.\d{2})",
        ],
        "subtotal": [
            r"(?:sub[\s-]*total|taxable\s*value)[\s:₹$€£Rs\.]*[\s]*([\d,]+\.?\d*)",
        ],
        "tax": [
            r"(?:total\s*(?:gst|vat|tax)|gst|vat|igst|sgst\+cgst)[\s:₹$€£]*[\s]*([\d,]+\.?\d*)",
        ],
        "tax_rate": [
            r"(?:gst|vat|tax|rate)[\s:]*(\d+\.?\d*)\s*%",
            r"(\d+\.?\d*)\s*%\s*(?:gst|vat|tax)",
            r"@[\s]*(\d+\.?\d*)\s*%",
        ],
    }

    @staticmethod
    def extract(text: str, field: str) -> Optional[str]:
        for pattern in InvoiceParser.PATTERNS.get(field, []):
            match = re.search(pattern, text, re.IGNORECASE | re.MULTILINE)
            if match:
                return match.group(1).strip()
        return None

    @staticmethod
    def parse_amount(s: Optional[str]) -> float:
        if not s:
            return 0.0
        cleaned = re.sub(r'[^\d.]', '', s)
        try:
            return float(cleaned)
        except ValueError:
            return 0.0

    @staticmethod
    def detect_currency(text: str) -> str:
        if re.search(r'₹|Rs\.?|INR', text, re.IGNORECASE):
            return "INR"
        if re.search(r'\$\s*\d|USD', text, re.IGNORECASE):
            return "USD"
        if re.search(r'€|EUR', text):
            return "EUR"
        if re.search(r'£|GBP', text):
            return "GBP"
        return "INR"

    @staticmethod
    def parse_line_items(text: str) -> list:
        items = []
        lines = text.strip().split('\n')
        in_section = False
        for line in lines:
            line = line.strip()
            if not line or len(line) < 3:
                continue
            if re.match(r'^(description|item|particular|s\.?no|qty|rate|amount|sr)', line, re.IGNORECASE):
                in_section = True
                continue
            if in_section:
                numbers = re.findall(r'[\d,]+\.?\d*', line)
                if numbers and len(line) > 5:
                    name = re.split(r'\d', line)[0].strip()
                    if name and len(name) > 2:
                        items.append({"description": name[:80], "amount": InvoiceParser.parse_amount(numbers[-1])})
                if len(items) > 15:
                    break
        return items[:10]

    @staticmethod
    def parse(raw_text: str) -> dict:
        result = {}
        result['invoice_number'] = InvoiceParser.extract(raw_text, 'invoice_number') or f"INV-{uuid.uuid4().hex[:6].upper()}"
        result['invoice_date'] = InvoiceParser.extract(raw_text, 'date')
        result['due_date'] = InvoiceParser.extract(raw_text, 'due_date')
        result['vendor_name'] = InvoiceParser.extract(raw_text, 'vendor') or "Unknown Vendor"
        result['currency'] = InvoiceParser.detect_currency(raw_text)
        result['total_amount'] = InvoiceParser.parse_amount(InvoiceParser.extract(raw_text, 'total'))
        result['subtotal'] = InvoiceParser.parse_amount(InvoiceParser.extract(raw_text, 'subtotal'))
        result['tax_amount'] = InvoiceParser.parse_amount(InvoiceParser.extract(raw_text, 'tax'))
        result['tax_rate'] = InvoiceParser.parse_amount(InvoiceParser.extract(raw_text, 'tax_rate'))

        # Calculate missing
        if result['subtotal'] > 0 and result['total_amount'] == 0:
            result['total_amount'] = result['subtotal'] + result['tax_amount']
        if result['total_amount'] > 0 and result['subtotal'] == 0 and result['tax_amount'] > 0:
            result['subtotal'] = result['total_amount'] - result['tax_amount']
        if result['tax_rate'] == 0 and result['subtotal'] > 0 and result['tax_amount'] > 0:
            result['tax_rate'] = round((result['tax_amount'] / result['subtotal']) * 100, 2)

        # Convert to INR
        rate = EXCHANGE_RATES_TO_INR.get(result['currency'], 1.0)
        result['total_inr'] = round(result['total_amount'] * rate, 2)

        # Line items
        result['line_items'] = InvoiceParser.parse_line_items(raw_text)

        # Confidence
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
    "Electronics": ["laptop", "computer", "phone", "monitor", "keyboard", "mouse", "electronics", "dell", "hp", "apple", "samsung"],
    "Cloud & Infrastructure": ["aws", "azure", "google cloud", "hosting", "server", "cloud", "domain", "ssl"],
    "Office Supplies": ["paper", "pen", "stapler", "folder", "printer", "toner", "ink", "office"],
    "Marketing & Printing": ["print", "brochure", "banner", "business card", "marketing", "advertising", "flyer"],
    "Software & SaaS": ["software", "subscription", "license", "saas", "figma", "slack", "notion", "canva"],
    "Utilities & Energy": ["electricity", "power", "water", "gas", "internet", "broadband", "phone bill"],
    "Travel & Transport": ["flight", "hotel", "uber", "ola", "taxi", "travel", "train", "airline"],
    "Food & Catering": ["food", "restaurant", "catering", "lunch", "dinner", "meal", "swiggy", "zomato"],
    "Professional Services": ["consulting", "legal", "audit", "accounting", "CA", "lawyer", "advisory"],
    "Health & Medical": ["medical", "hospital", "pharmacy", "medicine", "health", "clinic", "doctor"],
}

def auto_categorize(text: str, vendor: str) -> str:
    combined = (text + " " + vendor).lower()
    for category, keywords in CATEGORY_KEYWORDS.items():
        for kw in keywords:
            if kw in combined:
                return category
    return "Uncategorized"


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


@app.post("/api/scan")
async def scan_invoice(file: UploadFile = File(...), request: Request = None):
    """Upload and scan an invoice/receipt."""
    user_id = get_current_user(request) if request else None
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

        # Auto-categorize
        if parsed.get('category', 'Uncategorized') == 'Uncategorized':
            parsed['category'] = auto_categorize(raw_text or "", parsed.get('vendor_name', ''))
        
        # Ensure total_inr is calculated
        if parsed.get('total_inr', 0) == 0 and parsed.get('total_amount', 0) > 0:
            rate = EXCHANGE_RATES_TO_INR.get(parsed.get('currency', 'INR'), 1.0)
            parsed['total_inr'] = round(parsed['total_amount'] * rate, 2)

        # Save to database
        invoice_id = str(uuid.uuid4())
        conn = get_db_connection()
        
        insert_sql_pg = """
            INSERT INTO invoices (
                id, user_id, filename, original_name, vendor_name, invoice_number,
                invoice_date, subtotal, tax_amount, tax_rate, total_amount,
                currency, total_inr, category, payment_status, raw_text,
                line_items, confidence_score
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """
        insert_sql_lite = """
            INSERT INTO invoices (
                id, user_id, filename, original_name, vendor_name, invoice_number,
                invoice_date, subtotal, tax_amount, tax_rate, total_amount,
                currency, total_inr, category, payment_status, raw_text,
                line_items, confidence_score
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """
        
        values = (
            invoice_id,
            user_id,
            hashlib.md5(content).hexdigest() + os.path.splitext(filename)[1],
            filename,
            parsed.get('vendor_name', 'Unknown'),
            parsed.get('invoice_number', ''),
            parsed.get('invoice_date'),
            parsed.get('subtotal', 0),
            parsed.get('tax_amount', 0),
            parsed.get('tax_rate', 0),
            parsed.get('total_amount', 0),
            parsed.get('currency', 'INR'),
            parsed.get('total_inr', 0),
            parsed.get('category', 'Uncategorized'),
            'Pending',
            (raw_text or 'Demo scan')[:10000],
            json.dumps(parsed.get('line_items', [])),
            parsed.get('confidence_score', 75),
        )
        
        if USE_SQLITE_FALLBACK:
            conn.execute(insert_sql_lite, values)
        else:
            cur = conn.cursor()
            cur.execute(insert_sql_pg, values)
            cur.close()
        
        conn.commit()
        conn.close()

        return {
            "success": True,
            "invoice_id": invoice_id,
            "data": {
                "vendor_name": parsed['vendor_name'],
                "invoice_number": parsed['invoice_number'],
                "invoice_date": parsed.get('invoice_date'),
                "subtotal": parsed.get('subtotal', 0),
                "tax_amount": parsed.get('tax_amount', 0),
                "tax_rate": parsed.get('tax_rate', 0),
                "total_amount": parsed.get('total_amount', 0),
                "currency": parsed.get('currency', 'INR'),
                "total_inr": parsed.get('total_inr', 0),
                "category": parsed.get('category', 'Uncategorized'),
                "confidence_score": parsed.get('confidence_score', 75),
                "line_items": parsed.get('line_items', []),
            },
            "is_demo": is_demo,
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
        for k in ['subtotal', 'tax_amount', 'tax_rate', 'total_amount', 'total_inr', 'confidence_score']:
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
    for k in ['subtotal', 'tax_amount', 'tax_rate', 'total_amount', 'total_inr', 'confidence_score']:
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
            for k in ['subtotal', 'tax_amount', 'tax_rate', 'total_amount', 'total_inr', 'confidence_score']:
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
            for k in ['subtotal', 'tax_amount', 'tax_rate', 'total_amount', 'total_inr', 'confidence_score']:
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
