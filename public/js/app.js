// ═══ Invoice Scanner Pro — Enhanced Frontend ═══

let currentPage = 1;
let currentSort = 'created_at';
let currentOrder = 'desc';
let currentInvoiceId = null;
let searchTimeout = null;

// ─── Auth Helper ────────────────────────────────────────────────────────────
function getAuthHeaders() {
    const headers = {};
    if (window.__clerk && window.__clerk.session) {
        // Best effort if a token is already cached on the session object
        const token = window.__clerk.session.getToken?.();
        if (token && typeof token.then === 'function') {
            // getToken() is async; callers should use authFetch() instead.
            return headers;
        }
        if (token) headers['Authorization'] = `Bearer ${token}`;
    }
    return headers;
}

async function getAuthToken() {
    if (window.__clerk && window.__clerk.session) {
        try {
            return await window.__clerk.session.getToken();
        } catch(e) { return null; }
    }
    return null;
}

async function authFetch(url, options = {}) {
    const token = await getAuthToken();
    if (token) {
        options.headers = options.headers || {};
        options.headers['Authorization'] = `Bearer ${token}`;
    }
    return fetch(url, options);
}

document.addEventListener('DOMContentLoaded', () => {
    // Initial load is handled by the auth init script in HTML
    // This listener only runs if auth is disabled or already resolved
    // Hide scan badge after first view
    setTimeout(() => { const b = document.getElementById('scanBadge'); if(b) b.style.display='none'; }, 5000);
});

// ─── Animated Counter ───────────────────────────────────────────────────────
function animateValue(el, start, end, duration, prefix='', suffix='') {
    const startTime = performance.now();
    const diff = end - start;
    function update(currentTime) {
        const elapsed = currentTime - startTime;
        const progress = Math.min(elapsed / duration, 1);
        const eased = 1 - Math.pow(1 - progress, 3); // ease-out cubic
        const current = start + diff * eased;
        if (prefix === '₹') {
            el.textContent = prefix + Math.round(current).toLocaleString('en-IN') + suffix;
        } else {
            el.textContent = prefix + (Number.isInteger(end) ? Math.round(current) : current.toFixed(2)) + suffix;
        }
        if (progress < 1) requestAnimationFrame(update);
    }
    requestAnimationFrame(update);
}

// ─── Health Check ───────────────────────────────────────────────────────────
async function checkHealth() {
    try {
        const res = await authFetch('/api/health');
        const data = await res.json();
        const dot = document.querySelector('#statusIndicator .status-dot');
        const text = document.getElementById('statusText');
        if (data.status === 'healthy') {
            dot.className = 'status-dot green';
            text.textContent = 'All systems online';
        } else {
            dot.className = 'status-dot yellow';
            text.textContent = 'Limited functionality';
        }
    } catch {
        document.querySelector('#statusIndicator .status-dot').className = 'status-dot red';
        document.getElementById('statusText').textContent = 'Connection error';
    }
}

// ─── Navigation ─────────────────────────────────────────────────────────────
function showPage(page) {
    document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
    document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
    document.getElementById(`page-${page}`).classList.add('active');
    document.querySelector(`[data-page="${page}"]`)?.classList.add('active');
    
    switch(page) {
        case 'dashboard': loadDashboard(); break;
        case 'invoices': loadInvoices(); break;
        case 'analytics': loadAnalytics(); break;
        case 'gst': loadGSTReport(); break;
        case 'generator': loadTemplates(); break;
        case 'scan': resetScan(); break;
    }
    document.getElementById('sidebar').classList.remove('open');
    window.scrollTo({top: 0, behavior: 'smooth'});
}

function toggleSidebar() { document.getElementById('sidebar').classList.toggle('open'); }

// ─── Dashboard ──────────────────────────────────────────────────────────────
async function loadDashboard() {
    try {
        const res = await authFetch('/api/dashboard');
        const data = await res.json();
        
        // Animate stat values
        animateValue(document.getElementById('statTotal'), 0, data.total_invoices, 800);
        animateValue(document.getElementById('statAmount'), 0, data.total_amount, 1200, '₹');
        animateValue(document.getElementById('statPending'), 0, data.pending_amount, 1200, '₹');
        animateValue(document.getElementById('statPaid'), 0, data.paid_amount, 1200, '₹');
        
        // Recent invoices with stagger
        const recentHTML = (data.recent || []).map((inv, i) => `
            <div class="recent-item" style="animation: cardEnter 300ms var(--ease-out) ${i * 60}ms backwards" onclick="viewInvoice('${inv.id}')">
                <div class="recent-info">
                    <span class="recent-vendor">${esc(inv.vendor_name)}</span>
                    <span class="recent-meta">
                        ${esc(inv.invoice_number)}
                        <span class="dot"></span>
                        ${fmtDate(inv.invoice_date)}
                    </span>
                </div>
                <span class="recent-amount">${fmtCur(inv.total_inr, 'INR')}</span>
            </div>`).join('');
        
        document.getElementById('recentInvoices').innerHTML = recentHTML || `
            <div class="empty-state">
                <div class="empty-state-icon"><svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg></div>
                <h4>No invoices yet</h4>
                <p>Scan your first invoice to get started</p>
            </div>`;
        
        // Category breakdown with animated bars
        const maxAmt = Math.max(...(data.categories || []).map(c => c.amount), 1);
        const catHTML = (data.categories || []).map((cat, i) => `
            <div class="category-item" style="animation: cardEnter 300ms var(--ease-out) ${i * 80}ms backwards">
                <span class="category-name">${esc(cat.category)}</span>
                <div class="category-bar-wrapper">
                    <div class="category-bar-fill" style="width:0%" data-width="${(cat.amount/maxAmt)*100}%"></div>
                </div>
                <span class="category-amount">${fmtCur(cat.amount, 'INR')}</span>
            </div>`).join('');
        
        document.getElementById('categoryBreakdown').innerHTML = catHTML || '<div class="empty-state"><p>No data yet</p></div>';
        
        // Animate bars after render
        setTimeout(() => {
            document.querySelectorAll('.category-bar-fill[data-width]').forEach(bar => {
                bar.style.width = bar.dataset.width;
            });
        }, 100);
        
    } catch (err) {
        document.getElementById('recentInvoices').innerHTML = '<div class="empty-state"><p>Error loading data</p></div>';
    }
}

