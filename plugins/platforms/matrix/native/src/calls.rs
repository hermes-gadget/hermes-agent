use std::collections::VecDeque;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use futures_util::StreamExt;
use matrix_rtc_call::{CallKind, CallStatus};
use matrix_rtc_call_sdk::{LiveKitCall, LiveKitCallOptions};
use matrix_rtc_core::compat::MembershipFormat;
use matrix_rtc_transport::{AudioFrame, AudioSourceConfig, MediaStreamKind, PublishOptions};
use matrix_sdk::ruma::events::StateEventType;
use matrix_sdk::ruma::EventId;
use matrix_sdk::{Room, RoomMemberships};
use serde_json::{Value, json};
use tokio::sync::{mpsc, watch};

use crate::device::{Config, Error};
use crate::ipc::Output;

#[derive(Clone)]
pub struct Incoming {
    pub room: Room,
    pub event_id: String,
    pub generation: u64,
    pub format: MembershipFormat,
    pub slot: String,
    pub expires_at: u64,
}

pub fn now_ms() -> u64 {
    SystemTime::now().duration_since(UNIX_EPOCH).unwrap_or_default().as_millis() as u64
}

pub async fn private_pair(room: &Room, cfg: &Config) -> Result<bool, Error> {
    if !matches!(room.join_rule(), Some(matrix_sdk::ruma::room::JoinRule::Invite)) { return Ok(false); }
    if room.get_state_event(StateEventType::RoomEncryption, "").await?.is_none() { return Ok(false); }
    let members = room.members(RoomMemberships::JOIN).await?;
    Ok(members.len() == 2
        && members.iter().any(|m| m.user_id().as_str() == cfg.user_id)
        && members.iter().any(|m| m.user_id().as_str() == cfg.allowed_user))
}

pub async fn incoming(room: Room, mut event: Value, cfg: &Config, boot_ms: u64, generation: u64)
    -> Result<Option<Incoming>, Error>
{
    if event["type"] == "m.room.encrypted" {
        if event["sender"].as_str() != Some(cfg.allowed_user.as_str())
            || event["origin_server_ts"].as_u64().unwrap_or(0).saturating_add(2000) < boot_ms { return Ok(None); }
        let id = EventId::parse(event["event_id"].as_str().ok_or("missing encrypted event id")?)?;
        // A ring can precede its Megolm key in sync. Let the SDK decrypt it
        // after the encrypted to-device delivery; never inspect ciphertext.
        for _ in 0..10 {
            let fetched = room.event(&id, None).await?;
            event = fetched.raw().deserialize_as()?;
            if event["type"] != "m.room.encrypted" { break; }
            tokio::time::sleep(Duration::from_millis(500)).await;
        }
    }
    let kind = event["type"].as_str().unwrap_or_default();
    eprintln!("Matrix call candidate decrypted kind: {kind}");
    if !["m.rtc.notification", "org.matrix.msc4075.rtc.notification"].contains(&kind)
        || event["sender"].as_str() != Some(cfg.allowed_user.as_str()) { return Ok(None); }
    let content = &event["content"];
    let app = &content["application"];
    let ring = content["notification_type"].as_str().or_else(|| app["notification_type"].as_str());
    let intent = content["m.call.intent"].as_str().or_else(|| app["m.call.intent"].as_str());
    if ring != Some("ring") || intent.is_some_and(|s| s != "audio") { return Ok(None); }
    let ts = event["origin_server_ts"].as_u64().unwrap_or(0);
    let lifetime = content["lifetime"].as_u64().or_else(|| app["lifetime"].as_u64()).unwrap_or(90000).min(120000);
    let now = now_ms();
    if ts.saturating_add(2000) < boot_ms || ts > now.saturating_add(2000) || ts.saturating_add(lifetime) <= now {
        return Ok(None);
    }
    if !private_pair(&room, cfg).await? { eprintln!("Matrix call candidate rejected by private room policy"); return Ok(None); }
    eprintln!("Matrix call candidate passed private room policy");
    if app["type"].as_str().is_some_and(|kind| kind != "m.call") { return Ok(None); }
    if content["m.relates_to"]["rel_type"] != "m.reference" { return Ok(None); }
    let Some(reference) = content["m.relates_to"]["event_id"].as_str() else { return Ok(None); };
    let referenced = room.event(EventId::parse(reference)?.as_ref(), None).await?;
    let membership: Value = referenced.raw().deserialize_as()?;
    if membership["sender"].as_str() != Some(cfg.allowed_user.as_str()) { return Ok(None); }
    let member_type = membership["type"].as_str().unwrap_or_default();
    eprintln!("Matrix call reference kind: {member_type}");
    let mc = &membership["content"];
    let format = match member_type {
        "org.matrix.msc3401.call.member" => MembershipFormat::RoomState,
        "m.rtc.member" | "org.matrix.msc4143.rtc.member" => {
            if mc["member"]["device_id"].is_string() || mc["rtc_transports"].is_array() {
                MembershipFormat::Sticky2025
            } else { MembershipFormat::Current }
        }
        _ => return Ok(None),
    };
    let Some(event_id) = event["event_id"].as_str() else { return Ok(None); };
    Ok(Some(Incoming { room, event_id: event_id.to_owned(), generation, format,
                       slot: "m.call#room".to_owned(), expires_at:ts.saturating_add(lifetime) }))
}

pub async fn decline(incoming: &Incoming) -> Result<(), Error> {
    incoming.room.send_raw("org.matrix.msc4310.rtc.decline", &json!({
        "m.relates_to": {"rel_type": "m.reference", "event_id": incoming.event_id}
    })).await?;
    Ok(())
}

