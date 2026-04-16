import os
import json
import uuid
import time
import logging
import hmac
import hashlib
import requests
import secrets

from flask import Flask, request, jsonify, render_template, redirect, url_for, session, send_from_directory
from collections import deque
import hmac as hmac_module
from dotenv import load_dotenv

# ─── Load Environment ────────────────────────────────────────────────────────
load_dotenv()

# Configuration
META_APP_ID     = os.getenv('META_APP_ID')
META_APP_SECRET = os.getenv('META_APP_SECRET')
REDIRECT_URI    = os.getenv('REDIRECT_URI')
VERIFY_TOKEN    = os.getenv('VERIFY_TOKEN', 'nanovate_messenger_verify_2026')
SECRET_KEY      = os.getenv('FLASK_SECRET_KEY', 'dev_secret_key_123')

# Instagram Specific Configuration
INSTAGRAM_REDIRECT_URI = os.getenv('INSTAGRAM_REDIRECT_URI')
INSTAGRAM_SCOPES = 'instagram_basic,instagram_manage_messages,pages_messaging,pages_read_engagement,pages_show_list,pages_manage_metadata'

# Flask App Initialization
app = Flask(__name__)
app.secret_key = SECRET_KEY

# Logging setup
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Facebook Graph API version
GRAPH_VERSION = 'v22.0'
GRAPH_BASE    = f'https://graph.facebook.com/{GRAPH_VERSION}'

# Scopes required for the app review
SCOPES = 'pages_messaging,pages_manage_metadata,pages_read_engagement,pages_show_list'

# ─── Persistent Storage Files ────────────────────────────────────────────────
MESSAGES_FILE = os.path.join(os.path.dirname(__file__), 'recent_messages.json')
CONFIG_FILE   = os.path.join(os.path.dirname(__file__), 'config.json')
TOKEN_FILE    = os.path.join(os.path.dirname(__file__), 'page_tokens.json')
INSTAGRAM_MESSAGES_FILE = os.path.join(os.path.dirname(__file__), 'instagram_messages.json')
WEBHOOK_DEBUG_FILE = os.path.join(os.path.dirname(__file__), 'webhook_debug.json')

# In-memory storage for last 20 instagram messages if file doesn't exist
instagram_messages = []
first_messages_sent = set() # Track for automation disclosure session

# Automation Disclosure Texts
DISCLOSURE_EN = "This is an automated response from Nanovate AI customer support. \nType 'human' or 'agent' at any time to speak with a person."
DISCLOSURE_AR = "هذه استجابة آلية من نظام دعم العملاء في نانوفيت. \nاكتب 'مساعدة' في أي وقت للتحدث مع شخص حقيقي."

# In-memory webhook hit tracking
webhook_hits_log = deque(maxlen=10)
last_webhook_info = {
    'timestamp': None,
    'object_type': None,
    'entry_id': None,
    'sender_id': None
}

# ─── Storage Helpers ────────────────────────────────────────────────────────
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
    try:
        with open(TOKEN_FILE, 'w') as f: json.dump(tokens, f)
    except Exception as e:
        logger.error("Failed to write to token file: %s", e)

def get_page_token(page_id):
    if not os.path.exists(TOKEN_FILE): return None
    try:
        with open(TOKEN_FILE, 'r') as f:
            tokens = json.load(f)
            return tokens.get(page_id)
    except: return None

def load_messages():
    if not os.path.exists(MESSAGES_FILE): return []
    try:
        with open(MESSAGES_FILE, 'r') as f: return json.load(f)
    except: return []

def get_messages_for_page(page_id):
    if not page_id:
        return []
    return [msg for msg in load_messages() if msg.get('page_id') == page_id]

def save_message(msg):
    messages = load_messages()
    messages.insert(0, msg)
    messages = messages[:15] # Keep last 15
    try:
        with open(MESSAGES_FILE, 'w') as f: json.dump(messages, f)
    except Exception as e:
        logger.error("Failed to write to messages file: %s", e)

def record_messenger_text_event(page_id, sender_id, text, ts, source):
    save_message({
        'page_id': page_id,
        'sender_id': sender_id,
        'text': text,
        'timestamp': ts,
        'source': source
    })

    save_instagram_message({
        'sender_id': sender_id,
        'text': text,
        'timestamp': ts,
        'direction': 'inbound',
        'source': source
    })

    logger.info("Saved %s message from %s for page %s", source, sender_id, page_id)