// ─── Upload Zone ────────────────────────────────────────────────────────────
function setupUploadZone() {
    const zone = document.getElementById('uploadZone');
    const input = document.getElementById('fileInput');
    
    zone.addEventListener('click', () => input.click());
    zone.addEventListener('dragover', e => { e.preventDefault(); zone.classList.add('drag-over'); });
    zone.addEventListener('dragleave', () => zone.classList.remove('drag-over'));
    zone.addEventListener('drop', e => { e.preventDefault(); zone.classList.remove('drag-over'); if (e.dataTransfer.files.length) uploadFile(e.dataTransfer.files[0]); });
    input.addEventListener('change', e => { if (e.target.files.length) uploadFile(e.target.files[0]); });
}

async function uploadFile(file) {
    if (file.size > 10 * 1024 * 1024) { showToast('File too large (max 10MB)', 'error'); return; }
    
    document.getElementById('uploadZone').style.display = 'none';
    document.getElementById('processing').style.display = 'block';
    document.getElementById('scanResult').style.display = 'none';
    
    // Reset steps
    ['step1','step2','step3','step4'].forEach(id => {
        const el = document.getElementById(id);
        el.className = 'scan-step';
    });
    
    // Animate steps
    const steps = ['step1','step2','step3','step4'];
    let current = 0;
    
    function advanceStep() {
        if (current > 0) {
            document.getElementById(steps[current - 1]).className = 'scan-step done';
        }
        if (current < steps.length) {
            document.getElementById(steps[current]).className = 'scan-step active';
            current++;
        }
    }
    
    advanceStep();
    const interval = setInterval(advanceStep, 1200);
    
    try {
        const fd = new FormData();
        fd.append('file', file);
        const res = await authFetch('/api/scan', { method: 'POST', body: fd });
        const data = await res.json();
        clearInterval(interval);
        
        // Mark all steps as done
        steps.forEach(id => { document.getElementById(id).className = 'scan-step done'; });
        
        if (data.error) { showToast(data.error, 'error'); resetScan(); return; }
        
        // Brief pause before showing result
        setTimeout(() => displayResult(data.data, data.is_demo), 400);
        showToast(data.message || 'Scanned!', 'success');
    } catch (err) {
        clearInterval(interval);
        showToast('Scan failed. Please try again.', 'error');
        resetScan();
    }
}

function displayResult(d, isDemo) {
    document.getElementById('processing').style.display = 'none';
    document.getElementById('scanResult').style.display = 'block';
    document.getElementById('demoNotice').style.display = isDemo ? 'flex' : 'none';
    
    // Confidence with animation
    const conf = d.confidence_score || 75;
    setTimeout(() => {
        document.getElementById('confidenceFill').style.width = conf + '%';
    }, 100);
    document.getElementById('confidenceValue').textContent = conf + '%';
    
    // Data fields
    document.getElementById('resVendor').textContent = d.vendor_name || '—';
    document.getElementById('resInvoiceNo').textContent = d.invoice_number || '—';
    document.getElementById('resDate').textContent = fmtDate(d.invoice_date);
    document.getElementById('resDueDate').textContent = fmtDate(d.due_date);
    document.getElementById('resCurrency').textContent = d.currency || '—';
    document.getElementById('resSubtotal').textContent = fmtCur(d.subtotal, d.currency);
    document.getElementById('resTaxRate').textContent = d.tax_rate ? d.tax_rate + '%' : '—';
    document.getElementById('resTaxAmount').textContent = fmtCur(d.tax_amount, d.currency);
    document.getElementById('resTotal').textContent = fmtCur(d.total_amount, d.currency);
    document.getElementById('resTotalINR').textContent = fmtCur(d.total_inr, 'INR');
    document.getElementById('resCategory').textContent = d.category || 'Uncategorized';
    const catSrc = document.getElementById('resCategorySource');
    const catConf = d.category_confidence != null && d.category_confidence !== ''
        ? Math.round(Number(d.category_confidence)) + '%' : '';
    if (d.categorization_source === 'ai') {
        catSrc.textContent = catConf ? 'AI · ' + catConf : 'AI';
        catSrc.className = 'cat-source ai';
    } else if (d.categorization_source === 'rules') {
        catSrc.textContent = catConf ? 'Auto · ' + catConf : 'Auto';
        catSrc.className = 'cat-source rules';
    } else {
        catSrc.textContent = '';
        catSrc.className = 'cat-source';
    }
    
    // Line items
    const items = d.line_items || [];
    if (items.length > 0) {
        document.getElementById('lineItemsSection').style.display = 'block';
        document.getElementById('lineItemsBody').innerHTML = items.map(i => `<tr><td>${esc(i.description)}</td><td>${fmtCur(i.amount, d.currency)}</td></tr>`).join('');
    } else {
        document.getElementById('lineItemsSection').style.display = 'none';
    }
    
    // Raw OCR text (helps diagnose extraction issues)
    const raw = d.raw_text || '';
    const rawEl = document.getElementById('rawTextSection');
    if (raw && !isDemo) {
        document.getElementById('rawText').textContent = raw;
        rawEl.style.display = 'block';
    } else {
        rawEl.style.display = 'none';
    }
}

