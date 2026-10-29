mod calls;
mod device;
mod ipc;

use std::collections::{HashSet, VecDeque};
use std::io::Read;
use std::os::fd::FromRawFd;
use std::time::Duration;

use matrix_sdk::ruma::events::AnySyncMessageLikeEvent;
use matrix_sdk::ruma::serde::Raw;
use matrix_sdk::Room;
use matrix_sdk_ui::sync_service::SyncService;
use serde_json::{Value, json};
use tokio::sync::mpsc;

use device::{Config, Error};
use ipc::Packet;

fn main() {
    let _ = rustls::crypto::aws_lc_rs::default_provider().install_default();
    let runtime = tokio::runtime::Builder::new_multi_thread().worker_threads(2).enable_all().build().unwrap();
    let local = tokio::task::LocalSet::new();
    let result = runtime.block_on(local.run_until(async {
        match std::env::args().nth(1).as_deref() {
            Some("enrol") => enrol().await,
            Some("run") => run().await,
            _ => Err("expected enrol or run".into()),
        }
    }));
    if let Err(error) = result {
        // SDK HTTP errors can contain auth material. The controller gets a
        // bounded category over IPC; never dump the underlying error chain.
        let stage = error.downcast_ref::<device::OperationFailed>().map(|e|e.0).unwrap_or("transport_or_device_state");
        eprintln!("Matrix call helper failed at {stage}");
        std::process::exit(1);
    }
}

async fn enrol() -> Result<(), Error> {
    let mut bytes = Vec::new();
    std::io::stdin().take(ipc::MAX_PACKET as u64).read_to_end(&mut bytes)?;
    let request: Value = serde_json::from_slice(&bytes)?;
    let cfg: Config = serde_json::from_value(request["config"].clone())?;
    let _lock = device::lock_store(&cfg.store)?;
    let client = device::enrol(&cfg, request["password"].as_str(), request["recovery_key"].as_str()).await?;
    let verified = device::verified(&client).await?;
    println!("{}", json!({"verified":verified, "device_id":client.device_id().map(|d|d.as_str()), "user_id":cfg.user_id}));
    if !verified { return Err("call device needs verification".into()); }
    Ok(())
}

async fn stop_active(active: &mut Option<calls::Active>) {
    if let Some(mut call) = active.take() {
        let _ = call.stop.send(true);
        if tokio::time::timeout(Duration::from_secs(25), &mut call.task).await.is_err() {
            call.task.abort();
            let _ = call.task.await;
        }
    }
}

