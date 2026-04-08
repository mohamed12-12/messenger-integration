import os
import requests
from dotenv import load_dotenv

load_dotenv()

token = os.getenv('INSTAGRAM_PAGE_TOKEN')
url = f"https://graph.facebook.com/v22.0/me?fields=id,name&access_token={token}"

try:
    print(f"Checking token: {token[:10]}...")
    resp = requests.get(url)
    print(f"Status: {resp.status_code}")
    print(f"Response: {resp.text}")
    
    # Try to get accounts/pages
    pages_url = f"https://graph.facebook.com/v22.0/me/accounts?access_token={token}"
    resp_pages = requests.get(pages_url)
    print(f"Pages Response: {resp_pages.text}")

except Exception as e:
    print(f"Error: {e}")
