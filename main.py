import os
import json
import time
import pytz
import requests
import datetime
import threading
from flask import Flask, request, jsonify

app = Flask(__name__)

# Enhanced cache with state tracking
cache = {
    "video_id": None,
    "start_time": None,
    "last_checked": 0,
    "last_known_live_time": 0,  # Track when we last saw a live stream
    "consecutive_failures": 0,   # Track API failures
    "stream_status": "unknown"   # Track stream transitions
}

# --- Configuration ---
YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY")
CHANNEL_ID = os.getenv("YOUTUBE_CHANNEL_ID")
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK")
RENDER_URL = os.getenv("RENDER_URL")

# --- Improved Caching Strategy ---
CACHE_DURATION = 300  # 5 minutes for positive results
NEGATIVE_CACHE_DURATION = 30  # Reduced to 30 seconds for faster recovery
LAST_KNOWN_LIVE_TIMEOUT = 180  # 3 minutes grace period for known live streams
MAX_CONSECUTIVE_FAILURES = 3   # Retry threshold

def log_status_change(old_status, new_status, video_id=None):
    """Log stream status transitions for debugging."""
    timestamp = datetime.datetime.now().strftime("%H:%M:%S")
    if old_status != new_status:
        print(f"[🔄] {timestamp} Stream status: {old_status} → {new_status}")
        if video_id:
            print(f"    Video ID: {video_id}")

def safe_youtube_call(url, retries=2, delay=2):
    """Make YouTube API calls with retry logic and error handling."""
    for attempt in range(retries):
        try:
            print(f"[📡] API call attempt {attempt + 1}: {url}")
            resp = requests.get(url, timeout=15)
            
            # Check for quota/rate limiting
            if resp.status_code == 403:
                print("[❌] API quota exceeded or forbidden")
                return None, "quota_exceeded"
            elif resp.status_code == 429:
                print("[❌] Rate limited")
                return None, "rate_limited"
            
            resp.raise_for_status()
            data = resp.json()
            print(f"[✅] API call successful, found {len(data.get('items', []))} items")
            return data, "success"
            
        except requests.exceptions.RequestException as e:
            print(f"[❌] API call failed (attempt {attempt + 1}): {e}")
            if attempt < retries - 1:
                print(f"[⏳] Retrying in {delay} seconds...")
                time.sleep(delay)
    
    return None, "network_error"

def check_video_still_live(video_id):
    """Check if a specific video ID is still live."""
    video_url = (
        f"https://www.googleapis.com/youtube/v3/videos?part=liveStreamingDetails,snippet"
        f"&id={video_id}&key={YOUTUBE_API_KEY}"
    )
    
    data, error = safe_youtube_call(video_url, retries=1)
    if not data or not data.get("items"):
        return False, None
    
    video_data = data["items"][0]
    live_details = video_data.get("liveStreamingDetails", {})
    snippet = video_data.get("snippet", {})
    
    # Check if stream is live or upcoming (YouTube sometimes marks as upcoming briefly)
    broadcast_content = snippet.get("liveBroadcastContent", "")
    is_live = broadcast_content in ["live", "upcoming"]
    
    # If marked as upcoming, check if it has actually started
    if broadcast_content == "upcoming" and live_details.get("actualStartTime"):
        is_live = True
    
    # Check if stream has ended
    if live_details.get("actualEndTime"):
        is_live = False
    
    start_time = None
    if is_live and live_details.get("actualStartTime"):
        try:
            start_time_str = live_details["actualStartTime"]
            start_time = datetime.datetime.fromisoformat(start_time_str.replace("Z", "+00:00"))
        except (ValueError, TypeError) as e:
            print(f"[⚠️] Could not parse start time: {e}")
    
    return is_live, start_time

def search_for_live_streams():
    """Search for new live streams on the channel."""
    search_url = (
        f"https://www.googleapis.com/youtube/v3/search?part=snippet&channelId={CHANNEL_ID}"
        f"&eventType=live&type=video&order=date&maxResults=5&key={YOUTUBE_API_KEY}"
    )
    
    data, error = safe_youtube_call(search_url)
    if not data:
        return None, None, error
    
    if not data.get("items"):
        return None, None, "no_streams_found"
    
    # Try each found stream to see if it's actually live
    for item in data["items"]:
        video_id = item["id"]["videoId"]
        print(f"[🔍] Checking stream candidate: {video_id}")
        
        is_live, start_time = check_video_still_live(video_id)
        if is_live:
            return video_id, start_time, "success"
    
    return None, None, "no_live_streams"

