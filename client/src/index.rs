//! 本地索引缓存与扫描（Module 05 §3.2 / D-27 懒同步的本地侧）。
//!
//! **缓存位置**：`~/.cache/nova/<projectId>/index.json`，**不进工作区**。
//! 这是对参考实现（notace 放 `.not-ace-tool/index.json` 于项目内）的**有意改进**（`Background/01` §7-1）：
//! 不污染工作区、不与 `.gitignore` 纠缠，且同一仓库的不同 checkout 天然隔离（projectId 由 D-29 决定，
//! 而 D-29 含相对路径）。
//!
//! **已验证缓存命中（verified cache hit）**：`mtime + size` 只是**快路径**，最终以
//! `blob_hash` 内容 hash 一致为准——防"mtime 不变内容变"与"touch 即误判"（`Background/01` §3.2）。
//!
//! **上传成功才入缓存**：文件的所有 blob 都被服务端接受才写索引；部分接受/被跳过 → 该文件不入缓存
//! （下次重传），并计入 `skipped_paths`（服务端裁决权，S4）。
//!
//! 忽略语义（D-28 / R42）在 [`crate::ignore`] 模块；本模块只做编排与对账。

use std::collections::{BTreeMap, HashSet};
use std::path::{Path, PathBuf};

use anyhow::{anyhow, Context, Result};
use serde::{Deserialize, Serialize};

use crate::blobref::{blob_hash, is_binary, sanitize_content, BINARY_RATIO, MAX_FILE_BYTES};
use crate::ignore::IgnoreRules;

/// 缓存 schema 版本；不匹配即整体作废（安全阀）。
const CACHE_VERSION: u32 = 1;

/// 二进制探测窗口（与 core `_is_binary` 的 `probe` 默认值一致：前 8 KB）。
const BINARY_PROBE_BYTES: usize = 8 * 1024;

/// 单个缓存文件上限（超过即视为损坏，直接作废重扫）。
const MAX_CACHE_BYTES: u64 = 256 * 1024 * 1024;

/// 一份文件的缓存条目。
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct FileEntry {
    pub mtime_secs: u64,
    pub mtime_nanos: u32,
    pub size: u64,
    pub blob_hash: String,
}

/// 本地索引（`~/.cache/nova/<serverKey>/<projectId>/index.json`）。
///
/// TASK-100 修正（用户 2026-09-14 报告的"每次调用都重新初始化"）：
/// 缓存以前只按 `projectId` 分目录，**没有区分"这份缓存属于哪台服务端"**。
/// 而 `projectId` 是从仓库路径算出来的（同一个仓库连两个不同后端 → 同一个 id），
/// 于是出现两种坏事：
///
/// 1. 连新后端时缓存"看着有效"（`project_id` 与 `config_hash` 都匹配）→ 客户端认为
///    "内容没变不用传"，而新后端库里根本没数据 → 检索为空；
/// 2. 同名 `projectId` 的两台后端交替使用 → 两边都不停在"缓存 miss → 全量重传"。
///
/// 修正分两层（两道防线）：
/// - **目录分片**（`IndexManager::new` 的 `endpoint`）：不同后端各用一棵缓存目录，物理隔离；
/// - **字段自证**（本字段）：即使目录被混用（手动拷贝/回退版本），载入时也会因 `endpoint`
///   不匹配而作废——不依赖目录名这种约定。
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct IndexData {
    pub version: u32,
    /// 项目身份（D-29）：缓存自证属于哪个项目，换仓库即作废。
    pub project_id: String,
    /// 扫描配置指纹（阈值 + 忽略规则版本）；变更即作废。
    pub config_hash: String,
    /// 服务端端点（TASK-100）：缓存自证属于哪个后端。
    ///
    /// `#[serde(default)]`：旧缓存没有这个字段 → 反序列化成空串 → 与当前值不等 →
    /// **自动作废一次**（然后被新格式覆盖）。这是有意的：旧缓存缺少归属信息，
    /// 与其冒着"错认服务端"的风险复用它，不如让它重传一次。
    #[serde(default)]
    pub endpoint: String,
    pub entries: BTreeMap<String, FileEntry>,
}