def save_instagram_message(msg):
    messages = load_instagram_messages()
    messages.insert(0, msg)
    messages = messages[:20] # Keep last 20
    try:
        with open(INSTAGRAM_MESSAGES_FILE, 'w') as f: json.dump(messages, f)
    except Exception as e:
        logger.error("Failed to write to instagram messages file: %s", e)

def load_instagram_messages():
    if not os.path.exists(INSTAGRAM_MESSAGES_FILE): return []
    try:
        with open(INSTAGRAM_MESSAGES_FILE, 'r') as f: 
            return json.load(f)
    except: return []

# ─── Graph API Helpers ───────────────────────────────────────────────────────
def subscribe_page_to_webhook(page_id: str, page_access_token: str) -> bool:
    if not page_access_token:
        logger.error("Cannot subscribe page %s without a page access token.", page_id)
        return False
    try:
        subscribed_fields = (
            'messages,messaging_postbacks,messaging_optins,'
            'message_reads,message_deliveries,message_echoes,'
            'messaging_handovers,standby'
        )
        # Added message_reads, message_deliveries for better event tracking
        resp = requests.post(
            f'{GRAPH_BASE}/{page_id}/subscribed_apps',
            params={
                'subscribed_fields': subscribed_fields,
                'access_token':      page_access_token,
            },
            timeout=10
        )
        payload = resp.json()
        if resp.ok and payload.get('success', False):
            logger.info("Subscribed page %s to webhook successfully.", page_id)
            return True

        logger.error(
            "Webhook subscription failed for page %s. status=%s payload=%s",
            page_id,
            resp.status_code,
            payload
        )
    except Exception:
        logger.exception("Webhook subscription crashed for page %s.", page_id)
    return False

def send_graph_message(recipient_id: str, text: str, page_access_token: str) -> dict:
    try:
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
    except Exception as e:
        return {'error': str(e)}

def graph_get(path: str, params: dict) -> dict:
    resp = requests.get(f'{GRAPH_BASE}/{path}', params=params, timeout=10)
    resp.raise_for_status()
    return resp.json()

def format_oauth_exchange_error(exc: Exception, platform_name: str) -> str:
    default_msg = f'{platform_name} login failed. Please click Connect again and do not refresh the callback page.'
    response = getattr(exc, 'response', None)
    if response is None:
        return default_msg

    try:
        payload = response.json()
    except ValueError:
        logger.error("%s OAuth exchange failed with non-JSON response: %s", platform_name, response.text)
        return default_msg

    error = payload.get('error', {})
    message = error.get('message', '')
    code = error.get('code')
    subcode = error.get('error_subcode')

    logger.error(
        "%s OAuth exchange failed. status=%s code=%s subcode=%s message=%s",
        platform_name,
        response.status_code,
        code,
        subcode,
        message
    )

    lowered = message.lower()
    if 'authorization code' in lowered or 'verification code' in lowered or 'code has been used' in lowered:
        return f'{platform_name} login code expired or was already used. Please click Connect again and complete the login flow once.'

    return default_msg

def graph_get_all_items(path: str, params: dict) -> list:
    items = []
    next_url = f'{GRAPH_BASE}/{path}'
    next_params = dict(params)

    while next_url:
        resp = requests.get(next_url, params=next_params, timeout=10)
        resp.raise_for_status()
        payload = resp.json()
        items.extend(payload.get('data', []))
        next_url = payload.get('paging', {}).get('next')
        next_params = None

    return items

def get_user_pages(user_access_token: str) -> list:
    return graph_get_all_items('me/accounts', {'access_token': user_access_token})

# ─── Agent & AI Helpers ──────────────────────────────────────────────────────
def get_chat_agent_by_id(agent_id: str):
    """
    Mock helper to get agent data. In a real app, this would fetch from a database.
    We'll return the current session's instagram token if available.
    """
    token = session.get('instagram_page_token') or get_page_token(session.get('instagram_account_id'))
    return {
        "id": agent_id,
        "name": "Nanovate AI",
        "instagram_token": token
    }

async def generate_response(text: str, agent_data: dict) -> str:
    """
    Mock AI response generation.
    """
    text_lower = text.lower()
    if 'hello' in text_lower or 'hi' in text_lower:
        return "Hello! How can I assist you today with Nanovate services?"
    elif 'help' in text_lower or 'مساعدة' in text:
        return "I can help you with your account, billing, or technical issues. What do you need help with?"
    else:
        return f"I received your message: '{text}'. Our team will get back to you soon!"

def send_instagram_message(recipient_id: str, text: str, page_access_token: str):
    return send_graph_message(recipient_id, text, page_access_token)

