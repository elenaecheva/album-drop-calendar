"""
Album Drop Calendar
=================
"Which album from the artists I actually listen to is coming out soon?"

How it works:
 1. Fetches your top artists from Spotify (based on real listening, not follows).
 2. For each artist, checks the iTunes Search API for albums with a future
    release date (Apple Music "Coming Soon").
 3. If nothing is found, falls back to MusicBrainz.
 4. Prints a summary and writes an .ics file you can import into or subscribe
    to from any calendar app.

Setup (one time):
 - pip install -r requirements.txt
 - Create an app at https://developer.spotify.com/dashboard
   -> copy the Client ID and Client Secret
   -> add "http://127.0.0.1:8080/callback" as a Redirect URI
 - Set SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET as environment variables
   (recommended), or replace the placeholders below (never commit real keys).

The first run opens your browser so you can authorize the app
(scope: "user-top-read"). Later runs reuse the token saved in .cache.
"""

import os
import sys
import datetime
import requests

# --------------------------------------------------------------------------
# CONFIGURATION
# --------------------------------------------------------------------------
SPOTIFY_CLIENT_ID = os.environ.get("SPOTIFY_CLIENT_ID", "YOUR_CLIENT_ID_HERE")
SPOTIFY_CLIENT_SECRET = os.environ.get("SPOTIFY_CLIENT_SECRET", "YOUR_CLIENT_SECRET_HERE")
SPOTIFY_REDIRECT_URI = "http://127.0.0.1:8080/callback"

TIME_RANGE = "short_term"   # short_term (~4 weeks) | medium_term (~6 months) | long_term
TOP_N_ARTISTS = 50          # how many top artists to check (50 is Spotify's max)
DAYS_AHEAD = 60             # only look for releases within the next N days

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# Where the .ics file is written. Defaults to the script's folder; set the
# ICS_OUTPUT_DIR environment variable to save it elsewhere (for example a
# cloud-synced folder, so a calendar app can subscribe to it).
OUTPUT_DIR = os.environ.get("ICS_OUTPUT_DIR", SCRIPT_DIR)
ICS_FILENAME = os.path.join(OUTPUT_DIR, "upcoming_releases.ics")


def get_top_artists():
    """Return the names of your top artists on Spotify."""
    try:
        import spotipy
        from spotipy.oauth2 import SpotifyOAuth
    except ImportError:
        sys.exit("spotipy is missing. Install it with: pip install spotipy")

    if SPOTIFY_CLIENT_ID.startswith("YOUR_"):
        sys.exit(
            "Spotify credentials are missing.\n"
            "Set SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET as environment "
            "variables, or fill them in at the top of the script."
        )

    auth_manager = SpotifyOAuth(
        client_id=SPOTIFY_CLIENT_ID,
        client_secret=SPOTIFY_CLIENT_SECRET,
        redirect_uri=SPOTIFY_REDIRECT_URI,
        scope="user-top-read",
        open_browser=True,  # tries to open the browser and listens for the redirect
        cache_path=os.path.join(SCRIPT_DIR, ".cache"),  # always next to the script
    )

    # In case the browser doesn't open by itself, show the link anyway.
    print("If your browser doesn't open automatically, open this link yourself:")
    print(auth_manager.get_authorize_url())
    print("\nWaiting for you to authorize access in the browser...\n")

    sp = spotipy.Spotify(auth_manager=auth_manager)
    results = sp.current_user_top_artists(limit=TOP_N_ARTISTS, time_range=TIME_RANGE)
    return [item["name"] for item in results["items"]]