async fn run() -> Result<(), Error> {
    let fd: i32 = std::env::args().nth(2).ok_or("missing private IPC descriptor")?.parse()?;
    if fd < 3 { return Err("invalid private IPC descriptor".into()); }
    // This descriptor is inherited explicitly by the gateway, never a public
    // socket or a path another process can connect to.
    let socket = unsafe { std::os::unix::net::UnixStream::from_raw_fd(fd) };
    socket.set_nonblocking(true)?;
    let (mut reader, writer) = tokio::net::UnixStream::from_std(socket)?.into_split();
    let Packet::Control(init) = tokio::time::timeout(Duration::from_secs(10), ipc::read(&mut reader)).await??
        else { return Err("expected IPC init".into()); };
    if init["protocol"] != 1 { return Err("unsupported call IPC protocol".into()); }
    let cfg: Config = serde_json::from_value(init["config"].clone())?;
    let _lock = device::lock_store(&cfg.store)?;
    let client = device::restore(&cfg).await?;
    if !device::verified(&client).await? { return Err("call device is not verified".into()); }
    let output = ipc::writer(writer);
    let (packets, mut packet_rx) = mpsc::channel(100);
    // read_exact is not cancellation-safe. One task owns framing; select!
    // below only cancels channel receives, never a partially read PCM packet.
    let read_task = tokio::spawn(async move {
        while let Ok(packet) = ipc::read(&mut reader).await {
            if packets.send(packet).await.is_err() { break; }
        }
    });
    let (rings, mut incoming) = mpsc::channel::<(Room, Value)>(16);
    let event_rings = rings.clone();
    let allowed_user = cfg.allowed_user.clone();
    client.add_event_handler(move |event: Raw<AnySyncMessageLikeEvent>, room: Room| {
        let rings = event_rings.clone();
        let allowed_user = allowed_user.clone();
        async move {
            let Ok(value) = event.deserialize_as::<Value>() else { return; };
            if value["sender"].as_str() == Some(allowed_user.as_str())
                && matches!(value["type"].as_str(), Some("m.rtc.notification" | "org.matrix.msc4075.rtc.notification" | "m.room.encrypted")) {
                eprintln!("Matrix call candidate received from timeline");
                let _ = rings.try_send((room, value));
            }
        }
    });
    let boot = calls::now_ms();
    let sync = SyncService::builder(client.clone()).build().await?;
    sync.start().await;
    output.event(json!({"event":"ready", "protocol":1, "device_id":client.device_id().map(|d| d.as_str())}))?;
    let mut active: Option<calls::Active> = None;
    let mut pending: Option<calls::Incoming> = None;
    let mut seen = HashSet::new();
    let mut seen_order = VecDeque::new();
    let mut generation = boot;
    let mut lookups = tokio::task::JoinSet::new();
    let mut declines = tokio::task::JoinSet::new();
    let mut ticker = tokio::time::interval(Duration::from_secs(1));
    let mut terminate = tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())?;
    loop {
        tokio::select! {
            _ = terminate.recv() => break,
            packet = packet_rx.recv() => {
                let Some(packet) = packet else { break; };
                match packet {
                    Packet::Audio(id, bytes) => {
                        if let Some(call) = &active {
                            if call.generation == id { let _ = call.pcm.try_send(bytes); }
                        }
                    }
                    Packet::Control(command) => {
                        let id = command["generation"].as_u64().unwrap_or(0);
                        match command["op"].as_str() {
                            Some("quit") => break,
                            Some("accept") if pending.as_ref().is_some_and(|p|p.generation == id && p.expires_at > calls::now_ms()) && active.is_none() => {
                                active = Some(calls::start(pending.take().unwrap(), cfg.clone(), output.clone()));
                            }
                            Some("decline") if pending.as_ref().is_some_and(|p|p.generation == id) => {
                                let ring = pending.take().unwrap();
                                if declines.len() < 4 {
                                    declines.spawn_local(async move {
                                        let _ = tokio::time::timeout(Duration::from_secs(5), calls::decline(&ring)).await;
                                    });
                                }
                            }
                            Some("hangup") if active.as_ref().is_some_and(|c|c.generation == id) => stop_active(&mut active).await,
                            Some("flush") if active.as_ref().is_some_and(|c|c.generation == id) => {
                                let call = active.as_ref().unwrap();
                                call.flush.send_modify(|n|*n = n.wrapping_add(1));
                            }
                            _ => {}
                        }
                    }
                }
            }
            ring = incoming.recv() => {
                let Some((room, event)) = ring else { break; };
                let id = event["event_id"].as_str().unwrap_or_default().to_owned();
                if id.is_empty() || !seen.insert(id.clone()) { continue; }
                seen_order.push_back(id);
                while seen_order.len() > 256 {
                    if let Some(old) = seen_order.pop_front() { seen.remove(&old); }
                }
                generation = generation.wrapping_add(1);
                if lookups.len() < 4 {
                    let cfg = cfg.clone();
                    lookups.spawn_local(async move {
                        tokio::time::timeout(Duration::from_secs(10),
                            calls::incoming(room, event, &cfg, boot, generation)).await.ok().and_then(Result::ok).flatten()
                    });
                }
            }
            lookup = lookups.join_next(), if !lookups.is_empty() => {
                if let Some(Ok(Some(ring))) = lookup {
                    if active.is_some() || pending.is_some() {
                        if declines.len() < 4 {
                            declines.spawn_local(async move {
                                let _ = tokio::time::timeout(Duration::from_secs(5), calls::decline(&ring)).await;
                            });
                        }
                        continue;
                    }
                    output.event(json!({"event":"incoming", "generation":ring.generation,
                        "room_id":ring.room.room_id().as_str(), "user_id":cfg.allowed_user,
                        "notification_id":ring.event_id}))?;
                    pending = Some(ring);
                }
            }
            _ = declines.join_next(), if !declines.is_empty() => {}
            _ = ticker.tick() => {
                // Sticky notifications are delivered through the SDK's sticky
                // collection, not timeline event handlers. Read its decrypted
                // view; the same bounded validation/deduplication applies.
                for room in client.joined_rooms() {
                    for entry in room.sticky_events().live() {
                        if entry.key.sender.as_str() != cfg.allowed_user { continue; }
                        if !matches!(entry.key.event_type.to_string().as_str(), "m.rtc.notification" | "org.matrix.msc4075.rtc.notification") { continue; }
                        if let Ok(value) = entry.raw().deserialize_as::<Value>() {
                            let _ = rings.try_send((room.clone(), value));
                        }
                    }
                }
                if active.as_ref().is_some_and(|c| c.task.is_finished()) { stop_active(&mut active).await; }
                if pending.as_ref().is_some_and(|p|p.expires_at <= calls::now_ms()) {
                    let ring = pending.take().unwrap();
                    output.event(json!({"event":"ended", "generation":ring.generation, "reason":"ring_expired"}))?;
                }
            }
        }
    }
    stop_active(&mut active).await;
    lookups.abort_all();
    declines.abort_all();
    read_task.abort();
    let _ = read_task.await;
    sync.stop().await;
    Ok(())
}