function resetScan() {
    document.getElementById('uploadZone').style.display = 'block';
    document.getElementById('processing').style.display = 'none';
    document.getElementById('scanResult').style.display = 'none';
    document.getElementById('fileInput').value = '';
}

// ─── Invoice List ───────────────────────────────────────────────────────────
async function loadInvoices() {
    const search = document.getElementById('searchInput').value;
    const category = document.getElementById('categoryFilter').value;
    const status = document.getElementById('statusFilter').value;
    const params = new URLSearchParams({ page: currentPage, limit: 20, search, category, status, sort_by: currentSort, sort_order: currentOrder });
    
    try {
        const res = await authFetch(`/api/invoices?${params}`);
        const data = await res.json();
        
        // Populate category filter
        if (document.getElementById('categoryFilter').options.length <= 1) {
            const dashRes = await authFetch('/api/dashboard');
            const dashData = await dashRes.json();
            const sel = document.getElementById('categoryFilter');
            (dashData.categories || []).forEach(c => { const o = document.createElement('option'); o.value = c.category; o.textContent = c.category; sel.appendChild(o); });
        }
        
        const tbody = data.invoices.map((inv, i) => `
            <tr style="animation: cardEnter 200ms var(--ease-out) ${i * 30}ms backwards">
                <td><strong style="font-size:12px;color:var(--text-secondary)">${esc(inv.invoice_number)}</strong></td>
                <td class="vendor-cell">${esc(inv.vendor_name)}</td>
                <td style="color:var(--text-secondary)">${fmtDate(inv.invoice_date)}</td>
                <td class="amount-cell">${fmtCur(inv.total_amount, inv.currency)}</td>
                <td class="amount-cell" style="color:var(--text-secondary)">${fmtCur(inv.total_inr, 'INR')}</td>
                <td><span style="font-size:12px;color:var(--text-muted)">${esc(inv.category)}</span></td>
                <td><span class="status-badge ${inv.payment_status.toLowerCase()}"><span class="status-dot-sm"></span>${inv.payment_status}</span></td>
                <td><button class="action-btn" onclick="event.stopPropagation();viewInvoice('${inv.id}')">View</button></td>
            </tr>`).join('');
        
        document.getElementById('invoiceTableBody').innerHTML = tbody || `
            <tr><td colspan="8">
                <div class="empty-state">
                    <div class="empty-state-icon"><svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg></div>
                    <h4>No invoices found</h4>
                    <p>Try adjusting your search or filters</p>
                </div>
            </td></tr>`;
        
        renderPagination(data.page, data.total_pages);
    } catch (err) {
        document.getElementById('invoiceTableBody').innerHTML = '<tr><td colspan="8" class="loading">Error loading</td></tr>';
    }
}

function renderPagination(cur, total) {
    const c = document.getElementById('pagination');
    if (total <= 1) { c.innerHTML = ''; return; }
    let h = `<button ${cur<=1?'disabled':''} onclick="goPage(${cur-1})">‹</button>`;
    for (let i = 1; i <= total; i++) {
        if (i===cur || i===1 || i===total || Math.abs(i-cur)<=1) h += `<button class="${i===cur?'active':''}" onclick="goPage(${i})">${i}</button>`;
        else if (Math.abs(i-cur)===2) h += `<button disabled style="border:none;background:none">…</button>`;
    }
    h += `<button ${cur>=total?'disabled':''} onclick="goPage(${cur+1})">›</button>`;
    c.innerHTML = h;
}

function goPage(p) { currentPage = p; loadInvoices(); }
function sortBy(f) { currentOrder = (currentSort===f && currentOrder==='desc') ? 'asc' : 'desc'; currentSort = f; loadInvoices(); }
function debounceSearch() { clearTimeout(searchTimeout); searchTimeout = setTimeout(() => { currentPage=1; loadInvoices(); }, 300); }

