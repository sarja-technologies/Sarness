//! Recent projects (folders the user authorised), persisted as JSON next to the connection
//! profiles. Registering a folder is what authorises the agent to work inside it.

use serde::{Deserialize, Serialize};
use std::path::{Path, PathBuf};

#[derive(Serialize, Deserialize, Clone, Debug, PartialEq)]
pub struct Project {
    pub path: String,
    pub name: String,
    pub last_opened: u64,
}

fn now() -> u64 {
    std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_secs()).unwrap_or(0)
}

pub fn load(file: &Path) -> Vec<Project> {
    std::fs::read_to_string(file).ok().and_then(|s| serde_json::from_str(&s).ok()).unwrap_or_default()
}

fn save(file: &Path, list: &[Project]) -> Result<(), String> {
    if let Some(p) = file.parent() {
        std::fs::create_dir_all(p).map_err(|e| e.to_string())?;
    }
    std::fs::write(file, serde_json::to_vec_pretty(list).unwrap()).map_err(|e| e.to_string())
}

pub fn add(file: &Path, path: &Path) -> Result<Vec<Project>, String> {
    let canon = path.canonicalize().map_err(|e| format!("cannot open {}: {e}", path.display()))?;
    if !canon.is_dir() {
        return Err(format!("{} is not a folder", canon.display()));
    }
    if canon.components().any(|c| c.as_os_str() == ".git") {
        return Err("That is a Git internals folder. Choose the repository folder that contains it.".into());
    }
    if canon == Path::new("/") || dirs::home_dir().is_some_and(|h| h == canon) {
        return Err("Choose a project folder, not the filesystem root or your whole home directory.".into());
    }
    let mut list = load(file);
    list.retain(|p| Path::new(&p.path) != canon);
    list.insert(0, Project { path: canon.display().to_string(), name: canon.file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_default(), last_opened: now() });
    list.truncate(30);
    save(file, &list)?;
    Ok(list)
}

pub fn remove(file: &Path, path: &str) -> Result<Vec<Project>, String> {
    let mut list = load(file);
    list.retain(|p| p.path != path);
    save(file, &list)?;
    Ok(list)
}

/// Creates `<parent>/<name>` (must not exist) and registers it.
pub fn create(file: &Path, parent: &Path, name: &str) -> Result<PathBuf, String> {
    let ok = !name.is_empty() && name.len() <= 80 && !name.starts_with('.') && name.chars().all(|c| c.is_ascii_alphanumeric() || "._- ".contains(c));
    if !ok {
        return Err("Project names may use letters, digits, spaces, dots, dashes and underscores.".into());
    }
    let dir = parent.canonicalize().map_err(|e| format!("cannot open {}: {e}", parent.display()))?.join(name);
    if dir.exists() {
        return Err(format!("{} already exists", dir.display()));
    }
    std::fs::create_dir_all(&dir).map_err(|e| format!("cannot create {}: {e}", dir.display()))?;
    add(file, &dir)?;
    Ok(dir)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn add_create_remove_roundtrip() {
        let d = tempfile::tempdir().unwrap();
        let f = d.path().join("cfg/projects.json");
        let made = create(&f, d.path(), "my app").unwrap();
        assert!(made.is_dir());
        assert!(create(&f, d.path(), "my app").is_err());
        assert!(create(&f, d.path(), "../evil").is_err());
        let list = add(&f, &made).unwrap();
        assert_eq!(list.len(), 1);
        assert_eq!(load(&f)[0].name, "my app");
        assert!(add(&f, Path::new("/")).is_err());
        let git = made.join(".git");
        std::fs::create_dir(&git).unwrap();
        assert!(add(&f, &git).unwrap_err().contains("Git internals"));
        assert!(remove(&f, &list[0].path).unwrap().is_empty());
    }
}