def check_itunes(artist_name, days_ahead=DAYS_AHEAD):
    """Look for albums by this artist with a future release date on iTunes."""
    url = "https://itunes.apple.com/search"
    params = {
        "term": artist_name,
        "entity": "album",
        "attribute": "artistTerm",
        "limit": 10,
    }
    try:
        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()
        data = r.json()
    except requests.RequestException:
        return []

    today = datetime.date.today()
    limit_date = today + datetime.timedelta(days=days_ahead)
    upcoming = []

    for album in data.get("results", []):
        release_str = album.get("releaseDate")  # ISO format, e.g. 2026-11-06T07:00:00Z
        if not release_str:
            continue
        try:
            release_date = datetime.date.fromisoformat(release_str[:10])
        except ValueError:
            continue
        # Only future releases within the window
        if today < release_date <= limit_date:
            # Simple sanity check: the returned artist name should match
            if artist_name.lower() in album.get("artistName", "").lower():
                upcoming.append((album.get("collectionName", "?"), release_date, "Apple Music/iTunes"))

    return upcoming


def check_musicbrainz(artist_name, days_ahead=DAYS_AHEAD):
    """Fallback: look for album release groups on MusicBrainz."""
    headers = {"User-Agent": "UpcomingReleasesCalendar/0.1 (personal project)"}
    search_url = "https://musicbrainz.org/ws/2/release-group/"
    params = {
        "query": f'artist:"{artist_name}" AND primarytype:Album',
        "fmt": "json",
        "limit": 10,
    }
    try:
        r = requests.get(search_url, params=params, headers=headers, timeout=10)
        r.raise_for_status()
        data = r.json()
    except requests.RequestException:
        return []

    today = datetime.date.today()
    limit_date = today + datetime.timedelta(days=days_ahead)
    upcoming = []

    for rg in data.get("release-groups", []):
        date_str = rg.get("first-release-date")
        if not date_str or len(date_str) < 10:
            continue
        try:
            release_date = datetime.date.fromisoformat(date_str)
        except ValueError:
            continue
        if today < release_date <= limit_date:
            upcoming.append((rg.get("title", "?"), release_date, "MusicBrainz"))

    return upcoming


def ics_escape(text):
    """Escape characters that have a special meaning in iCalendar text fields."""
    return (
        text.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\n", "\\n")
    )


def generate_ics(found, filename=ICS_FILENAME):
    """Write an .ics file with one all-day event per release."""

    def fmt(d):
        return d.strftime("%Y%m%d")

    now_stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//UpcomingReleasesCalendar//EN",
        "CALSCALE:GREGORIAN",
    ]

    for date, artist, album, source in found:
        slug = "".join(c for c in f"{artist}{album}".lower() if c.isalnum())
        uid = f"{fmt(date)}-{slug}@upcoming-releases-calendar"
        end_date = date + datetime.timedelta(days=1)  # DTEND is exclusive for all-day events
        summary = ics_escape(f"\U0001F3B5 {artist} - {album}")
        description = ics_escape(f'Release of "{album}" by {artist} (source: {source})')
        lines += [
            "BEGIN:VEVENT",
            f"UID:{uid}",
            f"DTSTAMP:{now_stamp}",
            f"DTSTART;VALUE=DATE:{fmt(date)}",
            f"DTEND;VALUE=DATE:{fmt(end_date)}",
            f"SUMMARY:{summary}",
            f"DESCRIPTION:{description}",
            "END:VEVENT",
        ]

    lines.append("END:VCALENDAR")

    with open(filename, "w", encoding="utf-8", newline="") as f:
        f.write("\r\n".join(lines) + "\r\n")

    return filename


def main():
    print("Fetching your top artists from Spotify...\n")
    artists = get_top_artists()
    print(f"Checking {len(artists)} artists: {', '.join(artists)}\n")

    found = []

    for artist in artists:
        hits = check_itunes(artist)
        if not hits:
            hits = check_musicbrainz(artist)
        for album, date, source in hits:
            found.append((date, artist, album, source))

    if not found:
        print(f"No announced releases found in the next {DAYS_AHEAD} days.")
    else:
        found.sort(key=lambda x: x[0])
        print("Upcoming releases found:\n")
        for date, artist, album, source in found:
            print(f"  {date.strftime('%Y-%m-%d')} - {artist}: \"{album}\"  (source: {source})")

    ics_path = generate_ics(found)
    print(f"\nCalendar file written to: {ics_path}")


if __name__ == "__main__":
    main()
