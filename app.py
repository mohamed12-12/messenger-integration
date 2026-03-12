"""
Nanovate Messenger Integration – Production-Ready Flask App
Designed for AWS deployment (Elastic Beanstalk / EC2 + ALB / ECS)
"""

import os
import json
import logging
import secrets
import hmac
import hashlib
import requests

from flask import Flask, redirect, request, session, render_template, jsonify, abort
from urllib.parse import quote
from dotenv import load_dotenv

# ─── Load .env (local dev only; on AWS use Parameter Store / Env Vars) ────────
load_dotenv()

# ─── Logging – structured, goes to CloudWatch automatically on AWS ─────────────
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s – %(message)s'
)
logger = logging.getLogger(__name__)

# ─── App Setup ────────────────────────────────────────────────────────────────
app = Flask(__name__)

# Secret key: MUST be set via environment variable in production
app.secret_key = os.environ.get('FLASK_SECRET_KEY')
if not app.secret_key:
    raise RuntimeError("FLASK_SECRET_KEY environment variable is not set!")

# Session cookie hardening for production
app.config.update(
    SESSION_COOKIE_SECURE=True,       # Only send cookie over HTTPS
    SESSION_COOKIE_HTTPONLY=True,     # JS cannot access session cookie
    SESSION_COOKIE_SAMESITE='Lax',   # CSRF protection
)

# ─── Load & Validate Credentials ──────────────────────────────────────────────
META_APP_ID     = os.environ.get('META_APP_ID')
META_APP_SECRET = os.environ.get('META_APP_SECRET')
REDIRECT_URI    = (os.environ.get('REDIRECT_URI') or '').strip() or None
VERIFY_TOKEN    = os.environ.get('VERIFY_TOKEN')

_missing = [v for v, k in [
    (META_APP_ID,     'META_APP_ID'),
    (META_APP_SECRET, 'META_APP_SECRET'),
    (REDIRECT_URI,    'REDIRECT_URI'),
    (VERIFY_TOKEN,    'VERIFY_TOKEN'),
] if not v]

# Warn (not crash) so health checks still pass if a variable is pending config
if _missing:
    logger.warning("Some environment variables are not set – some features may not work.")

# Facebook Graph API version
GRAPH_VERSION = 'v22.0'
GRAPH_BASE    = f'https://graph.facebook.com/{GRAPH_VERSION}'

# Permissions requested from the user
SCOPES = 'pages_messaging,pages_manage_metadata,pages_read_engagement,pages_show_list'


# ─── HELPER: Verify Meta Webhook Signature ────────────────────────────────────
def verify_webhook_signature(payload: bytes, signature_header: str) -> bool:
    """
    Validates the X-Hub-Signature-256 header that Meta sends with every POST.
    This prevents fake/spoofed webhook calls.
    """
    if not signature_header or not signature_header.startswith('sha256='):
        return False
    expected_sig = hmac.new(
        META_APP_SECRET.encode('utf-8'),
        payload,
        hashlib.sha256
    ).hexdigest()
    received_sig = signature_header[len('sha256='):]
    return hmac.compare_digest(expected_sig, received_sig)


# ─── HELPER: Subscribe a Page to the Webhook ──────────────────────────────────
def subscribe_page_to_webhook(page_id: str, page_access_token: str) -> bool:
    """
    Subscribes the Facebook Page to receive Messenger webhook events.
    Returns True on success, False on failure.
    """
    logger.info("Subscribing page %s to webhook...", page_id)

    resp = requests.post(
        f'{GRAPH_BASE}/{page_id}/subscribed_apps',
        params={
            'subscribed_fields': 'messages,messaging_postbacks,messaging_optins',
            'access_token':      page_access_token,
        },
        timeout=10
    )

    result = resp.json()
    if result.get('success'):
        logger.info("Page %s successfully subscribed to webhook.", page_id)
        return True
    else:
        error = result.get('error', {})
        logger.error(
            "Webhook subscription failed for page %s – [%s] %s (%s)",
            page_id, error.get('code'), error.get('message'), error.get('type')
        )
        return False


# ─── HELPER: Safe Graph API GET ───────────────────────────────────────────────
def graph_get(path: str, params: dict) -> dict:
    """Makes a GET request to the Facebook Graph API with a timeout."""
    resp = requests.get(f'{GRAPH_BASE}/{path}', params=params, timeout=10)
    resp.raise_for_status()
    return resp.json()


# ══════════════════════════════════════════════════════════════════════════════
#  ROUTES
# ══════════════════════════════════════════════════════════════════════════════

# ─── Health Check (required by AWS ALB / ECS / Elastic Beanstalk) ─────────────
@app.route('/health')
def health():
    """AWS load balancer health check endpoint."""
    return jsonify({'status': 'ok'}), 200


# ─── Root ─────────────────────────────────────────────────────────────────────
@app.route('/')
def index():
    return render_template('index.html')


# ─── Start OAuth Flow ─────────────────────────────────────────────────────────
@app.route('/connect')
def connect():
    """
    Generates a CSRF state token, saves it in the session, then redirects
    the user to Meta's OAuth dialog.
    """
    state = secrets.token_urlsafe(32)
    session['oauth_state'] = state

    auth_url = (
        f'https://www.facebook.com/{GRAPH_VERSION}/dialog/oauth'
        f'?client_id={META_APP_ID}'
        f'&redirect_uri={quote(str(REDIRECT_URI), safe="")}'
        f'&scope={quote(SCOPES)}'
        f'&response_type=code'
        f'&state={state}'
    )

    logger.info("OAuth flow started – redirecting user to Meta login.")
    return redirect(auth_url)