// ─── Modal ──────────────────────────────────────────────────────────────────
async function viewInvoice(id) {
    currentInvoiceId = id;
    document.getElementById('modalOverlay').classList.add('active');
    document.getElementById('modalBody').innerHTML = '<div class="loading"><div class="skeleton" style="height:24px;margin:8px 0"></div><div class="skeleton" style="height:24px;margin:8px 0"></div><div class="skeleton" style="height:24px;margin:8px 0"></div></div>';
    
    try {
        const res = await authFetch(`/api/invoices/${id}`);
        const d = (await res.json()).invoice;
        document.getElementById('modalTitle').textContent = `Invoice ${d.invoice_number}`;
        
        let lineHTML = '';
        const items = d.line_items || [];
        if (items.length > 0) {
            lineHTML = `<div style="margin-top:12px"><h4 style="font-size:11px;font-weight:600;margin-bottom:8px;color:var(--text-muted);text-transform:uppercase;letter-spacing:0.5px">Line Items</h4>
                <table class="line-items-table"><thead><tr><th>Description</th><th>Amount</th></tr></thead><tbody>
                ${items.map(i=>`<tr><td>${esc(i.description)}</td><td>${fmtCur(i.amount,d.currency)}</td></tr>`).join('')}</tbody></table></div>`;
        }
        
        document.getElementById('modalBody').innerHTML = `
            <div class="modal-detail-row"><span class="label">Vendor</span><span class="value">${esc(d.vendor_name)}</span></div>
            <div class="modal-detail-row"><span class="label">Invoice #</span><span class="value">${esc(d.invoice_number)}</span></div>
            <div class="modal-detail-row"><span class="label">Date</span><span class="value">${fmtDate(d.invoice_date)}</span></div>
            <div class="modal-detail-row"><span class="label">Due Date</span><span class="value">${fmtDate(d.due_date)}</span></div>
            <div class="modal-detail-row"><span class="label">Subtotal</span><span class="value">${fmtCur(d.subtotal, d.currency)}</span></div>
            <div class="modal-detail-row"><span class="label">Tax (${d.tax_rate}%)</span><span class="value">${fmtCur(d.tax_amount, d.currency)}</span></div>
            ${(d.cgst_amount || d.sgst_amount || d.igst_amount) ? `
            <div class="modal-detail-row"><span class="label">CGST</span><span class="value">${fmtCur(d.cgst_amount, d.currency)}</span></div>
            <div class="modal-detail-row"><span class="label">SGST</span><span class="value">${fmtCur(d.sgst_amount, d.currency)}</span></div>
            <div class="modal-detail-row"><span class="label">IGST</span><span class="value">${fmtCur(d.igst_amount, d.currency)}</span></div>` : ''}
            <div class="modal-detail-row"><span class="label">Total</span><span class="value" style="color:var(--brand-400);font-size:16px">${fmtCur(d.total_amount, d.currency)}</span></div>
            <div class="modal-detail-row"><span class="label">Total (INR)</span><span class="value">${fmtCur(d.total_inr, 'INR')}</span></div>
            <div class="modal-detail-row"><span class="label">Currency</span><span class="value">${d.currency}</span></div>
            <div class="modal-detail-row"><span class="label">Category</span><span class="value">${esc(d.category)}</span></div>
            <div class="modal-detail-row"><span class="label">Status</span><span class="value"><span class="status-badge ${d.payment_status.toLowerCase()}"><span class="status-dot-sm"></span>${d.payment_status}</span></span></div>
            <div class="modal-detail-row"><span class="label">Confidence</span><span class="value">${d.confidence_score}%</span></div>
            ${lineHTML}`;
    } catch { document.getElementById('modalBody').innerHTML = '<p class="loading">Failed to load</p>'; }
}

function closeModal() { document.getElementById('modalOverlay').classList.remove('active'); currentInvoiceId = null; }

async function deleteInvoice() {
    if (!currentInvoiceId || !confirm('Delete this invoice?')) return;
    try {
        await authFetch(`/api/invoices/${currentInvoiceId}`, { method: 'DELETE' });
        closeModal();
        showToast('Invoice deleted', 'success');
        loadInvoices();
    } catch { showToast('Delete failed', 'error'); }
}

// ─── Analytics ──────────────────────────────────────────────────────────────
async function loadAnalytics() {
    try {
        const [dashRes, invRes] = await Promise.all([authFetch('/api/dashboard'), authFetch('/api/invoices?limit=100')]);
        const data = await dashRes.json();
        const invData = await invRes.json();
        
        // Monthly chart with animation
        const monthly = data.monthly || [];
        if (monthly.length > 0) {
            const maxAmt = Math.max(...monthly.map(m => m.amount), 1);
            document.getElementById('monthlyChart').innerHTML = monthly.map((m, i) => `
                <div class="bar-item">
                    <span class="bar-value">${fmtCur(m.amount,'INR')}</span>
                    <div class="bar-fill" style="height:0%" data-height="${(m.amount/maxAmt)*100}%"></div>
                    <span class="bar-label">${m.month}</span>
                </div>`).join('');
            // Animate bars
            setTimeout(() => {
                document.querySelectorAll('.bar-fill[data-height]').forEach(bar => {
                    bar.style.height = bar.dataset.height;
                });
            }, 100);
        } else {
            document.getElementById('monthlyChart').innerHTML = '<div class="empty-state"><p>No monthly data yet</p></div>';
        }
        
        // Category
        const cats = data.categories || [];
        const maxCat = Math.max(...cats.map(c=>c.amount), 1);
        document.getElementById('categoryChart').innerHTML = cats.map((c, i) => `
            <div class="category-item" style="animation: cardEnter 300ms var(--ease-out) ${i*60}ms backwards">
                <span class="category-name">${esc(c.category)}</span>
                <div class="category-bar-wrapper"><div class="category-bar-fill" style="width:0%;background:linear-gradient(90deg,#22c55e,#4ade80)" data-width="${(c.amount/maxCat)*100}%"></div></div>
                <span class="category-amount">${c.count}</span>
            </div>`).join('') || '<div class="empty-state"><p>No data</p></div>';
        setTimeout(() => { document.querySelectorAll('#categoryChart .category-bar-fill[data-width]').forEach(b => b.style.width = b.dataset.width); }, 150);
        
        // Top vendors
        const vMap = {};
        (invData.invoices || []).forEach(inv => { vMap[inv.vendor_name] = (vMap[inv.vendor_name]||0) + (inv.total_inr||0); });
        const topV = Object.entries(vMap).sort((a,b) => b[1]-a[1]).slice(0,5);
        const maxV = topV.length ? topV[0][1] : 1;
        document.getElementById('topVendors').innerHTML = topV.map(([n,a], i) => `
            <div class="category-item" style="animation: cardEnter 300ms var(--ease-out) ${i*60}ms backwards">
                <span class="category-name">${esc(n.length>20?n.substring(0,20)+'...':n)}</span>
                <div class="category-bar-wrapper"><div class="category-bar-fill" style="width:0%;background:linear-gradient(90deg,#eab308,#facc15)" data-width="${(a/maxV)*100}%"></div></div>
                <span class="category-amount">${fmtCur(a,'INR')}</span>
            </div>`).join('') || '<div class="empty-state"><p>No data</p></div>';
        setTimeout(() => { document.querySelectorAll('#topVendors .category-bar-fill[data-width]').forEach(b => b.style.width = b.dataset.width); }, 150);
        
        // Quick stats
        const total = invData.total;
        const avg = total > 0 ? data.total_amount / total : 0;
        const paid = (invData.invoices||[]).filter(i=>i.payment_status==='Paid').length;
        const vendors = new Set((invData.invoices||[]).map(i=>i.vendor_name)).size;
        document.getElementById('quickStats').innerHTML = `
            <div class="quick-stat-item"><div class="qs-value">${total}</div><div class="qs-label">Total Invoices</div></div>
            <div class="quick-stat-item"><div class="qs-value">${fmtCur(avg,'INR')}</div><div class="qs-label">Avg Value</div></div>
            <div class="quick-stat-item"><div class="qs-value">${vendors}</div><div class="qs-label">Unique Vendors</div></div>
            <div class="quick-stat-item"><div class="qs-value">${paid}</div><div class="qs-label">Paid</div></div>
            <div class="quick-stat-item"><div class="qs-value">${total-paid}</div><div class="qs-label">Pending</div></div>
            <div class="quick-stat-item"><div class="qs-value">${cats.length}</div><div class="qs-label">Categories</div></div>`;
    } catch (err) { console.error(err); }
}