# ══════════════════════════════════════════════════════════════════════════════
#  ROUTES
# ══════════════════════════════════════════════════════════════════════════════

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/connect')
def connect():
    state = secrets.token_urlsafe(16)
    session['oauth_state'] = state
    fb_url = (
        "https://www.facebook.com/v22.0/dialog/oauth"
        f"?client_id={META_APP_ID}"
        f"&redirect_uri={REDIRECT_URI}"
        f"&state={state}"
        f"&scope={SCOPES}"
        "&response_type=code"
        "&auth_type=rerequest"
    )
    return redirect(fb_url)

@app.route('/auth/callback')
def auth_callback():
    code = request.args.get('code')
    state = request.args.get('state')
    error = request.args.get('error')
    error_description = request.args.get('error_description')
    expected_state = session.pop('oauth_state', None)

    if not state or not expected_state or state != expected_state:
        return render_template('index.html', error='CSRF Error. Please try again.'), 400
    if error:
        return render_template('index.html', error=error_description or error), 400
    if not code:
        return render_template('index.html', error='Missing Facebook login code. Please click Connect again.'), 400
    try:
        token_data = graph_get('oauth/access_token', {
            'client_id':     META_APP_ID,
            'redirect_uri':  REDIRECT_URI,
            'client_secret': META_APP_SECRET,
            'code':          code
        })
        session['user_access_token'] = token_data.get('access_token')
        pages = get_user_pages(session['user_access_token'])
        logger.info("Messenger OAuth retrieved %s pages from Meta.", len(pages))

        page_options = [
            {'id': page.get('id'), 'name': page.get('name')}
            for page in pages
            if page.get('id') and page.get('name')
        ]
        if not page_options:
            return render_template(
                'select_page.html',
                pages=[],
                error='No Facebook Pages were returned. Reconnect and make sure the required Pages are selected in the Facebook dialog.'
            ), 400

        return render_template('select_page.html', pages=page_options)
    except requests.HTTPError as e:
        logger.exception("Messenger auth callback token exchange failed.")
        return render_template('index.html', error=format_oauth_exchange_error(e, 'Facebook')), 400
    except Exception as e:
        logger.exception("Messenger auth callback failed.")
        return render_template('index.html', error='Facebook login failed unexpectedly. Please try connecting again.'), 500

@app.route('/connect-page/<page_id>')
def connect_page(page_id):
    user_token = session.get('user_access_token')
    if not user_token: return redirect('/')
    try:
        pages = get_user_pages(user_token)
        page_data = next((page for page in pages if page.get('id') == page_id), None)
        page_options = [
            {'id': page.get('id'), 'name': page.get('name')}
            for page in pages
            if page.get('id') and page.get('name')
        ]

        if not page_data:
            return render_template(
                'select_page.html',
                pages=page_options,
                error='The selected page was not returned by Meta. Reconnect and make sure that page is selected in the Facebook dialog.'
            ), 400

        page_token = page_data.get('access_token')
        page_name = page_data.get('name') or page_id
        if not page_token:
            logger.error("Meta returned page %s without an access token. payload=%s", page_id, page_data)
            return render_template(
                'select_page.html',
                pages=page_options,
                error=f"Meta did not return a page access token for '{page_name}'. Reconnect and verify the granted Page permissions."
            ), 502

        if subscribe_page_to_webhook(page_id, page_token):
            session['connected_page_id'] = page_id
            session['connected_page_name'] = page_name
            session['page_access_token'] = page_token
            try:
                save_page_token(page_id, page_token)
            except: pass
            return redirect(url_for('dashboard'))
        return render_template(
            'select_page.html',
            pages=page_options,
            error=f"Meta rejected the webhook subscription for '{page_name}'. Check that this page is granted to the app, then reconnect and try again."
        ), 502
    except Exception as e:
        logger.exception("Connecting page %s failed.", page_id)
        return render_template('select_page.html', pages=[], error=str(e)), 500

@app.route('/dashboard')
def dashboard():
    page_name = session.get('connected_page_name')
    page_id = session.get('connected_page_id')
    if not page_name: return redirect('/')
    return render_template('dashboard.html', page_name=page_name, page_id=page_id)

# ══════════════════════════════════════════════════════════════════════════════
#  INSTAGRAM ROUTES
# ══════════════════════════════════════════════════════════════════════════════

@app.route('/instagram')
@app.route('/instagram/connect')
def instagram_connect():
    state = secrets.token_urlsafe(16)
    session['oauth_state'] = state
    # Added auth_type=rerequest to force the page selection dialog if they skipped it before
    fb_url = (
        "https://www.facebook.com/v22.0/dialog/oauth"
        f"?client_id={META_APP_ID}"
        f"&redirect_uri={INSTAGRAM_REDIRECT_URI}"
        f"&state={state}"
        f"&scope={INSTAGRAM_SCOPES}"
        "&auth_type=rerequest"
    )
    return redirect(fb_url)

