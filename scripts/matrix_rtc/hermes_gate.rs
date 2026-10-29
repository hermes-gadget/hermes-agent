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
    duplex(false);
}

#[test]
#[ignore = "requires the isolated Hermes MatrixRTC lab"]
fn legacy_duplex_without_slot() {
    assert_ne!(format_from_env(), MembershipFormat::Current);
    duplex(true);
}

fn duplex(skip_slot: bool) {
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
        if !skip_slot { open_test_slot(&cfg, &alice.client, &room_id, format).await?; }
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

/// Exercise the real separately enrolled callee and its framed PCM protocol.
#[test]
#[ignore = "requires the lab and the built Hermes helper"]
fn incoming_helper() {
    use std::io::Write;
    use std::os::fd::AsRawFd;
    use tokio::io::{AsyncReadExt, AsyncWriteExt};
    run_to_completion(OVERALL_DEADLINE, |cfg| async move {
        let format = format_from_env();
        let (a, b) = credentials(&cfg).await?;
        let alice = login_and_sync(&cfg, &a).await?;
        let server = alice.client.user_id().unwrap().server_name().to_string();
        let bid = UserId::parse(format!("@{}:{server}", b.user))?;
        let store = std::env::temp_dir().join(format!("hermes-callee-{}", provision::run_suffix()));
        let native = env::var("HERMES_MATRIX_HELPER").unwrap_or("/src/target/debug/hermes-matrix-call".into());
        let config = serde_json::json!({"homeserver":cfg.homeserver,"user_id":bid.as_str(),
            "allowed_user":alice.client.user_id().unwrap().as_str(),"store":store,"create_identity_if_missing":true});
        let mut enrol = std::process::Command::new(&native).arg("enrol")
            .stdin(std::process::Stdio::piped()).stdout(std::process::Stdio::piped()).spawn()?;
        enrol.stdin.take().unwrap().write_all(serde_json::to_string(&serde_json::json!({"config":config,"password":b.password}))?.as_bytes())?;
        let enrolled = enrol.wait_with_output()?;
        if !enrolled.status.success() { return Err("helper enrol failed".into()); }
        let bob = login_and_sync(&cfg, &b).await?;
        let room_id = create_encrypted_room(&alice.client, &[&bid]).await?;
        let ar = wait_for_room(&alice.client, &room_id).await?;
        let br = wait_for_room(&bob.client, &room_id).await?;
        br.join().await?;
        wait_for_joined_members(&ar,2,"alice").await?;
        bob.sync.stop().await;
        let (parent, child_fd) = std::os::unix::net::UnixStream::pair()?;
        // Test harness runs one test at a time; explicitly pass this one fd.
        unsafe extern "C" { fn fcntl(fd:i32, cmd:i32, value:i32)->i32; }
        if unsafe { fcntl(child_fd.as_raw_fd(),2,0) } < 0 { return Err("fd inheritance failed".into()); }
        let mut child = ChildGuard(std::process::Command::new(&native)
            .args(["run",&child_fd.as_raw_fd().to_string()])
            .stdout(std::process::Stdio::null()).spawn()?);
        drop(child_fd);
        parent.set_nonblocking(true)?;
        let mut ipc = tokio::net::UnixStream::from_std(parent)?;
        async fn send(ipc:&mut tokio::net::UnixStream, value:serde_json::Value)->Result<(),Box<dyn Error>> {
            let json=serde_json::to_vec(&value)?;
            ipc.write_u32((json.len()+1) as u32).await?;
            ipc.write_u8(0).await?; ipc.write_all(&json).await?; Ok(())
        }
        async fn control(ipc:&mut tokio::net::UnixStream, name:&str)->Result<serde_json::Value,Box<dyn Error>> {
            tokio::time::timeout(Duration::from_secs(60),async {
                loop {
                    let size=ipc.read_u32().await? as usize;
                    if size==0 || size>65536 { return Err("bad packet bound".into()); }
                    let mut data=vec![0;size];ipc.read_exact(&mut data).await?;
                    if data[0]==0 {
                        let value:serde_json::Value=serde_json::from_slice(&data[1..])?;
                        println!("[helper] control {}",value["event"]);
                        if value["event"]==name { return Ok(value); }
                        if value["event"]=="ended" { return Err(format!("helper ended: {}",value["reason"]).into()); }
                    }
                }
            }).await?
        }
        send(&mut ipc,serde_json::json!({"protocol":1,"config":config})).await?;
        control(&mut ipc,"ready").await?;
        // A legacy caller does not create a spec-current slot. The callee is
        // PL 0 and must answer without writing one on its behalf.
        if format==MembershipFormat::Current { open_test_slot(&cfg,&alice.client,&room_id,format).await?; }
        let call=LiveKitCall::join(&ar,LiveKitCallOptions {
            format,slot_id:cfg.slot_id.clone(),
            livekit_transport:Some(LiveKitTransport{livekit_service_url:cfg.livekit_service_url.clone()}),
            kind:matrix_rtc_call::CallKind::Direct{ring:Some(matrix_rtc_call::RingConfig{intent:Some("audio".into()),..Default::default()})},
            ..Default::default()
        }).await?;
        println!("[caller] call status {:?}",call.status());
        let ring=control(&mut ipc,"incoming").await?;
        assert!(ring["notification_id"].as_str().unwrap().starts_with('$'));
        let generation=ring["generation"].as_u64().unwrap();
        send(&mut ipc,serde_json::json!({"op":"accept","generation":generation})).await?;
        control(&mut ipc,"joined").await?;
        let _tone=media::publish_tone(&call.session(),440.0).await?;
        let capture=tokio::time::timeout(Duration::from_secs(15),async {
            loop {
                let size=ipc.read_u32().await? as usize;
                if size==0 || size>65536 { return Err("bad audio packet bound".into()); }
                let mut data=vec![0;size];ipc.read_exact(&mut data).await?;
                if data[0]==1 && data.len()>9 {
                    assert_eq!(u64::from_be_bytes(data[1..9].try_into().unwrap()),generation);
                    let energy:f64=data[9..].chunks_exact(2).map(|p|{let n=i16::from_le_bytes([p[0],p[1]]) as f64;n*n}).sum();
                    if energy>1e7 { return Ok::<_,Box<dyn Error>>(()); }
                }
            }
        }).await?;
        capture?;
        // Return a paced 660 Hz PCM stream through the gateway boundary.
        let writer=tokio::spawn(async move {
            for index in 0..250 {
                let mut data=vec![1];data.extend(generation.to_be_bytes());
                for sample in 0..480 {
                    let phase=(index*480+sample) as f64*660.0*std::f64::consts::TAU/24000.0;
                    data.extend(((phase.sin()*8000.0) as i16).to_le_bytes());
                }
                ipc.write_u32(data.len() as u32).await?;ipc.write_all(&data).await?;
                tokio::time::sleep(Duration::from_millis(20)).await;
            }
            send(&mut ipc,serde_json::json!({"op":"quit"})).await.map_err(|_|std::io::Error::other("quit"))?;
            Ok::<_,std::io::Error>(())
        });
        assert!(record_peer_tone(&call,"caller",660.0).await?);
        writer.await??;
        call.leave().await?;
        let exit=child.0.wait()?;
        assert!(exit.success());
        std::fs::remove_dir_all(store)?;
        println!("HERMES_GATE incoming_helper encrypted_pcm_duplex=true child_reaped=true");
        Ok(())
    });
}
