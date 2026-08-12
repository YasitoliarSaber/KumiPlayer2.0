use std::path::Path;

pub fn runtime_id_for(kind: &str, root: &Path) -> String {
    let normalized = root
        .to_string_lossy()
        .replace('\\', "/")
        .trim_end_matches('/')
        .to_lowercase();
    let mut hash = 0xcbf29ce484222325_u64;
    for byte in format!("{kind}|{normalized}").as_bytes() {
        hash ^= u64::from(*byte);
        hash = hash.wrapping_mul(0x100000001b3);
    }
    format!("{hash:016x}")
}

pub fn backend_is_compatible(
    expected_kind: &str,
    expected_runtime_id: &str,
    actual_kind: &str,
    actual_runtime_id: &str,
) -> bool {
    !expected_runtime_id.is_empty()
        && expected_kind == actual_kind
        && expected_runtime_id == actual_runtime_id
}
