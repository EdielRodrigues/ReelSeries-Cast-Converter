"""Authenticated, allowlisted IPTV-to-HLS relay for Chromecast."""
import asyncio
import hmac
import ipaddress
import logging
import os
import re
import secrets
import shutil
import socket
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit

import jwt
import requests
from cryptography import x509
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel

app = FastAPI(title="ReelSeries Cast Converter", docs_url=None, redoc_url=None)
ROOT = Path(tempfile.mkdtemp(prefix="reelcast-"))
KEY = os.environ.get("CAST_API_KEY", "")
FIREBASE_PROJECT_ID = os.environ.get("FIREBASE_PROJECT_ID", "").strip()
PUBLIC = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
HOSTS = {s.strip().lower() for s in os.environ.get("XTREAM_ALLOWED_HOSTS", "").split(",") if s.strip()}
ACTIVE = {}
LOCK = asyncio.Lock()
TTL = 60 * 30
FIREBASE_CERTS_URL = "https://www.googleapis.com/robot/v1/metadata/x509/securetoken@system.gserviceaccount.com"
_CERTS = {"keys": {}, "until": 0.0}


class StartRequest(BaseModel):
    url: str


def firebase_public_keys(force=False):
    """Fetch and cache Google's public Firebase token-signing certificates."""
    now = time.monotonic()
    if not force and _CERTS["keys"] and now < _CERTS["until"]:
        return _CERTS["keys"]
    response = requests.get(FIREBASE_CERTS_URL, timeout=8)
    response.raise_for_status()
    keys = {kid: x509.load_pem_x509_certificate(pem.encode("ascii")).public_key()
            for kid, pem in response.json().items()}
    if not keys:
        raise RuntimeError("No Firebase public keys returned")
    match = re.search(r"max-age=(\d+)", response.headers.get("Cache-Control", ""))
    ttl = min(int(match.group(1)), 3600) if match else 900
    _CERTS.update(keys=keys, until=now + max(60, ttl))
    return keys


def require_auth(authorization: str | None):
    """Verify signature, issuer, audience, expiry and user ID without service-account credentials."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Firebase authentication required")
    token = authorization[7:].strip()
    if KEY and hmac.compare_digest(token, KEY):
        return "admin"
    if not FIREBASE_PROJECT_ID:
        raise HTTPException(503, "FIREBASE_PROJECT_ID not configured")
    try:
        header = jwt.get_unverified_header(token)
        if header.get("alg") != "RS256" or not header.get("kid"):
            raise HTTPException(401, "Firebase token invalid")
        key = firebase_public_keys().get(header["kid"])
        if key is None:
            key = firebase_public_keys(force=True).get(header["kid"])
        if key is None:
            raise HTTPException(401, "Firebase signing key not recognized")
        claims = jwt.decode(token, key, algorithms=["RS256"],
                            audience=FIREBASE_PROJECT_ID,
                            issuer=f"https://securetoken.google.com/{FIREBASE_PROJECT_ID}",
                            options={"require": ["exp", "iat", "aud", "iss", "sub", "auth_time"]})
        uid = claims.get("sub")
        if not isinstance(uid, str) or not 1 <= len(uid) <= 128:
            raise HTTPException(401, "Firebase user ID invalid")
        if claims["iat"] > time.time() or claims["auth_time"] > time.time():
            raise HTTPException(401, "Firebase token timestamps invalid")
        return uid
    except HTTPException:
        raise
    except (jwt.PyJWTError, ValueError, KeyError, TypeError):
        raise HTTPException(401, "Firebase token invalid or expired")
    except Exception as exc:
        logging.getLogger("reelcast.auth").warning("Firebase verification failed: %s", type(exc).__name__)
        raise HTTPException(503, "Firebase public-key verification temporarily unavailable")


def validate_source(value: str):
    parsed = urlsplit(value)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
        raise HTTPException(400, "Invalid stream URL")
    if not HOSTS or parsed.hostname.lower() not in HOSTS:
        raise HTTPException(403, "IPTV host not allowlisted")
    try:
        addresses = socket.getaddrinfo(parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80))
    except socket.gaierror:
        raise HTTPException(400, "IPTV host cannot be resolved")
    for address in addresses:
        ip = ipaddress.ip_address(address[4][0])
        if not ip.is_global:
            raise HTTPException(403, "Private/reserved source IP denied")
    return value


async def terminate():
    proc = ACTIVE.pop("process", None)
    if proc and proc.returncode is None:
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), 3)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
    folder = ACTIVE.pop("folder", None)
    if folder:
        shutil.rmtree(folder, ignore_errors=True)
    ACTIVE.clear()


@app.get("/health")
def health():
    return {"ok": True, "configured": bool(FIREBASE_PROJECT_ID and HOSTS and PUBLIC), "active": bool(ACTIVE)}


@app.post("/api/streams")
async def start(payload: StartRequest, authorization: str | None = Header(default=None)):
    require_auth(authorization)
    if not FIREBASE_PROJECT_ID or not HOSTS:
        raise HTTPException(503, "Firebase authentication or IPTV host allowlist not configured")
    source = validate_source(payload.url)
    if not PUBLIC.startswith("https://"):
        raise HTTPException(503, "PUBLIC_BASE_URL must be HTTPS")
    async with LOCK:
        await terminate()
        token = secrets.token_urlsafe(32)
        folder = Path(tempfile.mkdtemp(dir=ROOT, prefix="stream-"))
        playlist = folder / "index.m3u8"
        args = ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-nostdin", "-y", "-i", source,
                "-map", "0:v:0?", "-map", "0:a:0?", "-c", "copy", "-f", "hls",
                "-hls_time", "4", "-hls_list_size", "6", "-hls_flags", "delete_segments+omit_endlist",
                "-hls_segment_filename", str(folder / "segment%05d.ts"), str(playlist)]
        proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.DEVNULL,
                                                    stderr=asyncio.subprocess.DEVNULL)
        ACTIVE.update(process=proc, folder=folder, token=token, started=time.monotonic())
    return {"playlist_url": f"{PUBLIC}/hls/{token}/index.m3u8", "expires_in": TTL,
            "note": "Wait a few seconds for the playlist; HLS remux does not transcode codecs."}


@app.delete("/api/streams")
async def stop(authorization: str | None = Header(default=None)):
    require_auth(authorization)
    async with LOCK:
        await terminate()
    return {"stopped": True}


@app.get("/hls/{token}/{filename}")
async def hls(token: str, filename: str):
    if not ACTIVE or not hmac.compare_digest(token, ACTIVE.get("token", "")):
        raise HTTPException(404)
    if time.monotonic() - ACTIVE["started"] > TTL:
        async with LOCK:
            await terminate()
        raise HTTPException(410)
    if filename != "index.m3u8" and not (filename.startswith("segment") and filename.endswith(".ts") and filename[7:-3].isdigit()):
        raise HTTPException(404)
    path = ACTIVE["folder"] / filename
    if not path.is_file():
        return Response(status_code=404, headers={"Cache-Control": "no-store"})
    return FileResponse(path, media_type="application/vnd.apple.mpegurl" if filename.endswith(".m3u8") else "video/mp2t",
                        headers={"Cache-Control": "no-store", "Access-Control-Allow-Origin": "*"})