// ─── GST Reports ────────────────────────────────────────────────────────────
async function loadGSTReport() {
    const year = document.getElementById('gstYear').value;
    const month = document.getElementById('gstMonth').value;
    const status = document.getElementById('gstStatus').value;
    const params = new URLSearchParams({ year, month, status });
    try {
        const res = await authFetch(`/api/gst/report?${params}`);
        const data = await res.json();

        // Summary cards
        const s = data.summary || {};
        document.getElementById('gstInvoices').textContent = s.invoices || 0;
        document.getElementById('gstTaxable').textContent = fmtCur(s.taxable_value, 'INR');
        document.getElementById('gstTotalTax').textContent = fmtCur(s.total_tax, 'INR');
        document.getElementById('gstInvoiceAmt').textContent = fmtCur(s.total_invoice_amount, 'INR');

        // Rate-wise table
        const rateRows = (data.by_rate || []).map(r => `
            <tr>
                <td><strong>${r.rate}%</strong></td>
                <td>${r.invoices}</td>
                <td class="amount-cell">${fmtCur(r.taxable_value, 'INR')}</td>
                <td class="amount-cell">${fmtCur(r.cgst, 'INR')}</td>
                <td class="amount-cell">${fmtCur(r.sgst, 'INR')}</td>
                <td class="amount-cell">${fmtCur(r.igst, 'INR')}</td>
                <td class="amount-cell">${fmtCur(r.total_tax, 'INR')}</td>
            </tr>`).join('') || '<tr><td colspan="7" class="empty-state">No GST data yet</td></tr>';
        document.getElementById('gstRateTable').innerHTML = rateRows;

        // Category-wise table
        const catRows = (data.by_category || []).map(c => `
            <tr>
                <td>${esc(c.category)}</td>
                <td>${c.invoices}</td>
                <td class="amount-cell">${fmtCur(c.taxable_value, 'INR')}</td>
                <td class="amount-cell">${fmtCur(c.total_tax, 'INR')}</td>
            </tr>`).join('') || '<tr><td colspan="4" class="empty-state">No GST data yet</td></tr>';
        document.getElementById('gstCategoryTable').innerHTML = catRows;

        // Monthly table
        const monthRows = (data.by_month || []).map(m => `
            <tr>
                <td><strong>${esc(m.month)}</strong></td>
                <td>${m.invoices}</td>
                <td class="amount-cell">${fmtCur(m.taxable_value, 'INR')}</td>
                <td class="amount-cell">${fmtCur(m.cgst, 'INR')}</td>
                <td class="amount-cell">${fmtCur(m.sgst, 'INR')}</td>
                <td class="amount-cell">${fmtCur(m.igst, 'INR')}</td>
                <td class="amount-cell">${fmtCur(m.total_tax, 'INR')}</td>
            </tr>`).join('') || '<tr><td colspan="7" class="empty-state">No GST data yet</td></tr>';
        document.getElementById('gstMonthTable').innerHTML = monthRows;
    } catch (err) {
        console.error(err);
        ['gstRateTable', 'gstCategoryTable', 'gstMonthTable'].forEach(id => {
            document.getElementById(id).innerHTML = '<tr><td colspan="7" class="empty-state">Error loading GST report</td></tr>';
        });
    }
}

