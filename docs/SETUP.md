# Setting up setlisted

This takes about 15 minutes, most of it in the Spotify developer dashboard. Every step below is
something the code actually does. The steps marked **CI checks this** are run on a fresh Ubuntu
machine every week by [`setup-check`](../.github/workflows/setup-check.yml), so if one of them breaks,
the badge goes red before you find out the hard way.

## What you need

| | |
|---|---|
| OS | Linux or macOS. Windows works under WSL. Nothing here is OS-specific apart from the shell script. |
| Python | 3.10 or newer (CI runs 3.10 and 3.12) |
| Python packages | `requests` (the only dependency) |
| Spotify | Any account for search and building playlists. **Premium** for playback control (play, pause, volume, queue). |
| A Spotify app | Free, from the developer dashboard. New apps start in Development Mode, which allows up to 5 users. |

## 1. Install (CI checks this)

```bash
git clone https://github.com/casareanderson/setlisted && cd setlisted
python3 -m pip install -r requirements.txt
python3 tests/test_sequence.py
```

The last line runs the offline tests. No network, no credentials. It should finish with:

```
ALL OFFLINE TESTS PASS
```

Then check the CLI loads:

```bash
python3 spotify_dj.py --help
```

## 2. Create the Spotify app (not checked by CI)

1. Go to <https://developer.spotify.com/dashboard> and create an app.
2. Tick **Web API**.
3. Set the redirect URI to exactly `http://127.0.0.1:8888/callback`. Spotify no longer accepts
   `localhost` here, and a mismatch is the most common reason the next step fails.
4. Copy the **Client ID** and **Client secret**.

## 3. Configure

Put the two values in `~/.spotify-dj.env` (create it, and keep it private: `chmod 600 ~/.spotify-dj.env`):

```
SPOTIFY_CLIENT_ID=your-client-id
SPOTIFY_CLIENT_SECRET=your-client-secret
```

Every setting, and where the code reads it. Values resolve in this order: environment variable, then the
env file, then the optional secrets backend, then the default.

| name | default | what it does |
|---|---|---|
| `SPOTIFY_CLIENT_ID` | none, required | your app's client id |
| `SPOTIFY_CLIENT_SECRET` | none, required | your app's client secret |
| `SPOTIFY_REFRESH_TOKEN` | none, required after step 4 | printed by `auth-exchange`, used to mint access tokens |
| `SPOTIFY_REDIRECT_URI` | `http://127.0.0.1:8888/callback` | must match the app's redirect URI exactly |
| `SPOTIFY_ENV_FILE` | `~/.spotify-dj.env` | where the credentials file lives |
| `SPOTIFY_TOKEN_CACHE` | `~/.spotify-dj-token.json` | access-token cache (holds the scopes it was minted with) |
| `SPOTIFY_SECRET_PATH` | `/Spotify` | folder used by the optional secrets backend |
| `HERMES_AGENT_DIR` | `/opt/hermes-agent` | where to look for an optional `hermes_secrets.py` backend. You don't need it. |

With nothing set, every command that needs Spotify stops with a plain message and exit code 1
(**CI checks this**):

```
error: SPOTIFY_CLIENT_ID is not set. Put it in the environment or /home/you/.spotify-dj.env (see README).
```

## 4. Authorise once (not checked by CI)

```bash
python3 spotify_dj.py auth-url
```

Open the printed link and approve. The browser then lands on a `127.0.0.1` page that doesn't load.
That's expected. Copy the **whole** URL from the address bar and pass it in:

```bash
python3 spotify_dj.py auth-exchange "http://127.0.0.1:8888/callback?code=..."
```

It prints a refresh token. Add it to `~/.spotify-dj.env`:

```
SPOTIFY_REFRESH_TOKEN=the-printed-token
```

## 5. First run and verify (not checked by CI)

Open Spotify on any device (phone, speaker, desktop), then:

```bash
python3 spotify_dj.py devices
```

Success is a list of your Spotify Connect devices. After that, try one read-only command and one that
builds something:

```bash
python3 spotify_dj.py top --limit 5
python3 spotify_dj.py sequence "Some playlist you own" --dry-run   # prints an order, changes nothing
```

## Running it every week

`weekly-mix.sh` builds two mixes and prints a short summary. It calls `./spotify_dj.py`, so run it from
the repo folder:

```bash
chmod +x spotify_dj.py weekly-mix.sh
crontab -e
# 08:30 every Monday
30 8 * * 1  cd /path/to/setlisted && bash weekly-mix.sh
```

## Common errors

These are the messages the code prints, and what they mean.

| you see | what's wrong |
|---|---|
| `SPOTIFY_CLIENT_ID is not set` | the env file isn't where the code looks. Check `SPOTIFY_ENV_FILE` or the path in the message. |
| `token exchange failed: HTTP 400 ...` | redirect URI mismatch, or the code was already used. Run `auth-url` again and paste the fresh URL straight away. |
| `no refresh_token in response (was the code already used?)` | each code works once. Run `auth-url` again. |
| `refresh failed: HTTP 400 — re-run auth-url/auth-exchange` | the refresh token was revoked (you removed the app's access, or changed the secret). Authorise again. |
| `404 — no active device. Open Spotify somewhere, then retry` | nothing is playing anywhere. Open Spotify on a device first. |
| `no Spotify Connect devices visible` | same cause: no device is online for your account. |
| `403 Forbidden on /...` | read the whole message. Spotify answers **403, not 404**, for endpoints it removed or renamed in February 2026, and for playback on a non-Premium account. |
| `HTTP 429` | you hit the daily quota. Stop and wait. Retrying in a loop makes the wait longer, not shorter. |

## Updating

```bash
cd setlisted
git pull
python3 -m pip install -r requirements.txt
python3 tests/test_sequence.py
```

Your credentials live outside the repo, so a pull never touches them.

## Uninstalling

```bash
rm -rf setlisted ~/.spotify-dj.env ~/.spotify-dj-token.json
```

Then remove any cron line you added, and delete the app in the Spotify dashboard if you don't need it.

## What CI can't prove

The weekly check installs from a fresh clone, runs the offline tests, loads every subcommand's `--help`,
and checks a missing credential gives a clean error. It never talks to Spotify: there are no credentials
in CI, and the API's quota and Premium rules make a live test a bad idea. Steps 2, 4 and 5 are the ones
you'll be doing by hand.
