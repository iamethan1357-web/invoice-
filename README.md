# 📄 Invoice Scanner Pro — Production Ready

> **Smart Invoice & Receipt Scanner** — Built with FastAPI + Neon PostgreSQL + OCR.Space  
> Deploy to Vercel in under 5 minutes.

---

## 🏗️ Architecture

```
┌─────────────────────────────────────────────────────────┐
│                    VERCEL (Hosting)                      │
│                                                         │
│  ┌──────────────┐     ┌──────────────────────────────┐  │
│  │  Static HTML │     │   Python Serverless Function  │  │
│  │  / CSS / JS  │────▶│   FastAPI + Mangum Adapter   │  │
│  │  (public/)   │     │   (api/index.py)             │  │
│  └──────────────┘     └─────────┬────────────────────┘  │
│                                  │                       │
└──────────────────────────────────┼───────────────────────┘
                                   │
                    ┌──────────────┴──────────────┐
                    │                              │
           ┌────────▼────────┐          ┌─────────▼─────────┐
           │  NEON PostgreSQL │          │   OCR.Space API    │
           │  (Database)      │          │   (Text Extraction)│
           │  FREE Tier       │          │   FREE 25K/mo      │
           └─────────────────┘          └───────────────────┘
```

---

## 🚀 DEPLOYMENT GUIDE (Step by Step)

### Step 1: Create Neon Database (FREE) — 2 minutes

