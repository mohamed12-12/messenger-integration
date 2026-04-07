You are a senior Meta Platform engineer. I need you to extend an existing 
Flask application that already has a working Facebook Messenger OAuth 
integration. I want to add Instagram Messaging API support following the 
exact same patterns already in the codebase.

EXISTING CODEBASE CONTEXT:
- Framework: Flask + Gunicorn on Linux server
- Already working: Facebook OAuth, pages_messaging, pages_manage_metadata, pages_show_list
- Existing OAuth callback: /auth/callback
- Existing success page: success.html
- Existing index page: index.html
- App ID: 676537285287395
- Existing .env variables: META_APP_ID, META_APP_SECRET, REDIRECT_URI, FLASK_SECRET_KEY, VERIFY_TOKEN

WHAT I NEED YOU TO BUILD:
Add Instagram Messaging API integration to the existing app.py with these exact routes:

1. GET /instagram
   - Landing page showing "Connect Your Instagram Business Account"
   - Same design as existing index.html
   - Button that triggers Instagram OAuth

2. GET /instagram/connect
   - Starts OAuth flow with these scopes ONLY:
     instagram_basic, instagram_manage_messages, pages_messaging, pages_read_engagement
   - Uses same CSRF state pattern as existing /connect route

3. GET /instagram/auth/callback
   - Receives OAuth code
   - Exchanges code for user access token
   - Calls /me/accounts to get Facebook Pages
   - For each page calls /{page-id}?fields=instagram_business_account to get Instagram account
   - Stores instagram_account_id and instagram_token
   - Subscribes page to Instagram webhook
   - Renders instagram_success.html showing Instagram username and account ID

4. GET /instagram/webhook
   - Webhook verification (hub.mode, hub.verify_token, hub.challenge)
   - Uses same VERIFY_TOKEN from .env

5. POST /instagram/webhook
   - Receives Instagram DM events
   - Verifies X-Hub-Signature-256 signature (same as existing webhook)
   - Parses entry.messaging events
   - Logs: sender_id, message text, timestamp
   - Returns EVENT_RECEIVED

6. GET /instagram/dashboard
   - Shows connected Instagram account name and ID (asset selection)
   - Shows incoming messages panel (last 20 messages from memory/session)
   - Has send message form with:
     - Recipient PSID field
     - Message textarea  
     - Submit button

7. POST /instagram/send
   - Receives recipient_psid and message from form
   - Calls POST /v22.0/me/messages with instagram page token
   - Returns success or error response to dashboard

TEMPLATES NEEDED:
- templates/instagram_index.html (connect page)
- templates/instagram_success.html (connected confirmation)
- templates/instagram_dashboard.html (send + receive UI)

TECHNICAL REQUIREMENTS:
- Follow exact same code patterns as existing app.py
- Use same error handling patterns
- Use same logging patterns  
- Use same graph_get() helper pattern
- Store instagram tokens in .env or session for now
- Add instagram_page_token and instagram_account_id to .env
- Webhook subscription fields: messages, messaging_postbacks, messaging_optins
- Add automation disclosure to every outgoing message:
  "This is an automated response from Nanovate AI customer support. Type 'human' at any time to speak with a person."

ADD TO .env:
INSTAGRAM_REDIRECT_URI=https://messenger-integration.nanovate.io/instagram/auth/callback
INSTAGRAM_PAGE_TOKEN=
INSTAGRAM_ACCOUNT_ID=

DO NOT:
- Change any existing routes
- Change any existing templates
- Break any existing functionality
- Add any database — use in-memory storage for messages for now
- Add any dependencies not already in requirements.txt except requests (already there)
- Hallucinate API endpoints — use only these verified Graph API endpoints:
  - GET /v22.0/oauth/access_token
  - GET /v22.0/me/accounts
  - GET /v22.0/{page-id}?fields=instagram_business_account
  - POST /v22.0/{page-id}/subscribed_apps
  - POST /v22.0/me/messages
  - GET /v22.0/me/messages (for reading)

VERIFIED GRAPH API FACTS:
- Instagram Messaging uses the same /me/messages endpoint as Messenger
- Instagram webhook events come under entry.messaging same as Messenger
- Instagram tokens are obtained through the Facebook Page, not directly
- Webhook subscription for Instagram uses same /{page-id}/subscribed_apps endpoint
- Instagram account ID is obtained via /{page-id}?fields=instagram_business_account

Please provide:
1. Complete updated app.py with all new routes added
2. templates/instagram_index.html
3. templates/instagram_success.html  
4. templates/instagram_dashboard.html
5. Updated .env.example with new variables
6. Any changes needed to existing requirements.txt

Start by showing me the new routes to add to app.py first, then the templates.