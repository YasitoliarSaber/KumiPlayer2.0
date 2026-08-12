#[path = "../src/api_session.rs"]
mod api_session;

use api_session::generate_api_token;

#[test]
fn generated_api_tokens_are_256_bit_hex_values_and_unique() {
    let first = generate_api_token().expect("first token");
    let second = generate_api_token().expect("second token");

    assert_eq!(first.len(), 64);
    assert!(first.chars().all(|value| value.is_ascii_hexdigit()));
    assert_ne!(first, second);
}
