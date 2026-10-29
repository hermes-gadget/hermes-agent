use std::fs::{File, OpenOptions};
use std::io::Write;
use std::os::unix::fs::{OpenOptionsExt, PermissionsExt};
use std::path::{Path, PathBuf};

use matrix_sdk::authentication::matrix::MatrixSession;
use matrix_sdk::encryption::EncryptionSettings;
use matrix_sdk::ruma::api::client::uiaa::{AuthData, Password, UserIdentifier};
use matrix_sdk::Client;
use serde::Deserialize;

pub type Error = Box<dyn std::error::Error + Send + Sync>;

#[derive(Debug)]
pub struct OperationFailed(pub &'static str);
impl std::fmt::Display for OperationFailed {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result { write!(f, "{}", self.0) }
}
impl std::error::Error for OperationFailed {}

#[derive(Clone, Deserialize)]
pub struct Config {
    pub homeserver: String,
    pub user_id: String,
    pub allowed_user: String,
    pub store: PathBuf,
    #[serde(default)]
    pub create_identity_if_missing: bool,
    #[serde(default)]
    pub ca_file: Option<PathBuf>,
}

pub fn lock_store(store: &Path) -> Result<File, Error> {
    std::fs::create_dir_all(store)?;
    std::fs::set_permissions(store, std::fs::Permissions::from_mode(0o700))?;
    let lock = OpenOptions::new().read(true).write(true).create(true).truncate(false)
        .mode(0o600).open(store.join("device.lock"))?;
    lock.try_lock().map_err(|_| "another process owns this Matrix call device")?;
    Ok(lock)
}

fn private_write(path: &Path, bytes: &[u8]) -> Result<(), Error> {
    let mut file = OpenOptions::new().write(true).create_new(true).mode(0o600).open(path)?;
    file.write_all(bytes)?;
    file.sync_all()?;
    Ok(())
}

async fn build(cfg: &Config, create: bool) -> Result<Client, Error> {
    let key_file = cfg.store.join("store.key");
    if !key_file.exists() && create {
        let random: [u8; 32] = rand::random();
        let key: String = random.iter().map(|b| format!("{b:02x}")).collect();
        private_write(&key_file, key.as_bytes())?;
    }
    let key = std::fs::read_to_string(key_file)
        .map_err(|_| "call device not enrolled; run Matrix call setup")?;
    let mut builder = Client::builder().homeserver_url(&cfg.homeserver)
        .sqlite_store(cfg.store.join("crypto"), Some(&key))
        .with_encryption_settings(EncryptionSettings {
            // Enrolling another device must never bootstrap/reset an existing
            // account identity. Recover it or verify the new device explicitly.
            auto_enable_cross_signing: false,
            ..Default::default()
        });
    if let Some(path) = &cfg.ca_file {
        builder = builder.add_root_certificates(matrix_sdk::reqwest::Certificate::from_pem_bundle(&std::fs::read(path)?)?);
    }
    Ok(builder.build().await.map_err(|_| OperationFailed("client_build"))?)
}

pub async fn restore(cfg: &Config) -> Result<Client, Error> {
    let client = build(cfg, false).await?;
    let session: MatrixSession = serde_json::from_slice(&std::fs::read(cfg.store.join("session.json"))?)?;
    if session.meta.user_id.as_str() != cfg.user_id {
        return Err("call device belongs to a different Matrix account".into());
    }
    client.restore_session(session).await?;
    client.whoami().await?;
    client.encryption().wait_for_e2ee_initialization_tasks().await;
    if let Some(user) = client.user_id() {
        // Uploading signatures does not update the cached device record. Read
        // the signed device back before declaring enrolment verified.
        client.encryption().request_user_identity(user).await.map_err(|_| OperationFailed("refresh_verification"))?;
    }
    Ok(client)
}

pub async fn verified(client: &Client) -> Result<bool, Error> {
    Ok(client.encryption().get_own_device().await?
        .is_some_and(|device| device.is_cross_signed_by_owner()))
}

pub async fn enrol(cfg: &Config, password: Option<&str>, recovery: Option<&str>) -> Result<Client, Error> {
    let client = if cfg.store.join("session.json").exists() {
        restore(cfg).await?
    } else {
        let client = build(cfg, true).await?;
        let password = password.ok_or("Matrix password is required for first enrolment")?;
        client.matrix_auth().login_username(&cfg.user_id, password)
            .initial_device_display_name("Hermes Matrix Voice").send().await
            .map_err(|_| OperationFailed("matrix_login"))?;
        let session = client.matrix_auth().session().ok_or("login returned no device session")?;
        private_write(&cfg.store.join("session.json"), &serde_json::to_vec(&session)?)?;
        client
    };
    if let Some(key) = recovery.filter(|s| !s.trim().is_empty()) {
        client.encryption().recovery().recover(key.trim()).await.map_err(|_| OperationFailed("recover_identity"))?;
    } else if cfg.create_identity_if_missing {
        let user = client.user_id().ok_or("missing Matrix account")?;
        // Ask the server, not just this new device's empty crypto cache. Only
        // first-ever identity creation is authorised here; never reset one.
        if client.encryption().request_user_identity(user).await.map_err(|error| {
            if let Some(api) = error.as_client_api_error() {
                eprintln!("Matrix identity lookup HTTP {} ({:?})", api.status_code, error.client_api_error_kind());
            } else {
                eprintln!("Matrix identity lookup failed ({:?})", std::mem::discriminant(&error));
            }
            OperationFailed("query_identity")
        })?.is_none() {
            let identifier = matrix_sdk::ruma::UserId::parse(&cfg.user_id)?;
            let auth = password.map(|password| AuthData::Password(Password::new(
                UserIdentifier::from(identifier), password.to_owned())));
            client.encryption().bootstrap_cross_signing_if_needed(auth).await.map_err(|_| OperationFailed("bootstrap_identity"))?;
            let key = client.encryption().recovery().enable().await.map_err(|_| OperationFailed("enable_recovery"))?;
            private_write(&cfg.store.join("recovery.key"), key.as_bytes())?;
        }
    }
    if let Some(user) = client.user_id() {
        client.encryption().request_user_identity(user).await.map_err(|_| OperationFailed("refresh_verification"))?;
    }
    Ok(client)
}
