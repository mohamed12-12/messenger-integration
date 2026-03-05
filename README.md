# Messenger Integration Project

This project allows you to connect a Facebook Page to an AI agent for messaging.

## How to Run

To run the project locally with public access (required for Facebook integration), follow these steps:

### 1. Start the Flask Application
Open a terminal and run:
```bash
python app.py
```

### 2. Start ngrok
Open a **second terminal** and run:
```bash
ngrok http 5000
```
*Note: Make sure your `REDIRECT_URI` in the `.env` file matches your current ngrok URL followed by `/auth/callback`.*

### 3. Update Meta Dashboard (If ngrok URL changed)
If ngrok gives you a new URL, you must update it in the [Meta for Developers Dashboard](https://developers.facebook.com/apps/676537285287395/fb-login/settings/):
1. Update **Valid OAuth Redirect URIs**.
2. Update **App Domains** in Basic Settings.
3. Update **Site URL** in the Website platform section.

## Environment Variables (.env)
Ensure your `.env` file contains:
- `META_APP_ID`: Your Facebook App ID
- `META_APP_SECRET`: Your Facebook App Secret
- `REDIRECT_URI`: Your public ngrok URL + `/auth/callback`
- `FLASK_SECRET_KEY`: A secure random string
