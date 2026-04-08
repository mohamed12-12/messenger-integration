import os
import requests
from dotenv import load_dotenv

load_dotenv()

token = os.getenv('INSTAGRAM_PAGE_TOKEN')
url = f"https://graph.instagram.com/me?fields=id,username&access_token={token}"

try:
    print(f"Checking Instagram Basic token: {token[:10]}...")
    resp = requests.get(url)
    print(f"Status: {resp.status_code}")
    print(f"Response: {resp.text}")
except Exception as e:
    print(f"Error: {e}")
