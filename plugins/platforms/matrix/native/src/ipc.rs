use serde_json::Value;
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::unix::{OwnedReadHalf, OwnedWriteHalf};
use tokio::sync::mpsc;

use crate::device::Error;

pub const MAX_PACKET: usize = 65536;

pub enum Packet {
    Control(Value),
    Audio(u64, Vec<u8>),
}

#[derive(Clone)]
pub struct Output {
    control: mpsc::Sender<Vec<u8>>,
    audio: mpsc::Sender<Vec<u8>>,
}

impl Output {
    pub fn event(&self, value: Value) -> Result<(), Error> {
        let mut packet = vec![0];
        packet.extend(serde_json::to_vec(&value)?);
        self.control.try_send(packet).map_err(|_| "call control queue unavailable")?;
        Ok(())
    }

    pub fn audio(&self, generation: u64, pcm: &[u8]) {
        let mut packet = vec![1];
        packet.extend(generation.to_be_bytes());
        packet.extend(pcm);
        // Media cannot block SDK sync or make the control queue grow.
        let _ = self.audio.try_send(packet);
    }
}

pub fn writer(mut stream: OwnedWriteHalf) -> Output {
    let (control, mut controls) = mpsc::channel::<Vec<u8>>(32);
    let (audio, mut frames) = mpsc::channel::<Vec<u8>>(100);
    tokio::spawn(async move {
        loop {
            let packet = tokio::select! {
                biased;
                p = controls.recv() => p,
                p = frames.recv() => p,
            };
            let Some(packet) = packet else { break; };
            let write = async {
                stream.write_u32(packet.len() as u32).await?;
                stream.write_all(&packet).await
            };
            if !matches!(tokio::time::timeout(std::time::Duration::from_secs(5), write).await, Ok(Ok(()))) {
                break;
            }
        }
    });
    Output { control, audio }
}

pub async fn read(stream: &mut OwnedReadHalf) -> Result<Packet, Error> {
    let size = stream.read_u32().await? as usize;
    if size == 0 || size > MAX_PACKET { return Err("invalid IPC packet size".into()); }
    let mut packet = vec![0; size];
    stream.read_exact(&mut packet).await?;
    match packet[0] {
        0 => Ok(Packet::Control(serde_json::from_slice(&packet[1..])?)),
        1 if packet.len() >= 9 && (packet.len() - 9) % 2 == 0 => {
            let id = u64::from_be_bytes(packet[1..9].try_into()?);
            Ok(Packet::Audio(id, packet[9..].to_vec()))
        }
        _ => Err("invalid IPC packet type".into()),
    }
}