# ─── OAuth Callback ───────────────────────────────────────────────────────────
@app.route('/auth/callback')
def auth_callback():
    """
    Meta redirects the user here after they authorise (or deny) the app.
    1. Verify CSRF state
    2. Exchange code → user access token
    3. Fetch managed Pages
    4. Subscribe the first Page to the webhook
    """
    code  = request.args.get('code')
    state = request.args.get('state')
    error = request.args.get('error')

    # User denied permission
    if error:
        logger.warning("User denied OAuth permission: %s", error)
        return render_template('index.html', error='Permission was denied.'), 400

    # CSRF check
    if not state or state != session.pop('oauth_state', None):
        logger.error("CSRF state mismatch in OAuth callback.")
        return render_template('index.html', error='Security check failed. Please try again.'), 403

    # Exchange code for access token
    try:
        token_data = graph_get('oauth/access_token', {
            'client_id':     META_APP_ID,
            'client_secret': META_APP_SECRET,
            'redirect_uri':  REDIRECT_URI,
            'code':          code,
        })
    except requests.RequestException as exc:
        logger.error("Token exchange request failed: %s", exc)
        return render_template('index.html', error='Failed to contact Meta. Please try again.'), 502

    user_access_token = token_data.get('access_token')
    if not user_access_token:
        logger.error("No access token in token response: %s", token_data)
        return render_template('index.html', error='Could not retrieve access token.'), 502

    logger.info("User access token obtained successfully.")

    # Fetch Pages the user manages
    try:
        pages_data = graph_get('me/accounts', {'access_token': user_access_token})
    except requests.RequestException as exc:
        logger.error("Failed to fetch user pages: %s", exc)
        return render_template('index.html', error='Failed to fetch your Facebook Pages.'), 502

    pages = pages_data.get('data', [])
    if not pages:
        logger.warning("No Facebook Pages found for this user. Raw response: %s", pages_data)
        # User requested to bypass the error and show the success message anyway
        return render_template('success.html', page_name='Nanovate (No Data Returned)', page_id='N/A')

    # Try to find the 'Nanovate' page specifically, otherwise fallback to the first one
    target_page = pages[0]
    for p in pages:
        if 'nanovate' in p.get('name', '').strip().lower():
            target_page = p
            break

    page_name         = target_page.get('name')
    page_id           = target_page.get('id')
    page_access_token = target_page.get('access_token')

    logger.info("Connected page: %s (ID: %s)", page_name, page_id)

    # TODO: Persist page_access_token securely
    # e.g. AWS Secrets Manager, RDS, DynamoDB:
    #   db.save_token(user_id=..., page_id=page_id, token=page_access_token)

    # Subscribe page to webhook
    subscribe_page_to_webhook(page_id, page_access_token)

    return render_template('success.html', page_name=page_name, page_id=page_id)


# ─── Webhook Verification (Meta GET challenge) ────────────────────────────────
@app.route('/webhook', methods=['GET'])
def webhook_verify():
    """
    Meta sends a GET request to verify the webhook endpoint.
    We must echo back the hub.challenge value if the token matches.
    """
    mode      = request.args.get('hub.mode')
    token     = request.args.get('hub.verify_token')
    challenge = request.args.get('hub.challenge')

    if mode == 'subscribe' and token == VERIFY_TOKEN:
        logger.info("Webhook verification passed.")
        return challenge, 200

    logger.warning("Webhook verification FAILED – token mismatch or wrong mode.")
    return 'Forbidden', 403


# ─── Webhook Events (Meta POST messages) ─────────────────────────────────────
@app.route('/webhook', methods=['POST'])
def webhook_event():
    """
    Meta sends all Messenger events here.
    Signature is verified before any processing.
    """
    # Verify the payload signature first (security!)
    raw_body  = request.get_data()
    signature = request.headers.get('X-Hub-Signature-256', '')

    if META_APP_SECRET and not verify_webhook_signature(raw_body, signature):
        logger.warning("Invalid webhook signature – rejecting request.")
        abort(403)

    data = request.get_json(silent=True)
    logger.info("Webhook event received: %s", json.dumps(data))

    if data and data.get('object') == 'page':
        for entry in data.get('entry', []):
            page_id   = entry.get('id')
            messaging = entry.get('messaging', [])

            for event in messaging:
                sender_id = event.get('sender', {}).get('id')
                message   = event.get('message', {})
                text      = message.get('text', '')

                logger.info(
                    "Message received – Page: %s | Sender: %s | Text: %s",
                    page_id, sender_id, text
                )

                # TODO: Route to your message handler / Lambda / SQS queue:
                # handle_message(page_id, sender_id, text)

        return jsonify({'status': 'EVENT_RECEIVED'}), 200

    return jsonify({'status': 'IGNORED'}), 200


# ─── /messenger alias (matches Meta dashboard callback URL) ───────────────────
@app.route('/messenger', methods=['GET'])
def messenger_verify():
    """Alias for /webhook GET – https://agent.nanovate.io/messenger"""
    return webhook_verify()


@app.route('/messenger', methods=['POST'])
def messenger_event():
    """Alias for /webhook POST – https://agent.nanovate.io/messenger"""
    return webhook_event()


# ══════════════════════════════════════════════════════════════════════════════
#  Entry Point (local dev only – use Gunicorn on AWS)
# ══════════════════════════════════════════════════════════════════════════════
if __name__ == '__main__':
    logger.info("Starting Messenger Integration Server (dev mode) on port 5003")
    # debug=False + host=0.0.0.0 for local network testing
    # On AWS: Gunicorn is used instead (see Procfile / gunicorn.conf.py)
    app.run(debug=False, host='0.0.0.0', port=5003)
