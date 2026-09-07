## Simulators — simemu

All simulator/emulator access goes through simemu. Never call xcrun simctl or adb directly.
Everything runs headless by default — no windows, no dock icons, no interruptions.

### Get a device

```bash
SESSION=$(simemu claim ios | jq -r .session)
SESSION=$(simemu claim android | jq -r .session)

# Options:
#   --version 26          specific OS version
#   --form-factor tablet  phone (default), tablet, watch, tv, vision
#   --real                prefer real device
#   --show                keep window visible (default: hidden)
#   --label "my task"     label for tracking
```

### Core operations

```bash
simemu do $SESSION install ./app.ipa
simemu do $SESSION launch com.example.app
simemu do $SESSION screenshot -o proof.png
simemu do $SESSION maestro flow.yaml
simemu do $SESSION url "myapp://deep/link"
simemu do $SESSION done
```

### UI interaction (prefer a11y-tap over coordinates)

```bash
simemu do $SESSION a11y-tap "Login Button"     # tap by label — works headless via Maestro
simemu do $SESSION tap 250 500                  # coordinate tap
simemu do $SESSION swipe 100 500 100 100        # swipe gesture
simemu do $SESSION long-press 250 500           # long press at coordinates
simemu do $SESSION scroll down                  # scroll direction: up, down, left, right
simemu do $SESSION back                         # go back (edge swipe on iOS, back key on Android)
simemu do $SESSION home                         # home screen
simemu do $SESSION type-submit "hello world"    # type text + press enter
simemu do $SESSION input "some text"            # type text into focused field
simemu do $SESSION key home                     # press a named key
simemu do $SESSION rotate landscape             # rotate device orientation
simemu do $SESSION shake                        # shake gesture (React Native dev menu)
```

### App management

```bash
simemu do $SESSION grant-all com.example.app    # grant ALL permissions
simemu do $SESSION dismiss-alert                # dismiss any system dialog
simemu do $SESSION accept-alert                 # tap Allow/OK on system dialog
simemu do $SESSION deny-alert                   # tap Don't Allow/Cancel
simemu do $SESSION auto-dismiss                 # reset privacy warnings + disable animations
simemu do $SESSION reset-app com.app ./app.ipa  # uninstall + reinstall + relaunch
simemu do $SESSION clear-data com.example.app   # clear app data (Android; iOS: use reset-app)
simemu do $SESSION terminate com.example.app    # force-stop app
simemu do $SESSION uninstall com.example.app    # remove app
simemu do $SESSION is-running com.example.app   # check if app process is running
simemu do $SESSION foreground-app               # get current foreground app
simemu do $SESSION app-info com.example.app     # app version, permissions, data size
simemu do $SESSION app-container com.example.app # get app data container path
```

### Capture & proof

```bash
simemu do $SESSION screenshot -o proof.png
simemu do $SESSION screenshot -o proof.png -f jpeg
simemu do $SESSION deeplink-proof "app://screen" -o proof.png   # open URL + wait + screenshot
simemu do $SESSION wait-for-render 3 -o proof.png               # wait N seconds + screenshot
simemu do $SESSION video-start -o recording.mp4                 # start screen recording
simemu do $SESSION video-stop <pid>                             # stop recording (pid from video-start)
simemu do $SESSION log-crash                                    # most recent crash report
simemu do $SESSION log-crash com.example.app                    # crash report for specific app
```

### Device state

```bash
simemu do $SESSION appearance dark                      # light or dark mode
simemu do $SESSION status-bar --time 9:41 --battery 100 # override status bar for screenshots
simemu do $SESSION status-bar --clear                   # clear status bar overrides
simemu do $SESSION location 37.7749 -122.4194           # set GPS location
simemu do $SESSION network offline                      # Android: offline, slow, normal
simemu do $SESSION keychain-reset                       # clear iOS keychain
simemu do $SESSION icloud-sync                          # trigger iCloud sync (iOS)
simemu do $SESSION clipboard-set "text"                 # set clipboard
simemu do $SESSION clipboard-get                        # read clipboard (iOS)
simemu do $SESSION font-size large                      # Android: small, default, large, xlarge
simemu do $SESSION reduce-motion on                     # Android: disable animations
simemu do $SESSION notifications-clear                  # Android: clear all notifications
```

### Files & media

```bash
simemu do $SESSION url "https://example.com"            # open URL / deep link
simemu do $SESSION add-media photo.jpg                  # add to Photos/Gallery
simemu do $SESSION push local.txt /sdcard/file.txt      # push file (Android)
simemu do $SESSION pull /sdcard/file.txt local.txt      # pull file (Android)
simemu do $SESSION contacts-import contacts.vcf         # import contacts from VCF
```

### Session management

```bash
simemu do $SESSION show                         # make window visible
simemu do $SESSION hide                         # hide window
simemu do $SESSION renew                        # extend session heartbeat
simemu do $SESSION boot                         # wake parked session
simemu do $SESSION reboot                       # restart simulator
simemu do $SESSION env                          # device info (UDID, serial, OS version)
simemu do $SESSION clone                        # clone iOS simulator
simemu do $SESSION build                        # build app (requires keel/execution.yaml)
simemu do $SESSION done                         # release session (alias: release)
```

### Advanced

```bash
simemu do $SESSION maestro flow.yaml            # run Maestro flow (resolves device ID)
simemu do $SESSION a11y-tree                    # dump accessibility tree (Android)
simemu do $SESSION siri "what time is it"       # Siri query (limited support)
```

### List sessions

```bash
simemu sessions                                 # list all active sessions
simemu sessions --json                          # JSON output
```

### If something fails

Error messages include the exact re-claim command. Just run it:

```bash
SESSION=$(simemu claim ios --version 26 --form-factor phone | jq -r .session)
```

### Rules

- **ONE command**: `simemu claim` to get a device, `simemu do` to use it
- **NEVER** call `xcrun simctl`, `adb`, `emulator`, or `simctl` directly
- **NEVER** manage device lifecycle (boot/shutdown) — simemu does this
- Use `a11y-tap` for UI interaction — works headless, no coordinates needed
- Use Maestro for complex multi-step flows
- A claim stays valid as long as its holder is alive, regardless of gaps between
  `simemu do` calls (a long build in between is fine) — it is only reaped if the
  holder process is confirmed dead, or after a very long absolute ceiling. If a
  `do` call ever returns `session_expired`, the error tells you the real reason
  (reaped vs. genuinely idle) and the exact re-claim command — just run it.
- **NEVER** fall back to raw `xcrun simctl`/`adb` against a remembered UDID after
  a claim error. The device may already belong to a different session by then —
  any screenshot or action against it is untrustworthy evidence. Re-claim first.
- Done with a session early? Call `simemu do $SESSION done` (or `release`) —
  don't wait for it to time out.