@app.route('/instagram/auth/callback')
def instagram_auth_callback():
    code = request.args.get('code')
    state = request.args.get('state')
    error = request.args.get('error')
    error_description = request.args.get('error_description')
    expected_state = session.pop('oauth_state', None)

    if not state or not expected_state or state != expected_state:
        return render_template('instagram_index.html', error='CSRF Error. Please try again.'), 400
    if error:
        return render_template('instagram_index.html', error=error_description or error), 400
    if not code:
        return render_template('instagram_index.html', error='Missing Facebook login code. Please click Connect again.'), 400
    try:
        # 1. Exchange code for user access token
        token_data = graph_get('oauth/access_token', {
            'client_id':     META_APP_ID,
            'redirect_uri':  INSTAGRAM_REDIRECT_URI,
            'client_secret': META_APP_SECRET,
            'code':          code
        })
        user_access_token = token_data.get('access_token')
        session['user_access_token'] = user_access_token
        
        # 2. Get Facebook Pages
        pages_data = graph_get('me/accounts', {'access_token': user_access_token})
        
        # Log the full response for debugging (sanitize in production)
        logger.info(f"Pages data response: {json.dumps(pages_data)}")
        
        page_list_count = len(pages_data.get('data', []))
        logger.info(f"Retrieved {page_list_count} pages for user.")
        
        # We need to find if any page has an instagram_business_account
        instagram_account = None
        target_page_id = None
        target_page_token = None
        
        for page in pages_data.get('data', []):
            p_id = page.get('id')
            p_name = page.get('name')
            p_token = page.get('access_token')
            
            logger.info(f"Checking Page: {p_name} ({p_id})")
            
            try:
                page_info = graph_get(p_id, {'fields': 'instagram_business_account,name', 'access_token': p_token})
                ig_account = page_info.get('instagram_business_account')
                
                if ig_account:
                    logger.info(f"SUCCESS: Found Instagram Account {ig_account.get('id')} linked to {p_name}")
                    instagram_account = ig_account
                    target_page_id = p_id
                    target_page_token = p_token
                    subscribe_page_to_webhook(p_id, p_token)
                    break
                else:
                    logger.warning(f"WAIT: Page '{p_name}' does not have an Instagram Business Account linked.")
            except Exception as page_err:
                logger.error(f"ERROR checking Page {p_name}: {str(page_err)}")
            
        if instagram_account:
            # Get Instagram username
            ig_id = instagram_account.get('id')
            ig_info = graph_get(ig_id, {'fields': 'username', 'access_token': target_page_token})
            
            session['instagram_account_id'] = ig_id
            session['instagram_username'] = ig_info.get('username')
            session['instagram_page_token'] = target_page_token
            
            # Save to env logic would normally go here, but for this app we'll use session/token file
            save_page_token(ig_id, target_page_token) # reuse for IG
            
            return render_template('instagram_success.html', 
                                 username=ig_info.get('username'), 
                                 account_id=ig_id)
        
        error_msg = 'No Instagram Business Account found linked to your pages.'
        if page_list_count == 0:
            error_msg = 'No Facebook Pages found. Make sure you selected at least one Page in the login dialog.'
        else:
            error_msg = f'Found {page_list_count} pages, but none have an Instagram Business Account linked. Please check your Page Settings on Facebook.'
            
        return render_template('instagram_index.html', error=error_msg), 400
    except requests.HTTPError as e:
        logger.exception("Instagram auth callback token exchange failed.")
        return render_template('instagram_index.html', error=format_oauth_exchange_error(e, 'Instagram')), 400
    except Exception as e:
        logger.exception("IG OAuth error")
        return render_template('instagram_index.html', error='Instagram login failed unexpectedly. Please try connecting again.'), 500

@app.route('/instagram/webhook', methods=['GET'])
@app.route('/instagram/webhook/<agent_id>', methods=['GET'])
def instagram_webhook_verify(agent_id=None):
    mode = request.args.get('hub.mode')
    token = request.args.get('hub.verify_token')
    challenge = request.args.get('hub.challenge')
    if mode == 'subscribe' and token == VERIFY_TOKEN:
        return challenge, 200
    return "Verification failed", 403