impl Default for IndexData {
    fn default() -> Self {
        Self {
            version: CACHE_VERSION,
            project_id: String::new(),
            config_hash: String::new(),
            endpoint: String::new(),
            entries: BTreeMap::new(),
        }
    }
}

impl IndexData {
    /// 全部 blob_hash（稳定排序）——scope 的来源。
    pub fn all_blob_hashes(&self) -> Vec<String> {
        self.entries
            .values()
            .map(|entry| entry.blob_hash.clone())
            .collect()
    }
}

/// 一次扫描的结果（对账后）。
#[derive(Debug, Default)]
pub struct ScanResult {
    /// 需要上传的文件（相对路径 → 内容）。
    pub to_upload: Vec<UploadFile>,
    /// 本轮无需上传（内容 hash 命中缓存）的文件数。
    pub cached_files: usize,
    /// 扫描后的完整索引（尚未提交）。
    pub index: IndexData,
    /// 被忽略/跳过并**如实记录原因**的路径（R43：跳过必须带原因）。
    pub skipped: Vec<SkippedPath>,
    /// 相对于缓存被删除的路径（需要通知服务端）。
    pub deleted: Vec<String>,
}

/// 待上传文件。
#[derive(Debug, Clone)]
pub struct UploadFile {
    pub path: String,
    pub content: String,
    pub blob_hash: String,
}

/// 被跳过的路径与原因（R43）。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SkippedPath {
    pub path: String,
    pub reason: String,
}

/// 本地索引管理器（一个仓库一个实例）。
pub struct IndexManager {
    root: PathBuf,
    project_id: String,
    cache_dir: PathBuf,
    rules: IgnoreRules,
    config_hash: String,
    /// 服务端端点（TASK-100）：与 `IndexData::endpoint` 对账，换后端即作废缓存。
    endpoint: String,
}

impl IndexManager {
    /// `cache_root` = 缓存根（通常是 `~/.cache/nova`）；实际写 `<cache_root>/<projectId>/`。
    pub fn new(root: PathBuf, project_id: String, cache_root: PathBuf, endpoint: &str) -> Self {
        let rules = IgnoreRules::load(&root);
        let config_hash = config_fingerprint(&rules);
        // 目录分片（TASK-100）：`<cache_root>/<serverKey>/<projectId>/`。
        //
        // `serverKey` 用 endpoint 的哈希前 16 位：base_url 可能含凭据类信息或很长，
        // 不适合直接做目录名；不够防碰撞（16 hex = 64 bit）但足够区分本地几台服务。
        let server_key = server_cache_key(endpoint);
        Self {
            cache_dir: cache_root.join(server_key).join(&project_id),
            root,
            project_id,
            endpoint: endpoint.to_string(),
            rules,
            config_hash,
        }
    }

    pub fn root(&self) -> &Path {
        &self.root
    }

    /// 缓存目录（测试与诊断用：TASK-100 的回归用例需要直接改写缓存文件）。
    #[cfg(test)]
    fn cache_dir(&self) -> &Path {
        &self.cache_dir
    }

    /// 扫描 + 对账（**纯本地、不联网**）。
    ///
    /// 判定顺序与 core/service 一致：目录剪枝（忽略规则）→ 大小阈值 → 二进制阈值 → 内容。
    pub fn scan(&self) -> Result<ScanResult> {
        self.scan_streaming(|_| {})
    }

