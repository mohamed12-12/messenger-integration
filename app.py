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

from flask import Flask, request, jsonify, render_template, redirect, url_for, session, send_from_directory
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

# ─── Persistent Config for Auto-Response ──────────────────────────────────────
CONFIG_FILE = os.path.join(os.path.dirname(__file__), 'config.json')
TOKEN_FILE  = os.path.join(os.path.dirname(__file__), 'page_tokens.json')

def load_config():
    if not os.path.exists(CONFIG_FILE): return {'auto_response': False}
    try:
        with open(CONFIG_FILE, 'r') as f: return json.load(f)
    except: return {'auto_response': False}

def save_config(cfg):
    with open(CONFIG_FILE, 'w') as f: json.dump(cfg, f)

def save_page_token(page_id, token):
    tokens = {}
    if os.path.exists(TOKEN_FILE):
        try:
            with open(TOKEN_FILE, 'r') as f: tokens = json.load(f)
        except: pass
    tokens[page_id] = token
    with open(TOKEN_FILE, 'w') as f: json.dump(tokens, f)

def get_page_token(page_id):
    if not os.path.exists(TOKEN_FILE): return None
    try:
        with open(TOKEN_FILE, 'r') as f:
            tokens = json.load(f)
            return tokens.get(page_id)
    except: return None

# ─── File-Based Store for Demo (Safe for Multi-Worker Gunicorn) ────────────────
SCOPES = 'pages_messaging,pages_manage_metadata,pages_read_engagement,pages_show_list'

# ─── File-Based Store for Demo (Safe for Multi-Worker Gunicorn) ────────────────
MESSAGES_FILE = os.path.join(os.path.dirname(__file__), 'recent_messages.json')
MAX_RECENT    = 10

def load_messages():
    if not os.path.exists(MESSAGES_FILE): return []
    try:
        with open(MESSAGES_FILE, 'r') as f: return json.load(f)
    except: return []

def save_message(msg):
    messages = []
    if os.path.exists(MESSAGES_FILE):
        try:
            with open(MESSAGES_FILE, 'r') as f: messages = json.load(f)
        except: pass
    
    # Keep only the last 15 messages
    messages.insert(0, msg)
    messages = messages[:15]
    
    try:
        with open(MESSAGES_FILE, 'w') as f: json.dump(messages, f)
        logger.info("Message saved successfully to %s", MESSAGES_FILE)
    except Exception as e:
        logger.error("CRITICAL: Failed to save message to file: %s", str(e))

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


# ─── HELPER: Send Graph API Message ───────────────────────────────────────────
def send_graph_message(recipient_id: str, text: str, page_access_token: str) -> dict:
    """Sends a message via the Meta Send API."""
    resp = requests.post(
        f'{GRAPH_BASE}/me/messages',
        params={'access_token': page_access_token},
        json={
            'recipient': {'id': recipient_id},
            'message': {'text': text}
        },
        timeout=10
    )
    return resp.json()


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

    session['user_access_token'] = user_access_token
    logger.info("User access token obtained successfully.")

    return redirect('/select-page')


# ─── Page Selection ──────────────────────────────────────────────────────────
@app.route('/select-page')
def select_page():
    """
    Lists the Facebook Pages the user manages so they can select one.
    This satisfies the 'Asset Selection' requirement for Meta App Review.
    """
    user_access_token = session.get('user_access_token')
    if not user_access_token:
        return redirect('/')

    try:
        pages_data = graph_get('me/accounts', {'access_token': user_access_token})
    except requests.RequestException as exc:
        logger.error("Failed to fetch user pages: %s", exc)
        return render_template('index.html', error='Failed to fetch your Facebook Pages.'), 502

    pages = pages_data.get('data', [])
    return render_template('select_page.html', pages=pages)


