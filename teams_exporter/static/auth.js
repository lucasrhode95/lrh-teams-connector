const loading = document.getElementById('auth-loading');
const statusText = document.getElementById('auth-status');
const errorText = document.getElementById('auth-error');
const retryButton = document.getElementById('retry-auth');

async function checkAuthentication() {
  loading.hidden = false;
  statusText.textContent = 'Confirming the saved Teams sign-in…';
  errorText.hidden = true;
  retryButton.hidden = true;
  try {
    const response = await fetch('/api/auth-status', { cache: 'no-store' });
    const result = await response.json();
    if (response.ok && result.authenticated) {
      window.location.replace('/chats');
      return;
    }
    loading.hidden = true;
    errorText.hidden = false;
    retryButton.hidden = false;
    if (response.status === 401 && result.error?.code === 'authentication_required') {
      errorText.innerHTML = 'Teams sign-in is required. In a terminal opened in the project folder, run <code>python -m teams_exporter auth</code>, complete Microsoft sign-in, then reload this page.';
      return;
    }
    errorText.textContent = result.error?.message || 'The sign-in check failed. Try again.';
  } catch (_) {
    loading.hidden = true;
    errorText.textContent = 'Could not reach the local server to confirm Teams sign-in. Check that it is running, then try again.';
    errorText.hidden = false;
    retryButton.hidden = false;
  }
}

retryButton.addEventListener('click', checkAuthentication);
checkAuthentication();