    /// [`Self::scan`] 的流式版本：每确定一个待上传文件就回调 `on_upload`，让上传与扫描重叠。
    ///
    /// 三步走：① 单线程遍历目录（忽略规则、去重、元数据层面的跳过），定下文件顺序；
    /// ② 按 [`SCAN_CHUNK`] 分块，块内多线程读取 / 判二进制 / 清理 / 计算 hash（冷盘读取与
    /// sha256 是首次同步客户端侧的主要耗时）；③ 按遍历顺序汇总。结果（`skipped` / `to_upload`
    /// 的顺序、缓存条目）与逐个串行处理完全一致，因此上传分批也与之前相同。
    pub fn scan_streaming(&self, mut on_upload: impl FnMut(&UploadFile)) -> Result<ScanResult> {
        let previous = self.load()?;
        let previous_entries = previous.entries;
        let walked = self.walk();
        let workers = std::thread::available_parallelism()
            .map(|count| count.get())
            .unwrap_or(1)
            .clamp(1, MAX_SCAN_WORKERS);

        let mut entries = BTreeMap::new();
        let mut to_upload = Vec::new();
        let mut cached_files = 0usize;
        let mut skipped = Vec::new();
        for chunk in walked.chunks(SCAN_CHUNK) {
            let contents = parallel_map(chunk, workers, |item| match item {
                Walked::File { relative, path, .. } => Some(read_content(relative, path)),
                Walked::Skip(_) => None,
            });
            for (item, content) in chunk.iter().zip(contents) {
                let (relative, size, mtime) = match item {
                    Walked::Skip(path) => {
                        skipped.push(path.clone());
                        continue;
                    }
                    Walked::File { relative, size, mtime, .. } => (relative, *size, *mtime),
                };
                let (content, hash) = match content {
                    Some(Content::Text { content, hash }) => (content, hash),
                    Some(Content::Skip(reason)) => {
                        skipped.push(SkippedPath {
                            path: relative.clone(),
                            reason: reason.to_string(),
                        });
                        continue;
                    }
                    None => unreachable!("文件条目必有内容结果"),
                };
                let file_entry = FileEntry {
                    mtime_secs: mtime.0,
                    mtime_nanos: mtime.1,
                    size,
                    blob_hash: hash.clone(),
                };
                // 已验证缓存命中：mtime+size 快路径 ≈ 且 **内容 hash 一致**才算命中。
                let verified_hit = previous_entries
                    .get(relative)
                    .is_some_and(|cached| cached.blob_hash == hash);
                if verified_hit {
                    cached_files += 1;
                } else {
                    let file = UploadFile {
                        path: relative.clone(),
                        content,
                        blob_hash: hash,
                    };
                    on_upload(&file);
                    to_upload.push(file);
                }
                entries.insert(relative.clone(), file_entry);
            }
        }

        let deleted: Vec<String> = previous_entries
            .keys()
            .filter(|path| !entries.contains_key(*path))
            .cloned()
            .collect();

        // 缓存命中率**必须可见**（TASK-100 排查的教训）：用户报告"每次调用都重新初始化"时，
        // 没有这行日志就只能靠猜。实测发现：同一批 287 个文件被连续两次全量上传，
        // 但当时 `cached_files` 已经算出来了却从未输出。
        //
        // 打到 stderr（不能打到 stdout：stdout 是 MCP 的 JSON-RPC 通道，写脏会破协议）。
        eprintln!(
            "nova-client: 扫描完成 project={} 文件={} 命中缓存={} 待上传={} 待删除={}",
            self.project_id,
            entries.len(),
            cached_files,
            to_upload.len(),
            deleted.len()
        );

        Ok(ScanResult {
            to_upload,
            cached_files,
            index: IndexData {
                version: CACHE_VERSION,
                project_id: self.project_id.clone(),
                config_hash: self.config_hash.clone(),
                endpoint: self.endpoint.clone(),
                entries,
            },
            skipped,
            deleted,
        })
    }

    /// 第一步：遍历（忽略语义 D-28 / TASK-097 + 元数据层面的跳过），定下处理顺序。
    ///
    /// 第 0 层白名单（强制包含）> `.novaignore` > `.gitignore` > 内置目录剪枝。前两层的遍历由
    /// `walker()` 完成；第 0 层由 `allowlist_walk()` 追加，因此这里迭代两者的**并集**，并用
    /// `seen` 按路径去重（既未被忽略又命中白名单的文件会同时出现在两个 walker 里）。
    fn walk(&self) -> Vec<Walked> {
        let mut walked = Vec::new();
        let mut seen: HashSet<String> = HashSet::new();
        for entry in self.rules.walk_union() {
            let entry = match entry {
                Ok(entry) => entry,
                Err(_) => continue,
            };
            if !entry.file_type().is_some_and(|kind| kind.is_file()) {
                continue;
            }
            let path = entry.path();
            let Some(relative) = relative_path(&self.root, path) else {
                continue;
            };
            if !seen.insert(relative.clone()) {
                continue;
            }
            let metadata = match entry.metadata() {
                Ok(metadata) => metadata,
                Err(_) => {
                    walked.push(Walked::Skip(SkippedPath {
                        path: relative,
                        reason: "unreadable_metadata".to_string(),
                    }));
                    continue;
                }
            };
            let size = metadata.len();
            if size > MAX_FILE_BYTES as u64 {
                walked.push(Walked::Skip(SkippedPath {
                    path: relative,
                    reason: format!("oversize:{size}"),
                }));
                continue;
            }
            walked.push(Walked::File {
                relative,
                path: path.to_path_buf(),
                size,
                mtime: mtime_of(&metadata),
            });
        }
        walked
    }