@app.route('/instagram/webhook', methods=['POST'])
@app.route('/instagram/webhook/<agent_id>', methods=['POST'])
def instagram_webhook_event(agent_id=None):
    import asyncio

    # Verify HMAC signature
    signature = request.headers.get('X-Hub-Signature-256')
    if signature:
        try:
            expected = 'sha256=' + hmac_module.new(
                META_APP_SECRET.encode('utf-8'),
                request.data,
                hashlib.sha256
            ).hexdigest()
            if not hmac_module.compare_digest(signature, expected):
                logger.warning(f"Signature mismatch! Meta sent {signature}, we expected {expected}")
            else:
                logger.info("✅ Signature verified correctly.")
        except Exception as e:
            logger.error(f"HMAC verification crashed: {e}")
    else:
        logger.warning("No X-Hub-Signature-256 found in headers.")

    data = request.get_json(force=True)
    
    # Store for debug endpoint
    webhook_hits_log.append({
        'endpoint': '/instagram/webhook',
        'timestamp': time.time(),
        'object': data.get('object'),
        'payload': data
    })
    
    last_webhook_info['timestamp'] = time.time()
    last_webhook_info['object_type'] = data.get('object')

    # RAW DEBUG LOGGING — saves every incoming payload for diagnosis
    try:
        with open(WEBHOOK_DEBUG_FILE, 'w') as f:
            json.dump({'timestamp': time.time(), 'agent_id': agent_id, 'headers': dict(request.headers), 'data': data}, f)
    except Exception as e:
        logger.error(f"Debug write failed: {e}")

    logger.info(f"📥 Instagram Webhook hit — object='{data.get('object')}' agent_id='{agent_id}'")

    obj_type = data.get('object')
    
    # Detailed logging for EVERY hit
    for entry in data.get('entry', []):
        entry_id = entry.get('id')
        last_webhook_info['entry_id'] = entry_id
        for messaging in entry.get('messaging', []):
            sender_id = messaging.get('sender', {}).get('id')
            last_webhook_info['sender_id'] = sender_id
            
            # CRITICAL META FIX: Ignore echoes (messages sent BY the page/IG account)
            # Both entry_id and instagram_account_id represent 'US'
            my_id = os.getenv('INSTAGRAM_ACCOUNT_ID')
            if sender_id == entry_id or (my_id and sender_id == my_id):
                logger.info(f"⏭️ Skipping webhook echo from ourselves (Sender: {sender_id})")
                continue

            message = messaging.get('message', {})
            text = message.get('text')

            if not text:
                continue

            # Save incoming message to UI feed
            save_instagram_message({
                'sender_id': sender_id,
                'text': text,
                'timestamp': messaging.get('timestamp', int(time.time() * 1000)),
                'direction': 'inbound'
            })
            logger.info(f"✅ Saved inbound message from {sender_id}: '{text}'")

            # AI Auto-Responder (only when agent_id route is used)
            if agent_id:
                agent_data = get_chat_agent_by_id(agent_id)
                token = agent_data.get("instagram_token")
                if token:
                    disclosure = ""
                    if sender_id not in first_messages_sent:
                        disclosure = f"{DISCLOSURE_EN}\n\n{DISCLOSURE_AR}\n\n"
                        first_messages_sent.add(sender_id)
                    response_text = asyncio.run(generate_response(text, agent_data))
                    full_reply = f"{disclosure}{response_text}"
                    send_instagram_message(sender_id, full_reply, token)
                    save_instagram_message({
                        'sender_id': 'AUTO_REPLY',
                        'text': full_reply,
                        'timestamp': int(time.time() * 1000),
                        'direction': 'outbound'
                    })
                    logger.info(f"📤 Sent auto-reply to {sender_id}")

    return "EVENT_RECEIVED", 200

@app.route('/instagram/dashboard')
def instagram_dashboard():
    ig_username = session.get('instagram_username')
    ig_id = session.get('instagram_account_id')
    
    # Fallback to .env if session is empty (helps after restarts)
    if not ig_id:
        ig_id = os.getenv('INSTAGRAM_ACCOUNT_ID')
        
    token = session.get('instagram_page_token') or get_page_token(ig_id) or os.getenv('INSTAGRAM_PAGE_TOKEN')
    
    # Diagnostic: Check if token is the wrong type
    token_error = None
    if token and token.startswith('IGAA'):
        token_error = "CRITICAL: You are using an Instagram Basic Display token. This TOKEN DOES NOT SUPPORT MESSAGING. Please reconnect using the OAuth flow to get a proper Page Access Token (starting with EA...)."
    elif not token:
        token_error = "No Access Token found. Please connect your account first."

    if not ig_username and not ig_id:
        return redirect(url_for('instagram_connect'))
        
    msgs = load_instagram_messages()
    return render_template('instagram_dashboard.html', 
                         username=ig_username or "Unknown", 
                         account_id=ig_id,
                         messages=msgs,
                         token_error=token_error,
                         last_hit=last_webhook_info)

