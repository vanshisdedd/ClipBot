import os
import json
import time
import pytz
import requests
import datetime
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
CACHE_DURATION = 300  # Cache live stream info for 5 minutes (300 seconds)

def get_cached_live_info():
    """
    Checks if a YouTube channel is live.
    Caches the result to avoid hitting the API rate limit excessively.
    Returns the video ID and the stream's start time if live.
    """
    now = time.time()
    print("[LOG] ---- get_cached_live_info called ----")
    print(f"[LOG] Monitoring channel ID: {CHANNEL_ID}")
    print(f"[LOG] YOUTUBE_API_KEY present: {'Yes' if YOUTUBE_API_KEY else 'No'}")
    print(f"[LOG] DISCORD_WEBHOOK_URL present: {'Yes' if DISCORD_WEBHOOK_URL else 'No'}")
    print(f"[LOG] Current cache: video_id={cache['video_id']}, start_time={cache['start_time']}, last_checked={cache['last_checked']}")

    # Ensure required environment variables are set
    if not YOUTUBE_API_KEY or not CHANNEL_ID:
        print("[❌] Missing YOUTUBE_API_KEY or YOUTUBE_CHANNEL_ID environment variable.")
        cache["video_id"] = None
        return None, None

    # Use cached data if it's recent enough
    if cache["video_id"] and (now - cache["last_checked"] < CACHE_DURATION):
        print(f"[ℹ️] Using cached video ID: {cache['video_id']} (age: {int(now - cache['last_checked'])}s)")
        return cache["video_id"], cache["start_time"]

    print(f"[🔍] Cache expired or empty. Checking for live stream on channel: {CHANNEL_ID}")
    search_url = (
        f"https://www.googleapis.com/youtube/v3/search?part=snippet&channelId={CHANNEL_ID}"
        f"&eventType=live&type=video&key={YOUTUBE_API_KEY}"
    )
    
    try:
        response = requests.get(search_url)
        response.raise_for_status()  # Raise an exception for bad status codes (4xx or 5xx)
        data = response.json()
        print("[📡] YouTube Search API Response:", json.dumps(data, indent=2))
    except requests.exceptions.RequestException as e:
        print(f"[❌] Error fetching from YouTube Search API: {e}")
        cache["video_id"] = None
        return None, None

    # Process the API response
    if data.get("items"):
        try:
            video_id = data["items"][0]["id"]["videoId"]
            print(f"[LOG] Extracted video_id: {video_id}")
        except (KeyError, IndexError) as e:
            print(f"[❌] Could not extract videoId from API response: {e}")
            cache["video_id"] = None
            return None, None
        
        # Update cache with the new video ID and check time
        cache["video_id"] = video_id
        cache["last_checked"] = now
        print(f"[✅] Live video found: {video_id}. Now fetching stream details.")

        # Get stream start time from the Videos endpoint
        video_url = (
            f"https://www.googleapis.com/youtube/v3/videos?part=liveStreamingDetails&id={video_id}"
            f"&key={YOUTUBE_API_KEY}"
        )
        try:
            details_response = requests.get(video_url)
            details_response.raise_for_status()
            details = details_response.json()
            print("[🕒] Live Streaming Details Response:", json.dumps(details, indent=2))
            
            start_time_str = details["items"][0]["liveStreamingDetails"]["actualStartTime"]
            print(f"[LOG] Extracted actualStartTime: {start_time_str}")
            
            # Parse the ISO 8601 timestamp and make it timezone-aware
            start_dt = datetime.datetime.fromisoformat(start_time_str.replace("Z", "+00:00"))
            cache["start_time"] = start_dt
            return video_id, start_dt
        except (requests.exceptions.RequestException, KeyError, IndexError, TypeError) as e:
            print(f"[⚠️] Could not parse start time: {e}")
            # We found a video but can't get the start time, so we can still clip but timestamp might be off
            return video_id, None
    
    print("[❌] No active live stream found (API response had no items).")
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
        # Read existing clips first to avoid overwriting
        with open("clips.json", "r") as f:
            clips = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        # If file doesn't exist or is invalid, start with an empty list
        clips = []

    clips.append(new_clip_data)

    # Write the updated list back to the file
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

    video_id, stream_start = get_cached_live_info()
    if not video_id:
        return "[❌] No active live stream found.", 404
    if not stream_start:
        return "[❌] Couldn’t retrieve stream start time. Cannot create a timestamped clip.", 500

    # Calculate timestamp relative to the stream start
    # We subtract a delay to account for stream latency
    now_utc = datetime.datetime.now(pytz.utc)
    delay = 35  # seconds
    clip_time = now_utc - datetime.timedelta(seconds=delay)
    
    seconds_since_start = max(0, int((clip_time - stream_start).total_seconds()))
    timestamp_str = str(datetime.timedelta(seconds=seconds_since_start))
    clip_url = f"https://youtu.be/{video_id}?t={seconds_since_start}s"

    save_clip(title, user, timestamp_str, clip_url)
    send_to_discord(title, user, timestamp_str, clip_url)

    return f"[✅] '{title}' clipped by {user} → {clip_url}"

@app.route("/clips")
def get_clips():
    """Returns a list of all saved clips."""
    try:
        with open("clips.json", "r") as f:
            clips = json.load(f)
        return jsonify(clips)
    except (FileNotFoundError, json.JSONDecodeError):
        # Return an empty list if the file doesn't exist or is empty
        return jsonify([])

@app.route("/clear")
def clear_clips():
    """Deletes all saved clips."""
    with open("clips.json", "w") as f:
        json.dump([], f) # Write an empty list to the file
    return "[🗑️] Cleared all clips."


if __name__ == "__main__":
    # Get port from environment variable or default to 10000
    port = int(os.environ.get("PORT", 10000))
    # Run the app, accessible from the network ('0.0.0.0')
    app.run(host="0.0.0.0", port=port)