# ─── Connect Selected Page ────────────────────────────────────────────────────
@app.route('/connect-page/<page_id>')
def connect_specific_page(page_id):
    """
    Subscribes the selected page to the webhook and shows the dashboard.
    """
    user_access_token = session.get('user_access_token')
    if not user_access_token:
        return redirect('/')

    try:
        # Fetch the specific page to get its access token
        page_data = graph_get(page_id, {
            'fields': 'name,access_token',
            'access_token': user_access_token
        })
    except requests.RequestException as exc:
        logger.error("Failed to fetch page details: %s", exc)
        return render_template('index.html', error='Failed to connect the selected page.'), 502

    page_name = page_data.get('name')
    page_access_token = page_data.get('access_token')

    # Subscribe page to webhook
    success = subscribe_page_to_webhook(page_id, page_access_token)

    if success:
        session['connected_page_id'] = page_id
        session['connected_page_name'] = page_name
        session['page_access_token'] = page_access_token
        
        # SAVE TOKEN PERSISTENTLY FOR AUTO-RESPONDER
        try:
            save_page_token(page_id, page_access_token)
        except Exception as e:
            logger.error("Error saving page token: %s", e)
        
        return redirect(url_for('dashboard'))
    else:
        return render_template('index.html', error='Failed to subscribe the page to webhooks.'), 500


# ─── Dashboard (Live Send Action) ─────────────────────────────────────────────
@app.route('/dashboard')
def dashboard():
    """
    The main app UI showing the connected asset and the live send action.
    """
    page_id = session.get('connected_page_id')
    page_name = session.get('connected_page_name')

    if not page_id:
        return redirect('/')

    # Filter messages for this specific page
    all_msgs = load_messages()
    page_messages = [m for m in all_msgs if m.get('page_id') == page_id]

    return render_template('dashboard.html', 
                          page_name=page_name, 
                          page_id=page_id,
                          recent_messages=page_messages)


# ─── Get Recent Messages AJAX ─────────────────────────────────────────────────
@app.route('/api/recent-messages')
def get_recent_messages():
    page_id = session.get('connected_page_id')
    if not page_id:
        return jsonify([])
    
    # Filter messages for this specific page
    all_msgs = load_messages()
    page_messages = [m for m in all_msgs if m.get('page_id') == page_id]
    return jsonify(page_messages)


# ─── Toggle Auto-Response API ──────────────────────────────────────────────────
@app.route('/api/toggle-auto-response', methods=['POST'])
def toggle_auto_response():
    current_cfg = load_config()
    current_cfg['auto_response'] = not current_cfg.get('auto_response', False)
    save_config(current_cfg)
    return jsonify(current_cfg)


@app.route('/api/config')
def get_config():
    return jsonify(load_config())


# ─── Send Message API ──────────────────────────────────────────────────────────
@app.route('/send-message', methods=['POST'])
def send_message():
    """
    Sends a message via the Meta Send API.
    Demonstrates the 'Live Send Action' for App Review.
    """
    recipient_id = request.form.get('recipient_id')
    message_text = request.form.get('message')
    page_access_token = session.get('page_access_token')

    try:
        page_access_token = session.get('page_access_token')
        result = send_graph_message(recipient_id, message_text, page_access_token)
        if 'message_id' in result:
            return jsonify({'success': True, 'result': result})
        else:
            return jsonify({'success': False, 'error': result}), 400
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


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

                if not sender_id or (not text and not message.get('attachments')):
                    continue

                logger.info(
                    "Message received – Page: %s | Sender: %s | Text: %s",
                    page_id, sender_id, text
                )

                # Store for "Live Dashboard" experience
                save_message({
                    'page_id': page_id,
                    'sender_id': sender_id,
                    'text': text or "[Attachment/Non-text]",
                    'timestamp': event.get('timestamp')
                })

                # CHECK AUTO-RESPONSE CONFIG
                config = load_config()
                if config.get('auto_response'):
                    token = get_page_token(page_id)
                    if token:
                        logger.info("Sending AUTO-RESPONSE to %s using stored token", sender_id)
                        response_text = "hello Iam niva, how can i help? "
                        result = send_graph_message(sender_id, response_text, token)
                        
                        if result and 'message_id' in result:
                            # Log and STORE the outgoing message so it appears on the dashboard
                            save_message({
                                'page_id': page_id,
                                'sender_id': 'AUTO_REPLY',
                                'text': f"AUTO: {response_text} (ID: {result['message_id']})",
                                'is_reply': True,
                                'timestamp': int(time.time() * 1000)
                            })
                        else:
                            logger.error("Auto-reply failed: %s", result)
                    else:
                        logger.warning("No stored token found for page %s to send auto-reply", page_id)

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