@app.route('/instagram/send', methods=['POST'])
def instagram_send():
    recipient_psid = request.form.get('recipient_psid')
    message_text = request.form.get('message')
    token = session.get('instagram_page_token')
    
    if not token:
        # try to get from token file if not in session
        ig_id = session.get('instagram_account_id')
        token = get_page_token(ig_id)

    if not token:
        return jsonify({'success': False, 'error': 'No page token found'}), 401
    
    # Add automation disclosure (Standardized from Task 2.4)
    disclosure = f"\n\n{DISCLOSURE_EN}\n\n{DISCLOSURE_AR}"
    full_message = f"{message_text}{disclosure}"
    
    result = send_graph_message(recipient_psid, full_message, token)
    
    if 'message_id' in result:
        # Save outgoing message too?
        save_instagram_message({
            'sender_id': 'YOU',
            'text': full_message,
            'timestamp': int(time.time() * 1000)
        })
        return jsonify({'success': True, 'result': result})
    return jsonify({'success': False, 'error': result}), 400

@app.route('/api/recent-messages')
def get_recent_messages():
    page_id = session.get('connected_page_id')
    return jsonify(get_messages_for_page(page_id))

@app.route('/api/recent-instagram-messages')
def get_recent_instagram_messages():
    # Fresh load on every request as requested
    return jsonify(load_instagram_messages())

@app.route('/api/webhook-last-hit')
def get_webhook_last_hit():
    return jsonify(list(webhook_hits_log))

@app.route('/api/messenger-debug')
def messenger_debug():
    page_id = session.get('connected_page_id')
    page_name = session.get('connected_page_name')
    user_token = session.get('user_access_token')
    token = get_page_token(page_id) if page_id else None

    debug_info = {
        'connected_page_id': page_id,
        'connected_page_name': page_name,
        'has_saved_page_token': bool(token),
        'message_count': len(get_messages_for_page(page_id)),
        'last_webhook_hit_timestamp': last_webhook_info['timestamp'],
        'last_object_type': last_webhook_info['object_type'],
        'last_entry_id': last_webhook_info['entry_id'],
        'last_sender_id': last_webhook_info['sender_id'],
        'last_hit_matches_connected_page': bool(page_id and last_webhook_info['entry_id'] == page_id),
        'is_subscribed': None,
        'subscribed_fields': [],
        'subscription_error': None
    }

    if not page_id or not user_token:
        return jsonify(debug_info)

    try:
        pages = get_user_pages(user_token)
        page_data = next((page for page in pages if page.get('id') == page_id), None)
        page_token = (page_data or {}).get('access_token') or token

        if page_token:
            subs = graph_get(f'{page_id}/subscribed_apps', {'access_token': page_token})
            subscribed_fields = []
            for sub in subs.get('data', []):
                subscribed_fields = sub.get('subscribed_fields', [])

            debug_info['is_subscribed'] = len(subscribed_fields) > 0
            debug_info['subscribed_fields'] = subscribed_fields
        else:
            debug_info['subscription_error'] = 'No page access token found for the connected page.'
    except Exception as e:
        debug_info['subscription_error'] = str(e)

    return jsonify(debug_info)

@app.route('/api/instagram-debug')
def instagram_debug():
    msgs = load_instagram_messages()
    return jsonify({
        'last_webhook_hit_timestamp': last_webhook_info['timestamp'],
        'last_object_type': last_webhook_info['object_type'],
        'last_entry_id': last_webhook_info['entry_id'],
        'message_count': len(msgs),
        'last_3_raw_messages': msgs[:3]
    })

@app.route('/api/toggle-auto-response', methods=['POST'])
def toggle_auto_response():
    cfg = load_config()
    cfg['auto_response'] = not cfg.get('auto_response', False)
    save_config(cfg)
    return jsonify(cfg)

@app.route('/api/config')
def get_config():
    return jsonify(load_config())

@app.route('/api/webhook-debug')
def get_webhook_debug():
    if not os.path.exists(WEBHOOK_DEBUG_FILE):
        return jsonify({'error': 'No debug logs found yet. Webhook hasn\'t been hit.'})
    try:
        with open(WEBHOOK_DEBUG_FILE, 'r') as f:
            return jsonify(json.load(f))
    except Exception as e:
        return jsonify({'error': str(e)})

