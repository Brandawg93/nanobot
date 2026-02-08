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

CODE_ASSIST_ENDPOINT = "https://cloudaicompanion.googleapis.com"

def discover_project(access_token: str) -> str | None:
    """
    Discover the Google Cloud project ID associated with the account.
    Mimics the logic in openclaw's discoverProject.
    """
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "X-Goog-Api-Client": "gl-python/nanobot",
    }
    
    # Try loadCodeAssist first
    load_body = {
        "metadata": {
            "ideType": "IDE_UNSPECIFIED",
            "platform": "PLATFORM_UNSPECIFIED",
            "pluginType": "GEMINI",
        }
    }
    
    try:
        response = httpx.post(
            f"{CODE_ASSIST_ENDPOINT}/v1internal:loadCodeAssist",
            headers=headers,
            json=load_body,
            timeout=10.0
        )
        if response.status_code == 200:
            data = response.json()
            project = data.get("cloudaicompanionProject")
            if isinstance(project, str) and project:
                return project
            if isinstance(project, dict) and project.get("id"):
                return project["id"]
    except Exception:
        pass
        
    return None

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
