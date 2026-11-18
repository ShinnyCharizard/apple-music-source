# Apple Music (Lossless)

Apple Music web browsing with a separate native lossless playback session.
This version replaces the earlier source under the same plugin ID. Linux
x86_64 and an Apple Music subscription are required. The separate experimental
lossless plugin has a different ID; its sessions are not imported.
Music browsing uses the name Apple Music; the plugin list distinguishes this
package as Apple Music (Lossless). Setup follows the player's language for
English and Spanish, with English used for other languages.

## Connect your account

Open the source's settings and follow its numbered assistant:

1. Prepare playback. Download the pinned playback components when prompted.
   The engine is included; Apple's libraries are downloaded only after a click.
2. Connect your web library. Open Apple's authorization window from the assistant.
   Return after granting access; the assistant checks the account's storefront.
3. Connect lossless playback. Enter the same Apple Account and password in the
   assistant, then enter Apple's verification code there if requested.
4. Review both connections. Disconnecting the web library keeps native playback
   and active audio tickets. Disconnecting playback keeps the web account.

Native engine startup and code submission run in a single bounded background
operation so the assistant can keep showing progress. Cancellation finishes
cleaning up the previous attempt before accepting another. Apple's sign-in
errors and numeric diagnostic codes are shown in the assistant; a verification
field is displayed when the native engine actually requests a code.

The native numbered step display requires a host with the generic setup-wizard
renderer. Older hosts can show the same fields and actions as a regular form.
The public Apple web bootstrap is discovered at connection time; this source
does not depend on a third-party token service. A successful bootstrap check
does not establish that browser authorization or authenticated playback works.

The web credential is saved as `web-account.json` in this source's private
data directory with mode 0600. Native session files remain in that directory's
`session` subdirectory. Passwords and verification codes are never saved or
returned to the settings form. No session is imported from another source.

## Current implementation

Web recommendations preserve ordinary recommendation shelves and their server
order. New releases, catalog search, personal playlists, artists, albums and
lyrics use the web client. The library has separate paged views for songs,
albums, artists and playlists; library membership does not imply a favorite.
Imported songs without a playable catalog counterpart remain visible as
unavailable. Favorites and library actions remain distinct.

Playback uses the existing native FairPlay runtime and opaque local audio
tickets. Audio delivery and format reporting stay on that path; the decoder
measures the delivered resolution. This source does not substitute web playback
for the native lossless stream.

Playlist and folder editing, station/video resource types and full visual
parity are not included in this first implementation. Playlist folders are
read-only. Those capabilities stay disabled until their operations are ready.
Live double sign-in and listening require validation with the listener's account.

## Build and check

The process follows SDK revision `76a4ad5`'s strict folder contract. It waits
for `initialize` and requires absolute `data_dir` and `cache_dir` paths.
Account state remains in `data_dir`; downloaded playback components and
disposable engine runs live in `cache_dir`. Components kept by an earlier
version may need downloading again. The package is found relative to its
executable. No writable Linux mount name or working directory is assumed.
The build bundles the native engine's non-glibc library dependencies in
`runtime/native-libs`, including OpenSSL and curl, with their SHA-256 inventory.
Set `NATIVE_LIBRARY_SYSROOT` to a compatible Linux runtime's `files` directory
to select its library closure. Packaging rejects ELF files needing glibc newer
than 2.39, and `abi.json` records the requirement of every packaged ELF.

Use Python 3.11 or later. Build the pinned native runtime in a disposable SDK
checkout with `python3 tools/build-runtime /path/to/sdk`; its revision is
recorded in `src/apple_music_plugin/engine.py`. Then use a Python environment
with PyInstaller to run `python3 tools/build-native` and
`python3 tools/package-zip`. The package excludes Apple's libraries.

Run `PYTHONPATH=src python3 -m unittest discover -s tests` for fixture tests.
`python3 tools/probe-web-bootstrap` checks only Apple's public bootstrap and
prints status and expiry without printing any token or using an account.
`python3 tools/smoke-runtime` exercises an isolated engine without credentials
or audio. Signed-in probes need an explicit isolated `PLUGIN_DATA_DIR`.

## Credits and licence

The native runtime and its local adaptations are preserved from Apple Music
source. The runtime is based on
[musickit-sdk-linux](https://github.com/silentone12725/musickit-sdk-linux) and
[apple-music-linux](https://github.com/silentone12725/apple-music-linux) by
SilentOne. Their notices remain in `NOTICE` and the runtime's licence files.

The adapter is GPL-3.0-or-later. Apple's libraries retain their own licences
and are not redistributed in the package. See `COPYING` and `NOTICE`.