pub struct Active {
    pub generation: u64,
    pub pcm: mpsc::Sender<Vec<u8>>,
    pub flush: watch::Sender<u64>,
    pub stop: watch::Sender<bool>,
    pub task: tokio::task::JoinHandle<()>,
}

pub fn start(incoming: Incoming, cfg: Config, output: Output) -> Active {
    let generation = incoming.generation;
    let (pcm, rx) = mpsc::channel(100);
    let (flush, flush_rx) = watch::channel(0);
    let (stop, stop_rx) = watch::channel(false);
    let task = tokio::task::spawn_local(async move {
        let reason = run(incoming, cfg, output.clone(), rx, flush_rx, stop_rx).await
            .unwrap_or("call_failed");
        let _ = output.event(json!({"event":"ended", "generation":generation, "reason":reason}));
    });
    Active { generation, pcm, flush, stop, task }
}

async fn run(incoming: Incoming, cfg: Config, output: Output,
             mut input: mpsc::Receiver<Vec<u8>>, mut flush: watch::Receiver<u64>,
             mut stop: watch::Receiver<bool>) -> Result<&'static str, Error>
{
    if !private_pair(&incoming.room, &cfg).await? { return Ok("room_changed"); }
    let call = LiveKitCall::join(&incoming.room, LiveKitCallOptions {
        format: incoming.format, slot_id: incoming.slot,
        kind: CallKind::Direct { ring: None }, ..Default::default()
    }).await?;
    // Once a call exists, every exit passes through leave, including publishing
    // errors. The server-side delayed leave covers process death.
    let result = media_loop(&call, &incoming.room, &cfg, &output, incoming.generation,
                            &mut input, &mut flush, &mut stop).await;
    let leave = tokio::time::timeout(Duration::from_secs(20), call.leave()).await;
    if !matches!(leave, Ok(Ok(()))) { return Ok("leave_failed"); }
    result
}

async fn media_loop(call: &LiveKitCall, room: &Room, cfg: &Config, output: &Output, generation: u64,
                    input: &mut mpsc::Receiver<Vec<u8>>, flush: &mut watch::Receiver<u64>,
                    stop: &mut watch::Receiver<bool>) -> Result<&'static str, Error>
{
    let mut options = PublishOptions::microphone();
    options.audio = Some(AudioSourceConfig { sample_rate: 24000, num_channels: 1 });
    let publication = call.publish(options).await?;
    let mut pending = VecDeque::<i16>::new();
    let mut ticker = tokio::time::interval(Duration::from_millis(20));
    ticker.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
    let mut remote: Option<futures_util::stream::BoxStream<'static, AudioFrame>> = None;
    let mut member_id = String::new();
    let mut ready = false;
    let mut ticks = 0u64;
    let started = tokio::time::Instant::now();
    loop {
        tokio::select! {
            biased;
            changed = stop.changed() => {
                if changed.is_err() || *stop.borrow() { return Ok("hangup"); }
            }
            changed = flush.changed() => {
                if changed.is_err() { return Ok("gateway_disconnected"); }
                pending.clear();
                while input.try_recv().is_ok() {}
            }
            bytes = input.recv() => {
                let Some(bytes) = bytes else { return Ok("gateway_disconnected"); };
                pending.extend(bytes.chunks_exact(2).map(|p| i16::from_le_bytes([p[0],p[1]])));
                while pending.len() > 48000 { pending.pop_front(); }
            }
            _ = ticker.tick() => {
                if *stop.borrow() { return Ok("hangup"); }
                if matches!(call.status(), CallStatus::Ended { .. }) { return Ok("peer_left"); }
                let peers: Vec<_> = call.participants().into_iter().filter(|p| !p.is_local).collect();
                if peers.len() > 1 || peers.iter().any(|p| p.user_id != cfg.allowed_user || p.device_id.is_none()) {
                    return Ok("unexpected_participant");
                }
                if let Some(peer) = peers.first() {
                    if !ready {
                        output.event(json!({"event":"joined", "generation":generation, "room_id":room.room_id().as_str()}))?;
                        ready = true;
                    }
                    if remote.is_none() || peer.member_id != member_id {
                        if let Some(track) = call.remote_track(&peer.member_id, MediaStreamKind::Microphone) {
                            remote = track.audio_frames();
                            member_id = peer.member_id.clone();
                        }
                    }
                } else if ready || started.elapsed() > Duration::from_secs(15) { return Ok("peer_left"); }
                ticks += 1;
                if ticks % 50 == 0 && !private_pair(room, cfg).await? { return Ok("room_changed"); }
                let data = (0..480).map(|_| pending.pop_front().unwrap_or(0)).collect();
                publication.capture_audio(AudioFrame { data, sample_rate:24000, num_channels:1, samples_per_channel:480 }).await?;
            }
            frame = async {
                match remote.as_mut() {
                    Some(stream) => stream.next().await,
                    None => std::future::pending().await,
                }
            } => {
                if let Some(frame) = frame {
                    if frame.sample_rate != 48000 || frame.num_channels != 1 { return Ok("unsupported_audio_format"); }
                    let pcm: Vec<u8> = frame.data.chunks_exact(2).flat_map(|p| {
                        (((p[0] as i32 + p[1] as i32) / 2) as i16).to_le_bytes()
                    }).collect();
                    output.audio(generation, &pcm);
                } else { remote = None; }
            }
        }
    }
}
