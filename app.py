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

def save_message(msg):
    messages = load_messages()
    messages.insert(0, msg)
    messages = messages[:15] # Keep last 15
    try:
        with open(MESSAGES_FILE, 'w') as f: json.dump(messages, f)
    except Exception as e:
        logger.error("Failed to write to messages file: %s", e)

def save_instagram_message(msg):
    global instagram_messages
    instagram_messages.insert(0, msg)
    instagram_messages = instagram_messages[:20] # Keep last 20
    try:
        with open(INSTAGRAM_MESSAGES_FILE, 'w') as f: json.dump(instagram_messages, f)
    except Exception as e:
        logger.error("Failed to write to instagram messages file: %s", e)

def load_instagram_messages():
    global instagram_messages
    if not os.path.exists(INSTAGRAM_MESSAGES_FILE): return []
    try:
        with open(INSTAGRAM_MESSAGES_FILE, 'r') as f: 
            instagram_messages = json.load(f)
            return instagram_messages
    except: return []

# ─── Graph API Helpers ───────────────────────────────────────────────────────
def subscribe_page_to_webhook(page_id: str, page_access_token: str) -> bool:
    try:
        # Added message_reads, message_deliveries for better event tracking
        resp = requests.post(
            f'{GRAPH_BASE}/{page_id}/subscribed_apps',
            params={
                'subscribed_fields': 'messages,messaging_postbacks,messaging_optins,message_reads,message_deliveries',
                'access_token':      page_access_token,
            },
            timeout=10
        )
        return resp.json().get('success', False)
    except: return False

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
    )
    return redirect(fb_url)

@app.route('/auth/callback')
def auth_callback():
    code = request.args.get('code')
    state = request.args.get('state')
    if state != session.get('oauth_state'):
        return render_template('index.html', error='CSRF Error. Please try again.'), 400
    try:
        token_data = graph_get('oauth/access_token', {
            'client_id':     META_APP_ID,
            'redirect_uri':  REDIRECT_URI,
            'client_secret': META_APP_SECRET,
            'code':          code
        })
        session['user_access_token'] = token_data.get('access_token')
        pages_data = graph_get('me/accounts', {'access_token': session['user_access_token']})
        return render_template('select_page.html', pages=pages_data.get('data', []))
    except Exception as e:
        return render_template('index.html', error=str(e)), 500

@app.route('/connect-page/<page_id>')
def connect_page(page_id):
    user_token = session.get('user_access_token')
    if not user_token: return redirect('/')
    try:
        page_data = graph_get(page_id, {'fields': 'access_token,name', 'access_token': user_token})
        page_token = page_data.get('access_token')
        if subscribe_page_to_webhook(page_id, page_token):
            session['connected_page_id'] = page_id
            session['connected_page_name'] = page_data.get('name')
            session['page_access_token'] = page_token
            try:
                save_page_token(page_id, page_token)
            except: pass
            return redirect(url_for('dashboard'))
        return render_template('index.html', error='Subscription failed'), 500
    except Exception as e:
        return render_template('index.html', error=str(e)), 500

@app.route('/dashboard')
def dashboard():
    page_name = session.get('connected_page_name')
    if not page_name: return redirect('/')
    return render_template('dashboard.html', page_name=page_name)

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
    if state != session.get('oauth_state'):
        return render_template('instagram_index.html', error='CSRF Error. Please try again.'), 400
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
        
    except Exception as e:
        logger.error(f"IG OAuth error: {str(e)}")
        return render_template('instagram_index.html', error=str(e)), 500

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
    # Verify signature
    signature = request.headers.get('X-Hub-Signature-256')
    if signature:
        expected = 'sha256=' + hmac.new(
            META_APP_SECRET.encode('utf-8'),
            request.data,
            hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(signature, expected):
            logger.warning("Invalid signature on Instagram webhook")
            return "Invalid signature", 403

    data = request.get_json(force=True)
    
    # RAW DEBUG LOGGING - Log everything to help diagnose missing events
    try:
        debug_info = {
            'timestamp': time.time(),
            'agent_id': agent_id,
            'headers': dict(request.headers),
            'data': data
        }
        with open(WEBHOOK_DEBUG_FILE, 'w') as f:
            json.dump(debug_info, f)
    except: pass

    logger.info(f"Incoming Instagram Webhook (Agent: {agent_id}): {json.dumps(data)}")
    
    if data.get('object') == 'instagram':
        for entry in data.get('entry', []):
            for messaging in entry.get('messaging', []):
                sender_id = messaging.get('sender', {}).get('id')
                message = messaging.get('message', {})
                text = message.get('text')
                
                if text:
                    # Log the message for the UI
                    save_instagram_message({
                        'sender_id': sender_id,
                        'text': text,
                        'timestamp': messaging.get('timestamp')
                    })
                    
                    # AI Auto-Responder with Disclosure logic
                    if agent_id:
                        import asyncio
                        agent_data = get_chat_agent_by_id(agent_id)
                        
                        if agent_data.get("instagram_token"):
                            # Check if this is the FIRST message this session
                            disclosure = ""
                            if sender_id not in first_messages_sent:
                                disclosure = f"{DISCLOSURE_EN}\n\n{DISCLOSURE_AR}\n\n"
                                first_messages_sent.add(sender_id)
                            
                            # Generate response (async helper called here)
                            response_text = asyncio.run(generate_response(text, agent_data))
                            
                            # Send reply
                            full_reply = f"{disclosure}{response_text}"
                            send_instagram_message(sender_id, full_reply, agent_data["instagram_token"])
                            
                            # Save the reply for the UI
                            save_instagram_message({
                                'sender_id': 'AUTO_REPLY',
                                'text': full_reply,
                                'timestamp': int(time.time() * 1000)
                            })
                            
        return "EVENT_RECEIVED", 200
    
    logger.warning(f"Ignored webhook object: {data.get('object')}")
    return "IGNORED", 200

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
                         token_error=token_error)

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
    return jsonify(load_messages())

@app.route('/api/recent-instagram-messages')
def get_recent_instagram_messages():
    return jsonify(load_instagram_messages())

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

@app.route('/send-message', methods=['POST'])
def send_message():
    recipient_id = request.form.get('recipient_id')
    message_text = request.form.get('message')
    token = session.get('page_access_token')
    result = send_graph_message(recipient_id, message_text, token)
    if 'message_id' in result:
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
    # Basic signature validation (Optional but recommended)
    data = request.json
    if data.get('object') == 'page':
        for entry in data.get('entry', []):
            page_id = entry.get('id')
            for event in entry.get('messaging', []):
                sender_id = event.get('sender', {}).get('id')
                message = event.get('message', {})
                text = message.get('text')
                if text:
                    save_message({
                        'page_id': page_id,
                        'sender_id': sender_id,
                        'text': text,
                        'timestamp': event.get('timestamp')
                    })
                    # Auto-Responder Logic
                    if load_config().get('auto_response'):
                        token = get_page_token(page_id)
                        if token:
                            reply_text = "hello Iam niva, how can i help? "
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
    return "IGNORED", 200

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