1. Go to **[https://console.neon.tech](https://console.neon.tech)**
2. Sign up with GitHub (easiest) or Email
3. Click **"New Project"** → Give it a name like `invoice-scanner`
4. Select region closest to you (e.g., `Asia Southeast` for India)
5. Click **"Create Project"**
6. Copy the **connection string** — it looks like:
   ```
   postgresql://username:password@ep-cool-name-123456.ap-southeast-1.aws.neon.tech/neondb?sslmode=require
   ```
7. **SAVE THIS** — you'll need it in Step 3

> 💡 Neon free tier: 0.5 GB storage, 190 compute hours/month — more than enough!
> ⚠️ **You do NOT need to run any SQL.** Tables are created automatically.

---

### Step 2: Get OCR.Space API Key (FREE) — 1 minute

1. Go to **[https://ocr.space/ocrspace/free](https://ocr.space/ocrspace/free)**
2. Fill in your email → They'll send an API key instantly
3. The key looks like: `helloworld` or `abc123xyz`
4. **SAVE THIS** — you'll need it in Step 3

> 💡 Free tier: 25,000 API calls/month. Without this, the app works in demo mode with realistic sample data.

---

### Step 2.5: Set Up Clerk Authentication (FREE) — 3 minutes

1. Go to **[https://dashboard.clerk.com](https://dashboard.clerk.com)**
2. Sign up with GitHub or Email
3. Click **"Create Application"**
   - Name: `Invoice Scanner Pro`
   - Choose sign-in methods: **Email, Google, GitHub** (all free)
4. Once created, go to **API Keys** in the sidebar
5. Copy both keys:
   - **Publishable Key** (starts with `pk_test_...`)
   - **Secret Key** (starts with `sk_test_...`)
6. **SAVE BOTH** — you'll need them in Step 3

> 💡 Clerk free tier: 10,000 monthly active users, unlimited social logins. More than enough to start!

---

### Step 3: Deploy to Vercel — 2 minutes

#### Option A: Deploy via GitHub (Recommended)

1. **Push this project to GitHub:**
   ```bash
   cd invoice-scanner-pro
   git init
   git add .
   git commit -m "Initial commit - Invoice Scanner Pro"
   git remote add origin https://github.com/YOUR_USERNAME/invoice-scanner-pro.git
   git push -u origin main
   ```

2. **Go to [https://vercel.com/new](https://vercel.com/new)**
3. Import your GitHub repository
4. Vercel auto-detects it as a Python project

5. **Add Environment Variables** (before clicking Deploy):
   
   | Variable | Value |
   |----------|-------|
   | `DATABASE_URL` | Your Neon PostgreSQL connection string from Step 1 |
   | `OCR_SPACE_API_KEY` | Your OCR.Space API key from Step 2 |
   | `CLERK_PUBLISHABLE_KEY` | Your Clerk publishable key (starts with `pk_test_`) |
   | `CLERK_SECRET_KEY` | Your Clerk secret key (starts with `sk_test_`) |

6. Click **"Deploy"** 🎉
7. Wait 1-2 minutes → Your app is LIVE!

#### Option B: Deploy via Vercel CLI

```bash
# Install Vercel CLI
npm i -g vercel

# Login
vercel login

# Deploy (from project directory)
cd invoice-scanner-pro
vercel

# Add environment variables
vercel env add DATABASE_URL
# Paste your Neon connection string

vercel env add OCR_SPACE_API_KEY
# Paste your OCR.Space API key

# Deploy to production
vercel --prod
```

---

### Step 4: Verify Deployment

1. Open your Vercel URL (e.g., `https://invoice-scanner-pro.vercel.app`)
2. You should see the dashboard
3. Go to **"Scan Invoice"** → Upload any invoice image
4. The extracted data should appear

**Check the health endpoint:**
```
https://your-app.vercel.app/api/health
```

Expected response:
```json
{
  "status": "healthy",
  "database": "connected",
  "ocr": "configured",
  "version": "2.0.0"
}
```

---

## 📁 Project Structure

```
invoice-scanner-pro/
├── api/
│   └── index.py              # FastAPI backend (Vercel serverless function)
├── public/                    # Static files served by Vercel CDN
│   ├── index.html            # Main dashboard UI
│   ├── css/
│   │   └── style.css         # Complete styling (dark theme)
│   └── js/
│       └── app.js            # Frontend logic
├── vercel.json               # Vercel routing & config
├── requirements.txt          # Python dependencies
├── .env.example              # Environment variables template
├── .gitignore
└── README.md                 # This file
```

---

## 🔌 API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/` | Redirects to dashboard |
| GET | `/api/health` | Health check (DB + OCR status) |
| POST | `/api/scan` | Upload & scan invoice (multipart form) |
| GET | `/api/invoices` | List invoices (paginated, filterable) |
| GET | `/api/invoices/:id` | Get single invoice details |
| PUT | `/api/invoices/:id` | Update invoice fields |
| DELETE | `/api/invoices/:id` | Delete invoice |
| GET | `/api/dashboard` | Dashboard stats |
| GET | `/api/export/csv` | Export all as CSV |
| GET | `/api/currencies` | Exchange rates |

### Example API Call:

```bash
# Scan an invoice
curl -X POST https://your-app.vercel.app/api/scan \
  -F "file=@invoice.jpg"

# List invoices
curl https://your-app.vercel.app/api/invoices?page=1&limit=10

# Dashboard stats
curl https://your-app.vercel.app/api/dashboard
```

---

## 💰 Monetization Strategy

### Pricing Tiers

| Plan | Price | Features |
|------|-------|----------|
| **Free** | ₹0/mo | 20 scans/month, basic extraction |
| **Starter** | ₹299/mo | 200 scans, CSV export, multi-currency |
| **Business** | ₹999/mo | Unlimited scans, API access, priority support |
| **Enterprise** | Custom | White-label, custom integrations |

### How to Add Payments

1. **Razorpay** (India): [https://razorpay.com](https://razorpay.com) — Easy integration
2. **Stripe** (Global): [https://stripe.com](https://stripe.com)
3. **Lemon Squeezy** (Simple): [https://lemonsqueezy.com](https://lemonsqueezy.com)

### Target Customers

- 🏢 **Small businesses** (India has 63M+ MSMEs)
- 📊 **CA firms & accountants**
- 🛒 **E-commerce sellers** tracking expenses
- 🏥 **Clinics/hospitals** managing supplier bills
- 🏗️ **Real estate companies** tracking vendor invoices

---

## 🛠️ Tech Stack

| Component | Technology | Why |
|-----------|-----------|-----|
| **Backend** | Python + FastAPI | Fast, async, great for APIs |
| **Database** | Neon PostgreSQL | Serverless, free tier, auto-scaling |
| **OCR** | OCR.Space API | Free 25K calls/month, accurate |
| **Hosting** | Vercel | Free tier, auto-deploy, global CDN |
| **Frontend** | Vanilla HTML/CSS/JS | No build step, fast loading |
| **Adapter** | Mangum | Converts FastAPI to serverless |

---

## 🎯 Next Steps After Deployment

### Immediate (Week 1)
- [ ] Add custom domain (₹500/year from Namecheap)
- [ ] Set up Google Analytics
- [ ] Create a landing page with pricing

### Short-term (Month 1)
- [ ] Add user authentication (use Clerk or Auth0)
- [ ] Add payment integration (Razorpay/Stripe)
- [ ] Add email notifications for scanned invoices
- [ ] Add bulk upload feature

### Medium-term (Month 2-3)
- [ ] Add GST reports for Indian businesses
- [ ] Add Tally/QuickBooks export format
- [ ] Add WhatsApp integration for sending receipts
- [ ] Add mobile app (React Native)

### Long-term (Month 4+)
- [ ] Add AI-powered categorization
- [ ] Add expense approval workflows
- [ ] Add multi-user team support
- [ ] Add invoice templates/generator

---

## 🐛 Troubleshooting

### "Database not configured" error
→ Check that `DATABASE_URL` environment variable is set in Vercel

### OCR returns demo data
→ Add `OCR_SPACE_API_KEY` environment variable (or it works in demo mode)

### Upload fails with "File too large"
→ Vercel free tier has 4.5MB body limit. Compress images before uploading.

### Cold start delays
→ First request after inactivity may take 2-3 seconds (Vercel serverless)

---

## 📞 Support & Resources

- **Neon Docs**: [https://neon.tech/docs](https://neon.tech/docs)
- **Vercel Docs**: [https://vercel.com/docs](https://vercel.com/docs)
- **OCR.Space Docs**: [https://ocr.space/API/Doc](https://ocr.space/API/Doc)
- **FastAPI Docs**: [https://fastapi.tiangolo.com](https://fastapi.tiangolo.com)

---

## 📄 License

MIT License — Free for personal and commercial use.

---

**Built with ❤️ for the Indian MSME market**

**Total Deployment Cost: ₹0/month** (using free tiers)
- Vercel Free: Unlimited deployments
- Neon Free: 0.5 GB database
- OCR.Space Free: 25,000 scans/month
