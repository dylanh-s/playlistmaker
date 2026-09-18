# playlistmaker

Build an **Apple Music** playlist from an **NTS** radio show or a **Spotify** playlist — without a paid Apple Developer account and without the Spotify API.

It scrapes the tracklist from the source, then searches the Apple Music catalog and adds each match to a playlist in your library.

## How it works

The Apple Music web player (music.apple.com) talks to a backend at `amp-api.music.apple.com`. This tool calls that same backend directly, using:

- the **developer token** — a public JWT Apple ships to every web visitor, scraped from the web player's JS bundle; and
- your **media-user-token** — your personal session token, read from your Firefox cookies.

Both are cached to `~/.playlistmaker_tokens.json` and refreshed automatically. Adding a song is then a plain API call — no UI automation, so it's fast, exact (never grabs a whole album or a station), and handles any characters (accents, CJK, …).

> ⚠️ **Unofficial.** `amp-api` is Apple's private web backend, not a documented API. There's no spec, no support, and it can change without notice (the token auto-refresh papers over minor drift; larger changes may need a code fix). This is a personal-use tool, not affiliated with or endorsed by Apple. Use it in line with the Apple Music Terms of Service, and only with your own account.

## Requirements

- **macOS** (the user-token is read from Firefox's cookie store; the path is macOS-specific)
- **Firefox**, signed into Apple Music at [music.apple.com](https://music.apple.com) — this is where your session token comes from
- An active **Apple Music subscription**
- **Python 3** and `requests`:

```bash
pip install -r requirements.txt
```

## Usage

```bash
# From a Spotify playlist (keeps the playlist's name)
python3 playlistmaker.py --mode spotify --path "https://open.spotify.com/playlist/<id>"

# From an NTS show
python3 playlistmaker.py --mode nts --path "https://www.nts.live/shows/<...>"

# Just test a catalog search
python3 playlistmaker.py --search "aphex twin windowlicker"

# Force a token refresh (also happens automatically on expiry)
python3 playlistmaker.py --refresh-tokens
```

Run with no flags for interactive prompts.

### Behaviour

- Creates the playlist if it doesn't exist, otherwise adds to the existing one of that name.
- **Skips duplicates** — both within a run and against tracks already in the playlist.
- **Skips, never guesses** — if it can't confidently match a track it reports it at the end for you to add by hand, rather than adding the wrong song.
- Handles Apple's localisation of non-Latin catalogues (e.g. Chinese titles translated to English, artist names romanised).

## Known limitations

- macOS + Firefox only (that's where the token is read from).
- Some tracks won't match: songs not available in your storefront, or source tracklists that list **pinyin/romanised titles** (Apple indexes those by their original characters, so the search can't find them). These are reported, not silently dropped.

## Security

`~/.playlistmaker_tokens.json` holds your Apple Music session token — treat it like a password. It's kept in your home directory (outside this repo) and is only ever sent to Apple's own hosts.

## License

MIT — see [LICENSE](LICENSE).