// ─── Invoice Templates & Generator ─────────────────────────────────────────
let generatorTemplates = [];
let editingTemplateId = null;
let lastInvoiceHTML = '';
let lastGeneratedId = null;
let _generatorInit = false;
let _currentTemplateId = '';

const GEN_CATEGORIES = [
    'Electronics', 'Cloud & Infrastructure', 'Office Supplies', 'Marketing & Printing',
    'Software & SaaS', 'Utilities & Energy', 'Travel & Transport', 'Food & Catering',
    'Professional Services', 'Health & Medical', 'Logistics & Shipping',
    'Banking & Finance', 'Construction & Repairs', 'Rent & Lease',
    'Other', 'Uncategorized'
];
const GEN_CURRENCIES = ['INR','USD','EUR','GBP','JPY','AUD','CAD','SGD','AED','SAR'];

function initGenerator() {
    if (_generatorInit) return;
    _generatorInit = true;
    const cySel = document.getElementById('genCurrency');
    cySel.innerHTML = GEN_CURRENCIES.map(c => `<option value="${c}">${c}</option>`).join('');
    document.getElementById('tplCurrency').innerHTML = GEN_CURRENCIES.map(c => `<option value="${c}">${c}</option>`).join('');
    const catSel = document.getElementById('genCategory');
    catSel.innerHTML = GEN_CATEGORIES.map(c => `<option value="${c}">${c}</option>`).join('');
    document.getElementById('genDate').value = new Date().toISOString().slice(0,10);
    addItemRow({ description: 'Consulting Service', quantity: 1, rate: 15000 });
    addItemRow({ description: 'Processing Fee', quantity: 1, rate: 1000 });
}

async function loadTemplates() {
    initGenerator();
    try {
        const res = await authFetch('/api/templates');
        const data = await res.json();
        generatorTemplates = data.templates || [];
        renderTemplateSelect();
        renderTemplateManagerList();
    } catch (err) {
        console.error(err);
        document.getElementById('tplSelect').innerHTML = '<option value="">Could not load templates</option>';
    }
}

function renderTemplateSelect() {
    const sel = document.getElementById('tplSelect');
    const current = sel.value;
    sel.innerHTML = generatorTemplates.map(t => `<option value="${t.id}">${esc(t.name)}${t.is_default ? ' (built-in)' : ''}</option>`).join('');
    if (current && generatorTemplates.some(t => t.id === current)) sel.value = current;
    if (!sel.value && sel.options.length) sel.value = sel.options[0].value;
    // Only refill defaults when the effective selection actually changes, so
    // navigating back to this page does not wipe the user's in-progress form.
    if (sel.value !== _currentTemplateId) {
        selectTemplate();
    }
}

function selectTemplate() {
    const template = generatorTemplates.find(t => t.id === document.getElementById('tplSelect').value);
    if (!template) return;
    _currentTemplateId = template.id;
    document.getElementById('genCurrency').value = template.currency || 'INR';
    document.getElementById('genTaxRate').value = template.tax_rate != null ? template.tax_rate : 18;
    document.getElementById('genDiscount').value = template.discount_rate != null ? template.discount_rate : 0;
    document.getElementById('genTerms').value = template.terms || '';
    document.getElementById('genFooter').value = template.footer || '';
    document.getElementById('genCustomerName').value = template.to_name || '';
    document.getElementById('genCustomerAddress').value = template.to_address || '';
    document.getElementById('genCustomerEmail').value = template.to_email || '';
    document.getElementById('genCustomerPhone').value = template.to_phone || '';
    document.getElementById('genCustomerGstin').value = template.to_gstin || '';
    if (template.line_items && template.line_items.length) {
        document.getElementById('genItemsBody').innerHTML = '';
        template.line_items.forEach(it => addItemRow(it));
    }
}

function addItemRow(item = {}) {
    const tbody = document.getElementById('genItemsBody');
    const tr = document.createElement('tr');
    tr.className = 'line-item-row';
    tr.innerHTML = `
        <td><input class="gen-input li-desc" value="${escAttr(item.description || '')}" placeholder="Item description"></td>
        <td><input class="gen-input li-qty num" type="number" min="0" step="0.01" value="${item.quantity != null ? item.quantity : 1}" oninput="updateItemAmount(this.closest('tr'))"></td>
        <td><input class="gen-input li-rate num" type="number" min="0" step="0.01" value="${item.rate != null ? item.rate : 0}" oninput="updateItemAmount(this.closest('tr'))"></td>
        <td class="li-amount num">${fmtCur(item.amount || 0, document.getElementById('genCurrency')?.value || 'INR')}</td>
        <td><button class="btn btn-danger btn-sm" onclick="removeLineItem(this)" title="Remove">×</button></td>`;
    tbody.appendChild(tr);
    updateItemAmount(tr, {animate:false});
    return tr;
}

function removeLineItem(btn) {
    btn.closest('tr').remove();
}

function updateItemAmount(row, opts = {}) {
    const qty = parseFloat(row.querySelector('.li-qty')?.value) || 0;
    const rate = parseFloat(row.querySelector('.li-rate')?.value) || 0;
    const amount = row.querySelector('.li-amount');
    if (amount) amount.textContent = fmtCur(qty * rate, document.getElementById('genCurrency')?.value || 'INR');
}

function collectLineItems() {
    const items = [];
    document.querySelectorAll('#genItemsBody .line-item-row').forEach(row => {
        const desc = row.querySelector('.li-desc')?.value.trim() || '';
        const qty = parseFloat(row.querySelector('.li-qty')?.value) || 0;
        const rate = parseFloat(row.querySelector('.li-rate')?.value) || 0;
        if (desc) items.push({ description: desc, quantity: qty, rate });
    });
    return items;
}

