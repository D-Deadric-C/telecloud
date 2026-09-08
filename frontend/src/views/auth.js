// Authentication: login/signup, the "verify your email" screen, the Supabase
// email-confirmation redirect, and logout.

import { Auth } from '../api.js';
import {
  session, setSession, setUser, clearSession, signInAnonymously,
} from '../session.js';
import { state } from '../state.js';
import { showToast, transitionToView, renderIcons } from '../ui.js';
import { loadDashboard } from './files.js';

export function switchAuthTab(tab) {
  state.currentAuthTab = tab;
  const loginTab = document.getElementById('tab-login');
  const signupTab = document.getElementById('tab-signup');
  const submitBtn = document.getElementById('auth-submit-btn');
  const note = document.getElementById('auth-switch-note');

  if (tab === 'login') {
    loginTab.classList.add('active');
    signupTab.classList.remove('active');
    submitBtn.textContent = 'Log in';
    note.innerHTML = "Don't have an account? <a href=\"#\" data-action=\"auth-tab\" data-tab=\"signup\">Sign up.</a>";
  } else {
    loginTab.classList.remove('active');
    signupTab.classList.add('active');
    submitBtn.textContent = 'Sign up';
    note.innerHTML = 'Already have an account? <a href="#" data-action="auth-tab" data-tab="login">Log in.</a>';
  }
  document.getElementById('auth-form-el').reset();
}

export async function handleAuthSubmit(event) {
  event.preventDefault();
  const email = document.getElementById('auth-email').value;
  const password = document.getElementById('auth-password').value;
  const submitBtn = document.getElementById('auth-submit-btn');
  const isLogin = state.currentAuthTab === 'login';

  submitBtn.disabled = true;
  submitBtn.innerHTML = '<div class="spinner" style="width:16px;height:16px;border-top-color:#fff;"></div>';

  try {
    if (isLogin) {
      const data = await Auth.login(email, password);
      setSession({
        access_token: data.access_token,
        refresh_token: data.refresh_token,
        expires_in: data.expires_in,
        user: data.user,
      });
      // Show the dashboard shell immediately; data streams in right after.
      transitionToView('dashboard-view');
      loadDashboard();
    } else {
      const data = await Auth.signup(email, password);
      state.pendingVerificationEmail = data.email || email;
      localStorage.setItem('pending_verification_email', state.pendingVerificationEmail);
      showToast(data.message || 'Check your email for a verification link.');
      transitionToView('verify-view');
    }
  } catch (err) {
    // Supabase rejects login until the email is confirmed.
    if (/not confirmed|not verified|confirm your email/i.test(err.message)) {
      state.pendingVerificationEmail = email;
      localStorage.setItem('pending_verification_email', email);
      showToast('Please verify your email first. Check your inbox.', 'error');
      transitionToView('verify-view');
    } else {
      showToast(err.message, 'error');
    }
  } finally {
    submitBtn.disabled = false;
    submitBtn.textContent = isLogin ? 'Log in' : 'Sign up';
  }
}

export async function resendVerificationEmail() {
  const targetEmail = state.pendingVerificationEmail || session.user?.email;
  if (!targetEmail) {
    showToast('Enter your email on the login screen first.', 'error');
    return;
  }
  try {
    const res = await Auth.resend(targetEmail);
    if (res.status === 202) {
      showToast('Verification email sent. Check your inbox.');
    } else {
      const data = await res.json().catch(() => ({}));
      throw new Error(data.error?.message || 'Could not send verification email.');
    }
  } catch (err) {
    showToast(err.message, 'error');
  }
}

// "Try Demo": one click, one throwaway anonymous account, straight into the
// dashboard. This is the sanctioned "let me poke at it first" path — real email
// signups must verify (there is no longer a skip button on the verify screen).
export async function handleTryDemo(button) {
  const original = button.innerHTML;
  button.disabled = true;
  button.innerHTML = '<div class="spinner" style="width:16px;height:16px;"></div>';

  try {
    await signInAnonymously();
    setUser(await Auth.me());
    showToast('Demo account ready — you\'re in a private sandbox.');
    transitionToView('dashboard-view');
    loadDashboard();
  } catch (err) {
    showToast(err.message, 'error');
  } finally {
    button.disabled = false;
    button.innerHTML = original;
    renderIcons(button);
  }
}

// Supabase confirmation redirect: tokens (or an error) arrive in the URL fragment.
export async function handleSupabaseRedirect(hash) {
  const params = new URLSearchParams(hash.replace(/^#/, ''));
  const err = params.get('error_description') || params.get('error');
  const accessToken = params.get('access_token');
  window.history.replaceState({}, document.title, window.location.pathname);

  if (err || !accessToken) {
    transitionToView('confirm-view');
    const box = document.getElementById('confirm-icon-box');
    box.innerHTML = '<i data-lucide="alert-triangle" style="width:32px;height:32px;color:var(--destructive)"></i>';
    box.style.backgroundColor = '#fee2e2';
    document.getElementById('confirm-title').textContent = 'Verification Failed';
    document.getElementById('confirm-description').textContent = err
      ? decodeURIComponent(err.replace(/\+/g, ' '))
      : 'This confirmation link is invalid or has expired.';
    const btn = document.getElementById('confirm-action-btn');
    btn.textContent = 'Back to Log in';
    btn.style.display = 'block';
    renderIcons(document.getElementById('confirm-view'));
    return;
  }

  setSession({
    access_token: accessToken,
    refresh_token: params.get('refresh_token'),
    expires_in: Number(params.get('expires_in')) || 0,
  });
  localStorage.removeItem('pending_verification_email');
  state.pendingVerificationEmail = null;

  try {
    setUser(await Auth.me());
    showToast('Email verified! Your account is now fully active.');
    await loadDashboard();
    transitionToView('dashboard-view');
  } catch {
    transitionToView('auth-view');
    showToast('Email verified. Please log in to continue.');
  }
}

export async function handleLogout() {
  try {
    if (session.access) await Auth.logout(session.refresh);
  } catch (e) {
    console.error('Stateless logout fallback executed', e);
  }
  clearSession();
  state.currentFolderId = null;
  state.navigationHistory = [];
  if (window.location.search) {
    window.history.replaceState({}, document.title, window.location.pathname);
  }
  transitionToView('auth-view');
  switchAuthTab('login');
}
