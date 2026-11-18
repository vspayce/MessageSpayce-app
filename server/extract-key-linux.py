#!/usr/bin/env python3
"""
Build the iMessage bridge's "hardware key" on an Intel Mac that's running Linux.

The bridge's own extract-key tool only runs on macOS. On an Intel Mac running Linux the same
identifiers are available from the firmware (SMBIOS/DMI and EFI variables). The bridge
computes the encrypted variants (Gq3489ugfi etc.) from these plain values itself.

    sudo python3 extract-key-linux.py
    python3 extract-key-linux.py --from-openbubbles    # convert an OpenBubbles "OABS..." code instead

Prints the base64 key to stdout (paste it into `sculpin-server imessage-login`) and a summary
to stderr. Treat the key like a password: it is this Mac's identity.

Only use this on a Mac you own and will keep. Don't use the same Mac's identity in two
places at once (e.g. this bridge and an OpenBubbles/Sculpin app).
"""
import base64
import glob
import json
import os
import re
import subprocess
import sys
import uuid

APPLE_NVRAM_GUID = "4d1ede05-38c7-4a6a-9cc6-4bcca8b38c14"
# A real macOS release this Mac model can run. The bridge needs some version/build pair;
# the Mac isn't running macOS any more, so we report a plausible one.
MACOS_VERSION = os.environ.get("MACOS_VERSION", "14.6")
MACOS_BUILD = os.environ.get("MACOS_BUILD", "23G80")
DARWIN_VERSION = os.environ.get("DARWIN_VERSION", "23.6.0")


def err(*a):
    print(*a, file=sys.stderr)


def read_dmi(name):
    try:
        with open(f"/sys/class/dmi/id/{name}") as f:
            return f.read().strip()
    except OSError:
        return ""


def read_efivar(name):
    """EFI variables have a 4-byte attributes header before the value."""
    path = f"/sys/firmware/efi/efivars/{name}-{APPLE_NVRAM_GUID}"
    try:
        with open(path, "rb") as f:
            data = f.read()
        return data[4:] if len(data) > 4 else b""
    except OSError:
        return b""


def permanent_mac():
    """MAC of the built-in Wi-Fi (en0 on these MacBooks), ignoring randomized addresses."""
    candidates = sorted(glob.glob("/sys/class/net/wl*")) + sorted(glob.glob("/sys/class/net/en*"))
    for dev in candidates:
        iface = os.path.basename(dev)
        try:
            out = subprocess.run(["ethtool", "-P", iface], capture_output=True, text=True).stdout
            m = re.search(r"([0-9a-f]{2}(:[0-9a-f]{2}){5})", out, re.I)
            if m and m.group(1) != "00:00:00:00:00:00":
                return bytes.fromhex(m.group(1).replace(":", "")), iface, "permanent"
        except FileNotFoundError:
            pass
        try:
            with open(f"{dev}/address") as f:
                addr = f.read().strip()
            if re.fullmatch(r"([0-9a-f]{2}:){5}[0-9a-f]{2}", addr, re.I):
                b = bytes.fromhex(addr.replace(":", ""))
                if not (b[0] & 0x02):  # skip locally-administered (randomized) addresses
                    return b, iface, "current"
        except OSError:
            pass
    return b"", "", ""


def varint(b, i):
    r = sh = 0
    while True:
        c = b[i]; i += 1
        r |= (c & 0x7F) << sh; sh += 7
        if c < 0x80:
            return r, i


def proto_fields(b):
    i, out = 0, {}
    while i < len(b):
        key, i = varint(b, i)
        f, t = key >> 3, key & 7
        if t == 0:
            v, i = varint(b, i)
        elif t == 2:
            n, i = varint(b, i)
            v, i = b[i:i + n], i + n
        else:
            raise ValueError(f"unexpected protobuf wire type {t}")
        out[f] = v
    return out


