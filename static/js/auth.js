/**
 * Business AI — client-side auth helper.
 * Same pattern as Shri AI's auth.js (proven), pointed at this app's routes.
 */
(function (window) {
  'use strict';

  const TOKEN_KEY = 'ba_auth_token';
  const TENANT_KEY = 'ba_tenant_id';
  const ROLE_KEY = 'ba_role';

  const BizAuth = {
    setSession(token, tenantId, role) {
      localStorage.setItem(TOKEN_KEY, token);
      if (tenantId) localStorage.setItem(TENANT_KEY, tenantId);
      if (role) localStorage.setItem(ROLE_KEY, role);
    },

    getToken() {
      return localStorage.getItem(TOKEN_KEY);
    },

    getTenantId() {
      return localStorage.getItem(TENANT_KEY);
    },

    getRole() {
      return localStorage.getItem(ROLE_KEY);
    },

    isAuthenticated() {
      return !!this.getToken();
    },

    logout(redirectUrl = '/login') {
      localStorage.removeItem(TOKEN_KEY);
      localStorage.removeItem(TENANT_KEY);
      localStorage.removeItem(ROLE_KEY);
      if (redirectUrl) window.location.href = redirectUrl;
    },

    getAuthHeaders(extra = {}) {
      const token = this.getToken();
      const headers = { ...extra };
      if (token) headers['Authorization'] = 'Bearer ' + token;
      return headers;
    },

    // Only bare same-origin relative paths survive — never trust a raw
    // return_url query param for navigation (this exact bug, and this
    // exact fix, shipped in Shri AI this same session).
    sanitizeReturnUrl(raw, fallback = '/dashboard') {
      if (!raw || typeof raw !== 'string') return fallback;
      if (!/^\/(?!\/)[^\s\\]*$/.test(raw)) return fallback;
      try {
        const resolved = new URL(raw, window.location.origin);
        if (resolved.origin !== window.location.origin) return fallback;
        return resolved.pathname + resolved.search + resolved.hash;
      } catch (e) {
        return fallback;
      }
    },

    requireAuth(loginUrl = '/login') {
      if (!this.isAuthenticated()) {
        const returnUrl = encodeURIComponent(window.location.pathname + window.location.search);
        window.location.href = loginUrl + '?return_url=' + returnUrl;
        return false;
      }
      return true;
    },

    formatError(data, fallback = 'An unexpected error occurred.') {
      if (!data) return fallback;
      if (typeof data === 'string') return data;
      if (data.detail) {
        if (typeof data.detail === 'string') return data.detail;
        if (Array.isArray(data.detail)) {
          const msgs = data.detail.map(err => {
            const field = Array.isArray(err.loc) ? err.loc.filter(x => x !== 'body').join('.') : '';
            const msg = err.msg || 'Invalid value';
            return field ? `${field}: ${msg}` : msg;
          }).filter(Boolean);
          if (msgs.length > 0) return msgs.join(', ');
        }
        if (typeof data.detail === 'object') {
          try { return JSON.stringify(data.detail); } catch (e) { return fallback; }
        }
      }
      if (data.message && typeof data.message === 'string') return data.message;
      return fallback;
    },
  };

  window.BizAuth = BizAuth;

  if (typeof document !== 'undefined') {
    function handleSignOutClick(e) {
      if (!e.target || !e.target.closest) return;
      if (e.target.closest('[data-signout]')) {
        e.preventDefault();
        BizAuth.logout('/login');
      }
    }
    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', () => document.addEventListener('click', handleSignOutClick));
    } else {
      document.addEventListener('click', handleSignOutClick);
    }
  }
})(window);
