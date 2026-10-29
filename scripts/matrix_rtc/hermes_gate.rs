//! Hermes transport release gates. Compiled as a child of the pinned upstream
//! e2e_call harness; all accounts and rooms are disposable lab resources.

use super::*;

fn format_from_env() -> MembershipFormat {
    match env::var("HERMES_RTC_TEST_FORMAT").as_deref() {
        Ok("state") => MembershipFormat::RoomState,
        Ok("sticky") => MembershipFormat::Sticky2025,
        _ => MembershipFormat::Current,
    }
}

async fn open_test_slot(
    cfg: &Config,
    client: &Client,
    room_id: &RoomId,
    format: MembershipFormat,
) -> Result<(), Box<dyn Error>> {
    if format != MembershipFormat::RoomState {
        open_slot(
            client, room_id.as_str(), &cfg.slot_id, "m.call",
            Some(SlotEncryption {
                encryption_type: "m.per_member".to_owned(),
                extra: Default::default(),
            }),
        ).await?;
    }
    Ok(())
}

#[test]
#[ignore = "requires the isolated Hermes MatrixRTC lab"]
fn encrypted_duplex() {
    run_to_completion(OVERALL_DEADLINE, |cfg| async move {
        let format = format_from_env();
        let (a, b) = credentials(&cfg).await?;
        let alice = login_and_sync(&cfg, &a).await?;
        let bob = login_and_sync(&cfg, &b).await?;
        let bob_id = bob.client.user_id().ok_or("missing bob identity")?.to_owned();
        let room_id = create_encrypted_room(&alice.client, &[&bob_id]).await?;
        let ar = wait_for_room(&alice.client, &room_id).await?;
        let br = wait_for_room(&bob.client, &room_id).await?;
        br.join().await?;
        wait_for_joined_members(&ar, 2, "alice").await?;
        wait_for_joined_members(&br, 2, "bob").await?;
        open_test_slot(&cfg, &alice.client, &room_id, format).await?;
        let alice = join_call(&cfg, alice, ar, &a.user, &cfg.livekit_service_url, format).await?;
        let bob = join_call(&cfg, bob, br, &b.user, &cfg.livekit_service_url, format).await?;
        let roster = wait_for_members(&alice.call, 2, "alice").await
            && wait_for_members(&bob.call, 2, "bob").await;
        let _tone_a = media::publish_tone(&alice.call.session(), 440.0).await?;
        let _tone_b = media::publish_tone(&bob.call.session(), 660.0).await?;
        let forward = record_peer_tone(&bob.call, "bob", 440.0).await?;
        let reverse = record_peer_tone(&alice.call, "alice", 660.0).await?;
        let identity = alice.call.participants().iter()
            .any(|p| !p.is_local && p.user_id == bob_id.as_str());
        let leave_a = alice.call.leave().await;
        let leave_b = bob.call.leave().await;
        leave_a?;
        leave_b?;
        println!("HERMES_GATE duplex roster={roster} authenticated_peer={identity} forward={forward} reverse={reverse}");
        if !(roster && identity && forward && reverse) {
            return Err("encrypted duplex media gate failed".into());
        }
        Ok(())
    });
}

struct ChildGuard(std::process::Child);

impl Drop for ChildGuard {
    fn drop(&mut self) {
        let _ = self.0.kill();
        let _ = self.0.wait();
    }
}

#[test]
#[ignore = "requires the isolated Hermes MatrixRTC lab"]
fn crash_cleanup() {
    run_to_completion(OVERALL_DEADLINE, |cfg| async move {
        let format = format_from_env();
        let (a, b) = credentials(&cfg).await?;
        let alice = login_and_sync(&cfg, &a).await?;
        let server_name = alice.client.user_id().ok_or("missing alice identity")?
            .server_name().to_string();
        let bob_id = UserId::parse(format!("@{}:{server_name}", b.user))?;
        let room_id = create_encrypted_room(&alice.client, &[&bob_id]).await?;
        let ar = wait_for_room(&alice.client, &room_id).await?;
        open_test_slot(&cfg, &alice.client, &room_id, format).await?;
        let mut child = ChildGuard(std::process::Command::new(env::current_exe()?)
            .args(["--ignored", "--exact", "hermes_gate::crash_peer", "--nocapture"])
            .env("HERMES_RTC_TEST_USER", &b.user)
            .env("HERMES_RTC_TEST_PASSWORD", &b.password)
            .env("HERMES_RTC_TEST_ROOM", room_id.as_str())
            .stdout(std::process::Stdio::null())
            .spawn()?);
        wait_for_joined_members(&ar, 2, "alice").await?;
        let alice = join_call(&cfg, alice, ar, &a.user, &cfg.livekit_service_url, format).await?;
        if !wait_for_members(&alice.call, 2, "observer").await {
            return Err("crash peer never became visible".into());
        }
        if !record_peer_tone(&alice.call, "observer-before-crash", 660.0).await? {
            return Err("crash peer's encrypted audio was not decoded".into());
        }
        child.0.kill()?;
        child.0.wait()?;
        // Default SDK delayed leave is 30 s. Allow 15 s for server/sync delivery.
        let deadline = tokio::time::Instant::now() + Duration::from_secs(45);
        let cleared = loop {
            if !alice.call.participants().iter().any(|p| p.user_id == bob_id.as_str()) {
                break true;
            }
            if tokio::time::Instant::now() >= deadline { break false; }
            tokio::time::sleep(Duration::from_millis(250)).await;
        };
        alice.call.leave().await?;
        println!("HERMES_GATE crash peer_removed_within_45s={cleared}");
        if !cleared {
            return Err("crashed native peer remains in the call roster after 45s".into());
        }
        Ok(())
    });
}

#[test]
#[ignore = "child process of crash_cleanup; never run directly"]
fn crash_peer() {
    // No-op when the entire ignored test binary is run by an upstream workflow.
    let Ok(user) = env::var("HERMES_RTC_TEST_USER") else { return; };
    run_to_completion(OVERALL_DEADLINE, |cfg| async move {
        let password = env::var("HERMES_RTC_TEST_PASSWORD")?;
        let room_id = RoomId::parse(env::var("HERMES_RTC_TEST_ROOM")?)?;
        let synced = login_and_sync(&cfg, &Credentials { user: user.clone(), password }).await?;
        let room = wait_for_room(&synced.client, &room_id).await?;
        room.join().await?;
        wait_for_joined_members(&room, 2, "crash-peer").await?;
        let peer = join_call(&cfg, synced, room, &user, &cfg.livekit_service_url, format_from_env()).await?;
        let _tone = media::publish_tone(&peer.call.session(), 660.0).await?;
        std::future::pending::<()>().await;
        Ok(())
    });
}
