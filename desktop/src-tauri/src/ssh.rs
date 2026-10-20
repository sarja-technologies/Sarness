//! OpenSSH-based port forwarding and remote command execution.
//!
//! Key-based authentication only (`BatchMode=yes`): Sarness never asks for or
//! stores SSH passwords. Users load keys into their agent or select a key file.

use crate::config::SshOptions;
use std::process::Stdio;
use std::time::Duration;
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::process::{Child, Command};

fn base_args(ssh: &SshOptions) -> Vec<String> {
    let mut a = vec![
        "-o".into(), "BatchMode=yes".into(),
        "-o".into(), "ConnectTimeout=10".into(),
        "-o".into(), "ServerAliveInterval=15".into(),
        "-o".into(), "ServerAliveCountMax=3".into(),
        "-o".into(), "StrictHostKeyChecking=accept-new".into(),
        "-p".into(), ssh.port.to_string(),
    ];
    if let Some(k) = ssh.key_path.as_ref().filter(|k| !k.trim().is_empty()) {
        a.push("-i".into());
        a.push(k.clone());
        a.push("-o".into());
        a.push("IdentitiesOnly=yes".into());
    }
    a
}

fn target(ssh: &SshOptions) -> String {
    format!("{}@{}", ssh.user, ssh.host)
}

pub fn free_port() -> u16 {
    std::net::TcpListener::bind("127.0.0.1:0").and_then(|l| l.local_addr()).map(|a| a.port()).unwrap_or(0)
}

pub struct Tunnel {
    pub child: Child,
    pub local_port: u16,
}

/// Arguments of `ssh -N -L 127.0.0.1:<local>:<remote_host>:<remote_port>`.
pub fn tunnel_args(ssh: &SshOptions, local_port: u16, remote_host: &str, remote_port: u16) -> Vec<String> {
    let remote_host = if remote_host.contains(':') && !remote_host.starts_with('[') { format!("[{remote_host}]") } else { remote_host.to_string() };
    let mut args = base_args(ssh);
    args.extend([
        "-N".into(),
        "-o".into(), "ExitOnForwardFailure=yes".into(),
        "-L".into(), format!("127.0.0.1:{local_port}:{remote_host}:{remote_port}"),
        target(ssh),
    ]);
    args
}

/// Opens the tunnel for a profile. Which remote port is forwarded depends on the
/// transport (the SNP port for SNP, the HTTP API port otherwise): tunnelling and
/// transport are independent, and TLS/authentication stay end-to-end through it.
pub async fn open_profile_tunnel(profile: &crate::config::Profile) -> Result<Tunnel, String> {
    let ssh = profile.ssh.as_ref().ok_or("SSH details are required for SSH mode")?;
    open_tunnel(ssh, &profile.host, profile.active_port()).await
}

/// Starts `ssh -N -L` and waits until the forwarded port accepts connections.
pub async fn open_tunnel(ssh: &SshOptions, remote_host: &str, remote_port: u16) -> Result<Tunnel, String> {
    let local_port = free_port();
    let args = tunnel_args(ssh, local_port, remote_host, remote_port);
    let mut child = Command::new("ssh")
        .args(&args)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::piped())
        .kill_on_drop(true)
        .spawn()
        .map_err(|e| format!("could not run ssh: {e}"))?;
    for _ in 0..150 {
        if let Ok(Some(status)) = child.try_wait() {
            let mut err = String::new();
            if let Some(mut s) = child.stderr.take() {
                use tokio::io::AsyncReadExt;
                let _ = s.read_to_string(&mut err).await;
            }
            return Err(friendly_ssh_error(&err, status.code()));
        }
        if tokio::net::TcpStream::connect(("127.0.0.1", local_port)).await.is_ok() {
            return Ok(Tunnel { child, local_port });
        }
        tokio::time::sleep(Duration::from_millis(100)).await;
    }
    let _ = child.kill().await;
    Err("timed out establishing the SSH tunnel".into())
}

pub fn friendly_ssh_error(stderr: &str, code: Option<i32>) -> String {
    let s = stderr.trim();
    if s.contains("Permission denied") {
        return "SSH authentication failed. Sarness uses key-based login: add your key to ssh-agent or choose a key file.".into();
    }
    if s.contains("Could not resolve hostname") {
        return "SSH host name could not be resolved.".into();
    }
    if s.contains("Connection refused") {
        return "SSH connection refused (is sshd running on the server?).".into();
    }
    if s.contains("Host key verification failed") || s.contains("REMOTE HOST IDENTIFICATION HAS CHANGED") {
        return "The server's SSH host key changed. Verify the server and update ~/.ssh/known_hosts.".into();
    }
    if s.is_empty() {
        format!("ssh exited with status {}", code.unwrap_or(-1))
    } else {
        s.lines().last().unwrap_or(s).to_string()
    }
}

