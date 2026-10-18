//! The authorised workspace: path resolution that cannot be escaped with `..`,
//! absolute paths or symlinks, plus classification of sensitive locations.

use std::path::{Component, Path, PathBuf};

#[derive(Clone, Debug)]
pub struct Workspace {
    pub root: PathBuf, // canonical
}

#[derive(Clone, Debug)]
pub struct Resolved {
    pub path: PathBuf,
    pub inside: bool,
}

impl Workspace {
    pub fn open(path: &str) -> Result<Self, String> {
        let p = PathBuf::from(path);
        let root = p.canonicalize().map_err(|e| format!("cannot open workspace {path}: {e}"))?;
        if !root.is_dir() {
            return Err(format!("{} is not a directory", root.display()));
        }
        Ok(Self { root })
    }

    /// Resolves `p` (relative to the workspace, `~/…`, or absolute). Symlinks are followed on
    /// the deepest existing ancestor so a link inside the workspace pointing out is "outside".
    pub fn resolve(&self, p: &str) -> Result<Resolved, String> {
        if p.contains('\0') {
            return Err("path contains a NUL byte".into());
        }
        let raw = if p.is_empty() { "." } else { p };
        let expanded = if raw == "~" || raw.starts_with("~/") {
            let home = dirs::home_dir().ok_or("no home directory")?;
            home.join(raw.trim_start_matches('~').trim_start_matches('/'))
        } else {
            PathBuf::from(raw)
        };
        let joined = if expanded.is_absolute() { expanded } else { self.root.join(expanded) };
        let norm = normalize(&joined);
        // canonicalise the deepest existing ancestor, then re-append the rest
        let mut existing = norm.clone();
        let mut rest: Vec<std::ffi::OsString> = vec![];
        while !existing.exists() {
            match (existing.file_name().map(|f| f.to_os_string()), existing.parent().map(|p| p.to_path_buf())) {
                (Some(name), Some(parent)) => {
                    rest.push(name);
                    existing = parent;
                }
                _ => break,
            }
        }
        let mut real = existing.canonicalize().map_err(|e| format!("cannot resolve {p}: {e}"))?;
        for part in rest.into_iter().rev() {
            real.push(part);
        }
        let inside = real.starts_with(&self.root);
        Ok(Resolved { path: real, inside })
    }

    pub fn rel(&self, p: &Path) -> String {
        p.strip_prefix(&self.root).map(|r| r.display().to_string()).unwrap_or_else(|_| p.display().to_string())
    }
}

fn normalize(p: &Path) -> PathBuf {
    let mut out = PathBuf::new();
    for c in p.components() {
        match c {
            Component::ParentDir => {
                out.pop();
            }
            Component::CurDir => {}
            other => out.push(other.as_os_str()),
        }
    }
    out
}

/// Files that commonly hold credentials. Their contents would go to the model server, so
/// reading them always needs explicit approval and bulk tools skip them.
pub fn is_sensitive(p: &Path) -> bool {
    if p.components().any(|c| matches!(c, Component::Normal(n) if matches!(n.to_string_lossy().as_ref(), ".ssh" | ".gnupg" | ".aws" | ".kube" | ".password-store"))) {
        return true;
    }
    if let Some(h) = dirs::home_dir() {
        // Sarness' own connection profiles / token fallback file and engine data.
        if p.starts_with(h.join(".config/sarness")) || p.starts_with(h.join(".local/share/sarness-engine")) {
            return true;
        }
    }
    let name = p.file_name().map(|n| n.to_string_lossy().to_ascii_lowercase()).unwrap_or_default();
    if name == ".env" || (name.starts_with(".env.") && !matches!(name.as_str(), ".env.example" | ".env.sample" | ".env.template" | ".env.dist")) {
        return true;
    }
    name.starts_with("id_rsa")
        || name.starts_with("id_ed25519")
        || name.starts_with("id_ecdsa")
        || name.ends_with(".pem")
        || name.ends_with(".key")
        || name.ends_with(".p12")
        || name.ends_with(".pfx")
        || name == ".netrc"
        || name == ".npmrc"
        || name == ".pypirc"
        || name.starts_with("credentials")
        || name == "secrets.json"
        || name == "secrets.yml"
        || name == "secrets.yaml"
}

pub fn touches_git_dir(p: &Path) -> bool {
    p.components().any(|c| matches!(c, Component::Normal(n) if n == ".git"))
}

pub fn is_system_dir(p: &Path) -> bool {
    ["/etc", "/boot", "/root", "/proc", "/sys", "/dev", "/var", "/usr", "/bin", "/sbin", "/lib", "/lib64", "/opt"]
        .iter()
        .any(|d| p.starts_with(d))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn dotdot_and_absolute_escape_are_outside() {
        let d = tempfile::tempdir().unwrap();
        let ws = Workspace::open(d.path().to_str().unwrap()).unwrap();
        assert!(ws.resolve("a/b.txt").unwrap().inside);
        assert!(!ws.resolve("../x").unwrap().inside);
        assert!(!ws.resolve("a/../../x").unwrap().inside);
        assert!(!ws.resolve("/etc/passwd").unwrap().inside);
        assert!(ws.resolve("a/../b").unwrap().inside);
    }

    #[test]
    fn symlink_out_of_workspace_is_outside() {
        let d = tempfile::tempdir().unwrap();
        let other = tempfile::tempdir().unwrap();
        std::fs::write(other.path().join("s.txt"), "x").unwrap();
        #[cfg(unix)]
        std::os::unix::fs::symlink(other.path(), d.path().join("link")).unwrap();
        let ws = Workspace::open(d.path().to_str().unwrap()).unwrap();
        #[cfg(unix)]
        {
            assert!(!ws.resolve("link/s.txt").unwrap().inside);
            assert!(!ws.resolve("link/new-file.txt").unwrap().inside); // not-yet-existing target too
        }
    }

    #[test]
    fn sensitive_detection() {
        for s in ["/p/.env", "/p/.env.local", "/p/key.pem", "/p/id_rsa", "/p/.npmrc", "/p/credentials.json"] {
            assert!(is_sensitive(Path::new(s)), "{s}");
        }
        for s in ["/p/.env.example", "/p/src/main.rs", "/p/README.md", "/p/keyboard.ts"] {
            assert!(!is_sensitive(Path::new(s)), "{s}");
        }
    }
}
