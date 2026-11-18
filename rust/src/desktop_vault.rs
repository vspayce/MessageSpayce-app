//! MessageSpayce: per-install encryption key for the desktop keystore.
//!
//! Upstream OpenBubbles encrypts `keystore.plist` (your iMessage private keys) on desktop with a
//! key hard-coded in the source, so anyone with the file could decrypt it. Here every install gets
//! its own random 256-bit key, kept in the operating system's credential store:
//!   - Windows: Credential Manager (DPAPI-protected)
//!   - macOS:   the login Keychain
//!   - Linux:   the Secret Service (GNOME Keyring / KDE Wallet)
//! If no credential store is available, the key goes in `vault.key` next to the data (0600 on
//! Unix) — still unique per install, so no worse than before.
//!
//! Existing installs are migrated in place: each entry is decrypted with the old hard-coded key
//! and re-encrypted with the new one. Migration is idempotent and safe to interrupt.

use std::path::Path;

use ::keystore::software::{SoftwareEncryptor, SoftwareKeystoreEncryptor};
use log::{error, info, warn};
use sha2::{Digest, Sha256};

const LEGACY_KEY: [u8; 32] = *b"desktopisinsecureyoushouldn'tber";
const SERVICE: &str = "MessageSpayce";
/// Records where the key lives ("keyring" or "file"), so a temporarily locked keyring never
/// causes a new key to be generated (which would make the existing keystore unreadable).
const MARKER: &str = "vault.json";
const KEY_FILE: &str = "vault.key";

/// One keyring entry per data folder, so separate profiles don't collide.
fn entry_name(dir: &Path) -> String {
    let h = Sha256::digest(dir.to_string_lossy().as_bytes());
    let hex: String = h[..8].iter().map(|b| format!("{b:02x}")).collect();
    format!("keystore-key-{hex}")
}

fn read_keyring(dir: &Path) -> Result<Option<[u8; 32]>, String> {
    let entry = keyring::Entry::new(SERVICE, &entry_name(dir)).map_err(|e| e.to_string())?;
    match entry.get_secret() {
        Ok(v) => <[u8; 32]>::try_from(v.as_slice()).map(Some).map_err(|_| "stored key has the wrong length".to_string()),
        Err(keyring::Error::NoEntry) => Ok(None),
        Err(e) => Err(e.to_string()),
    }
}

fn store_in_keyring(dir: &Path, key: &[u8; 32]) -> Result<(), String> {
    let entry = keyring::Entry::new(SERVICE, &entry_name(dir)).map_err(|e| e.to_string())?;
    entry.set_secret(key).map_err(|e| e.to_string())?;
    // Read it back: some environments accept writes they can't return later.
    match entry.get_secret() {
        Ok(v) if v.as_slice() == key.as_slice() => Ok(()),
        Ok(_) => Err("keyring returned a different value".into()),
        Err(e) => Err(e.to_string()),
    }
}

fn read_key_file(dir: &Path) -> Result<[u8; 32], String> {
    let bytes = std::fs::read(dir.join(KEY_FILE)).map_err(|e| e.to_string())?;
    <[u8; 32]>::try_from(bytes.as_slice()).map_err(|_| "vault.key has the wrong length".to_string())
}

fn write_key_file(dir: &Path, key: &[u8; 32]) -> std::io::Result<()> {
    let path = dir.join(KEY_FILE);
    std::fs::write(&path, key)?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o600))?;
    }
    Ok(())
}

fn create_key(dir: &Path) -> [u8; 32] {
    let key: [u8; 32] = rand::random();
    match store_in_keyring(dir, &key) {
        Ok(()) => {
            if let Err(e) = std::fs::write(dir.join(MARKER), "keyring") {
                error!("vault: couldn't write {MARKER}: {e}");
            }
            info!("vault: new keystore key stored in the system keyring");
        }
        Err(e) => {
            warn!("vault: system keyring unavailable ({e}); storing key in {KEY_FILE}");
            if let Err(e) = write_key_file(dir, &key) {
                panic!("MessageSpayce: couldn't save the encryption key ({e})");
            }
            if let Err(e) = std::fs::write(dir.join(MARKER), "file") {
                error!("vault: couldn't write {MARKER}: {e}");
            }
        }
    }
    key
}

/// Returns this install's keystore key, creating and migrating on first run.
pub fn keystore_key(dir: &Path) -> [u8; 32] {
    let marker = std::fs::read_to_string(dir.join(MARKER)).ok();
    let key = match marker.as_deref().map(str::trim) {
        Some("keyring") => match read_keyring(dir) {
            Ok(Some(k)) => k,
            Ok(None) => panic!("MessageSpayce: the encryption key is missing from the system keyring. \
                                Your iMessage keys can't be unlocked without it."),
            Err(e) => panic!("MessageSpayce: couldn't read the encryption key from the system keyring ({e}). \
                              Unlock your keyring and restart the app."),
        },
        Some("file") => read_key_file(dir).unwrap_or_else(|e| panic!("MessageSpayce: couldn't read {KEY_FILE} ({e})")),
        _ => create_key(dir),
    };
    migrate_keystore(dir, &key);
    key
}

/// Re-encrypt any entries still sealed with the old hard-coded key. Idempotent.
fn migrate_keystore(dir: &Path, key: &[u8; 32]) {
    let path = dir.join("keystore.plist");
    let Ok(mut root) = plist::Value::from_file(&path) else { return };
    let new = SoftwareEncryptor(*key);
    let legacy = SoftwareEncryptor(LEGACY_KEY);
    let (mut migrated, mut failed) = (0usize, 0usize);

    if let Some(dict) = root.as_dictionary_mut() {
        for section in ["keys", "secrets"] {
            let Some(map) = dict.get_mut(section).and_then(|v| v.as_dictionary_mut()) else { continue };
            for value in map.values_mut() {
                let result = {
                    let Some(data) = value.as_data() else { continue };
                    if new.decrypt(data).is_ok() { continue } // already on the new key
                    legacy.decrypt(data).and_then(|plain| new.encrypt(&plain))
                };
                match result {
                    Ok(enc) => { *value = plist::Value::Data(enc); migrated += 1 }
                    Err(_) => failed += 1,
                }
            }
        }
    }

    if migrated > 0 {
        let tmp = dir.join("keystore.plist.migrating");
        match root.to_file_xml(&tmp).map_err(|e| e.to_string())
            .and_then(|_| std::fs::rename(&tmp, &path).map_err(|e| e.to_string())) {
            Ok(()) => info!("vault: moved {migrated} keystore entries off the old built-in key"),
            Err(e) => error!("vault: couldn't save migrated keystore ({e}); will retry next launch"),
        }
    }
    if failed > 0 {
        warn!("vault: {failed} keystore entries couldn't be decrypted with either key");
    }
}