/// Runs a command remotely, streaming combined output lines to `on_line`.
pub async fn run_remote<F: FnMut(&str)>(ssh: &SshOptions, command: &str, stdin_data: Option<&[u8]>, mut on_line: F) -> Result<i32, String> {
    let mut args = base_args(ssh);
    args.push(target(ssh));
    args.push(command.to_string());
    let mut child = Command::new("ssh")
        .args(&args)
        .stdin(if stdin_data.is_some() { Stdio::piped() } else { Stdio::null() })
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .kill_on_drop(true)
        .spawn()
        .map_err(|e| format!("could not run ssh: {e}"))?;
    if let (Some(data), Some(mut stdin)) = (stdin_data, child.stdin.take()) {
        let data = data.to_vec();
        tokio::spawn(async move {
            let _ = stdin.write_all(&data).await;
            let _ = stdin.shutdown().await;
        });
    }
    let stdout = child.stdout.take().unwrap();
    let stderr = child.stderr.take().unwrap();
    let mut out = BufReader::new(stdout).lines();
    let mut err = BufReader::new(stderr).lines();
    let mut last_err = String::new();
    loop {
        tokio::select! {
            l = out.next_line() => match l { Ok(Some(l)) => on_line(&l), _ => break },
            l = err.next_line() => if let Ok(Some(l)) = l { last_err = l.clone(); on_line(&l) },
        }
    }
    while let Ok(Some(l)) = err.next_line().await {
        last_err = l.clone();
        on_line(&l);
    }
    let status = child.wait().await.map_err(|e| e.to_string())?;
    let code = status.code().unwrap_or(-1);
    if code == 255 {
        return Err(friendly_ssh_error(&last_err, Some(code)));
    }
    Ok(code)
}

/// Copies a local file to the remote host with scp.
pub async fn copy_to(ssh: &SshOptions, local: &std::path::Path, remote: &str) -> Result<(), String> {
    let mut args: Vec<String> = base_args(ssh)
        .into_iter()
        .map(|a| if a == "-p" { "-P".into() } else { a })
        .collect();
    args.push(local.to_string_lossy().into_owned());
    args.push(format!("{}:{}", target(ssh), remote));
    let out = Command::new("scp").args(&args).output().await.map_err(|e| format!("could not run scp: {e}"))?;
    if out.status.success() {
        Ok(())
    } else {
        Err(friendly_ssh_error(&String::from_utf8_lossy(&out.stderr), out.status.code()))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn args_never_include_passwords() {
        let o = SshOptions { user: "me".into(), host: "box".into(), port: 2222, key_path: Some("/k".into()) };
        let a = base_args(&o).join(" ");
        assert!(a.contains("BatchMode=yes"));
        assert!(a.contains("-p 2222"));
        assert!(a.contains("-i /k"));
    }

    #[test]
    fn tunnel_forwards_the_requested_port() {
        use crate::config::{ConnectionMode, Profile, Transport};
        let o = SshOptions { user: "me".into(), host: "box".into(), port: 22, key_path: None };
        let a = tunnel_args(&o, 40000, "127.0.0.1", 8744);
        assert!(a.contains(&"127.0.0.1:40000:127.0.0.1:8744".to_string()));
        assert_eq!(tunnel_args(&o, 1, "::1", 8742).iter().find(|x| x.starts_with("127.0.0.1:1:")).unwrap(), "127.0.0.1:1:[::1]:8742");
        let mut p = Profile {
            id: "p".into(), name: "n".into(), host: "127.0.0.1".into(), port: 8742, scheme: "http".into(), mode: ConnectionMode::Ssh,
            ssh: Some(o), has_token: false, transport: Transport::Snp, snp_port: 8744, snp_pin: None, snp_server_name: None,
            preferred_tasks: vec![],
        };
        assert_eq!(p.active_port(), 8744);
        p.transport = Transport::Https;
        assert_eq!(p.active_port(), 8742);
    }

    #[test]
    fn friendly_errors() {
        assert!(friendly_ssh_error("me@box: Permission denied (publickey).", Some(255)).contains("key-based"));
    }
}
