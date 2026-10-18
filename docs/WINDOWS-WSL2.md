# Run Sarness on Windows using WSL2

> **Experimental / not yet validated:** This guide describes running the **Linux** Sarness Desktop package in Ubuntu under Windows Subsystem for Linux 2 (WSL2), using WSLg for its graphical window. Sarness **has not yet been verified to launch or work end-to-end under WSL2**. It is **not** a native Windows build, and problems with WebKitGTK, graphics, desktop integration, keyrings, or networking are possible.
>
> The commands below target the published **Sarness v0.1.0** Linux `.deb` release, which may have an older interface than unreleased branches.

## What you need

- **Windows 11** or **Windows 10 build 19044 or later**, 64-bit, with virtualization available/enabled.
- **WSL2 + Ubuntu + WSLg** (Linux GUI application support), and current Windows graphics drivers.
- Internet access to install WSL/Ubuntu packages and download the release.
- A **working Sarness Engine** on a separate, reachable server (or an existing engine installation) and its **API token**. Installing this desktop `.deb` does *not* automatically install or configure your GPU inference server.
- Enough disk space and RAM for WSL2 and the desktop app. If inference runs on your server, a dedicated NVIDIA GPU is **not required on the Windows client**.

WSLg displays Linux desktop applications as Windows windows. See [Microsoft's WSL GUI documentation](https://learn.microsoft.com/en-us/windows/wsl/tutorials/gui-apps).

## 1. Install WSL2 and Ubuntu (Windows PowerShell)

Open **PowerShell as Administrator** and run:

```powershell
wsl --install -d Ubuntu
```

Restart Windows if prompted. Launch **Ubuntu** from Start and complete the Linux username/password setup. This is a *separate Linux user*, not necessarily your Windows login.

Back in PowerShell, update WSL and confirm Ubuntu is running with **VERSION 2**:

```powershell
wsl --update
wsl --list --verbose
```

If Ubuntu is shown as **VERSION 1**, switch it to WSL2:

```powershell
wsl --set-version Ubuntu 2
```

After updating WSL, you can restart its environment with:

```powershell
wsl --shutdown
```

Then reopen **Ubuntu**. If `wsl --install` reports that WSL is already present but Ubuntu is missing, use `wsl --list --online` and `wsl --install -d Ubuntu`. Microsoft has [additional installation guidance](https://learn.microsoft.com/en-us/windows/wsl/install).

## 2. Download the Sarness Linux package (Ubuntu terminal)

These commands go in the **Ubuntu terminal**, **not** PowerShell:

```bash
sudo apt update
sudo apt install -y ca-certificates curl
cd ~
curl -fL --retry 3 \
  -o Sarness_0.1.0_amd64.deb \
  https://github.com/crossainthero-lab/Sarness/releases/download/v0.1.0/Sarness_0.1.0_amd64.deb
sudo apt install ./Sarness_0.1.0_amd64.deb
```

The filename above is the actual published v0.1.0 release asset. For another version, use the corresponding `.deb` filename from [Sarness Releases](https://github.com/crossainthero-lab/Sarness/releases), rather than assuming the filename is unchanged.

Alternatively, if you downloaded the `.deb` with a Windows browser, you can access your Windows downloads folder from Ubuntu at `/mnt/c/Users/<WINDOWS_USERNAME>/Downloads` and install the package from there. Replace the username and filename to match your system.

## 3. Launch Sarness

In the Ubuntu terminal:

```bash
sarness
```

WSLg should create a graphical Sarness window on the **Windows** desktop. An installed Linux app might also appear in the Windows Start menu under the Ubuntu/WSL application group. **This exact Sarness launch path has not been tested on Windows/WSL2 yet**; if the command is not found, inspect what the package installed with:

```bash
dpkg -L sarness | grep -E '/(bin|applications)/'
```

Do not assume that successful `apt` installation proves the GUI will work: Sarness uses **Tauri 2 + WebKitGTK**, which can behave differently inside WSLg.

## 4. Connect to your AI server

On Sarness's first-run connection screen:

1. Enter the reachable **server IP or hostname** and the configured Sarness Engine port (normally **8742**).
2. Enter the **Sarness Engine API token** from your server installation.
3. Select **Test connection**, then **Save & connect** when the test succeeds.

Consult [Deploying Sarness](DEPLOYMENT.md) for Sarness Engine setup, address/bind modes, tokens, and SSH tunnel instructions.

**WSL2 networking caveat:** Do not assume that routes, VPNs and DNS names that work on *Windows* (for example Tailscale and its MagicDNS names) also work from *inside Ubuntu/WSL2*. Check reachability *from the Ubuntu terminal*. Depending on your network and WSL configuration, you may need the VPN configured within WSL2 or another approved connection path. A server bound to `127.0.0.1` on a **different computer** will not be reachable by entering `127.0.0.1` in WSL2.

For a quick check from Ubuntu, substitute a real reachable server address (without angle brackets):

```bash
curl -v http://SERVER_IP:8742/v1/health
```

This is only a basic connectivity/health check; a successful response does not verify authentication or full model functionality. Do **not** expose an unauthenticated or plaintext service to the public internet. Follow the server's existing VPN/SSH/trusted-LAN security configuration.

## 5. Troubleshooting

### The Ubuntu terminal works, but no Sarness window appears

First confirm you're on **WSL2**, and update/restart WSL (PowerShell):

```powershell
wsl --list --verbose
wsl --update
wsl --shutdown
```

Reopen Ubuntu and verify that the graphical display environment is present:

```bash
echo "$DISPLAY"
echo "$WAYLAND_DISPLAY"
```

If both are empty, WSLg may be unavailable or misconfigured. You can test WSLg with a simple GUI app:

```bash
sudo apt install -y x11-apps
xclock
```

If `xclock` cannot open a window, repair/update WSLg before troubleshooting Sarness. If `xclock` works but `sarness` doesn't, run `sarness` from Ubuntu and inspect its terminal error output. WebKitGTK/Tauri-specific display or graphics problems remain possible.

### Sarness opens, but cannot reach the AI server

- Test `curl http://SERVER_IP:8742/v1/health` **inside Ubuntu**.
- Confirm the engine is running, its bind address and port are correct, and that you are on a trusted reachable network.
- If the server is reached through a VPN or by a VPN-provided name, verify network routing and DNS *from WSL2*, not only Windows.
- For SSH tunnel mode, install/use SSH and keys **inside WSL2** and follow [DEPLOYMENT.md](DEPLOYMENT.md). Windows-side SSH configuration is not automatically identical.
- Confirm the API token through the Sarness connection screen; do not paste it into issue reports or public logs.

### Credentials or file dialogs behave differently

Sarness normally uses the Linux Secret Service/GNOME keyring and may fall back to a user-owned `~/.config/sarness/secrets.json` file with restrictive permissions if a keyring is unavailable. WSL Ubuntu often doesn't provide a full GNOME desktop session. Keep your WSL user account secure, and remember that Linux home paths and Windows `C:\` paths differ (Windows drives are exposed under `/mnt/`).

### Update or remove Sarness

To replace v0.1.0, download the desired new Linux `.deb` from [Releases](https://github.com/crossainthero-lab/Sarness/releases) and install it with `sudo apt install ./FILE.deb`.

To remove the desktop package from Ubuntu:

```bash
sudo apt remove sarness
```

This does not uninstall WSL2/Ubuntu, remove your Windows files, or uninstall a separately hosted Sarness Engine.

## Known limitations and support status

- **Untested:** Installation, Tauri/WebKitGTK graphical startup, media rendering, file pickers, keyring integration, and complete remote-server workflows on Windows 10/11 + WSL2 have **not** been validated for this release.
- This is a **Linux application inside WSL2**, not a Windows `.exe`/`.msi`. Windows desktop integration may be incomplete.
- Sarness's local file paths, configuration and caches live in the **WSL Ubuntu filesystem** unless exported to a Windows path.
- The `v0.1.0` release may not include work still in active pull requests.
- Running the client in WSL2 does not imply the Windows computer can perform server-side inference; Sarness still needs its engine.

When reporting a problem, include your Windows version, the output of `wsl --version` and `wsl --list --verbose`, Ubuntu version (`lsb_release -a`), and a *redacted* copy of the Sarness launch error. **Never include API tokens or private keys.**

**References:** [Microsoft: GUI apps with WSL](https://learn.microsoft.com/en-us/windows/wsl/tutorials/gui-apps) · [Microsoft: install WSL](https://learn.microsoft.com/en-us/windows/wsl/install) · [Sarness deployment guide](DEPLOYMENT.md) · [Sarness releases](https://github.com/crossainthero-lab/Sarness/releases)
