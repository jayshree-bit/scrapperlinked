from playwright.sync_api import sync_playwright
from app import config

# User-visible session check only. No automated profile visits or extraction.
# The login is stored in a persistent profile so you only sign in once.
# Tip: sign in with your LinkedIn email + password (Google sign-in is blocked
# inside automated browsers).


def main():
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            config.BROWSER_PROFILE_DIR, headless=False
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded", timeout=45000)
        print("A visible browser is open. Sign in manually if needed; do not bypass challenges.")
        print("Session is saved in:", config.BROWSER_PROFILE_DIR)
        input("Sign in / inspect the page, then press Enter to check status and close...")
        url = page.url.lower()
        title = page.title().lower()
        print("Current URL:", page.url)
        print("Page title:", page.title())
        if "login" in url or "authwall" in url or "sign in" in title or "log in" in title:
            print("Status: login required or session not authenticated.")
        elif any(w in url + " " + title for w in ("checkpoint", "challenge", "captcha", "restricted")):
            print("Status: challenge/restriction detected. Stop and resolve through official support.")
        else:
            print("Status: no obvious login/challenge redirect detected.")
        context.close()


if __name__ == "__main__":
    main()
