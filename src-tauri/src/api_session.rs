pub fn generate_api_token() -> Result<String, String> {
    let mut bytes = [0_u8; 32];
    getrandom::fill(&mut bytes).map_err(|error| format!("无法生成桌面 API 安全令牌：{error}"))?;
    Ok(bytes.iter().map(|value| format!("{value:02x}")).collect())
}
