# Apple Music source

Browse your Apple Music catalogue, manage favourites and play music through a
local native runtime. You'll need an Apple Music subscription. For now, the
runtime supports Linux x86_64.

## Getting started

The package includes the local engine and adapters. Choose Install in the
settings page to download the 22 pinned FairPlay libraries. Each file is
checked against its recorded size and SHA-256 digest; Apple's libraries are
not included in the package.

Sign in and enter verification codes through a private page in your browser.
The runtime looks after the session, without saving your password or
verification codes. Preferences and account files stay in the plugin's private
storage. Signing out removes the saved account session.

## Music and your library

Browse personal recommendations, recently played items, mixes, playlists,
albums and artists. Favourites and library actions are separate. Account
folders are read-only. Lyrics include timing and translation markup where
provided.

The local audio relay uses opaque tickets, so runtime credentials never reach
the player. Lossless ALAC playback uses complete temporary buffering;
AAC/Widevine-only tracks aren't supported. The decoder measures the delivered
sample rate and bit depth rather than guessing from catalogue data. If the
runtime needs to recover, it keeps the account, revokes stale tickets and
avoids replaying an account or playback action whose outcome is uncertain.

## A few rough edges

Search can run into rate limits, and there may be a wait between searches.
We're aware of both and are working on them.

## Building and checking

Use Python 3.11 or later. Build the pinned runtime in a disposable SDK checkout
with `python3 tools/build-runtime /path/to/sdk`, then package the adapter with
`python3 tools/build-native` and `python3 tools/package-zip`. The SDK revision
is pinned in `src/apple_music_plugin/engine.py`.

Run the adapter tests with `PYTHONPATH=src python3 -m unittest discover -s tests`.
`python3 tools/smoke-runtime` checks an isolated engine without credentials or
audio. `python3 tools/smoke-package /path/to/package` checks sandbox startup,
the explicit component installation step and shutdown. Signed-in checks need
an explicit `PLUGIN_DATA_DIR`; they never select a personal profile for you.

## Licence

The adapter is distributed under GPL-3.0-or-later. The native SDK and wrapper
retain their upstream notices. Apple's native libraries have their own
licences; the SDK's MIT licence does not cover them. See `COPYING` and `NOTICE`.