def from_openbubbles(code):
    """OpenBubbles codes are 'OABS' + 1 byte + protobuf of the same MacOSConfig the bridge uses."""
    raw = base64.b64decode(code.strip())
    if raw[:4] != b"OABS":
        sys.exit("That isn't an OpenBubbles hardware code (should start with OABS...).")
    top = proto_fields(raw[5:])
    hw = proto_fields(top[1])
    s = lambda d, k: d.get(k, b"").decode() if isinstance(d.get(k, b""), bytes) else ""
    by = lambda d, k: list(d.get(k, b""))
    inner = {
        "product_name": s(hw, 1), "io_mac_address": by(hw, 2), "platform_serial_number": s(hw, 3),
        "platform_uuid": s(hw, 4), "root_disk_uuid": s(hw, 5), "board_id": s(hw, 6), "os_build_num": s(hw, 7),
        "platform_serial_number_enc": by(hw, 8), "platform_uuid_enc": by(hw, 9), "root_disk_uuid_enc": by(hw, 10),
        "rom": by(hw, 11), "rom_enc": by(hw, 12), "mlb": s(hw, 13), "mlb_enc": by(hw, 14),
    }
    config = {
        "inner": inner, "version": s(top, 2), "protocol_version": top.get(3, 1660), "device_id": s(top, 4),
        "icloud_ua": s(top, 5), "aoskit_version": s(top, 6),
    }
    err(f"\n  ✓ Converted OpenBubbles code for {inner['product_name']}")
    err("  Don't run this identity in the bridge AND an OpenBubbles/BlueBubbles app at the same time.\n")
    print(base64.b64encode(json.dumps(config, separators=(",", ":")).encode()).decode())


def main():
    if "--from-openbubbles" in sys.argv:
        from getpass import getpass
        return from_openbubbles(getpass("Paste the OpenBubbles code (hidden): "))
    if os.geteuid() != 0:
        sys.exit("Run with sudo (the serial number and firmware variables are root-only).")
    vendor = read_dmi("sys_vendor")
    if "Apple" not in vendor:
        sys.exit(f"This doesn't look like a Mac (system vendor: {vendor or 'unknown'}).")

    product = read_dmi("product_name")      # e.g. MacBookPro16,1
    serial = read_dmi("product_serial")
    platform_uuid = read_dmi("product_uuid").upper()
    board_id = read_dmi("board_name")       # Mac-XXXXXXXXXXXXXXXX
    mac, iface, mac_kind = permanent_mac()

    rom = read_efivar("ROM")
    rom_src = "EFI NVRAM"
    if not rom and mac:
        rom, rom_src = mac, f"MAC of {iface} (fallback)"
    mlb = read_efivar("MLB").rstrip(b"\x00").decode("ascii", "ignore")
    mlb_src = "EFI NVRAM"
    if not mlb:
        mlb, mlb_src = read_dmi("board_serial"), "SMBIOS board serial (fallback)"

    # The macOS system volume is gone; use a stable UUID derived from this Mac's serial.
    root_disk_uuid = str(uuid.uuid5(uuid.NAMESPACE_OID, "rootdisk:" + serial)).upper()

    missing = [k for k, v in [("serial", serial), ("platform UUID", platform_uuid), ("board-id", board_id),
                              ("ROM", rom), ("MLB", mlb), ("MAC address", mac)] if not v]
    if missing:
        sys.exit("Couldn't read: " + ", ".join(missing) + ". The key wouldn't work.")
    if len(mac) != 6:
        sys.exit("MAC address isn't 6 bytes.")

    inner = {
        "product_name": product,
        "io_mac_address": list(mac),
        "platform_serial_number": serial,
        "platform_uuid": platform_uuid,
        "root_disk_uuid": root_disk_uuid,
        "board_id": board_id,
        "os_build_num": MACOS_BUILD,
        # Left empty on purpose: the bridge computes these from the plain values.
        "platform_serial_number_enc": [],
        "platform_uuid_enc": [],
        "root_disk_uuid_enc": [],
        "rom": list(rom),
        "rom_enc": [],
        "mlb": mlb,
        "mlb_enc": [],
    }
    config = {
        "inner": inner,
        "version": MACOS_VERSION,
        "protocol_version": 1660,
        "device_id": platform_uuid,
        "icloud_ua": f"com.apple.iCloudHelper/282 CFNetwork/1568.100.1 Darwin/{DARWIN_VERSION}",
        "aoskit_version": "com.apple.AOSKit/282 (com.apple.accountsd/113)",
    }

    err("")
    err("  ✓ Hardware key built from this Mac's firmware")
    err("  ─────────────────────────────────────────────")
    err(f"  Model:    {product}")
    err(f"  Serial:   {serial}")
    err(f"  Board-id: {board_id}")
    err(f"  UUID:     {platform_uuid}")
    err(f"  MLB:      {mlb}   ({mlb_src})")
    err(f"  ROM:      {rom.hex()}   ({rom_src})")
    err(f"  MAC:      {':'.join(f'{b:02x}' for b in mac)}   ({iface}, {mac_kind})")
    err(f"  Reported: macOS {MACOS_VERSION} ({MACOS_BUILD})")
    err("")
    err("  Paste the line below into: sculpin-server imessage-login")
    err("")
    print(base64.b64encode(json.dumps(config, separators=(",", ":")).encode()).decode())


if __name__ == "__main__":
    main()