def get_live_info():
    """
    Enhanced live stream detection with multiple fallback strategies.
    """
    now = time.time()
    old_status = cache["stream_status"]
    print(f"\n[LOG] ---- get_live_info called at {datetime.datetime.now().strftime('%H:%M:%S')} ----")

    # --- Strategy 1: Use cached positive result if still valid ---
    if cache.get("video_id") and now - cache["last_checked"] < CACHE_DURATION:
        print(f"[💾] Using cached video ID: {cache['video_id']} (age: {int(now - cache['last_checked'])}s)")
        return cache["video_id"], cache["start_time"]

    # --- Strategy 2: Grace period for recently live streams ---
    if (cache.get("video_id") and 
        cache["last_known_live_time"] > 0 and 
        now - cache["last_known_live_time"] < LAST_KNOWN_LIVE_TIMEOUT):
        
        print(f"[⏰] In grace period, double-checking last known stream: {cache['video_id']}")
        is_live, start_time = check_video_still_live(cache["video_id"])
        if is_live:
            print("[✅] Stream still live during grace period!")
            cache["last_checked"] = now
            cache["last_known_live_time"] = now
            cache["consecutive_failures"] = 0
            return cache["video_id"], start_time

    # --- Strategy 3: Check for negative cache but allow background refresh ---
    if (not cache.get("video_id") and 
        now - cache["last_checked"] < NEGATIVE_CACHE_DURATION and
        cache["consecutive_failures"] < MAX_CONSECUTIVE_FAILURES):
        
        print(f"[⏳] In negative cache period, but scheduling background check")
        # Schedule background check if we haven't hit max failures
        threading.Thread(target=background_stream_check, daemon=True).start()
        return None, None

    # --- Strategy 4: Full API check ---
    print("[🔍] Performing full live stream check...")
    
    if not YOUTUBE_API_KEY or not CHANNEL_ID:
        print("[❌] Missing YOUTUBE_API_KEY or YOUTUBE_CHANNEL_ID environment variable.")
        cache["stream_status"] = "config_error"
        return None, None

    # First, if we have a cached video_id, check if it's still live
    if cache.get("video_id"):
        print(f"[🔄] Checking if cached stream {cache['video_id']} is still live...")
        is_live, start_time = check_video_still_live(cache["video_id"])
        if is_live:
            print("[✅] Cached stream is still live!")
            cache["last_checked"] = now
            cache["last_known_live_time"] = now
            cache["consecutive_failures"] = 0
            cache["stream_status"] = "live"
            log_status_change(old_status, "live", cache["video_id"])
            return cache["video_id"], start_time

    # Search for new live streams
    print("[🔍] Searching for new live streams...")
    video_id, start_time, search_error = search_for_live_streams()
    
    # Update cache based on results
    cache["last_checked"] = now
    
    if video_id and start_time:
        print(f"[🎉] Found live stream: {video_id}")
        cache["video_id"] = video_id
        cache["start_time"] = start_time
        cache["last_known_live_time"] = now
        cache["consecutive_failures"] = 0
        cache["stream_status"] = "live"
        log_status_change(old_status, "live", video_id)
        return video_id, start_time
    else:
        print(f"[❌] No live stream found. Error: {search_error}")
        cache["consecutive_failures"] += 1
        
        # Only clear video_id if we're confident the stream is down
        if search_error not in ["quota_exceeded", "rate_limited", "network_error"]:
            cache["video_id"] = None
            cache["start_time"] = None
            cache["stream_status"] = "offline"
        else:
            cache["stream_status"] = "api_error"
        
        log_status_change(old_status, cache["stream_status"])
        return None, None

def background_stream_check():
    """Background thread to check for streams without affecting main request."""
    print("[🔄] Background stream check started...")
    video_id, start_time, _ = search_for_live_streams()
    if video_id:
        print(f"[🎉] Background check found live stream: {video_id}")
        cache["video_id"] = video_id
        cache["start_time"] = start_time
        cache["last_known_live_time"] = time.time()
        cache["consecutive_failures"] = 0
        cache["stream_status"] = "live"

def self_ping():
    """Pings the deployed application to prevent it from sleeping on free hosting services."""
    ping_interval = 150  # 2.5 minutes
    while True:
        time.sleep(ping_interval)
        if not RENDER_URL:
            print("[ℹ️] RENDER_URL not set. Self-pinging is disabled.")
            continue

        ping_endpoint = f"{RENDER_URL}/ping"
        try:
            print(f"[PING] Pinging self at {ping_endpoint} to stay awake.")
            requests.get(ping_endpoint, timeout=10)
            print("[PING] Self-ping successful.")
        except requests.exceptions.RequestException as e:
            print(f"[❌] Self-ping failed: {e}")

