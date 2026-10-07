//! 文件级 blob 与 CF-02 哈希（**与 `core/nova_core/hashing.py` 逐字节一致**）。
//!
//! 与参考实现（notace）的一处**关键差异**：nova 的 `blob_hash` 带 `0x00` 分隔符
//! （`sha256(path_bytes || 0x00 || content_bytes)`，CF-02 冻结），而 notace 是
//! `sha256(path_bytes || content_bytes)` 无分隔符。**必须带分隔符**，否则服务端账本对不上。
//!
//! 另一处差异：nova **不做行级切块**（D-02：切片是服务端 AST 符号级的职责，客户端只传文件级
//! blob）。因此本模块只产出"一文件一 blob"。

use sha2::{Digest, Sha256};

/// 单文件上限（与 core `DEFAULT_MAX_FILE_BYTES` / Module 05 §3.1 一致：`>128KB` 跳过）。
pub const MAX_FILE_BYTES: usize = 128 * 1024;

/// 二进制判定的不可打印字符比例上限（Module 05 §3.1：`> 10%` 判二进制）。
pub const BINARY_RATIO: f64 = 0.10;

/// `sha256(path_bytes || 0x00 || content_bytes)`（CF-02 / D-43）。
pub fn blob_hash(path: &str, content: &[u8]) -> String {
    let mut hasher = Sha256::new();
    hasher.update(path.as_bytes());
    hasher.update(b"\x00");
    hasher.update(content);
    hex::encode(hasher.finalize())
}

/// 清理控制字符（保留 `\n` / `\r` / `\t`）——与 notace `sanitize_content` 同语义。
///
/// nova 侧的等价物在 core 的读取路径上；客户端保持一致，保证同一文件两端算出同一 hash。
pub fn sanitize_content(content: &str) -> String {
    content
        .chars()
        .filter(|c| !matches!(c, '\x00'..='\x08' | '\x0B' | '\x0C' | '\x0E'..='\x1F' | '\x7F'))
        .collect()
}

/// 前 `probe` 字节中不可打印字符比例 `> ratio` → 判二进制。
///
/// **与 core `_is_binary` 逐条同口径**（`core/nova_core/pipeline/ignore.py`）：
/// 1. 只看前 `probe` 字节（默认 8 KB）；
/// 2. 窗口内有 `NUL` → **直接判二进制**（文本文件不会有 NUL）；
/// 3. 否则按 **ASCII 控制符**（`byte < 0x20` 且非 `\t`/`\n`/`\r`）比例判定
///    —— 按**字节**算，UTF-8 多字节字符的高位字节不算不可打印（否则中文文档会被整片误判）。
pub fn is_binary(bytes: &[u8], probe: usize, ratio: f64) -> bool {
    let window = &bytes[..bytes.len().min(probe)];
    if window.is_empty() {
        return false;
    }
    if window.contains(&0x00) {
        return true;
    }
    let control = window
        .iter()
        .filter(|byte| **byte < 0x20 && !matches!(**byte, 0x09 | 0x0A | 0x0D))
        .count();
    (control as f64) / (window.len() as f64) > ratio
}

#[cfg(test)]
mod tests {
    use super::*;

    /// 跨语言常量：由 Python `nova_core.hashing.blob_hash` 生成（TASK-040R 执行记录）。
    #[test]
    fn blob_hash_matches_python_cf02_vectors() {
        let vectors: [(&str, &[u8], &str); 4] = [
            (
                "src/a.rs",
                b"fn main() {}",
                "a693c4c5091e3a31e11dd591000e962cc37d9f89e9dbde96f85b75ef66e1a8d9",
            ),
            (
                "src/a.rs",
                b"fn main() {}\n",
                "0b87c9a7ef3d44fd876533dbc18d1ce83607f00a7a428240898c8f74f1aedb7b",
            ),
            (
                "docs/README.md",
                b"# hi\n",
                "b363170a48ba2c744a4885fc04620021fac23934c6340f4ab6d74c366f6b57c0",
            ),
            (
                "a/b/c.py",
                "中文内容\n".as_bytes(),
                "d14518101f30b23037bace573b5bb9749d0f1138021a57854dfce198ebef45d7",
            ),
        ];
        for (path, content, expected) in vectors {
            assert_eq!(blob_hash(path, content), expected, "path={path}");
        }
        assert_eq!(
            blob_hash("", b""),
            "6e340b9cffb37a989ca544e6bb780a2c78901d3fb33738768511a30617afa01d"
        );
    }

    /// 分隔符存在性：无分隔符会给另一个值（notace 的口径），证明我们有 0x00。
    #[test]
    fn separator_is_present_so_path_and_content_do_not_blur() {
        let with_separator = blob_hash("ab", b"c");
        let without_separator = {
            let mut hasher = Sha256::new();
            hasher.update(b"abc");
            hex::encode(hasher.finalize())
        };
        assert_ne!(with_separator, without_separator);
    }

    #[test]
    fn sanitize_keeps_newline_and_tab() {
        assert_eq!(sanitize_content("left\0\n\tright"), "left\n\tright");
    }

    #[test]
    fn binary_detection_follows_core_rules() {
        assert!(!is_binary(b"", 8192, BINARY_RATIO));
        assert!(!is_binary("普通文本内容\n".as_bytes(), 8192, BINARY_RATIO));
        // 规则 2：一个 NUL 即可定性
        assert!(is_binary(b"\0abcdefghij", 8192, BINARY_RATIO));
        assert!(!is_binary(b"abcdefghij", 8192, BINARY_RATIO));
        // 规则 3：控制符比例 > 10%
        let mut binary = vec![0x01u8; 20];
        binary.extend_from_slice(b"abcdefghijklmnopqrstuvwxyzabcdefghijklmnopqrstuvwxyz");
        assert!(is_binary(&binary, 8192, BINARY_RATIO));
        // 制表符/换行不计入控制符
        assert!(!is_binary(b"a\tb\nc\r\nd\n", 8192, BINARY_RATIO));
    }

    #[test]
    fn size_limit_matches_contract() {
        assert_eq!(MAX_FILE_BYTES, 128 * 1024);
    }
}
