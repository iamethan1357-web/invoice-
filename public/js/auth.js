// ═══ Clerk loader — shared by the login page and the app shell ═══
//
// Clerk's own docs load clerk-js from your instance's Frontend API domain
// (https://<instance>.clerk.accounts.dev/npm/@clerk/clerk-js@5/...), so that is
// tried first; jsdelivr is kept as a fallback for instances whose domain cannot
// be derived from the publishable key.

(function (global) {
    'use strict';

    function loadScript(src) {
        return new Promise(function (resolve, reject) {
            const s = document.createElement('script');
            s.src = src;
            s.async = true;
            s.crossOrigin = 'anonymous';
            s.onload = function () { resolve(src); };
            s.onerror = function () { reject(new Error('Could not load ' + src)); };
            document.head.appendChild(s);
        });
    }

    // Derive the Frontend API domain from the publishable key when the backend
    // did not send one. pk_test_<b64> decodes to "<instance>.clerk.accounts.dev$".
    function frontendApiFromKey(pk) {
        if (!pk) return '';
        const parts = String(pk).split('_');
        if (parts.length < 3) return '';
        try {
            let payload = parts[2];
            while (payload.length % 4) payload += '=';
            return atob(payload).replace(/\$$/, '');
        } catch (e) {
            return '';
        }
    }

    async function loadClerkJs(config) {
        const domain = (config && config.frontend_api) || frontendApiFromKey(config && config.publishable_key);
        const sources = [];
        if (domain) sources.push('https://' + domain + '/npm/@clerk/clerk-js@5/dist/clerk.browser.js');
        sources.push('https://cdn.jsdelivr.net/npm/@clerk/clerk-js@5/dist/clerk.browser.js');

        let lastError = null;
        for (const src of sources) {
            try {
                await loadScript(src);
                if (global.Clerk) return global.Clerk;
                lastError = new Error('Clerk was not defined after loading ' + src);
            } catch (err) {
                lastError = err;
            }
        }
        throw lastError || new Error('Clerk JS is unavailable');
    }

    // Fetch /api/auth/config. Never throws: a backend hiccup must not brick the UI.
    async function fetchAuthConfig() {
        try {
            const res = await fetch('/api/auth/config');
            if (!res.ok) return { enabled: false, error: 'HTTP ' + res.status };
            return await res.json();
        } catch (err) {
            return { enabled: false, error: String(err && err.message || err) };
        }
    }

    // Load clerk-js and return a loaded Clerk instance, or throw with a reason.
    async function initClerk(config) {
        const Ctor = await loadClerkJs(config);
        const clerk = new Ctor(config.publishable_key);
        global.__clerk = clerk;
        await clerk.load({
            signInUrl: '/login',
            signUpUrl: '/login?mode=sign-up',
            afterSignInUrl: '/',
            afterSignUpUrl: '/',
            afterSignOutUrl: '/login'
        });
        return clerk;
    }

    function initialsOf(user) {
        if (!user) return '';
        const first = (user.firstName || '').trim();
        const last = (user.lastName || '').trim();
        if (first || last) return ((first[0] || '') + (last[0] || '')).toUpperCase();
        const email = user.primaryEmailAddress && user.primaryEmailAddress.emailAddress || user.emailAddress || '';
        return email ? email.slice(0, 2).toUpperCase() : 'U';
    }

    global.InvoiceAuth = {
        loadClerkJs: loadClerkJs,
        fetchAuthConfig: fetchAuthConfig,
        initClerk: initClerk,
        frontendApiFromKey: frontendApiFromKey,
        initialsOf: initialsOf
    };
})(window);
