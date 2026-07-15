use serde::Serialize;
use sysinfo::{Disks, Networks, System};
use tokio::time::{interval, Duration};

#[derive(Serialize)]
struct Telemetry {
    cpu: f32,
    memory_used: u64,
    memory_total: u64,
    disk_used: u64,
    disk_total: u64,
    rx_bytes: u64,
    tx_bytes: u64,
    timestamp: u64,
}

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let nc = async_nats::connect("127.0.0.1:4222").await?;
    println!("StarAgent connected to NATS");

    let mut sys = System::new();
    let mut interval = interval(Duration::from_secs(10));
    let mut prev_rx: u64 = 0;
    let mut prev_tx: u64 = 0;

    loop {
        interval.tick().await;
        sys.refresh_cpu_all();
        sys.refresh_memory();

        let cpu = sys.global_cpu_usage();
        let memory_used = sys.used_memory();
        let memory_total = sys.total_memory();

        let disks = Disks::new_with_refreshed_list();
        let disk_total: u64 = disks.iter().map(|d| d.total_space()).sum();
        let disk_used: u64 = disks.iter().map(|d| d.total_space() - d.available_space()).sum();

        let networks = Networks::new_with_refreshed_list();
        let (rx, tx): (u64, u64) = networks
            .iter()
            .fold((0, 0), |(r, t), (_, n)| (r + n.received(), t + n.transmitted()));

        let rx_delta = if prev_rx > 0 { rx - prev_rx } else { 0 };
        let tx_delta = if prev_tx > 0 { tx - prev_tx } else { 0 };
        prev_rx = rx;
        prev_tx = tx;

        let ts = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_secs();

        let telemetry = Telemetry {
            cpu,
            memory_used,
            memory_total,
            disk_used,
            disk_total,
            rx_bytes: rx_delta,
            tx_bytes: tx_delta,
            timestamp: ts,
        };

        let payload = serde_json::to_vec(&telemetry)?;
        // Dual-publish: starship.* (primary) + agnetic.* (legacy Alpha 2.0)
        for subject in ["starship.telemetry", "agnetic.telemetry"] {
            let b: bytes::Bytes = payload.clone().into();
            nc.publish(*subject, b).await?;
        }
        nc.flush().await?;
    }
}
