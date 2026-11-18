# Sculpin Server

A private Matrix server for your household, with bridges to other chat networks.
It starts with **iMessage**; WhatsApp, Signal and LINE can be added later.

- Runs in Docker: Synapse (Matrix) + Postgres + the RustPush iMessage bridge
  ([jasonlaguidice/imessage](https://github.com/jasonlaguidice/imessage), early development).
- Reachable **only through your Tailscale network**: no public ports, no federation.
- Messages between the bridge and your apps are end-to-end encrypted, so the server's
  database only stores ciphertext.

## Install (on an x86_64 Ubuntu machine)

```bash
curl -fsSLO https://raw.githubusercontent.com/vspayce/MessageSpayce-app/sculpin/server/setup.sh
bash setup.sh
```

It installs Docker and Tailscale if needed, signs the machine into your tailnet as `matrix`,
sets everything up in `~/sculpin-server`, and asks you for a Matrix password.
It's safe to run again; finished steps are skipped.

Then sign in from any device on your tailnet with a Matrix app (Element, or Sculpin's
Matrix mode later): homeserver `https://matrix.<your-tailnet>.ts.net`.

## Connect iMessage

You need three things:

1. **Contact Key Verification turned off** on your Apple ID
   (iPhone: Settings › your name › Contact Key Verification).
2. **A hardware key from an Intel Mac** (once; the Mac isn't needed afterwards).
   On the Intel Mac (High Sierra 10.13 or newer):
   ```bash
   xcode-select --install            # if the developer tools aren't there yet
   curl -fsSL https://github.com/jasonlaguidice/imessage/archive/refs/heads/main.tar.gz | tar xz
   cd imessage-main/tools/extract-key && ./build.sh && ./extract-key
   ```
   Copy the long base64 text it prints. Treat it like a password.
   Use a Mac you'll keep: the key is that Mac's identity.
   (Apple silicon Macs work too, but then a relay must keep running on that Mac.)
3. **Your Apple ID, password and a 2FA code.**

Then on the server:
```bash
sculpin-server imessage-login
```

Don't run the same Mac's identity in two places at once (for example this bridge and the
Sculpin app's built-in iMessage); use one or the other, or a different donor Mac for each.

## Everyday commands

```bash
sculpin-server status            # what's running
sculpin-server logs imessage     # follow the bridge's log
sculpin-server restart
sculpin-server update            # pull new versions and restart
```

## Moving to another machine later

The Matrix server's name is the machine's Tailscale name (`matrix.<tailnet>.ts.net`) and can't
change. To move: copy `~/sculpin-server` to the new machine, remove the old machine from
Tailscale, name the new one `matrix`, and run `setup.sh` there.
