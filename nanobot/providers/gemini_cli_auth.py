import os
import re
import shutil
from pathlib import Path
import httpx

# These credentials belong to the Google Gemini CLI (Cloud AI Companion).
# They are for a Desktop/Installed application and are not treated as secrets.
# We extract them dynamically from the local installation to avoid including
# them in the source code directly, meeting security and hygiene requirements.
# See: https://developers.google.com/identity/protocols/oauth2#installed

TOKEN_URL = "https://oauth2.googleapis.com/token"

def extract_credentials() -> tuple[str, str]:
    """
    Attempt to extract Gemini CLI OAuth credentials from the locally installed package.
    Throws RuntimeError if extraction fails or CLI is not found.
    """
    gemini_path = shutil.which("gemini")
    if not gemini_path:
        raise RuntimeError(
            "Gemini CLI not found in PATH. "
            "Please ensure gemini-cli is installed (pnpm install -g @google/gemini-cli)"
        )

    # Resolve symlinks to find the actual installation directory
    resolved_path = Path(gemini_path).resolve()
    
    # Search for oauth2.js in common locations relative to the executable
    search_roots = [
        resolved_path.parents[1], # .../node_modules/@google/gemini-cli
        resolved_path.parents[2], # .../node_modules/@google
    ]
    
    for root in search_roots:
        # We look for the file in the nested node_modules of the core package
        potential_file = root / "node_modules" / "@google" / "gemini-cli-core" / "dist" / "src" / "code_assist" / "oauth2.js"
        if potential_file.exists():
            content = potential_file.read_text(encoding="utf-8")
            
            id_match = re.search(r"(\d+-[a-z0-9]+\.apps\.googleusercontent\.com)", content)
            secret_match = re.search(r"(GOCSPX-[A-Za-z0-9_-]+)", content)
            
            if id_match and secret_match:
                return id_match.group(1), secret_match.group(1)
                
    raise RuntimeError(
        f"Could not extract OAuth credentials from Gemini CLI at {resolved_path}. "
        "The package structure may have changed."
    )

CODE_ASSIST_ENDPOINT = "https://cloudcode-pa.googleapis.com"

def discover_project(access_token: str) -> str | None:
    """
    Discover the Google Cloud project ID associated with the account.
    This is a near-exact port of openclaw's discoverProject logic.
    """
    env_project = os.environ.get("GOOGLE_CLOUD_PROJECT") or os.environ.get("GOOGLE_CLOUD_PROJECT_ID")
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "User-Agent": "google-api-nodejs-client/9.15.1",
        "X-Goog-Api-Client": "gl-node/openclaw",
    }

    try:
        # 1. Load current status
        load_body = {
            "cloudaicompanionProject": env_project,
            "metadata": {
                "ideType": "IDE_UNSPECIFIED",
                "platform": "PLATFORM_UNSPECIFIED",
                "pluginType": "GEMINI",
                "duetProject": env_project,
            }
        }
        r = httpx.post(f"{CODE_ASSIST_ENDPOINT}/v1internal:loadCodeAssist", headers=headers, json=load_body, timeout=10.0)
        r.raise_for_status()
        data = r.json()

        # 2. Check for project in various response fields (cloudaicompanionProject, etc)
        project = data.get("cloudaicompanionProject")
        if project:
            if isinstance(project, str): return project
            if isinstance(project, dict) and project.get("id"): return project["id"]

        # 3. If already provisioned with a tier, but no project explicitly returned, fallback to env
        if data.get("currentTier"):
            if env_project: return env_project
            return None

        # 4. If not provisioned, Onboard the user (matches openclaw's default tier logic)
        allowed = data.get("allowedTiers", [])
        tier = next((t for t in allowed if t.get("isDefault")), None) or (allowed[0] if allowed else {"id": "free-tier"})
        tier_id = tier.get("id", "free-tier")

        onboard_body = {
            "tierId": tier_id,
            "metadata": {
                "ideType": "IDE_UNSPECIFIED",
                "platform": "PLATFORM_UNSPECIFIED",
                "pluginType": "GEMINI",
            }
        }
        if tier_id != "free-tier" and env_project:
            onboard_body["cloudaicompanionProject"] = env_project

        or_ = httpx.post(f"{CODE_ASSIST_ENDPOINT}/v1internal:onboardUser", headers=headers, json=onboard_body, timeout=10.0)
        or_.raise_for_status()
        lro = or_.json()

        # 5. Simple polling for the LRO (Onboarding can take time)
        import time
        for _ in range(12): # Poll for up to 60 seconds
            if lro.get("done"): break
            if not lro.get("name"): break
            time.sleep(5)
            pr = httpx.get(f"{CODE_ASSIST_ENDPOINT}/v1internal/{lro['name']}", headers=headers)
            if pr.status_code == 200:
                lro = pr.json()

        pid = lro.get("response", {}).get("cloudaicompanionProject", {}).get("id")
        return pid or env_project

    except Exception as e:
        print(f"[DEBUG] Discovery error: {e}")
        return env_project

def refresh_access_token(refresh_token: str) -> tuple[str, str | None]:
    """
    Refresh the Google OAuth access token and discover the project ID.
    Returns (access_token, project_id).
    """
    if not refresh_token:
        raise ValueError("Refresh token is required")

    client_id, client_secret = extract_credentials()

    data = {
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }

    response = httpx.post(TOKEN_URL, data=data)
    response.raise_for_status()
    
    tokens = response.json()
    access_token = tokens.get("access_token")
    if not access_token:
        raise ValueError("Failed to obtain access token from response")
        
    project_id = discover_project(access_token)
    return access_token, project_id