@app.route('/api/check-subscription')
def check_subscription():
    # CRITICAL: me/accounts requires a USER access token, NOT a page/IG token
    user_token = session.get('user_access_token')
    
    if not user_token:
        return jsonify({
            'success': False, 
            'error': 'No user session found. You MUST reconnect via the OAuth button — manually pasting an IGAA token does not work for this check.'
        })
    
    # Detect wrong token type early
    if user_token.startswith('IGAA'):
        return jsonify({
            'success': False,
            'error': 'Wrong token type (IGAA = Basic Display). You must reconnect via the Instagram OAuth flow to get an EAA... Page Access Token.'
        })
    
    try:
        # me/accounts returns all Facebook Pages the user manages
        pages_data = graph_get('me/accounts', {'access_token': user_token})
        page_status = []
        
        for page in pages_data.get('data', []):
            p_id = page.get('id')
            p_token = page.get('access_token')
            # Check what fields this page is subscribed to
            try:
                subs = graph_get(f'{p_id}/subscribed_apps', {'access_token': p_token})
                subscribed_fields = []
                for sub in subs.get('data', []):
                    subscribed_fields = sub.get('subscribed_fields', [])
                page_status.append({
                    'page_name': page.get('name'),
                    'page_id': p_id,
                    'subscribed_fields': subscribed_fields,
                    'is_subscribed': len(subscribed_fields) > 0
                })
            except Exception as sub_err:
                page_status.append({
                    'page_name': page.get('name'),
                    'page_id': p_id,
                    'subscribed_fields': [],
                    'is_subscribed': False,
                    'error': str(sub_err)
                })
            
        return jsonify({'success': True, 'pages_found': len(page_status), 'data': page_status})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@app.route('/send-message', methods=['POST'])
def send_message():
    recipient_id = request.form.get('recipient_id')
    message_text = request.form.get('message')
    token = session.get('page_access_token')
    page_id = session.get('connected_page_id')

    if not token:
        return jsonify({'success': False, 'error': 'No connected page token found. Please reconnect the page.'}), 401

    result = send_graph_message(recipient_id, message_text, token)
    if 'message_id' in result:
        save_message({
            'page_id': page_id,
            'sender_id': 'MANUAL_REPLY',
            'text': f"{message_text} (ID: {result['message_id']})",
            'is_reply': True,
            'timestamp': int(time.time() * 1000)
        })
        return jsonify({'success': True, 'result': result})
    return jsonify({'success': False, 'error': result}), 400

# ─── WEBHOOKS ───────────────────────────────────────────────────────────────
@app.route('/webhook', methods=['GET'])
@app.route('/messenger', methods=['GET'])
def webhook_verify():
    mode = request.args.get('hub.mode')
    token = request.args.get('hub.verify_token')
    challenge = request.args.get('hub.challenge')
    if mode == 'subscribe' and token == VERIFY_TOKEN:
        return challenge, 200
    return "Verification failed", 403

