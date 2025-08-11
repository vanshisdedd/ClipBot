import os
import json
import time
import pytz
import requests
import datetime
import threading
from flask import Flask, request, jsonify

app = Flask(__name__)

# Cached values for the live stream information
cache = {
    "video_id": None,
    "start_time": None,
    "last_checked": 0
}

# --- Configuration ---
# Load sensitive information and settings from environment variables
YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY")
CHANNEL_ID = os.getenv("YOUTUBE_CHANNEL_ID")
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK")
RENDER_URL = os.getenv("RENDER_URL")

# --- Improved Caching Strategy ---
# Cache a successful find for 5 minutes to reduce API calls.
CACHE_DURATION = 300
# Cache a "not found" result for only 1 minute.
# This allows for quick recovery from temporary API errors.
NEGATIVE_CACHE_DURATION = 60

def self_ping():
    """
    Pings the deployed application to prevent it from sleeping on free hosting services.
    """
    ping_interval = 150 # 2.5 minutes
    while True:
        time.sleep(ping_interval)
        if not RENDER_URL:
            print("[ℹ️] RENDER_URL not set. Self-pinging is disabled.")
            continue # Skip this cycle if the URL isn't set

        ping_endpoint = f"{RENDER_URL}/ping"
        try:
            print(f"[PING] Pinging self at {ping_endpoint} to stay awake.")
            requests.get(ping_endpoint, timeout=10)
            print("[PING] Self-ping successful.")
        except requests.exceptions.RequestException as e:
            print(f"[❌] Self-ping failed: {e}")

def get_live_info():
    """
    Checks if a YouTube channel is live using a robust caching strategy.

    - Caches a positive result (stream found) for CACHE_DURATION.
    - Caches a negative result (stream not found) for NEGATIVE_CACHE_DURATION.
    
    This prevents temporary API errors from causing a long outage in the clipper.
    """
    now = time.time()
    print("[LOG] ---- get_live_info called ----")

    # --- Step 1: Check the cache before making an API call ---
    
    # If we have a cached video_id, it was a positive result. Use the longer cache duration.
    if cache.get("video_id"):
        if now - cache["last_checked"] < CACHE_DURATION:
            print(f"[ℹ️] Using cached video ID: {cache['video_id']} (age: {int(now - cache['last_checked'])}s)")
            return cache["video_id"], cache["start_time"]
    # If the cache has no video_id, the last result was negative. Use the shorter cache duration.
    else:
        if now - cache["last_checked"] < NEGATIVE_CACHE_DURATION:
            print(f"[ℹ️] Using cached 'not found' result. (age: {int(now - cache['last_checked'])}s)")
            return None, None

    # --- Step 2: If cache is expired or empty, query the YouTube API ---
    print(f"[🔍] Cache invalid. Checking for live stream on channel: {CHANNEL_ID}")

    if not YOUTUBE_API_KEY or not CHANNEL_ID:
        print("[❌] Missing YOUTUBE_API_KEY or YOUTUBE_CHANNEL_ID environment variable.")
        return None, None

    search_url = (
        f"https://www.googleapis.com/youtube/v3/search?part=snippet&channelId={CHANNEL_ID}"
        f"&eventType=live&type=video&key={YOUTUBE_API_KEY}"
    )

    try:
        response = requests.get(search_url, timeout=15)
        response.raise_for_status()
        data = response.json()
        print("[📡] Youtube API Response:", json.dumps(data, indent=2))
    except requests.exceptions.RequestException as e:
        print(f"[❌] Error fetching from Youtube API: {e}")
        # Do not update cache on network error, just try again next time.
        return None, None

    # --- Step 3: Process the API response and update the cache ---
    
    # Update the 'last_checked' timestamp regardless of the outcome.
    # This is the key to the caching logic.
    cache["last_checked"] = now

    if data.get("items"):
        try:
            video_id = data["items"][0]["id"]["videoId"]
            print(f"[LOG] Extracted video_id: {video_id}")
            
            # Get stream start time from the Videos endpoint
            video_url = (
                f"https://www.googleapis.com/youtube/v3/videos?part=liveStreamingDetails&id={video_id}"
                f"&key={YOUTUBE_API_KEY}"
            )
            details_response = requests.get(video_url, timeout=15)
            details_response.raise_for_status()
            details = details_response.json()
            print("[🕒] Live Streaming Details Response:", json.dumps(details, indent=2))

            start_time_str = details["items"][0]["liveStreamingDetails"]["actualStartTime"]
            start_dt = datetime.datetime.fromisoformat(start_time_str.replace("Z", "+00:00"))

            # --- Cache the successful result ---
            print(f"[✅] Live video found: {video_id}. Caching result for {CACHE_DURATION} seconds.")
            cache["video_id"] = video_id
            cache["start_time"] = start_dt
            return video_id, start_dt

        except (requests.exceptions.RequestException, KeyError, IndexError, TypeError) as e:
            print(f"[⚠️] Found a stream but could not parse details: {e}")
            # Cache as a negative result since we can't get all info
            cache["video_id"] = None
            cache["start_time"] = None
            return None, None
    else:
        # --- Cache the negative result ---
        print(f"[❌] No active live stream found. Caching 'not found' result for {NEGATIVE_CACHE_DURATION} seconds.")
        cache["video_id"] = None
        cache["start_time"] = None
        return None, None


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
    return "✅ Clipper is running."

@app.route("/ping")
def ping():
    """A simple endpoint for uptime monitoring."""
    print("[PING] Self-ping received.")
    return "pong", 200

@app.route("/clip")
def clip():
    """The main endpoint to create a clip."""
    user = request.args.get("user", "someone")
    message = request.args.get("message", "").strip()
    title = message if message else "Clip"

    video_id, stream_start = get_live_info() # Changed to use the new function name
    if not video_id:
        return "[❌] No active live stream found.", 404
    if not stream_start:
        return "[❌] Couldn’t retrieve stream start time. Cannot create a timestamped clip.", 500

    now_utc = datetime.datetime.now(pytz.utc)
    delay = 35  # seconds to account for stream latency
    clip_time = now_utc - datetime.timedelta(seconds=delay)
    seconds_since_start = max(0, int((clip_time - stream_start).total_seconds()))
    timestamp_str = str(datetime.timedelta(seconds=seconds_since_start))
    clip_url = f"https://www.youtube.com/watch?v={video_id}&t={seconds_since_start}s"

    save_clip(title, user, timestamp_str, clip_url)
    send_to_discord(title, user, timestamp_str, clip_url)

    return "Clip Saved and sent to discord."

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
    return "[🗑️] Cleared all clips."


if __name__ == "__main__":
    ping_thread = threading.Thread(target=self_ping)
    ping_thread.daemon = True
    ping_thread.start()
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