    /// 应用服务端裁决：**只有全部 blob 被接受且未被跳过**的文件才保留在索引里。
    ///
    /// `accepted` = 服务端返回的 `accepted` blobHash 列表；`skipped_paths` = `skipped` 路径列表。
    pub fn apply_upload_outcome(
        index: &mut IndexData,
        uploaded: &[UploadFile],
        accepted: &HashSet<String>,
        skipped_paths: &HashSet<String>,
    ) -> Vec<SkippedPath> {
        let mut rejected = Vec::new();
        for file in uploaded {
            let accepted_ok = accepted.contains(&file.blob_hash);
            // 服务端按"path"裁决；blob 名里的 path 与我们上传的 path 一致。
            let server_skipped = skipped_paths.contains(&file.path);
            if !accepted_ok || server_skipped {
                index.entries.remove(&file.path);
                rejected.push(SkippedPath {
                    path: file.path.clone(),
                    reason: if server_skipped {
                        "server_skipped".to_string()
                    } else {
                        "not_accepted".to_string()
                    },
                });
            }
        }
        rejected
    }

    /// 从缓存中"遗忘"服务端报告为 stale 的路径（checkpoint 自愈路径 2）。
    pub fn forget_paths(&self, paths: &[String]) -> Result<usize> {
        if paths.is_empty() {
            return Ok(0);
        }
        let mut index = self.load()?;
        let before = index.entries.len();
        for path in paths {
            index.entries.remove(path);
        }
        let removed = before - index.entries.len();
        if removed != 0 {
            self.commit(&index)?;
        }
        Ok(removed)
    }

    /// 读缓存（缺失/损坏/版本或指纹不符 → 空索引）。
    pub fn load(&self) -> Result<IndexData> {
        let path = self.cache_file();
        let Ok(metadata) = std::fs::metadata(&path) else {
            return Ok(IndexData::default());
        };
        if !metadata.is_file() || metadata.len() > MAX_CACHE_BYTES {
            return Ok(IndexData::default());
        }
        let Ok(bytes) = std::fs::read(&path) else {
            return Ok(IndexData::default());
        };
        let Ok(data) = serde_json::from_slice::<IndexData>(&bytes) else {
            return Ok(IndexData::default());
        };
        if data.version == CACHE_VERSION
            && data.config_hash == self.config_hash
            && data.project_id == self.project_id
            // TASK-100：缓存必须自证属于**同一个服务端**（否则两合后端交替使用时
            // 会错认"内容已上传"——它其实只传给了另一台）。
            && data.endpoint == self.endpoint
        {
            Ok(data)
        } else {
            Ok(IndexData::default())
        }
    }

    /// 原子提交（tmp + rename；Windows 先删后改名）。
    pub fn commit(&self, index: &IndexData) -> Result<()> {
        std::fs::create_dir_all(&self.cache_dir).with_context(|| {
            format!(
                "failed to create cache directory: {}",
                self.cache_dir.display()
            )
        })?;
        let bytes = serde_json::to_vec(index).context("failed to serialize index cache")?;
        let target = self.cache_file();
        let temporary = target.with_extension("json.tmp");
        std::fs::write(&temporary, bytes)
            .with_context(|| format!("failed to write temporary cache: {}", temporary.display()))?;
        #[cfg(windows)]
        if target.exists() {
            std::fs::remove_file(&target)
                .with_context(|| format!("failed to replace cache: {}", target.display()))?;
        }
        std::fs::rename(&temporary, &target)
            .with_context(|| format!("failed to commit cache: {}", target.display()))?;
        Ok(())
    }

    fn cache_file(&self) -> PathBuf {
        self.cache_dir.join("index.json")
    }
}