def save_clip(title, user, timestamp, url):
    """Saves a clip's metadata to a local JSON file."""
    new_clip_data = {
        "title": title,
        "user": user,
        "timestamp": timestamp,
        "url": url,
        "time": datetime.datetime.now().isoformat()
    }
    try:
        with open("clips.json", "r") as f:
            clips = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        clips = []
    clips.append(new_clip_data)
    with open("clips.json", "w") as f:
        json.dump(clips, f, indent=2)

def send_to_discord(title, user, timestamp, url):
    """Sends a formatted clip message to a Discord webhook."""
    if not DISCORD_WEBHOOK_URL:
        print("[ℹ️] DISCORD_WEBHOOK_URL not set. Skipping notification.")
        return
    content = f"🎬 **{title}** by `{user}`\n⏱️ Timestamp: `{timestamp}`\n🔗 {url}"
    try:
        response = requests.post(DISCORD_WEBHOOK_URL, json={"content": content})
        response.raise_for_status()
        print("[✅] Successfully sent clip to Discord.")
    except requests.exceptions.RequestException as e:
        print(f"[❌] Failed to send clip to Discord: {e}")

# --- Flask API Routes ---

@app.route("/")
def home():
    """Homepage route to confirm the server is running."""
    status_info = {
        "status": "running",
        "stream_status": cache["stream_status"],
        "cached_video_id": cache.get("video_id"),
        "last_checked": cache["last_checked"],
        "consecutive_failures": cache["consecutive_failures"]
    }
    return jsonify(status_info)

@app.route("/ping")
def ping():
    """A simple endpoint for uptime monitoring."""
    print("[PING] Self-ping received.")
    return "pong", 200

@app.route("/status")
def status():
    """Detailed status endpoint for debugging."""
    now = time.time()
    return jsonify({
        "cache": cache,
        "current_time": now,
        "cache_age_seconds": int(now - cache["last_checked"]) if cache["last_checked"] else None,
        "config": {
            "has_api_key": bool(YOUTUBE_API_KEY),
            "has_channel_id": bool(CHANNEL_ID),
            "has_discord_webhook": bool(DISCORD_WEBHOOK_URL)
        }
    })

@app.route("/force-refresh")
def force_refresh():
    """Force a cache refresh for debugging."""
    cache["last_checked"] = 0  # Force cache expiry
    video_id, start_time = get_live_info()
    return jsonify({
        "refreshed": True,
        "video_id": video_id,
        "start_time": start_time.isoformat() if start_time else None,
        "cache_status": cache
    })

@app.route("/clip")
def clip():
    """The main endpoint to create a clip."""
    user = request.args.get("user", "someone")
    message = request.args.get("message", "").strip()
    title = message if message else "Clip"

    video_id, stream_start = get_live_info()
    if not video_id:
        return "[❌] No active live stream found.", 404
        
    if not stream_start:
        return "[❌] Couldn't retrieve stream start time. Cannot create a timestamped clip.", 500

    now_utc = datetime.datetime.now(pytz.utc)
    delay = 35  # seconds to account for stream latency
    clip_time = now_utc - datetime.timedelta(seconds=delay)
    seconds_since_start = max(0, int((clip_time - stream_start).total_seconds()))
    timestamp_str = str(datetime.timedelta(seconds=seconds_since_start))
    clip_url = f"https://www.youtube.com/watch?v={video_id}&t={seconds_since_start}s"

    save_clip(title, user, timestamp_str, clip_url)
    send_to_discord(title, user, timestamp_str, clip_url)

    return f"🎥 Clip Saved and sent to Discord | {title}"

@app.route("/clips")
def get_clips():
    """Returns a list of all saved clips."""
    try:
        with open("clips.json", "r") as f:
            clips = json.load(f)
        return jsonify(clips)
    except (FileNotFoundError, json.JSONDecodeError):
        return jsonify([])

@app.route("/clear")
def clear_clips():
    """Deletes all saved clips."""
    with open("clips.json", "w") as f:
        json.dump([], f)
    return jsonify({"message": "Cleared all clips"})

if __name__ == "__main__":
    print("[🚀] Starting YouTube Live Stream Clipper...")
    print(f"[📋] Config check:")
    print(f"    - API Key: {'✅' if YOUTUBE_API_KEY else '❌'}")
    print(f"    - Channel ID: {'✅' if CHANNEL_ID else '❌'}")
    print(f"    - Discord Webhook: {'✅' if DISCORD_WEBHOOK_URL else '❌'}")
    print(f"    - Render URL: {'✅' if RENDER_URL else '❌'}")
    
    # Start self-ping thread
    ping_thread = threading.Thread(target=self_ping, daemon=True)
    ping_thread.start()
    
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