@app.route('/webhook', methods=['POST'])
@app.route('/messenger', methods=['POST'])
def webhook_event():
    data = request.get_json(force=True)

    # RAW DEBUG — also log here so we can see if Instagram DMs arrive at this endpoint
    # Store for debug endpoint
    webhook_hits_log.append({
        'endpoint': '/webhook',
        'timestamp': time.time(),
        'object': data.get('object'),
        'payload': data
    })

    try:
        with open(WEBHOOK_DEBUG_FILE, 'w') as f:
            json.dump({
                'timestamp': time.time(),
                'endpoint': '/webhook',
                'headers': dict(request.headers),
                'data': data
            }, f)
    except Exception as e:
        logger.error(f"Messenger debug write failed: {e}")
    
    last_webhook_info['timestamp'] = time.time()
    last_webhook_info['object_type'] = data.get('object')

    # Verify HMAC signature (non-blocking)
    signature = request.headers.get('X-Hub-Signature-256')
    if signature:
        try:
            expected = 'sha256=' + hmac_module.new(
                META_APP_SECRET.encode('utf-8'),
                request.data,
                hashlib.sha256
            ).hexdigest()
            if hmac_module.compare_digest(signature, expected):
                logger.info("✅ /webhook Signature verified.")
            else:
                logger.warning(f"/webhook Signature mismatch! Meta: {signature}, Expected: {expected}")
        except Exception as e:
            logger.error(f"/webhook HMAC error: {e}")

    logger.info(f"📥 /webhook hit — object='{data.get('object')}'")

    if data.get('object') == 'page':
        for entry in data.get('entry', []):
            page_id = entry.get('id')
            last_webhook_info['entry_id'] = page_id
            handled_entry = False

            for channel_name, source_name in (
                ('messaging', 'messenger_webhook'),
                ('standby', 'messenger_standby')
            ):
                for event in entry.get(channel_name, []):
                    handled_entry = True
                    sender_id = event.get('sender', {}).get('id')
                    last_webhook_info['sender_id'] = sender_id

                    my_id = os.getenv('INSTAGRAM_ACCOUNT_ID')
                    if sender_id == page_id or (my_id and sender_id == my_id):
                        logger.info("Skipping echo from %s", sender_id)
                        continue

                    message = event.get('message', {})
                    text = message.get('text')
                    if not text:
                        continue

                    ts = event.get('timestamp', int(time.time() * 1000))
                    record_messenger_text_event(page_id, sender_id, text, ts, source_name)

                    if channel_name == 'messaging' and load_config().get('auto_response'):
                        token = get_page_token(page_id)
                        if token:
                            reply_text = "hello I am niva, how can I help? "
                            res = send_graph_message(sender_id, reply_text, token)
                            if 'message_id' in res:
                                save_message({
                                    'page_id': page_id,
                                    'sender_id': 'AUTO_REPLY',
                                    'text': f"NIVA: {reply_text} (ID: {res['message_id']})",
                                    'is_reply': True,
                                    'timestamp': int(time.time() * 1000),
                                    'source': 'messenger_auto_reply'
                                })

            if handled_entry:
                continue
            for event in entry.get('messaging', []):
                sender_id = event.get('sender', {}).get('id')
                last_webhook_info['sender_id'] = sender_id
                
                # CRITICAL META FIX: Ignore echoes
                my_id = os.getenv('INSTAGRAM_ACCOUNT_ID')
                if sender_id == page_id or (my_id and sender_id == my_id):
                    logger.info(f"⏭️ /webhook: Skipping echo from {sender_id}")
                    continue

                message = event.get('message', {})
                text = message.get('text')
                if text:
                    ts = event.get('timestamp', int(time.time() * 1000))

                    # Save to Messenger feed
                    save_message({
                        'page_id': page_id,
                        'sender_id': sender_id,
                        'text': text,
                        'timestamp': ts
                    })

                    # ★ ALSO save to Instagram feed
                    save_instagram_message({
                        'sender_id': sender_id,
                        'text': text,
                        'timestamp': ts,
                        'direction': 'inbound',
                        'source': 'messenger_webhook'
                    })
                    logger.info(f"✅ Saved message from {sender_id} to BOTH feeds")

                    # Auto-Responder Logic (Messenger)
                    if load_config().get('auto_response'):
                        token = get_page_token(page_id)
                        if token:
                            reply_text = "hello I am niva, how can I help? "
                            res = send_graph_message(sender_id, reply_text, token)
                            if 'message_id' in res:
                                save_message({
                                    'page_id': page_id,
                                    'sender_id': 'AUTO_REPLY',
                                    'text': f"NIVA: {reply_text} (ID: {res['message_id']})",
                                    'is_reply': True,
                                    'timestamp': int(time.time() * 1000)
                                })
        return "EVENT_RECEIVED", 200

    # Also handle instagram object type here (fallback)
    if data.get('object') == 'instagram':
        for entry in data.get('entry', []):
            for messaging in entry.get('messaging', []):
                sender_id = messaging.get('sender', {}).get('id')
                text = messaging.get('message', {}).get('text')
                if sender_id and text:
                    save_instagram_message({
                        'sender_id': sender_id,
                        'text': text,
                        'timestamp': messaging.get('timestamp', int(time.time() * 1000)),
                        'direction': 'inbound',
                        'source': 'messenger_webhook_ig'
                    })
                    logger.info(f"✅ Saved Instagram DM from {sender_id}")
        return "EVENT_RECEIVED", 200

    return "IGNORED", 200

# ─── COMPLIANCE ENDPOINTS ─────────────────────────────────────────────────────
@app.route('/instagram/deauth', methods=['POST'])
def instagram_deauth():
    """Facebook/Instagram App Deauthorization Callback"""
    logger.warning("App deauthorized by a user.")
    return jsonify({'success': True}), 200

@app.route('/instagram/data-deletion', methods=['POST'])
def instagram_data_deletion():
    """Facebook/Instagram Data Deletion Request Callback"""
    logger.warning("Data deletion requested.")
    # In a real app, you would handle the deletion logic here
    # and return a confirmation code/URL
    return jsonify({
        'url': 'https://messenger-integration.nanovate.io/instagram/data-deletion-status',
        'confirmation_code': str(uuid.uuid4())
    }), 200


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