function openTemplateManager() {
    loadTemplates();
    document.getElementById('templateModalOverlay').classList.add('active');
}

function closeTemplateManager() {
    document.getElementById('templateModalOverlay').classList.remove('active');
    resetTemplateEditor();
}

function renderTemplateManagerList() {
    const wrap = document.getElementById('templateList');
    if (!generatorTemplates.length) {
        wrap.innerHTML = '<p style="color:var(--text-muted);font-size:13px;">No templates yet. Create one on the right.</p>';
        return;
    }
    wrap.innerHTML = generatorTemplates.map(t => `
        <div class="template-cell">
            <div>
                <strong>${esc(t.name)}</strong>
                <span class="template-meta">${esc(t.currency || 'INR')} · ${t.tax_rate != null ? t.tax_rate + '%' : '18%'}${t.is_default ? ' · Built-in' : ''}</span>
            </div>
            <div class="template-actions">
                <button class="action-btn" onclick="editTemplate('${t.id}')">Edit</button>
                ${t.is_default ? '<button class="action-btn" disabled>Default</button>' : `<button class="action-btn" style="color:var(--danger);border-color:rgba(239,68,68,.25)" onclick="deleteTemplate('${t.id}')">Delete</button>`}
            </div>
        </div>`).join('');
}

function editTemplate(id) {
    const t = generatorTemplates.find(x => x.id === id);
    if (!t) return;
    editingTemplateId = id;
    document.getElementById('templateEditorTitle').textContent = t.name || 'Edit Template';
    document.getElementById('tplName').value = t.name || '';
    document.getElementById('tplFromName').value = t.from_name || '';
    document.getElementById('tplFromAddress').value = t.from_address || '';
    document.getElementById('tplFromEmail').value = t.from_email || '';
    document.getElementById('tplFromPhone').value = t.from_phone || '';
    document.getElementById('tplFromGstin').value = t.from_gstin || '';
    document.getElementById('tplToName').value = t.to_name || '';
    document.getElementById('tplToAddress').value = t.to_address || '';
    document.getElementById('tplToEmail').value = t.to_email || '';
    document.getElementById('tplToPhone').value = t.to_phone || '';
    document.getElementById('tplToGstin').value = t.to_gstin || '';
    document.getElementById('tplCurrency').value = t.currency || 'INR';
    document.getElementById('tplTaxRate').value = t.tax_rate != null ? t.tax_rate : 18;
    document.getElementById('tplDiscount').value = t.discount_rate != null ? t.discount_rate : 0;
    document.getElementById('tplAccent').value = t.accent_color || '#4f46e5';
    document.getElementById('tplTerms').value = t.terms || '';
    document.getElementById('tplFooter').value = t.footer || '';
    document.getElementById('tplLineItems').value = (t.line_items || []).map(i => `${i.description} | ${i.quantity || 1} | ${i.rate || 0}`).join('\n');
    document.getElementById('saveTemplateBtn').textContent = 'Update Template';
}

function resetTemplateEditor() {
    editingTemplateId = null;
    document.getElementById('templateEditorTitle').textContent = 'New Template';
    ['tplName','tplFromName','tplFromAddress','tplFromEmail','tplFromPhone','tplFromGstin','tplToName','tplToAddress','tplToEmail','tplToPhone','tplToGstin','tplTerms','tplFooter','tplLineItems'].forEach(id => document.getElementById(id).value = '');
    document.getElementById('tplCurrency').value = 'INR';
    document.getElementById('tplTaxRate').value = '18';
    document.getElementById('tplDiscount').value = '0';
    document.getElementById('tplAccent').value = '#4f46e5';
    document.getElementById('saveTemplateBtn').textContent = 'Save Template';
}

async function saveTemplate() {
    const payload = {
        name: document.getElementById('tplName').value.trim() || 'Untitled Template',
        from_name: document.getElementById('tplFromName').value,
        from_address: document.getElementById('tplFromAddress').value,
        from_email: document.getElementById('tplFromEmail').value,
        from_phone: document.getElementById('tplFromPhone').value,
        from_gstin: document.getElementById('tplFromGstin').value,
        to_name: document.getElementById('tplToName').value,
        to_address: document.getElementById('tplToAddress').value,
        to_email: document.getElementById('tplToEmail').value,
        to_phone: document.getElementById('tplToPhone').value,
        to_gstin: document.getElementById('tplToGstin').value,
        currency: document.getElementById('tplCurrency').value,
        tax_rate: parseFloat(document.getElementById('tplTaxRate').value) || 0,
        discount_rate: parseFloat(document.getElementById('tplDiscount').value) || 0,
        accent_color: document.getElementById('tplAccent').value,
        terms: document.getElementById('tplTerms').value,
        footer: document.getElementById('tplFooter').value,
    };
    const lines = document.getElementById('tplLineItems').value.split('\n').map(l => l.trim()).filter(Boolean).map(l => {
        const p = l.split('|').map(x => x.trim());
        return { description: p[0], quantity: parseFloat(p[1]) || 1, rate: parseFloat(p[2]) || 0 };
    });
    payload.line_items = lines;

    try {
        let res;
        if (editingTemplateId) {
            res = await authFetch(`/api/templates/${editingTemplateId}`, { method: 'PUT', headers: {'Content-Type':'application/json'}, body: JSON.stringify(payload) });
        } else {
            res = await authFetch('/api/templates', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(payload) });
        }
        const data = await res.json();
        if (data.error) { showToast(data.error || 'Template save failed', 'error'); return; }
        const savedId = data.template?.id;
        if (savedId) {
            document.getElementById('tplSelect').value = savedId;
        }
        showToast('Template saved', 'success');
        await loadTemplates();
        document.getElementById('tplSelect').value = savedId || '';
        selectTemplate();
        resetTemplateEditor();
    } catch { showToast('Template save failed', 'error'); }
}

