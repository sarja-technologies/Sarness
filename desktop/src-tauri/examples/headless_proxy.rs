//! Usage: headless_proxy --out <file> [--profile <id>] [--transport snp|http] [--pin sha256:…] [--snp-port N] [--ssh-user U]
#[tokio::main]
async fn main() {
    env_logger::init();
    if let Err(e) = sarness_lib::headless::run(std::env::args().collect()).await {
        eprintln!("error: {e}");
        std::process::exit(1);
    }
}