/// 并行扫描的块大小：块内多线程读取 / hash，块与块之间按遍历顺序汇总并流式交出待上传文件。
const SCAN_CHUNK: usize = 128;
/// 扫描线程上限（冷盘读取 + sha256；2 vCPU 机器上即 2）。
const MAX_SCAN_WORKERS: usize = 4;

/// 遍历产物：元数据层面已判定跳过的路径，或待读取内容的文件。
enum Walked {
    Skip(SkippedPath),
    File {
        relative: String,
        path: PathBuf,
        size: u64,
        mtime: (u64, u32),
    },
}

/// 内容判定结果：不可读 / 二进制跳过，或清理后的文本与其 blob hash。
enum Content {
    Skip(&'static str),
    Text { content: String, hash: String },
}

fn read_content(relative: &str, path: &Path) -> Content {
    let Ok(raw) = std::fs::read(path) else {
        return Content::Skip("unreadable");
    };
    if is_binary(&raw, BINARY_PROBE_BYTES, BINARY_RATIO) {
        return Content::Skip("binary");
    }
    let content = sanitize_content(&String::from_utf8_lossy(&raw));
    let hash = blob_hash(relative, content.as_bytes());
    Content::Text { content, hash }
}

/// 有序并行 map：把 `items` 均分给最多 `workers` 个线程，结果按输入顺序拼回。
fn parallel_map<T: Sync, R: Send>(
    items: &[T],
    workers: usize,
    map: impl Fn(&T) -> R + Sync,
) -> Vec<R> {
    if workers <= 1 || items.len() <= 1 {
        return items.iter().map(map).collect();
    }
    let size = items.len().div_ceil(workers);
    let map = &map;
    std::thread::scope(|scope| {
        let handles: Vec<_> = items
            .chunks(size)
            .map(|part| scope.spawn(move || part.iter().map(map).collect::<Vec<R>>()))
            .collect();
        handles
            .into_iter()
            .flat_map(|handle| handle.join().expect("扫描线程异常退出"))
            .collect()
    })
}

/// 扫描配置指纹：阈值 + 忽略规则内容。任一变化 → 缓存作废（避免脏索引）。
fn config_fingerprint(rules: &IgnoreRules) -> String {
    let material = format!(
        "v{CACHE_VERSION}:{MAX_FILE_BYTES}:{BINARY_RATIO}:{BINARY_PROBE_BYTES}:{}",
        rules.fingerprint()
    );
    blob_hash("nova-client-config", material.as_bytes())
}

/// 服务端端点的缓存目录名（TASK-100）：取端点哈希的前 16 hex。
///
/// 为什么不直接用 base_url 做目录名：它可能很长、含 `:`/`/`（部分平台不合法字符），
/// 也可能内嵌凭据。哈希同时解决了这三个问题；16 hex（64 bit）足够区分本地几台服务。
fn server_cache_key(endpoint: &str) -> String {
    let digest = blob_hash("nova-client-endpoint", endpoint.as_bytes());
    digest.chars().take(16).collect()
}

fn relative_path(root: &Path, path: &Path) -> Option<String> {
    let relative = path.strip_prefix(root).ok()?;
    Some(relative.to_string_lossy().replace('\\', "/"))
}

fn mtime_of(metadata: &std::fs::Metadata) -> (u64, u32) {
    let modified = match metadata.modified() {
        Ok(modified) => modified,
        Err(_) => return (0, 0),
    };
    match modified.duration_since(std::time::UNIX_EPOCH) {
        Ok(duration) => (duration.as_secs(), duration.subsec_nanos()),
        Err(error) => {
            let duration = error.duration();
            (0, duration.subsec_nanos())
        }
    }
}

/// 便捷入口：把 `ScanResult` 里待上传文件转成 `(path, content)` 列表（上传层用）。
pub fn upload_payload(files: &[UploadFile]) -> Vec<(String, String)> {
    files
        .iter()
        .map(|file| (file.path.clone(), file.content.clone()))
        .collect()
}

/// 空扫描防御：索引为空时报可读错误（与 MCP 工具的错误面一致）。
pub fn require_non_empty(index: &IndexData) -> Result<()> {
    if index.entries.is_empty() {
        return Err(anyhow!(
            "扫描后没有任何可上传的文件（可能全部被忽略规则/大小/二进制阈值跳过）"
        ));
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;

    fn manager(root: &Path, cache: &Path) -> IndexManager {
        IndexManager::new(
            root.to_path_buf(),
            "testproject0000".to_string(),
            cache.to_path_buf(),
            "http://127.0.0.1:1",
        )
    }

    #[test]
    fn scan_skips_git_directory_and_reports_reasons() -> Result<()> {
        let project = tempfile::tempdir()?;
        let cache = tempfile::tempdir()?;
        fs::create_dir_all(project.path().join(".git"))?;
        fs::write(project.path().join(".git/config"), "metadata")?;
        fs::write(project.path().join("included.rs"), "pub fn included() {}")?;

        let scan = manager(project.path(), cache.path()).scan()?;

        assert!(scan.index.entries.contains_key("included.rs"));
        assert!(!scan.index.entries.contains_key(".git/config"));
        assert_eq!(scan.to_upload.len(), 1);
        Ok(())
    }

    #[test]
    fn oversize_and_binary_files_are_skipped_with_reasons() -> Result<()> {
        let project = tempfile::tempdir()?;
        let cache = tempfile::tempdir()?;
        fs::write(
            project.path().join("big.txt"),
            vec![b'a'; MAX_FILE_BYTES + 1],
        )?;
        fs::write(project.path().join("blob.bin"), [0u8, 1, 2, 3, 4])?;
        fs::write(project.path().join("ok.txt"), "fine")?;

        let scan = manager(project.path(), cache.path()).scan()?;
        let reasons: BTreeMap<&str, &str> = scan
            .skipped
            .iter()
            .map(|item| (item.path.as_str(), item.reason.as_str()))
            .collect();

        assert!(reasons["big.txt"].starts_with("oversize:"));
        assert_eq!(reasons["blob.bin"], "binary");
        assert!(scan.index.entries.contains_key("ok.txt"));
        Ok(())
    }

    #[test]
    fn verified_cache_hit_avoids_reupload() -> Result<()> {
        let project = tempfile::tempdir()?;
        let cache = tempfile::tempdir()?;
        fs::write(project.path().join("a.txt"), "same")?;
        let manager = manager(project.path(), cache.path());

        let first = manager.scan()?;
        assert_eq!(first.to_upload.len(), 1);
        manager.commit(&first.index)?;

        let second = manager.scan()?;
        assert_eq!(second.to_upload.len(), 0);
        assert_eq!(second.cached_files, 1);
        Ok(())
    }

    #[test]
    fn changed_content_is_detected_even_when_size_is_equal() -> Result<()> {
        let project = tempfile::tempdir()?;
        let cache = tempfile::tempdir()?;
        let file = project.path().join("a.txt");
        fs::write(&file, "aaaa")?;
        let manager = manager(project.path(), cache.path());
        let first = manager.scan()?;
        manager.commit(&first.index)?;

        // 同长度不同内容（mtime+size 快路径会误判，content hash 不会）
        fs::write(&file, "bbbb")?;
        let second = manager.scan()?;

        assert_eq!(second.to_upload.len(), 1);
        assert_eq!(second.cached_files, 0);
        Ok(())
    }

    #[test]
    fn deleted_file_is_reported_for_server_notification() -> Result<()> {
        let project = tempfile::tempdir()?;
        let cache = tempfile::tempdir()?;
        let file = project.path().join("gone.txt");
        fs::write(&file, "bye")?;
        let manager = manager(project.path(), cache.path());
        let first = manager.scan()?;
        manager.commit(&first.index)?;

        fs::remove_file(&file)?;
        let second = manager.scan()?;

        assert_eq!(second.deleted, vec!["gone.txt".to_string()]);
        Ok(())
    }

    #[test]
    fn partial_upload_does_not_persist_the_file() -> Result<()> {
        let project = tempfile::tempdir()?;
        let cache = tempfile::tempdir()?;
        fs::write(project.path().join("a.txt"), "payload")?;
        let manager = manager(project.path(), cache.path());
        let mut scan = manager.scan()?;

        let rejected = IndexManager::apply_upload_outcome(
            &mut scan.index,
            &scan.to_upload,
            &HashSet::new(),
            &HashSet::new(),
        );

        assert_eq!(rejected.len(), 1);
        assert!(scan.index.entries.is_empty());
        Ok(())
    }

    #[test]
    fn server_skipped_path_is_not_persisted() -> Result<()> {
        let project = tempfile::tempdir()?;
        let cache = tempfile::tempdir()?;
        fs::write(project.path().join("a.txt"), "payload")?;
        let manager = manager(project.path(), cache.path());
        let mut scan = manager.scan()?;
        let accepted: HashSet<String> = vec![scan.to_upload[0].blob_hash.clone()]
            .into_iter()
            .collect();
        let skipped: HashSet<String> = vec!["a.txt".to_string()].into_iter().collect();

        let rejected = IndexManager::apply_upload_outcome(
            &mut scan.index,
            &scan.to_upload,
            &accepted,
            &skipped,
        );

        assert_eq!(rejected.len(), 1);
        assert_eq!(rejected[0].reason, "server_skipped");
        assert!(scan.index.entries.is_empty());
        Ok(())
    }

    #[test]
    fn corrupt_or_foreign_cache_loads_as_empty() -> Result<()> {
        let project = tempfile::tempdir()?;
        let cache = tempfile::tempdir()?;
        fs::write(project.path().join("a.txt"), "x")?;
        let manager = manager(project.path(), cache.path());
        let scan = manager.scan()?;
        manager.commit(&scan.index)?;

        // 换 project_id → 缓存自证不符 → 作废
        let other = IndexManager::new(
            project.path().to_path_buf(),
            "otherproject0000".to_string(),
            cache.path().to_path_buf(),
            "http://127.0.0.1:1",
        );
        assert!(other.load()?.entries.is_empty());
        Ok(())
    }

    /// TASK-100 回归：**换服务端端点 → 缓存作废**（用户报告的"每次调用都重新初始化"
    /// 的根因之一：同名 projectId 的两台后端共用一份缓存目录）。
    #[test]
    fn cache_is_invalidated_when_endpoint_changes() -> Result<()> {
        let project = tempfile::tempdir()?;
        let cache = tempfile::tempdir()?;
        fs::write(project.path().join("a.txt"), "x")?;

        let first = manager(project.path(), cache.path());
        let scan = first.scan()?;
        first.commit(&scan.index)?;
        // 同一端点再读 → 命中。
        assert_eq!(first.load()?.entries.len(), 1);

        // 换端点（同一 projectId、同一 config，只有 base_url 不同）→ 必须作废。
        let other_endpoint = IndexManager::new(
            project.path().to_path_buf(),
            "testproject0000".to_string(),
            cache.path().to_path_buf(),
            "http://127.0.0.1:2",
        );
        assert!(
            other_endpoint.load()?.entries.is_empty(),
            "换服务端后缓存必须失效（否则会把\"已上传给 A\"误认为\"已上传给 B\"）"
        );
        Ok(())
    }

    /// TASK-100 回归：**旧缓存（无 endpoint 字段）自动作废一次**。
    #[test]
    fn legacy_cache_without_endpoint_is_rejected() -> Result<()> {
        let project = tempfile::tempdir()?;
        let cache = tempfile::tempdir()?;
        fs::write(project.path().join("a.txt"), "x")?;
        let manager = manager(project.path(), cache.path());
        let scan = manager.scan()?;
        manager.commit(&scan.index)?;

        // 取出现缓存里的字段，除去 `endpoint`，写成升级前那份文件的形状。
        let current: serde_json::Value =
            serde_json::from_str(&fs::read_to_string(manager.cache_file())?)?;
        let mut legacy = current.clone();
        legacy.as_object_mut().expect("对象").remove("endpoint");
        assert!(
            !legacy.to_string().contains("endpoint"),
            "夹具必须先真的去掉 endpoint 字段"
        );
        fs::create_dir_all(manager.cache_dir())?;
        fs::write(manager.cache_file(), legacy.to_string())?;

        // 缺归属信息 → 不复用（宁可重传一次，也不能冒着认错服务端的风险）。
        assert!(manager.load()?.entries.is_empty());

        // 反向对照：带 endpoint 的当前格式必须能读回（证明作废原因确实是缺字段）。
        fs::write(manager.cache_file(), current.to_string())?;
        assert_eq!(manager.load()?.entries.len(), 1);
        Ok(())
    }
}