async function deleteTemplate(id) {
    if (!confirm('Delete this template?')) return;
    try {
        await authFetch(`/api/templates/${id}`, { method: 'DELETE' });
        showToast('Template deleted', 'success');
        await loadTemplates();
    } catch { showToast('Delete failed', 'error'); }
}

function getGeneratorPayload() {
    const template = generatorTemplates.find(t => t.id === document.getElementById('tplSelect').value);
    return {
        template_id: template?.id || '',
        invoice_number: document.getElementById('genNumber').value.trim(),
        invoice_date: document.getElementById('genDate').value,
        due_date: document.getElementById('genDue').value,
        customer: {
            name: document.getElementById('genCustomerName').value,
            address: document.getElementById('genCustomerAddress').value,
            email: document.getElementById('genCustomerEmail').value,
            phone: document.getElementById('genCustomerPhone').value,
            gstin: document.getElementById('genCustomerGstin').value,
        },
        line_items: collectLineItems(),
        tax_rate: parseFloat(document.getElementById('genTaxRate').value) || 0,
        discount: parseFloat(document.getElementById('genDiscount').value) || 0,
        currency: document.getElementById('genCurrency').value,
        category: document.getElementById('genCategory').value,
        payment_status: document.getElementById('genStatus').value,
        notes: document.getElementById('genNotes').value,
        terms: document.getElementById('genTerms').value || template?.terms || '',
        footer: document.getElementById('genFooter').value || template?.footer || '',
        accent_color: template?.accent_color || '#4f46e5',
        save_to_library: document.getElementById('genSave').checked,
    };
}

async function generateInvoice() {
    const items = collectLineItems();
    if (!items.length) { showToast('Add at least one line item', 'error'); return; }
    if (!document.getElementById('genCustomerName').value.trim()) { showToast('Add a customer name', 'error'); return; }
    try {
        const res = await authFetch('/api/invoices/generate', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(getGeneratorPayload()) });
        const data = await res.json();
        if (data.error) { showToast(data.error, 'error'); return; }
        lastInvoiceHTML = data.html;
        lastGeneratedId = data.invoice_id || null;
        const frame = document.getElementById('invoicePreviewFrame');
        frame.srcdoc = data.html;
        document.getElementById('genSaveStatus').textContent = data.saved ? 'Saved to library ✓' : 'Preview only';
        showToast(data.message || 'Invoice generated', 'success');
    } catch (err) {
        console.error(err);
        showToast('Generation failed', 'error');
    }
}

function printInvoice() {
    const frame = document.getElementById('invoicePreviewFrame');
    if (frame.contentWindow) frame.contentWindow.print();
}

function downloadInvoiceHTML() {
    if (!lastInvoiceHTML) { showToast('Generate a preview first', 'error'); return; }
    const blob = new Blob([lastInvoiceHTML], { type: 'text/html' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = `invoice-${document.getElementById('genNumber').value || 'generated'}.html`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(a.href);
    showToast('HTML invoice downloaded', 'success');
}

// ─── Export ─────────────────────────────────────────────────────────────────
function exportCSV() {
    window.open('/api/export/csv', '_blank');
    showToast('Download started', 'success');
}

function exportTally() {
    window.open('/api/export/tally', '_blank');
    showToast('Tally export downloading...', 'success');
}

function exportQuickBooks() {
    window.open('/api/export/quickbooks', '_blank');
    showToast('QuickBooks export downloading...', 'success');
}

// ─── Utilities ──────────────────────────────────────────────────────────────
function fmtCur(amt, cur) {
    if (amt === null || amt === undefined) return '—';
    const n = parseFloat(amt);
    const syms = {INR:'₹',USD:'$',EUR:'€',GBP:'£',JPY:'¥',AUD:'A$',CAD:'C$',SGD:'S$',AED:'AED',SAR:'SAR'};
    const s = syms[cur] || cur || '';
    if (cur === 'INR') return s + n.toLocaleString('en-IN', {minimumFractionDigits:0, maximumFractionDigits:2});
    return s + n.toLocaleString('en-US', {minimumFractionDigits:2, maximumFractionDigits:2});
}

function fmtDate(d) {
    if (!d) return '—';
    try { return new Date(d).toLocaleDateString('en-IN', {day:'numeric',month:'short',year:'numeric'}); } catch { return d; }
}

function esc(s) {
    if (!s) return '';
    const el = document.createElement('span');
    el.textContent = s;
    return el.innerHTML;
}

function escAttr(s) {
    return String(s == null ? '' : s)
        .replace(/&/g, '&amp;')
        .replace(/"/g, '&quot;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;');
}

function showToast(msg, type='success') {
    const t = document.getElementById('toast');
    t.textContent = msg;
    t.className = `toast show ${type}`;
    setTimeout(() => t.className = 'toast', 3500);
}

// Close modal on Escape
document.addEventListener('keydown', e => { if (e.key === 'Escape') closeModal(); });
